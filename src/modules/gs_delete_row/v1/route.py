import json
import traceback
from typing import Any, Dict, List, Optional, Union

import requests
from flask import request as flask_request
from urllib.parse import quote

from workflows_cdk import Response, Request, ManagedError
from main import router
from src.utils.google_auth_helper import get_service_account_token

SHEETS_META_URL = "https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
VALUES_GET_TMPL = "https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{sheet_name}!{range_a1}"
BATCH_UPDATE_URL = "https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate"
DRIVE_FILES_LIST = "https://www.googleapis.com/drive/v3/files"


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


def _find_row_by_match(
    spreadsheet_id: str,
    sheet_name: str,
    token: str,
    match_key: str,
    match_value: str,
    headers: List[str],
) -> Optional[int]:
    if not headers:
        return None
    try:
        col_idx = headers.index(match_key) + 1  # 1-based
    except ValueError:
        return None
    col_letter = _col_name(col_idx)
    url = VALUES_GET_TMPL.format(
        spreadsheet_id=spreadsheet_id,
        sheet_name=quote(sheet_name, safe=""),
        range_a1=f"{col_letter}2:{col_letter}100000",
    )
    params = {"valueRenderOption": "UNFORMATTED_VALUE", "majorDimension": "ROWS"}
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
    r.raise_for_status()
    values = r.json().get("values", []) or []
    for idx, row in enumerate(values, start=2):
        cell = row[0] if row else ""
        if str(cell) == str(match_value):
            return idx
    return None


def _get_sheet_id_by_title(spreadsheet_id: str, sheet_title: str, token: str) -> Optional[int]:
    url = SHEETS_META_URL.format(spreadsheet_id=spreadsheet_id)
    params = {"fields": "sheets(properties(sheetId,title))"}
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
    r.raise_for_status()
    for s in r.json().get("sheets", []):
        props = s.get("properties", {})
        if props.get("title") == sheet_title:
            return props.get("sheetId")
    return None


@router.route("/content", methods=["POST"])
def content():
    try:
        print("[gs_delete_row.content] invoked")
        req = Request(flask_request)
        data = req.data or {}
        form_data = data.get("form_data", {}) or {}
        names_in = data.get("content_object_names", []) or []
        print("[gs_delete_row.content] form_data keys:", list(form_data.keys()))
        print("[gs_delete_row.content] content_object_names (raw):", names_in)

        names: List[str] = []
        for n in names_in:
            if isinstance(n, str):
                names.append(n)
            elif isinstance(n, dict) and "id" in n:
                names.append(str(n["id"]))
        print("[gs_delete_row.content] content_object_names (normalized):", names)

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
            print("[gs_delete_row.content] DRIVE files.list status:", r.status_code)
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
            print("[gs_delete_row.content] SHEETS meta status:", r.status_code)
            r.raise_for_status()

            titles = [s.get("properties", {}).get("title") for s in r.json().get("sheets", [])]
            titles = [t for t in titles if t]
            values = [{"label": t, "value": {"id": t, "label": t}} for t in titles]
            objs.append({"content_object_name": "sheets", "data": values})

        return Response(data={"content_objects": objs})

    except ManagedError as e:
        print("[gs_delete_row.content] ManagedError:", str(e))
        return Response.error(str(e))
    except requests.HTTPError as e:
        print("[gs_delete_row.content] Google API HTTPError:", getattr(e.response, "text", str(e)))
        print("[gs_delete_row.content] traceback:\n", traceback.format_exc())
        return Response.error(f"Google API error: {getattr(e.response, 'text', str(e))}")
    except Exception as e:
        print("[gs_delete_row.content] Unexpected Error:", repr(e))
        print("[gs_delete_row.content] traceback:\n", traceback.format_exc())
        return Response.error(str(e))


@router.route("/execute", methods=["POST"])
def execute():
    try:
        print("[gs_delete_row.execute] invoked")
        req = Request(flask_request)
        data = req.data or {}
        print("[gs_delete_row.execute] input keys:", list(data.keys()))

        spreadsheet_id = _extract_id(data.get("spreadsheet_id"), "spreadsheet_id")
        sheet_name = _extract_id(data.get("sheet_name"), "sheet_name")

        # Choose row by explicit index (1-based) or by match
        row_index = data.get("row_index")  # may be str or int
        match_key = data.get("match_key") or ""
        match_value = data.get("match_value") or ""

        # Auth
        token = get_service_account_token()
        headers_http = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # Resolve target row
        target_row_idx: Optional[int] = None
        if row_index:
            try:
                target_row_idx = int(row_index)
            except Exception:
                raise ManagedError("row_index must be an integer")
            if target_row_idx < 1:
                raise ManagedError("row_index must be >= 1")
        elif match_key and match_value:
            headers_row = _get_headers(spreadsheet_id, sheet_name, token)
            if not headers_row:
                return Response.error("No header row found to match against")
            target_row_idx = _find_row_by_match(spreadsheet_id, sheet_name, token, match_key, match_value, headers_row)
            if target_row_idx is None:
                return Response.error(f"No row found where {match_key} == {match_value}")
        else:
            raise ManagedError("Provide either 'row_index' or both 'match_key' and 'match_value'")

        # Convert to zero-based indexes for DeleteDimensionRequest, excluding header row
        # Google expects startIndex inclusive, endIndex exclusive, zero-based over all rows.
        # Row 1 is the header, so:
        start = target_row_idx - 1
        end = target_row_idx

        # Need sheetId (numeric) for the tab
        sheet_id = _get_sheet_id_by_title(spreadsheet_id, sheet_name, token)
        if sheet_id is None:
            raise ManagedError(f"Could not resolve sheetId for tab '{sheet_name}'")

        body = {
            "requests": [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": start,
                            "endIndex": end
                        }
                    }
                }
            ]
        }
        url = BATCH_UPDATE_URL.format(spreadsheet_id=spreadsheet_id)
        print("[gs_delete_row.execute] POST batchUpdate", url, body)
        r = requests.post(url, headers=headers_http, data=json.dumps(body), timeout=30)
        print("[gs_delete_row.execute] status:", r.status_code, "body:", r.text[:300])
        r.raise_for_status()

        return Response(
            data={"deleted_row_index": target_row_idx},
            metadata={"affected_records": 1, "message": f"Deleted row {target_row_idx}"}
        )

    except ManagedError as e:
        print("[gs_delete_row.execute] ManagedError:", str(e))
        return Response.error(str(e))
    except requests.HTTPError as e:
        print("[gs_delete_row.execute] Google API HTTPError:", getattr(e.response, "text", str(e)))
        print("[gs_delete_row.execute] traceback:\n", traceback.format_exc())
        return Response.error(f"Google API error: {getattr(e.response, 'text', str(e))}")
    except Exception as e:
        print("[gs_delete_row.execute] Unexpected Error:", repr(e))
        print("[gs_delete_row.execute] traceback:\n", traceback.format_exc())
        return Response.error(str(e))
