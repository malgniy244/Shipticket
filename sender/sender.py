"""
sender.py — Ship Ticket PDF Sender (GUI entry point)
=====================================================
Tkinter GUI that drives sender_core.py.
Run directly:  python sender.py
Build .exe:    pyinstaller --onefile --windowed --name STS-Sender sender.py
"""
from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from pathlib import Path
from datetime import datetime

from sender_core import (
    acquire_token, send_pdf,
    load_sent_log, append_sent_log, clear_sent_log,
    PDF_PATTERN, TO_EMAIL, SUBJECT, SEND_DELAY,
)


class SenderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Ship Ticket Sender")
        self.geometry("720x600")
        self.resizable(True, True)
        self.configure(bg="#f4f5f7")

        self._folder: Path | None = None   # used for sent.log location
        self._pdfs: list[Path] = []
        self._token: str | None = None
        self._running = False
        self._mode: str = "folder"  # "folder" or "files"

        self._build_ui()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header ──
        hdr = tk.Frame(self, bg="#1e3a5f", pady=12)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Ship Ticket Sender", font=("Segoe UI", 16, "bold"),
                 bg="#1e3a5f", fg="white").pack(side="left", padx=20)
        tk.Label(hdr, text=f"→ {TO_EMAIL}  |  Subject: {SUBJECT}",
                 font=("Segoe UI", 10), bg="#1e3a5f", fg="#93c5fd").pack(side="left")

        # ── Picker row ──
        folder_frame = tk.Frame(self, bg="#f4f5f7", pady=10, padx=16)
        folder_frame.pack(fill="x")
        self._folder_var = tk.StringVar(value="(nothing selected)")
        tk.Label(folder_frame, textvariable=self._folder_var, font=("Segoe UI", 10),
                 bg="#f4f5f7", fg="#374151", width=52, anchor="w").pack(side="left", padx=(0, 8))
        tk.Button(folder_frame, text="Select Folder…", command=self._pick_folder,
                  font=("Segoe UI", 9), bg="#e5e7eb", relief="flat",
                  padx=10, pady=4, cursor="hand2").pack(side="left")
        tk.Button(folder_frame, text="Select Files…", command=self._pick_files,
                  font=("Segoe UI", 9), bg="#e5e7eb", relief="flat",
                  padx=10, pady=4, cursor="hand2").pack(side="left", padx=(6, 0))

        # ── File list ──
        list_frame = tk.Frame(self, bg="#f4f5f7", padx=16)
        list_frame.pack(fill="both", expand=False)
        tk.Label(list_frame, text="Files to send:", font=("Segoe UI", 9, "bold"),
                 bg="#f4f5f7", fg="#6b7280").pack(anchor="w")

        list_container = tk.Frame(list_frame, bg="#f4f5f7")
        list_container.pack(fill="x")
        self._listbox = tk.Listbox(list_container, height=7, font=("Consolas", 10),
                                   selectmode="browse", bg="white", relief="flat",
                                   borderwidth=1, highlightthickness=1,
                                   highlightbackground="#d1d5db")
        scrollbar = tk.Scrollbar(list_container, orient="vertical",
                                 command=self._listbox.yview)
        self._listbox.config(yscrollcommand=scrollbar.set)
        self._listbox.pack(side="left", fill="x", expand=True)
        scrollbar.pack(side="right", fill="y")

        # ── Auth status ──
        auth_frame = tk.Frame(self, bg="#f4f5f7", padx=16, pady=6)
        auth_frame.pack(fill="x")
        tk.Label(auth_frame, text="Sign-in:", font=("Segoe UI", 9, "bold"),
                 bg="#f4f5f7", fg="#6b7280").pack(side="left")
        self._auth_var = tk.StringVar(value="Not signed in")
        self._auth_label = tk.Label(auth_frame, textvariable=self._auth_var,
                                    font=("Segoe UI", 9), bg="#f4f5f7", fg="#dc2626")
        self._auth_label.pack(side="left", padx=8)
        self._sign_in_btn = tk.Button(auth_frame, text="Sign in…",
                                      command=self._start_sign_in,
                                      font=("Segoe UI", 9), bg="#2563eb", fg="white",
                                      relief="flat", padx=10, pady=3, cursor="hand2")
        self._sign_in_btn.pack(side="left")

        # ── Device-code panel (hidden until needed) ──
        self._dc_frame = tk.Frame(self, bg="#eff6ff", padx=16, pady=12,
                                  relief="flat", bd=1)
        # Not packed initially — shown only during device-code flow

        tk.Label(self._dc_frame, text="Sign in to Microsoft",
                 font=("Segoe UI", 12, "bold"), bg="#eff6ff", fg="#1e40af").pack(anchor="w")
        tk.Label(self._dc_frame,
                 text="1. Open the link below in your browser (click to copy)",
                 font=("Segoe UI", 9), bg="#eff6ff", fg="#374151").pack(anchor="w", pady=(6, 0))
        self._dc_url_var = tk.StringVar()
        self._dc_url_label = tk.Label(self._dc_frame, textvariable=self._dc_url_var,
                                      font=("Segoe UI", 10, "underline"),
                                      bg="#eff6ff", fg="#2563eb", cursor="hand2")
        self._dc_url_label.pack(anchor="w", padx=8)
        self._dc_url_label.bind("<Button-1>", self._copy_url)

        tk.Label(self._dc_frame, text="2. Enter this code:",
                 font=("Segoe UI", 9), bg="#eff6ff", fg="#374151").pack(anchor="w", pady=(8, 0))
        self._dc_code_var = tk.StringVar()
        code_row = tk.Frame(self._dc_frame, bg="#eff6ff")
        code_row.pack(anchor="w", padx=8)
        self._dc_code_label = tk.Label(code_row, textvariable=self._dc_code_var,
                                       font=("Consolas", 22, "bold"),
                                       bg="#dbeafe", fg="#1e40af",
                                       padx=12, pady=4, relief="flat", bd=0)
        self._dc_code_label.pack(side="left")
        tk.Button(code_row, text="Copy", command=self._copy_code,
                  font=("Segoe UI", 9), bg="#e5e7eb", relief="flat",
                  padx=8, pady=4, cursor="hand2").pack(side="left", padx=8)

        self._dc_status_var = tk.StringVar(value="Waiting for sign-in…")
        tk.Label(self._dc_frame, textvariable=self._dc_status_var,
                 font=("Segoe UI", 9, "italic"), bg="#eff6ff", fg="#6b7280").pack(anchor="w", pady=(8, 0))

        # ── Progress bar ──
        prog_frame = tk.Frame(self, bg="#f4f5f7", padx=16, pady=4)
        prog_frame.pack(fill="x")
        self._progress = ttk.Progressbar(prog_frame, orient="horizontal",
                                         mode="determinate", length=680)
        self._progress.pack(fill="x")
        self._prog_label_var = tk.StringVar(value="")
        tk.Label(prog_frame, textvariable=self._prog_label_var,
                 font=("Segoe UI", 9), bg="#f4f5f7", fg="#6b7280").pack(anchor="e")

        # ── Log ──
        log_frame = tk.Frame(self, bg="#f4f5f7", padx=16, pady=4)
        log_frame.pack(fill="both", expand=True)
        tk.Label(log_frame, text="Activity log:", font=("Segoe UI", 9, "bold"),
                 bg="#f4f5f7", fg="#6b7280").pack(anchor="w")
        self._log = scrolledtext.ScrolledText(log_frame, height=8, font=("Consolas", 9),
                                              bg="#1f2937", fg="#d1fae5",
                                              insertbackground="white", relief="flat",
                                              state="disabled")
        self._log.pack(fill="both", expand=True)

        # ── Action buttons ──
        btn_frame = tk.Frame(self, bg="#f4f5f7", padx=16, pady=10)
        btn_frame.pack(fill="x")
        self._send_btn = tk.Button(btn_frame, text="Send All PDFs",
                                   command=self._start_send,
                                   font=("Segoe UI", 11, "bold"),
                                   bg="#16a34a", fg="white", relief="flat",
                                   padx=20, pady=8, cursor="hand2",
                                   state="disabled")
        self._send_btn.pack(side="left")
        self._resend_btn = tk.Button(btn_frame, text="Resend All (ignore sent.log)",
                                     command=self._resend_all,
                                     font=("Segoe UI", 9), bg="#e5e7eb",
                                     relief="flat", padx=10, pady=6,
                                     cursor="hand2", state="disabled")
        self._resend_btn.pack(side="left", padx=10)
        self._stop_btn = tk.Button(btn_frame, text="Stop",
                                   command=self._stop_send,
                                   font=("Segoe UI", 9), bg="#dc2626", fg="white",
                                   relief="flat", padx=10, pady=6,
                                   cursor="hand2", state="disabled")
        self._stop_btn.pack(side="left")

    # ── Pickers ──────────────────────────────────────────────────────────────

    def _pick_folder(self):
        folder = filedialog.askdirectory(title="Select folder containing PDFs")
        if not folder:
            return
        self._mode = "folder"
        self._folder = Path(folder)
        self._folder_var.set(str(self._folder))
        self._scan_folder()

    def _pick_files(self):
        files = filedialog.askopenfilenames(
            title="Select PDF file(s) to send",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")]
        )
        if not files:
            return
        self._mode = "files"
        paths = [Path(f) for f in files]
        # Use the parent of the first file as the sent.log location
        self._folder = paths[0].parent
        self._folder_var.set(
            f"{len(paths)} file(s) selected from {self._folder}"
        )
        self._load_explicit_files(paths)

    def _load_explicit_files(self, paths: list):
        sent = load_sent_log(self._folder)
        matching = [p for p in paths if PDF_PATTERN.match(p.name)]
        non_matching = [p for p in paths if not PDF_PATTERN.match(p.name)]

        self._pdfs = matching
        self._listbox.delete(0, "end")

        for p in matching:
            tag = " ✓ sent" if p.name in sent else ""
            self._listbox.insert("end", f"  {p.name}{tag}")
            if p.name in sent:
                idx = self._listbox.size() - 1
                self._listbox.itemconfig(idx, fg="#9ca3af")

        for p in non_matching:
            self._listbox.insert("end", f"  [skip — name doesn't match ticket pattern] {p.name}")
            idx = self._listbox.size() - 1
            self._listbox.itemconfig(idx, fg="#d1d5db")

        unsent = [p for p in matching if p.name not in sent]
        self._log_msg(f"Selected {len(matching)} matching PDF(s), "
                      f"{len(sent & {p.name for p in matching})} already sent, "
                      f"{len(unsent)} to send.")
        if non_matching:
            self._log_msg(f"Skipping {len(non_matching)} file(s) whose names "
                          f"don't match the ticket number pattern.")
        self._update_send_btn()

    def _scan_folder(self):
        if not self._folder:
            return
        all_pdfs = sorted(self._folder.glob("*.pdf"))
        matching = [p for p in all_pdfs if PDF_PATTERN.match(p.name)]
        non_matching = [p for p in all_pdfs if not PDF_PATTERN.match(p.name)]
        sent = load_sent_log(self._folder)

        self._pdfs = matching
        self._listbox.delete(0, "end")

        for p in matching:
            tag = " ✓ sent" if p.name in sent else ""
            self._listbox.insert("end", f"  {p.name}{tag}")
            if p.name in sent:
                idx = self._listbox.size() - 1
                self._listbox.itemconfig(idx, fg="#9ca3af")

        for p in non_matching:
            self._listbox.insert("end", f"  [skip] {p.name}")
            idx = self._listbox.size() - 1
            self._listbox.itemconfig(idx, fg="#d1d5db")

        unsent = [p for p in matching if p.name not in sent]
        self._log_msg(f"Folder: {self._folder}")
        self._log_msg(f"Found {len(matching)} matching PDF(s), "
                      f"{len(sent)} already sent, {len(unsent)} to send.")

        self._update_send_btn()

    # ── Auth ─────────────────────────────────────────────────────────────────

    def _start_sign_in(self):
        self._sign_in_btn.config(state="disabled", text="Signing in…")
        threading.Thread(target=self._do_sign_in, daemon=True).start()

    def _do_sign_in(self):
        def on_device_code(url: str, code: str):
            self.after(0, self._show_device_code, url, code)

        def on_waiting():
            self.after(0, lambda: self._dc_status_var.set(
                "Waiting for sign-in… (browser tab open)"))

        token = acquire_token(on_device_code, on_waiting)
        self.after(0, self._on_sign_in_done, token)

    def _show_device_code(self, url: str, code: str):
        self._dc_url_var.set(url)
        self._dc_code_var.set(code)
        self._dc_frame.pack(fill="x", padx=16, pady=4)
        self._log_msg(f"Device-code sign-in: {url}  code: {code}")

    def _on_sign_in_done(self, token):
        self._dc_frame.pack_forget()
        self._sign_in_btn.config(state="normal", text="Sign in…")
        if token:
            self._token = token
            self._auth_var.set("Signed in ✓")
            self._auth_label.config(fg="#16a34a")
            self._log_msg("Sign-in successful.")
        else:
            self._auth_var.set("Sign-in failed")
            self._auth_label.config(fg="#dc2626")
            self._log_msg("Sign-in failed. Check the Azure app registration and try again.")
        self._update_send_btn()

    def _copy_url(self, _event=None):
        url = self._dc_url_var.get()
        if url:
            self.clipboard_clear()
            self.clipboard_append(url)
            self._dc_status_var.set(
                "URL copied — paste it in your browser, then enter the code.")

    def _copy_code(self):
        code = self._dc_code_var.get()
        if code:
            self.clipboard_clear()
            self.clipboard_append(code)

    # ── Send ─────────────────────────────────────────────────────────────────

    def _update_send_btn(self):
        ready = (self._token is not None
                 and self._folder is not None
                 and len(self._pdfs) > 0
                 and not self._running)
        self._send_btn.config(state="normal" if ready else "disabled")
        self._resend_btn.config(
            state="normal" if (self._folder and not self._running) else "disabled")

    def _start_send(self):
        if not self._folder or not self._token:
            return
        sent = load_sent_log(self._folder)
        to_send = [p for p in self._pdfs if p.name not in sent]
        if not to_send:
            messagebox.showinfo(
                "Nothing to send",
                "All PDFs in this folder have already been sent.\n"
                "Use 'Resend All' to override.")
            return
        self._run_send(to_send)

    def _resend_all(self):
        if not self._folder or not self._token:
            return
        if not self._pdfs:
            messagebox.showinfo("No PDFs", "No matching PDFs found in this folder.")
            return
        if not messagebox.askyesno(
                "Resend all?",
                f"This will resend all {len(self._pdfs)} PDF(s), "
                f"ignoring sent.log.\n\nContinue?"):
            return
        clear_sent_log(self._folder)
        self._scan_folder()
        self._run_send(list(self._pdfs))

    def _run_send(self, pdfs: list):
        self._running = True
        self._send_btn.config(state="disabled")
        self._resend_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        self._progress["maximum"] = len(pdfs)
        self._progress["value"] = 0
        threading.Thread(target=self._send_worker, args=(pdfs,), daemon=True).start()

    def _stop_send(self):
        self._running = False
        self._log_msg("Stop requested — will stop after current send.")

    def _send_worker(self, pdfs: list):
        import requests as req
        total = len(pdfs)
        ok = 0
        failed = 0

        for i, pdf in enumerate(pdfs):
            if not self._running:
                self.after(0, self._log_msg, "Stopped by user.")
                break

            self.after(0, self._prog_label_var.set,
                       f"Sending {i+1}/{total}: {pdf.name}")
            self.after(0, self._log_msg, f"→ Sending {pdf.name}…")

            try:
                send_pdf(self._token, pdf)
                append_sent_log(self._folder, pdf.name)
                ok += 1
                self.after(0, self._log_msg, f"  ✓ Sent {pdf.name}")
                self.after(0, self._mark_sent_in_list, pdf.name)
            except req.HTTPError as e:
                failed += 1
                status = e.response.status_code if e.response is not None else "?"
                self.after(0, self._log_msg,
                           f"  ✗ FAILED {pdf.name} — HTTP {status}: {e}")
                if status == 401:
                    self.after(0, self._log_msg,
                               "  Token expired. Please sign in again and retry.")
                    self._token = None
                    self.after(0, self._auth_var.set, "Token expired — sign in again")
                    self.after(0, self._auth_label.config, {"fg": "#dc2626"})
                    self._running = False
                    break
            except Exception as e:
                failed += 1
                self.after(0, self._log_msg, f"  ✗ ERROR {pdf.name}: {e}")

            self.after(0, self._progress.__setitem__, "value", i + 1)

            if i < total - 1 and self._running:
                time.sleep(SEND_DELAY)

        self._running = False
        summary = f"Done. {ok} sent, {failed} failed."
        self.after(0, self._log_msg, summary)
        self.after(0, self._prog_label_var.set, summary)
        self.after(0, self._scan_folder)
        self.after(0, self._update_send_btn)
        self.after(0, self._stop_btn.config, {"state": "disabled"})

    def _mark_sent_in_list(self, filename: str):
        for i in range(self._listbox.size()):
            item = self._listbox.get(i)
            if filename in item and "✓ sent" not in item:
                self._listbox.delete(i)
                self._listbox.insert(i, f"  {filename} ✓ sent")
                self._listbox.itemconfig(i, fg="#9ca3af")
                break

    # ── Log ──────────────────────────────────────────────────────────────────

    def _log_msg(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log.config(state="normal")
        self._log.insert("end", f"[{ts}] {msg}\n")
        self._log.see("end")
        self._log.config(state="disabled")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    app = SenderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
