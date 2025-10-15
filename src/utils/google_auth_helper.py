# src/utils/google_auth_helper.py
import os
# Force pure-Python RSA before any google-auth imports
os.environ["GOOGLE_AUTH_USE_NATIVE_PYTHON_RSA"] = "true"

import json
import traceback

import google.auth  # type: ignore
from google.auth.transport.requests import Request as GoogleRequest  # type: ignore
from google.oauth2 import service_account  # type: ignore
from google.auth.crypt import _python_rsa as py_rsa  # type: ignore

from workflows_cdk import ManagedError

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def _normalize_private_key(info: dict) -> dict:
    pk = info.get("private_key")
    if not pk:
        raise ManagedError("Service account JSON missing 'private_key'")
    if isinstance(pk, str) and "\\n" in pk:
        info["private_key"] = pk.replace("\\n", "\n")
    return info

def get_service_account_token():
    try:
        print("[auth] google-auth version:", getattr(google.auth, "__version__", "unknown"))
        print("[auth] GOOGLE_AUTH_USE_NATIVE_PYTHON_RSA:", os.getenv("GOOGLE_AUTH_USE_NATIVE_PYTHON_RSA"))

        sa_json = os.getenv("GS_SA_JSON")
        print("[auth] GS_SA_JSON present:", bool(sa_json))
        if not sa_json:
            raise ManagedError("Missing service account credentials (GS_SA_JSON not set)")

        info = _normalize_private_key(json.loads(sa_json))

        # Build creds once, then force-replace signer with pure-Python signer
        base = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        try:
            current_signer = type(getattr(base, "_signer", None)).__name__
            print("[auth] initial signer class:", current_signer)
        except Exception:
            pass

        # Construct a pure-Python signer and new Credentials explicitly
        signer = py_rsa.RSASigner.from_string(info["private_key"], info["client_email"])
        creds = service_account.Credentials(
            signer=signer,
            service_account_email=info["client_email"],
            token_uri=info.get("token_uri", "https://oauth2.googleapis.com/token"),
            project_id=info.get("project_id"),
            scopes=SCOPES,
            subject=None,
            additional_claims=None,
        )

        print("[auth] forcing signer class:", type(getattr(creds, "_signer", None)).__name__)
        creds.refresh(GoogleRequest())
        token = creds.token
        print("[auth] Token acquired. Length:", len(token) if token else 0)
        return token
    except ManagedError:
        raise
    except Exception as e:
        print("[auth] google-auth refresh failed:", repr(e))
        print("[auth] traceback:\n", traceback.format_exc())
        raise ManagedError(f"Error getting service account token: {e}")
