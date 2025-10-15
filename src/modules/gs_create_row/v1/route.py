import json
import traceback
from typing import Any, Dict, List, Union

import requests
from flask import request as flask_request
from urllib.parse import quote

from workflows_cdk import Response, Request, ManagedError
from main import router
from src.utils.google_auth_helper import get_service_account_token

# URLs
GOOGLE_SHEETS_APPEND_URL_TMPL = (
    "https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{sheet_name}!A1:append"
)
SHEETS_META_URL = "https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
DRIVE_FILES_LIST = "https://www.googleapis.com/drive/v3/files"


# ---------- helpers ----------

def _extract_id(value: Union[str, Dict[str, Any]], field_name: str) -> str:
    """
    Support both:
      - string: "abcd123"
      - object: {"id": "abcd123", "label": "My Sheet"}
    """
    if isinstance(value, str):
        if not value:
            raise ManagedError(f"Missing '{field_name}'")
        return value
    if isinstance(value, dict):
        v = value.get("id")
        if not v:
            raise ManagedError(f"Field '{field_name}' is missing 'id'")
        return str(v)
    raise ManagedError(f"Invalid '{field_name}' format")


def _col_name(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _get_headers(spreadsheet_id: str, sheet_name: str, token: str) -> List[str]:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{quote(sheet_name, safe='')}!1:1"
    params = {"valueRenderOption": "UNFORMATTED_VALUE", "majorDimension": "ROWS"}
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
    r.raise_for_status()
    vals = r.json().get("values", [])
    return vals[0] if vals and vals[0] else []


def _ensure_header_row(spreadsheet_id: str, sheet_name: str, token: str, headers_row: List[str]) -> None:
    if not headers_row:
        return
    encoded = quote(sheet_name, safe="")
    last_col = _col_name(len(headers_row)) or "A"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{encoded}!A1:{last_col}1"
    params = {"valueInputOption": "RAW"}
    body = {"values": [headers_row]}
    r = requests.put(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params=params,
        data=json.dumps(body),
        timeout=30,
    )
    r.raise_for_status()


def _expand_headers_if_needed(
    spreadsheet_id: str,
    sheet_name: str,
    token: str,
    existing_headers: List[str],
    obj_rows: List[Dict[str, Any]],
) -> List[str]:
    """Keep existing header order, append new keys seen in obj_rows, and write back if expanded."""
    seen = set(existing_headers)
    extras_ordered: List[str] = []
    for obj in obj_rows:
        for k in obj.keys():
            if k not in seen:
                seen.add(k)
                extras_ordered.append(k)

    if not extras_ordered:
        return existing_headers

    new_headers = existing_headers + extras_ordered
    encoded = quote(sheet_name, safe="")
    last_col = _col_name(len(new_headers)) or "A"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{encoded}!A1:{last_col}1"
    params = {"valueInputOption": "RAW"}
    body = {"values": [new_headers]}
    r = requests.put(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params=params,
        data=json.dumps(body),
        timeout=30,
    )
    r.raise_for_status()
    print("[gs_create_row.execute] headers expanded by extras:", extras_ordered)
    return new_headers


def _normalize_object_rows(obj_rows: List[Dict[str, Any]], headers: List[str]) -> List[List[str]]:
    out: List[List[str]] = []
    for obj in obj_rows:
        row = ["" if obj.get(h) is None else str(obj.get(h)) for h in headers]
        out.append(row)
    return out


# ---------- routes ----------

@router.route("/content", methods=["POST"])
def content():
    try:
        print("[gs_create_row.content] invoked")
        req = Request(flask_request)
        data = req.data or {}
        form_data = data.get("form_data", {}) or {}
        names_in = data.get("content_object_names", []) or []
        print("[gs_create_row.content] form_data keys:", list(form_data.keys()))
        print("[gs_create_row.content] content_object_names (raw):", names_in)

        # Normalize to ["spreadsheets", "sheets", ...]
        names: List[str] = []
        for n in names_in:
            if isinstance(n, str):
                names.append(n)
            elif isinstance(n, dict) and "id" in n:
                names.append(str(n["id"]))
        print("[gs_create_row.content] content_object_names (normalized):", names)

        objs: List[Dict[str, Any]] = []

        token = get_service_account_token()
        headers = {"Authorization": f"Bearer {token}"}

        # List spreadsheets the service account can access
        if "spreadsheets" in names:
            q = (
                "mimeType='application/vnd.google-apps.spreadsheet' "
                "and trashed=false "
                "and ('me' in owners or 'me' in writers or 'me' in readers)"
            )
            params = {
                "q": q,
                "fields": "files(id,name),nextPageToken",
                "pageSize": 100,
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
                "spaces": "drive",
            }
            print("[gs_create_row.content] GET Drive files.list", params)
            r = requests.get(DRIVE_FILES_LIST, headers=headers, params=params, timeout=30)
            print("[gs_create_row.content] DRIVE files.list status:", r.status_code, "body:", r.text[:200])
            r.raise_for_status()
            files = r.json().get("files", []) or []
            values = [
                {"label": f.get("name"), "value": {"id": f.get("id"), "label": f.get("name")}}
                for f in files
                if f.get("id") and f.get("name")
            ]
            objs.append({"content_object_name": "spreadsheets", "data": values})
            print("[gs_create_row.content] returned spreadsheets:", [v["label"] for v in values])

        # Given a spreadsheet, list its tabs
        if "sheets" in names:
            raw_spreadsheet = form_data.get("spreadsheet_id")
            if not raw_spreadsheet:
                raise ManagedError("Select a Spreadsheet first")

            spreadsheet_id = _extract_id(raw_spreadsheet, "spreadsheet_id")
            url = SHEETS_META_URL.format(spreadsheet_id=spreadsheet_id)
            params = {"fields": "sheets(properties(title))"}
            print("[gs_create_row.content] GET Sheets meta", url, params)
            r = requests.get(url, headers=headers, params=params, timeout=30)
            print("[gs_create_row.content] SHEETS meta status:", r.status_code, "body:", r.text[:200])
            r.raise_for_status()

            titles = [s.get("properties", {}).get("title") for s in r.json().get("sheets", [])]
            titles = [t for t in titles if t]
            values = [{"label": t, "value": {"id": t, "label": t}} for t in titles]
            objs.append({"content_object_name": "sheets", "data": values})
            print("[gs_create_row.content] returned tabs:", titles)

        return Response(data={"content_objects": objs})

    except ManagedError as e:
        print("[gs_create_row.content] ManagedError:", str(e))
        return Response.error(str(e))
    except requests.HTTPError as e:
        print("[gs_create_row.content] Google API HTTPError:", getattr(e.response, "text", str(e)))
        print("[gs_create_row.content] traceback:\n", traceback.format_exc())
        return Response.error(f"Google API error: {getattr(e.response, 'text', str(e))}")
    except Exception as e:
        print("[gs_create_row.content] Unexpected Error:", repr(e))
        print("[gs_create_row.content] traceback:\n", traceback.format_exc())
        return Response.error(str(e))


@router.route("/execute", methods=["POST"])
def execute():
    try:
        print("[gs_create_row.execute] invoked")
        req = Request(flask_request)
        data = req.data or {}
        print("[gs_create_row.execute] input keys:", list(data.keys()))

        spreadsheet_id = _extract_id(data.get("spreadsheet_id"), "spreadsheet_id")
        sheet_name = _extract_id(data.get("sheet_name"), "sheet_name")

        raw_values = data.get("values_json")
        value_input_option = data.get("value_input_option") or "USER_ENTERED"
        if raw_values is None:
            raise ManagedError("Missing 'values_json'")

        # Parse
        if isinstance(raw_values, str):
            print("[gs_create_row.execute] values_json is str, len:", len(raw_values))
            try:
                parsed = json.loads(raw_values)
            except json.JSONDecodeError as e:
                raise ManagedError(f"values_json is not valid JSON: {e}")
        else:
            print("[gs_create_row.execute] values_json type:", type(raw_values).__name__)
            parsed = raw_values

        # Normalize shapes
        # 1) Single object -> wrap in list
        if isinstance(parsed, dict):
            print("[gs_create_row.execute] normalizing single object to array")
            parsed = [parsed]

        # 2) If they sent a 2D array, convert it using header logic
        if isinstance(parsed, list) and parsed and not isinstance(parsed[0], dict):
            print("[gs_create_row.execute] detected 2D array. Converting to object rows")
            # we need token now to read or create headers
            token = get_service_account_token()
            headers_http = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

            existing_headers = _get_headers(spreadsheet_id, sheet_name, token)
            cols = len(parsed[0])
            if existing_headers:
                # extend if not enough header columns
                if len(existing_headers) < cols:
                    need = cols - len(existing_headers)
                    extra = [f"col_{i+1+len(existing_headers)}" for i in range(need)]
                    _ensure_header_row(spreadsheet_id, sheet_name, token, existing_headers + extra)
                    existing_headers = existing_headers + extra
                header_order = existing_headers
            else:
                header_order = [f"col_{i+1}" for i in range(cols)]
                _ensure_header_row(spreadsheet_id, sheet_name, token, header_order)

            tmp_objs = [dict(zip(header_order, row)) for row in parsed]
            parsed = tmp_objs
        # 3) Final guard
        if not (isinstance(parsed, list) and parsed and all(isinstance(x, dict) for x in parsed)):
            print("[gs_create_row.execute] parsed shape debug:", type(parsed).__name__, parsed[:1] if isinstance(parsed, list) else parsed)
            raise ManagedError("values_json must be an array of objects")

        # Auth (if we did the 2D conversion, token already exists; reuse it)
        try:
            token  # noqa: F821
        except NameError:
            token = get_service_account_token()
        headers_http = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # Header order
        existing_headers = _get_headers(spreadsheet_id, sheet_name, token)
        if existing_headers:
            header_order = _expand_headers_if_needed(spreadsheet_id, sheet_name, token, existing_headers, parsed)
            print("[gs_create_row.execute] using headers:", header_order)
        else:
            header_order = list(parsed[0].keys())
            seen = set(header_order)
            for obj in parsed[1:]:
                for k in obj.keys():
                    if k not in seen:
                        header_order.append(k)
                        seen.add(k)
            print("[gs_create_row.execute] creating headers:", header_order)
            _ensure_header_row(spreadsheet_id, sheet_name, token, header_order)

        # Map objects to rows and append
        rows_2d = _normalize_object_rows(parsed, header_order)
        encoded_sheet = quote(sheet_name, safe="")
        url = GOOGLE_SHEETS_APPEND_URL_TMPL.format(spreadsheet_id=spreadsheet_id, sheet_name=encoded_sheet)
        params = {
            "valueInputOption": value_input_option,
            "insertDataOption": "INSERT_ROWS",
            "includeValuesInResponse": "true",
        }
        body = {"values": rows_2d}

        print("[gs_create_row.execute] POST", url, params)
        r = requests.post(url, headers=headers_http, params=params, data=json.dumps(body), timeout=30)
        print("[gs_create_row.execute] status:", r.status_code, "body:", r.text[:300])
        r.raise_for_status()

        updates = r.json().get("updates", {}) or {}
        updated_range = updates.get("updatedRange")
        updated_rows = updates.get("updatedRows", 0)
        return Response(
            data={
                "updated_range": updated_range,
                "updated_rows": updated_rows,
                "header_order": header_order
            },
            metadata={"affected_records": updated_rows, "message": f"Appended {updated_rows} row(s) to {sheet_name}"}
        )

    except ManagedError as e:
        print("[gs_create_row.execute] ManagedError:", str(e))
        return Response.error(str(e))
    except requests.HTTPError as e:
        print("[gs_create_row.execute] Google API HTTPError:", getattr(e.response, "text", str(e)))
        print("[gs_create_row.execute] traceback:\n", traceback.format_exc())
        return Response.error(f"Google API error: {getattr(e.response, 'text', str(e))}")
    except Exception as e:
        print("[gs_create_row.execute] Unexpected Error:", repr(e))
        print("[gs_create_row.execute] traceback:\n", traceback.format_exc())
        return Response.error(str(e))
