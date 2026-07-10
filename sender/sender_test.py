"""
sender_test.py — Auth + single-send test for Ship Ticket Sender
================================================================
PURPOSE:  Verify the device-code sign-in and Graph send work on your machine
          before running the full GUI tool.  Sends exactly ONE email.

USAGE:
    1. Create a test folder, e.g.  C:\test_send\
    2. Put ONE PDF in it, e.g.     C:\test_send\999999.pdf
       (any PDF will do — even a blank one-page file)
    3. Run:  python sender_test.py C:\test_send\999999.pdf
    4. Follow the device-code sign-in prompt in your browser.
    5. Check cng@stacksbowers.com for the email.

WHAT IT PRINTS:
    [AUTH]  — every step of the MSAL token acquisition
    [GRAPH] — the exact HTTP request and response from Microsoft Graph
    [OK] / [FAIL] — final outcome

No credentials are stored in this file.
Client ID and Tenant ID are public app-registration identifiers, not secrets.
"""

import base64
import json
import os
import sys
import time
from pathlib import Path

import msal
import requests

# ─── App registration (not secrets) ──────────────────────────────────────────
CLIENT_ID = "61249134-e089-422b-bd52-688eb7cafa01"
TENANT_ID = "893a34dd-cb02-4c70-957d-794446df8feb"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES    = ["Mail.Send"]

# ─── Send target (locked) ────────────────────────────────────────────────────
TO_EMAIL  = "cng@stacksbowers.com"
SUBJECT   = "123"
GRAPH_URL = "https://graph.microsoft.com/v1.0/me/sendMail"

# ─── Token cache (persists across runs so you don't re-auth every time) ──────
CACHE_DIR  = os.path.join(os.path.expanduser("~"), ".sts_sender")
CACHE_FILE = os.path.join(CACHE_DIR, "token_cache.json")


def _log(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}", flush=True)


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            cache.deserialize(f.read())
        _log("AUTH", f"Loaded token cache from {CACHE_FILE}")
    else:
        _log("AUTH", "No existing token cache — will do device-code flow")
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            f.write(cache.serialize())
        _log("AUTH", f"Token cache saved to {CACHE_FILE}")


def acquire_token() -> str:
    """
    Get an access token.  Silent if cached, device-code flow otherwise.
    Exits the script with a clear message if auth fails.
    """
    cache = _load_cache()
    app = msal.PublicClientApplication(
        CLIENT_ID, authority=AUTHORITY, token_cache=cache
    )

    # ── Try silent first ──────────────────────────────────────────────────────
    accounts = app.get_accounts()
    if accounts:
        _log("AUTH", f"Found cached account: {accounts[0].get('username', '?')}")
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _log("AUTH", "Silent token acquisition succeeded (no sign-in needed)")
            _save_cache(cache)
            return result["access_token"]
        else:
            _log("AUTH", f"Silent acquisition failed: {result.get('error_description', result)}")

    # ── Device-code flow ──────────────────────────────────────────────────────
    _log("AUTH", "Starting device-code flow…")
    flow = app.initiate_device_flow(scopes=SCOPES)

    if "user_code" not in flow:
        _log("AUTH", f"FAILED to initiate device flow: {flow}")
        sys.exit(1)

    print()
    print("=" * 60)
    print("  SIGN IN REQUIRED")
    print("=" * 60)
    print(f"  1. Open this URL in your browser:")
    print(f"     {flow['verification_uri']}")
    print()
    print(f"  2. Enter this code when prompted:")
    print(f"     {flow['user_code']}")
    print()
    print("  Waiting for you to complete sign-in…")
    print("=" * 60)
    print()

    result = app.acquire_token_by_device_flow(flow)

    if "access_token" in result:
        _log("AUTH", "Device-code sign-in succeeded")
        _log("AUTH", f"Signed in as: {result.get('id_token_claims', {}).get('preferred_username', '?')}")
        _save_cache(cache)
        return result["access_token"]
    else:
        error = result.get("error", "unknown_error")
        desc  = result.get("error_description", "no description")
        _log("AUTH", f"FAILED: {error} — {desc}")
        if "AADSTS65001" in desc or "consent" in desc.lower():
            print()
            print("=" * 60)
            print("  ADMIN CONSENT REQUIRED")
            print("  Your IT administrator needs to grant consent for this")
            print("  application to send email on your behalf.")
            print()
            print("  Send your IT admin this information:")
            print(f"    App name:   Ship Ticket Sender")
            print(f"    Client ID:  {CLIENT_ID}")
            print(f"    Tenant ID:  {TENANT_ID}")
            print(f"    Permission: Mail.Send (delegated)")
            print("=" * 60)
        sys.exit(1)


def send_one_pdf(token: str, pdf_path: Path) -> None:
    """
    Send a single PDF via Graph /me/sendMail.
    Prints every detail of the request and response.
    """
    _log("GRAPH", f"Reading file: {pdf_path} ({pdf_path.stat().st_size:,} bytes)")
    with open(pdf_path, "rb") as f:
        content_bytes = f.read()
    content_b64 = base64.b64encode(content_bytes).decode("ascii")

    payload = {
        "message": {
            "subject": SUBJECT,
            "body": {"contentType": "Text", "content": ""},
            "toRecipients": [{"emailAddress": {"address": TO_EMAIL}}],
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

    _log("GRAPH", f"POST {GRAPH_URL}")
    _log("GRAPH", f"  To:          {TO_EMAIL}")
    _log("GRAPH", f"  Subject:     {SUBJECT}")
    _log("GRAPH", f"  Attachment:  {pdf_path.name}  ({len(content_bytes):,} bytes)")

    start = time.time()
    resp = requests.post(GRAPH_URL, headers=headers, json=payload, timeout=60)
    elapsed = time.time() - start

    _log("GRAPH", f"  HTTP status: {resp.status_code}  ({elapsed:.1f}s)")

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "10"))
        _log("GRAPH", f"  Throttled (429). Retry-After: {retry_after}s. Retrying…")
        time.sleep(retry_after + 1)
        resp = requests.post(GRAPH_URL, headers=headers, json=payload, timeout=60)
        _log("GRAPH", f"  Retry HTTP status: {resp.status_code}")

    if resp.status_code in (200, 202):
        _log("GRAPH", "  Response: accepted (no body expected for sendMail)")
    else:
        _log("GRAPH", f"  Response body: {resp.text[:500]}")
        resp.raise_for_status()


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        print("Usage:  python sender_test.py <path_to_pdf>")
        sys.exit(1)

    pdf_path = Path(sys.argv[1])

    if not pdf_path.exists():
        print(f"[ERROR] File not found: {pdf_path}")
        sys.exit(1)

    if not pdf_path.suffix.lower() == ".pdf":
        print(f"[ERROR] File must be a .pdf: {pdf_path}")
        sys.exit(1)

    print()
    print("Ship Ticket Sender — Auth + Send Test")
    print(f"  File:    {pdf_path}")
    print(f"  To:      {TO_EMAIL}")
    print(f"  Subject: {SUBJECT}")
    print()

    # Step 1: Auth
    token = acquire_token()

    # Step 2: Send
    print()
    _log("SEND", f"Sending {pdf_path.name}…")
    try:
        send_one_pdf(token, pdf_path)
        print()
        print("=" * 60)
        print(f"  [OK]  Email sent successfully.")
        print(f"  Check {TO_EMAIL} for:")
        print(f"    Subject:    {SUBJECT}")
        print(f"    Attachment: {pdf_path.name}")
        print("=" * 60)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        print()
        print("=" * 60)
        print(f"  [FAIL]  Graph returned HTTP {status}")
        if e.response is not None:
            print(f"  Response: {e.response.text[:500]}")
        print("=" * 60)
        sys.exit(1)
    except Exception as e:
        print()
        print(f"  [FAIL]  Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
