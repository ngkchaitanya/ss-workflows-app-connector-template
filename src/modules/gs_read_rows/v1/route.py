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
VALUES_GET_TMPL = "https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{sheet_name}!{range_a1}"
SHEETS_META_URL = "https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
DRIVE_FILES_LIST = "https://www.googleapis.com/drive/v3/files"


# ---------- shared helpers (same shapes as Create) ----------

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


# ---------- content (same dropdowns as Create) ----------

@router.route("/content", methods=["POST"])
def content():
    try:
        print("[gs_read_rows.content] invoked")
        req = Request(flask_request)
        data = req.data or {}
        form_data = data.get("form_data", {}) or {}
        names_in = data.get("content_object_names", []) or []
        print("[gs_read_rows.content] form_data keys:", list(form_data.keys()))
        print("[gs_read_rows.content] content_object_names (raw):", names_in)

        names: List[str] = []
        for n in names_in:
            if isinstance(n, str):
                names.append(n)
            elif isinstance(n, dict) and "id" in n:
                names.append(str(n["id"]))
        print("[gs_read_rows.content] content_object_names (normalized):", names)

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
            print("[gs_read_rows.content] DRIVE files.list status:", r.status_code)
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
            print("[gs_read_rows.content] SHEETS meta status:", r.status_code)
            r.raise_for_status()

            titles = [s.get("properties", {}).get("title") for s in r.json().get("sheets", [])]
            titles = [t for t in titles if t]
            values = [{"label": t, "value": {"id": t, "label": t}} for t in titles]
            objs.append({"content_object_name": "sheets", "data": values})

        return Response(data={"content_objects": objs})

    except ManagedError as e:
        print("[gs_read_rows.content] ManagedError:", str(e))
        return Response.error(str(e))
    except requests.HTTPError as e:
        print("[gs_read_rows.content] Google API HTTPError:", getattr(e.response, "text", str(e)))
        print("[gs_read_rows.content] traceback:\n", traceback.format_exc())
        return Response.error(f"Google API error: {getattr(e.response, 'text', str(e))}")
    except Exception as e:
        print("[gs_read_rows.content] Unexpected Error:", repr(e))
        print("[gs_read_rows.content] traceback:\n", traceback.format_exc())
        return Response.error(str(e))


# ---------- execute ----------

@router.route("/execute", methods=["POST"])
def execute():
    try:
        print("[gs_read_rows.execute] invoked")
        req = Request(flask_request)
        data = req.data or {}
        print("[gs_read_rows.execute] input keys:", list(data.keys()))

        spreadsheet_id = _extract_id(data.get("spreadsheet_id"), "spreadsheet_id")
        sheet_name = _extract_id(data.get("sheet_name"), "sheet_name")

        # Inputs
        range_a1 = data.get("range_a1") or "A1:ZZZ1000"
        value_render_option = data.get("value_render_option") or "FORMATTED_VALUE"
        include_header = bool(data.get("include_header", True))

        token = get_service_account_token()
        headers = {"Authorization": f"Bearer {token}"}
        params = {
            "valueRenderOption": value_render_option,
            "dateTimeRenderOption": "FORMATTED_STRING",
            "majorDimension": "ROWS",
        }
        url = VALUES_GET_TMPL.format(
            spreadsheet_id=spreadsheet_id,
            sheet_name=quote(sheet_name, safe=""),
            range_a1=range_a1,
        )

        print("[gs_read_rows.execute] GET", url, params)
        r = requests.get(url, headers=headers, params=params, timeout=30)
        print("[gs_read_rows.execute] status:", r.status_code)
        r.raise_for_status()

        values = r.json().get("values", []) or []
        if not values:
            return Response(data={"headers": [], "rows": [], "rows_as_objects": []}, metadata={"affected_records": 0})

        if include_header:
            headers_row, rows = values[0], values[1:]
        else:
            headers_row, rows = [], values

        rows_as_objects: List[Dict[str, Any]] = []
        if headers_row:
            # Normalize rows to same length as headers
            width = len(headers_row)
            for row in rows:
                padded = row + [""] * (width - len(row))
                rows_as_objects.append(dict(zip(headers_row, padded)))
        else:
            rows_as_objects = []

        return Response(
            data={
                "headers": headers_row,
                "rows": rows,
                "rows_as_objects": rows_as_objects
            },
            metadata={"affected_records": len(rows)}
        )

    except ManagedError as e:
        print("[gs_read_rows.execute] ManagedError:", str(e))
        return Response.error(str(e))
    except requests.HTTPError as e:
        print("[gs_read_rows.execute] Google API HTTPError:", getattr(e.response, "text", str(e)))
        print("[gs_read_rows.execute] traceback:\n", traceback.format_exc())
        return Response.error(f"Google API error: {getattr(e.response, 'text', str(e))}")
    except Exception as e:
        print("[gs_read_rows.execute] Unexpected Error:", repr(e))
        print("[gs_read_rows.execute] traceback:\n", traceback.format_exc())
        return Response.error(str(e))
