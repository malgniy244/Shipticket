# Ship Ticket Sender — Setup & Test Instructions

## Prerequisites

- **Python 3.11 or 3.12** (64-bit, Windows)
  Download from: https://www.python.org/downloads/
  During install: check **"Add Python to PATH"**

- **Two packages** (install once, takes ~10 seconds):
  ```
  pip install msal requests
  ```

---

## Step 1 — Verify Python is installed

Open **Command Prompt** (`Win + R` → type `cmd` → Enter) and run:

```
python --version
```

Expected output: `Python 3.11.x` or `Python 3.12.x`

If you see an error, Python is not on PATH — re-run the installer and check "Add Python to PATH".

---

## Step 2 — Install dependencies

In the same Command Prompt:

```
pip install msal requests
```

You should see `Successfully installed msal-...` and `requests-...`.

---

## Step 3 — Create a test folder with one dummy PDF

1. Create a folder, e.g. `C:\test_send\`
2. Put **one PDF** in it. It can be any PDF — even a blank one.
   Name it with a ticket-style name, e.g. `999999.pdf`
   (The filename is what appears as the email attachment name.)

---

## Step 4 — Run the auth + send test

In Command Prompt, navigate to the folder containing `sender_test.py`:

```
cd C:\path\to\sender
python sender_test.py C:\test_send\999999.pdf
```

---

## Step 5 — Complete the sign-in

The script will print something like:

```
============================================================
  SIGN IN REQUIRED
============================================================
  1. Open this URL in your browser:
     https://microsoft.com/devicelogin

  2. Enter this code when prompted:
     ABCD1234

  Waiting for you to complete sign-in…
============================================================
```

1. Open `https://microsoft.com/devicelogin` in your browser
2. Enter the code shown (e.g. `ABCD1234`)
3. Sign in with your `cng@stacksbowers.com` Microsoft account
4. You may be asked to approve the app — click **Accept**

---

## Step 6 — Check the outcome

**Success looks like:**
```
[GRAPH] POST https://graph.microsoft.com/v1.0/me/sendMail
[GRAPH]   To:          cng@stacksbowers.com
[GRAPH]   Subject:     123
[GRAPH]   Attachment:  999999.pdf  (12,345 bytes)
[GRAPH]   HTTP status: 202  (1.3s)
[GRAPH]   Response: accepted

============================================================
  [OK]  Email sent successfully.
  Check cng@stacksbowers.com for:
    Subject:    123
    Attachment: 999999.pdf
============================================================
```

Check your inbox at `cng@stacksbowers.com` for the email.

---

## Possible outcomes

| What you see | What it means | What to do |
|---|---|---|
| `[OK] Email sent successfully` | Auth and send both work | Report back as **(a)** — we proceed to the .exe |
| `ADMIN CONSENT REQUIRED` printed | IT needs to grant consent | Report back as **(b)** — take the consent request to IT |
| `HTTP status: 400` with `InvalidRecipients` | Graph accepted auth but rejected the send | Screenshot the full output and report as **(c)** |
| Any other error | Something unexpected | Screenshot the full output and report as **(c)** |

---

## Notes

- **Token caching**: after the first sign-in, the token is cached at `%USERPROFILE%\.sts_sender\token_cache.json`. Subsequent runs will not ask you to sign in again until the token expires (typically 1 hour for access token, but MSAL refreshes silently using the refresh token which lasts much longer).
- **No secrets in the script**: `CLIENT_ID` and `TENANT_ID` are public app-registration identifiers, not passwords or keys.
- **The test sends exactly one email** — no batch logic, no sent.log, no loop.
