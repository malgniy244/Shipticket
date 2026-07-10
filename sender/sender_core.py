"""
sender_core.py — Pure logic for Ship Ticket Sender (no tkinter dependency).
Imported by both sender.py (GUI) and test_sender.py (unit tests).
"""
from __future__ import annotations

import base64
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import msal
import requests

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

CLIENT_ID  = "61249134-e089-422b-bd52-688eb7cafa01"
TENANT_ID  = "893a34dd-cb02-4c70-957d-794446df8feb"
AUTHORITY  = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES     = ["Mail.Send"]

TO_EMAIL   = "cng@stacksbowers.com"
SUBJECT    = "123"

SEND_DELAY = 4          # seconds between sends (3-5 as specced)
GRAPH_URL  = "https://graph.microsoft.com/v1.0/me/sendMail"

# Filename pattern: 5-7 digits, optional suffix like -1 or -2
PDF_PATTERN = re.compile(r"^\d{5,7}(-\d{1,2})?\.pdf$", re.IGNORECASE)

SENT_LOG   = "sent.log"


# ─────────────────────────────────────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────────────────────────────────────

def _cache_path() -> str:
    """Platform-appropriate token cache path."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    else:
        base = os.path.expanduser("~")
    cache_dir = os.path.join(base, ".sts_sender")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "token_cache.json")


def _build_app():
    cache = msal.SerializableTokenCache()
    cache_file = _cache_path()
    if os.path.exists(cache_file):
        with open(cache_file, "r") as f:
            cache.deserialize(f.read())
    app = msal.PublicClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        token_cache=cache,
    )
    return app, cache, cache_file


def _save_cache(cache: msal.SerializableTokenCache, cache_file: str) -> None:
    if cache.has_state_changed:
        with open(cache_file, "w") as f:
            f.write(cache.serialize())


def acquire_token(
    on_device_code,
    on_waiting,
) -> str | None:
    """
    Acquire an access token.
    - Tries silent first (cached).
    - Falls back to device-code flow, calling on_device_code(url, code)
      so the GUI can display the sign-in prompt.
    Returns the access token string, or None on failure.
    """
    app, cache, cache_file = _build_app()

    # Try silent first
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_cache(cache, cache_file)
            return result["access_token"]

    # Device-code flow
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        return None

    on_device_code(flow["verification_uri"], flow["user_code"])
    on_waiting()

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" in result:
        _save_cache(cache, cache_file)
        return result["access_token"]

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Graph send helper
# ─────────────────────────────────────────────────────────────────────────────

def send_pdf(token: str, pdf_path: Path) -> None:
    """
    Send one PDF as an email via Graph /me/sendMail.
    Raises requests.HTTPError on failure (caller handles retry / skip).
    """
    with open(pdf_path, "rb") as f:
        content_bytes = f.read()
    content_b64 = base64.b64encode(content_bytes).decode("ascii")

    payload = {
        "message": {
            "subject": SUBJECT,
            "body": {
                "contentType": "Text",
                "content": "",
            },
            "toRecipients": [
                {"emailAddress": {"address": TO_EMAIL}}
            ],
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": pdf_path.name,
                    "contentType": "application/pdf",
                    "contentBytes": content_b64,
                }
            ],
        },
        "saveToSentItems": "true",
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    resp = requests.post(GRAPH_URL, headers=headers, json=payload, timeout=60)

    # Handle 429 throttling with retry-after
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "10"))
        time.sleep(retry_after + 1)
        resp = requests.post(GRAPH_URL, headers=headers, json=payload, timeout=60)

    resp.raise_for_status()


# ─────────────────────────────────────────────────────────────────────────────
# sent.log helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_sent_log(folder: Path) -> set:
    """Return set of filenames already sent (from sent.log in folder)."""
    log_path = folder / SENT_LOG
    if not log_path.exists():
        return set()
    sent = set()
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split("\t")
                if parts:
                    sent.add(parts[0])
    return sent


def append_sent_log(folder: Path, filename: str) -> None:
    """Append a sent entry to sent.log."""
    log_path = folder / SENT_LOG
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{filename}\t{ts}\t{TO_EMAIL}\t{SUBJECT}\n")


def clear_sent_log(folder: Path) -> None:
    """Remove sent.log (for resend-all)."""
    log_path = folder / SENT_LOG
    if log_path.exists():
        log_path.unlink()
