import json
import traceback
from typing import Any, Dict, List, Union, Optional

import requests
from flask import request as flask_request
from urllib.parse import quote

from workflows_cdk import Response, Request, ManagedError
from main import router
from src.utils.google_auth_helper import get_service_account_token

# URLs
SHEETS_META_URL = "https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
VALUES_GET_TMPL = "https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{sheet_name}!{range_a1}"
VALUES_UPDATE_TMPL = "https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{sheet_name}!{range_a1}"
DRIVE_FILES_LIST = "https://www.googleapis.com/drive/v3/files"


# ---------- helpers (same style as Create) ----------

def _extract_id(value: Union[str, Dict[str, Any]], field_name: str) -> str:
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
    url = VALUES_GET_TMPL.format(
        spreadsheet_id=spreadsheet_id,
        sheet_name=quote(sheet_name, safe=""),
        range_a1="1:1",
    )
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
    url = VALUES_UPDATE_TMPL.format(
        spreadsheet_id=spreadsheet_id,
        sheet_name=encoded,
        range_a1=f"A1:{last_col}1",
    )
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
    incoming_obj: Dict[str, Any],
) -> List[str]:
    seen = set(existing_headers)
    extras: List[str] = [k for k in incoming_obj.keys() if k not in seen]
    if not extras:
        return existing_headers
    new_headers = existing_headers + extras
    _ensure_header_row(spreadsheet_id, sheet_name, token, new_headers)
    return new_headers


def _map_object_to_row(obj: Dict[str, Any], headers: List[str]) -> List[str]:
    return ["" if obj.get(h) is None else str(obj.get(h)) for h in headers]


def _find_row_by_match(
    spreadsheet_id: str,
    sheet_name: str,
    token: str,
    match_key: str,
    match_value: str,
    headers: List[str],
) -> Optional[int]:
    """Return 1-based row index of the first match (including header row as 1). Data rows start at 2."""
    if not headers:
        return None
    try:
        col_idx = headers.index(match_key) + 1  # 1-based
    except ValueError:
        return None

    col_letter = _col_name(col_idx)
    # Read the full column from row 2 down
    url = VALUES_GET_TMPL.format(
        spreadsheet_id=spreadsheet_id,
        sheet_name=quote(sheet_name, safe=""),
        range_a1=f"{col_letter}2:{col_letter}100000",
    )
    params = {"valueRenderOption": "UNFORMATTED_VALUE", "majorDimension": "ROWS"}
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
    r.raise_for_status()
    values = r.json().get("values", []) or []
    for idx, row in enumerate(values, start=2):  # row index in sheet
        cell = row[0] if row else ""
        if str(cell) == str(match_value):
            return idx
    return None


# ---------- content (same dropdowns as Create/Read) ----------

@router.route("/content", methods=["POST"])
def content():
    try:
        print("[gs_update_row.content] invoked")
        req = Request(flask_request)
        data = req.data or {}
        form_data = data.get("form_data", {}) or {}
        names_in = data.get("content_object_names", []) or []
        print("[gs_update_row.content] form_data keys:", list(form_data.keys()))
        print("[gs_update_row.content] content_object_names (raw):", names_in)

        names: List[str] = []
        for n in names_in:
            if isinstance(n, str):
                names.append(n)
            elif isinstance(n, dict) and "id" in n:
                names.append(str(n["id"]))
        print("[gs_update_row.content] content_object_names (normalized):", names)

        objs: List[Dict[str, Any]] = []

        token = get_service_account_token()
        headers = {"Authorization": f"Bearer {token}"}

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
            r = requests.get(DRIVE_FILES_LIST, headers=headers, params=params, timeout=30)
            print("[gs_update_row.content] DRIVE files.list status:", r.status_code)
            r.raise_for_status()
            files = r.json().get("files", []) or []
            values = [
                {"label": f.get("name"), "value": {"id": f.get("id"), "label": f.get("name")}}
                for f in files if f.get("id") and f.get("name")
            ]
            objs.append({"content_object_name": "spreadsheets", "data": values})

        if "sheets" in names:
            raw_spreadsheet = form_data.get("spreadsheet_id")
            if not raw_spreadsheet:
                raise ManagedError("Select a Spreadsheet first")
            spreadsheet_id = _extract_id(raw_spreadsheet, "spreadsheet_id")

            url = SHEETS_META_URL.format(spreadsheet_id=spreadsheet_id)
            params = {"fields": "sheets(properties(title))"}
            r = requests.get(url, headers=headers, params=params, timeout=30)
            print("[gs_update_row.content] SHEETS meta status:", r.status_code)
            r.raise_for_status()

            titles = [s.get("properties", {}).get("title") for s in r.json().get("sheets", [])]
            titles = [t for t in titles if t]
            values = [{"label": t, "value": {"id": t, "label": t}} for t in titles]
            objs.append({"content_object_name": "sheets", "data": values})

        return Response(data={"content_objects": objs})

    except ManagedError as e:
        print("[gs_update_row.content] ManagedError:", str(e))
        return Response.error(str(e))
    except requests.HTTPError as e:
        print("[gs_update_row.content] Google API HTTPError:", getattr(e.response, "text", str(e)))
        print("[gs_update_row.content] traceback:\n", traceback.format_exc())
        return Response.error(f"Google API error: {getattr(e.response, 'text', str(e))}")
    except Exception as e:
        print("[gs_update_row.content] Unexpected Error:", repr(e))
        print("[gs_update_row.content] traceback:\n", traceback.format_exc())
        return Response.error(str(e))


# ---------- execute ----------

@router.route("/execute", methods=["POST"])
def execute():
    try:
        print("[gs_update_row.execute] invoked")
        req = Request(flask_request)
        data = req.data or {}
        print("[gs_update_row.execute] input keys:", list(data.keys()))

        spreadsheet_id = _extract_id(data.get("spreadsheet_id"), "spreadsheet_id")
        sheet_name = _extract_id(data.get("sheet_name"), "sheet_name")

        match_key = data.get("match_key") or ""
        match_value = data.get("match_value") or ""
        upsert_if_missing = bool(data.get("upsert_if_missing", True))
        value_input_option = data.get("value_input_option") or "USER_ENTERED"

        values_json = data.get("values_json")
        if values_json is None:
            raise ManagedError("Missing 'values_json'")

        # Parse values
        if isinstance(values_json, str):
            try:
                parsed = json.loads(values_json)
            except json.JSONDecodeError as e:
                raise ManagedError(f"values_json is not valid JSON: {e}")
        else:
            parsed = values_json

        # Accept single object or array of objects (use first)
        if isinstance(parsed, dict):
            obj = parsed
        elif isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            obj = parsed[0]
        else:
            raise ManagedError("values_json must be an object or an array of objects")

        # Auth
        token = get_service_account_token()
        headers_http = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # Headers and row target
        headers_row = _get_headers(spreadsheet_id, sheet_name, token)
        if not headers_row:
            header_order = list(obj.keys())
            _ensure_header_row(spreadsheet_id, sheet_name, token, header_order)
            target_row_idx = None
        else:
            header_order = _expand_headers_if_needed(spreadsheet_id, sheet_name, token, headers_row, obj)
            target_row_idx = None
            if match_key and match_value:
                target_row_idx = _find_row_by_match(
                    spreadsheet_id, sheet_name, token, match_key, match_value, header_order
                )

        row_values = _map_object_to_row(obj, header_order)

        if target_row_idx is None:
            if not upsert_if_missing:
                return Response(
                    data={"matched": 0, "updated_rows": 0},
                    metadata={"affected_records": 0, "message": "No matching row found and upsert is disabled"},
                )
            # Append
            encoded = quote(sheet_name, safe="")
            url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{encoded}!A1:append"
            params = {
                "valueInputOption": value_input_option,
                "insertDataOption": "INSERT_ROWS",
                "includeValuesInResponse": "true",
            }
            body = {"values": [row_values]}
            print("[gs_update_row.execute] POST append", url, params)
            r = requests.post(url, headers=headers_http, params=params, data=json.dumps(body), timeout=30)
            print("[gs_update_row.execute] status:", r.status_code, "body:", r.text[:300])
            r.raise_for_status()
            updates = r.json().get("updates", {}) or {}
            return Response(
                data={"matched": 0, "updated_rows": updates.get("updatedRows", 0), "header_order": header_order},
                metadata={"affected_records": updates.get("updatedRows", 0), "message": "Appended new row"},
            )

        # Update in place
        # Range like A{row}:<lastcol>{row}
        last_col = _col_name(len(header_order)) or "A"
        encoded = quote(sheet_name, safe="")
        url = VALUES_UPDATE_TMPL.format(
            spreadsheet_id=spreadsheet_id,
            sheet_name=encoded,
            range_a1=f"A{target_row_idx}:{last_col}{target_row_idx}",
        )
        params = {"valueInputOption": value_input_option}
        body = {"values": [row_values]}
        print("[gs_update_row.execute] PUT update", url, params)
        r = requests.put(url, headers=headers_http, params=params, data=json.dumps(body), timeout=30)
        print("[gs_update_row.execute] status:", r.status_code, "body:", r.text[:300])
        r.raise_for_status()
        return Response(
            data={"matched": 1, "updated_rows": 1, "row_index": target_row_idx, "header_order": header_order},
            metadata={"affected_records": 1, "message": f"Updated row {target_row_idx}"},
        )

    except ManagedError as e:
        print("[gs_update_row.execute] ManagedError:", str(e))
        return Response.error(str(e))
    except requests.HTTPError as e:
        print("[gs_update_row.execute] Google API HTTPError:", getattr(e.response, "text", str(e)))
        print("[gs_update_row.execute] traceback:\n", traceback.format_exc())
        return Response.error(f"Google API error: {getattr(e.response, 'text', str(e))}")
    except Exception as e:
        print("[gs_update_row.execute] Unexpected Error:", repr(e))
        print("[gs_update_row.execute] traceback:\n", traceback.format_exc())
        return Response.error(str(e))

