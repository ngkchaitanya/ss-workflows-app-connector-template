import json
import traceback
from typing import Any, Dict, List

import requests
from flask import request as flask_request
from workflows_cdk import Response, Request, ManagedError
from main import router

from src.utils.google_auth_helper import get_service_account_token

GOOGLE_SHEETS_APPEND_URL_TMPL = (
    "https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{sheet_name}!A1:append"
)
GOOGLE_SHEETS_META_URL_TMPL = "https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"


@router.route("/content", methods=["POST"])
def content():
    try:
        print("[gs_create_row.content] invoked")
        req = Request(flask_request)
        data = req.data or {}
        form_data = data.get("form_data", {}) or {}
        names = data.get("content_object_names", []) or []
        print("[gs_create_row.content] form_data keys:", list(form_data.keys()))
        print("[gs_create_row.content] content_object_names:", names)

        objs: List[Dict[str, Any]] = []

        if "sheets" in names:
            spreadsheet_id = form_data.get("spreadsheet_id")
            if not spreadsheet_id:
                raise ManagedError("Enter Spreadsheet ID then refresh the Sheet field")

            token = get_service_account_token()
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            url = GOOGLE_SHEETS_META_URL_TMPL.format(spreadsheet_id=spreadsheet_id)
            params = {"fields": "sheets(properties(title))"}
            print("[gs_create_row.content] GET", url, params)

            r = requests.get(url, headers=headers, params=params, timeout=30)
            print("[gs_create_row.content] status:", r.status_code, "body:", r.text[:300])
            r.raise_for_status()

            values = []
            for s in r.json().get("sheets", []):
                title = s.get("properties", {}).get("title")
                if title:
                    values.append({"value": {"id": title, "label": title}, "label": title})

            objs.append({"content_object_name": "sheets", "data": values})
            print("[gs_create_row.content] returned tabs:", [v["label"] for v in values])

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
        print("[gs_create_row.execute] raw values_json length:",
              len(data.get("values_json")) if isinstance(data.get("values_json"), str) else "not-str")

        spreadsheet_id = data.get("spreadsheet_id")
        sheet_name = data.get("sheet_name")
        values_json = data.get("values_json")
        value_input_option = data.get("value_input_option") or "USER_ENTERED"

        if not spreadsheet_id:
            raise ManagedError("Missing 'spreadsheet_id'")
        if not sheet_name:
            raise ManagedError("Missing 'sheet_name'")
        if not values_json:
            raise ManagedError("Missing 'values_json'")

        try:
            parsed = json.loads(values_json) if isinstance(values_json, str) else values_json
        except json.JSONDecodeError as e:
            raise ManagedError(f"values_json is not valid JSON: {e}")

        if not isinstance(parsed, list) or not parsed or not isinstance(parsed[0], list):
            raise ManagedError('values_json must be a 2D array, for example: [["A","B","C"]]')

        first_row = ["" if v is None else str(v) for v in parsed[0]]
        print("[gs_create_row.execute] first_row:", first_row)

        token = get_service_account_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        params = {
            "valueInputOption": value_input_option,
            "insertDataOption": "INSERT_ROWS",
            "includeValuesInResponse": "true"
        }
        url = GOOGLE_SHEETS_APPEND_URL_TMPL.format(spreadsheet_id=spreadsheet_id, sheet_name=sheet_name)
        body = {"values": [first_row]}

        print("[gs_create_row.execute] POST", url, params)
        r = requests.post(url, headers=headers, params=params, data=json.dumps(body), timeout=30)
        print("[gs_create_row.execute] status:", r.status_code, "body:", r.text[:300])
        r.raise_for_status()

        updates = r.json().get("updates", {})
        updated_range = updates.get("updatedRange")
        updated_rows = updates.get("updatedRows", 0)
        print("[gs_create_row.execute] success. updated_range:", updated_range, "updated_rows:", updated_rows)

        return Response(
            data={"updated_range": updated_range, "updated_rows": updated_rows, "values_sent": first_row},
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
