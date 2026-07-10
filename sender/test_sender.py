"""
test_sender.py — Unit tests for sender_core.py logic (no tkinter dependency).
Tests: PDF filename pattern, sent.log read/write/clear, send_pdf payload shape.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, os.path.dirname(__file__))
import sender_core as S


class TestPdfPattern(unittest.TestCase):
    def _match(self, name):
        return bool(S.PDF_PATTERN.match(name))

    def test_5_digit(self):
        self.assertTrue(self._match("12345.pdf"))

    def test_6_digit(self):
        self.assertTrue(self._match("248256.pdf"))

    def test_7_digit(self):
        self.assertTrue(self._match("1234567.pdf"))

    def test_6_digit_suffix_1(self):
        self.assertTrue(self._match("253027-1.pdf"))

    def test_6_digit_suffix_2(self):
        self.assertTrue(self._match("253027-2.pdf"))

    def test_uppercase_extension(self):
        self.assertTrue(self._match("248256.PDF"))

    def test_4_digit_rejected(self):
        self.assertFalse(self._match("1234.pdf"))

    def test_8_digit_rejected(self):
        self.assertFalse(self._match("12345678.pdf"))

    def test_non_numeric_rejected(self):
        self.assertFalse(self._match("invoice.pdf"))

    def test_suffix_3_accepted(self):
        """Suffix -3 is accepted: pattern allows any 1-2 digit suffix."""
        self.assertTrue(self._match("253027-3.pdf"))

    def test_no_extension_rejected(self):
        self.assertFalse(self._match("248256"))


class TestSentLog(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.folder = Path(self.tmpdir)

    def test_empty_log_returns_empty_set(self):
        result = S.load_sent_log(self.folder)
        self.assertEqual(result, set())

    def test_append_and_load(self):
        S.append_sent_log(self.folder, "248256.pdf")
        S.append_sent_log(self.folder, "248258.pdf")
        sent = S.load_sent_log(self.folder)
        self.assertIn("248256.pdf", sent)
        self.assertIn("248258.pdf", sent)

    def test_load_does_not_include_comments(self):
        log_path = self.folder / S.SENT_LOG
        with open(log_path, "w") as f:
            f.write("# this is a comment\n")
            f.write("248256.pdf\t2026-07-09T10:00:00Z\tcng@stacksbowers.com\t123\n")
        sent = S.load_sent_log(self.folder)
        self.assertIn("248256.pdf", sent)
        self.assertNotIn("# this is a comment", sent)

    def test_clear_sent_log(self):
        S.append_sent_log(self.folder, "248256.pdf")
        S.clear_sent_log(self.folder)
        sent = S.load_sent_log(self.folder)
        self.assertEqual(sent, set())

    def test_clear_nonexistent_log_is_safe(self):
        S.clear_sent_log(self.folder)  # should not raise

    def test_log_entry_format(self):
        """Each log line must have: filename, ISO timestamp, recipient, subject."""
        S.append_sent_log(self.folder, "248256.pdf")
        log_path = self.folder / S.SENT_LOG
        with open(log_path) as f:
            line = f.read().strip()
        parts = line.split("\t")
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], "248256.pdf")
        self.assertRegex(parts[1], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
        self.assertEqual(parts[2], S.TO_EMAIL)
        self.assertEqual(parts[3], S.SUBJECT)


class TestSendPdfPayload(unittest.TestCase):
    """Verify send_pdf builds the correct Graph payload without making real HTTP calls."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.pdf_path = Path(self.tmpdir) / "248256.pdf"
        self.pdf_path.write_bytes(b"%PDF-1.4 fake content")

    def test_payload_structure(self):
        captured = {}

        def fake_post(url, headers, json, timeout):
            captured["url"] = url
            captured["payload"] = json
            resp = MagicMock()
            resp.status_code = 202
            resp.raise_for_status = MagicMock()
            return resp

        with patch("sender_core.requests.post", side_effect=fake_post):
            S.send_pdf("fake_token", self.pdf_path)

        self.assertEqual(captured["url"], S.GRAPH_URL)
        msg = captured["payload"]["message"]
        self.assertEqual(msg["subject"], S.SUBJECT)
        self.assertEqual(msg["toRecipients"][0]["emailAddress"]["address"], S.TO_EMAIL)
        att = msg["attachments"][0]
        self.assertEqual(att["name"], "248256.pdf")
        self.assertEqual(att["contentType"], "application/pdf")
        import base64
        decoded = base64.b64decode(att["contentBytes"])
        self.assertEqual(decoded, b"%PDF-1.4 fake content")

    def test_429_retry(self):
        """On 429, send_pdf must wait retry-after seconds and retry once."""
        call_count = [0]

        def fake_post(url, headers, json, timeout):
            call_count[0] += 1
            resp = MagicMock()
            if call_count[0] == 1:
                resp.status_code = 429
                resp.headers = {"Retry-After": "1"}
                resp.raise_for_status = MagicMock(side_effect=None)
            else:
                resp.status_code = 202
                resp.raise_for_status = MagicMock()
            return resp

        with patch("sender_core.requests.post", side_effect=fake_post), \
             patch("sender_core.time.sleep") as mock_sleep:
            S.send_pdf("fake_token", self.pdf_path)

        self.assertEqual(call_count[0], 2, "Must retry exactly once after 429")
        mock_sleep.assert_called_once_with(2)  # retry_after(1) + 1

    def test_http_error_raises(self):
        """A non-429 HTTP error must raise HTTPError (caller skips the file)."""
        import requests as req

        def fake_post(url, headers, json, timeout):
            resp = MagicMock()
            resp.status_code = 500
            http_err = req.HTTPError(response=resp)
            resp.raise_for_status = MagicMock(side_effect=http_err)
            return resp

        with patch("sender_core.requests.post", side_effect=fake_post):
            with self.assertRaises(req.HTTPError):
                S.send_pdf("fake_token", self.pdf_path)


class TestConstants(unittest.TestCase):
    def test_to_email(self):
        self.assertEqual(S.TO_EMAIL, "cng@stacksbowers.com")

    def test_subject(self):
        self.assertEqual(S.SUBJECT, "123")

    def test_send_delay_in_range(self):
        self.assertGreaterEqual(S.SEND_DELAY, 3)
        self.assertLessEqual(S.SEND_DELAY, 5)

    def test_client_id(self):
        self.assertEqual(S.CLIENT_ID, "61249134-e089-422b-bd52-688eb7cafa01")

    def test_tenant_id(self):
        self.assertEqual(S.TENANT_ID, "893a34dd-cb02-4c70-957d-794446df8feb")


if __name__ == "__main__":
    unittest.main(verbosity=2)
