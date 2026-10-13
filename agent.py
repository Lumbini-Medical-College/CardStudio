#!/usr/bin/env python3
"""
Print agent for LMC Card Studio (server mode).

Runs on the PC that has the SD160 plugged in. It only makes outgoing requests
to the server: reports printer status, collects queued card sides, prints them
through the local driver and reports the result.

First time (saves agent.json next to this file):
    python agent.py --server http://192.168.1.50:8765 --key <agent key shown by the server>
Afterwards:
    python agent.py            (or double-click start_agent.bat)
Options:
    --printer "XPS Card Printer"   exact Windows printer name (first run picks the Entrust printer)
    --insecure                     accept a self-signed HTTPS certificate
"""

import argparse
import io
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "agent.json"
POLL_SECONDS = 2
FINISH_TIMEOUT = 240     # longest a single card side may take to come out


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    with open(HERE / "agent.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


class Server:
    def __init__(self, url, key, insecure=False):
        self.url, self.key = url.rstrip("/"), key
        self.ctx = ssl._create_unverified_context() if insecure else None

    def request(self, path, body=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method="POST" if data else "GET",
                                     headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30, context=self.ctx) as r:
            payload = r.read()
        return payload if raw else json.loads(payload or b"{}")


def wait_until_finished(printer):
    """After spooling, wait for the card to come out (queue empty and printer not busy)."""
    start = time.time()
    time.sleep(3)
    while time.time() - start < FINISH_TIMEOUT:
        s = printer.status()
        if s["status"] == "Error" or not s["link"]:
            return s["message"] or "Printer went offline while printing."
        if s["status"] == "Ready" and not s["queue"]:
            return None
        time.sleep(2)
    return "The printer did not finish within 4 minutes."


def handle(server, printer, job):
    from PIL import Image
    jid = job["id"]
    log(f"Job {jid}: {job['employee_id']} {job['side']}")
    server.request(f"/api/agent/jobs/{jid}", {"status": "printing"})
    try:
        page = Image.open(io.BytesIO(server.request(f"/api/agent/jobs/{jid}/page", raw=True))).convert("RGB")
        mask = Image.open(io.BytesIO(server.request(f"/api/agent/jobs/{jid}/mask", raw=True))).convert("RGBA")
        boxes = [tuple(b) for b in job["boxes"]]
        printer_job = printer.print_card({"page": page, "mask": mask, "boxes": boxes},
                                         f"{job['employee_id']} {job['side']}")
        error = wait_until_finished(printer)
    except Exception as e:  # report every failure to the server
        printer_job, error = None, str(e)
    server.request(f"/api/agent/jobs/{jid}", {"status": "failed" if error else "done",
                                              "printer_job": printer_job, "error": error})
    log(f"Job {jid}: {'failed: ' + error if error else 'done'}")


def main():
    ap = argparse.ArgumentParser(description="LMC Card Studio print agent")
    ap.add_argument("--server", help="server address, e.g. http://192.168.1.50:8765")
    ap.add_argument("--key", help="agent key shown when the server starts")
    ap.add_argument("--printer", help="exact printer name (Windows)")
    ap.add_argument("--insecure", action="store_true", help="accept a self-signed HTTPS certificate")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    for k in ("server", "key", "printer"):
        if getattr(args, k):
            cfg[k] = getattr(args, k)
    if args.insecure:
        cfg["insecure"] = True
    if not cfg.get("server") or not cfg.get("key"):
        sys.exit("First run: python agent.py --server http://<server-ip>:8765 --key <agent key>")
    CONFIG.write_text(json.dumps(cfg, indent=2))
    if not cfg.get("printer") and sys.platform == "win32":   # first run: take the Entrust printer, remember it
        import printer_windows
        cfg["printer"] = printer_windows.suggest_printer()
        if not cfg["printer"]:
            sys.exit('No Entrust card printer found. Run: python agent.py --printer "Exact Printer Name"')
        CONFIG.write_text(json.dumps(cfg, indent=2))
        log(f"Using printer {cfg['printer']} (change with --printer)")
    if cfg.get("printer"):
        os.environ["CARD_PRINTER"] = cfg["printer"]

    sys.path.insert(0, str(HERE))
    import printer

    server = Server(cfg["server"], cfg["key"], cfg.get("insecure", False))
    host = socket.gethostname()
    log(f"Print agent started on {host}, server {cfg['server']}")
    last_error = None
    while True:
        try:
            status = printer.status()
            reply = server.request("/api/agent/heartbeat", {"status": status, "host": host})
            if last_error:
                log("Connected to the server again.")
                last_error = None
            if reply.get("job"):
                handle(server, printer, reply["job"])
                continue
        except urllib.error.HTTPError as e:
            msg = "Wrong agent key: check --key." if e.code == 401 else f"Server error {e.code}"
            if msg != last_error:
                log(msg)
                last_error = msg
        except Exception as e:  # network down, server restarting, ...
            msg = f"Cannot reach the server ({e}). Retrying..."
            if msg != last_error:
                log(msg)
                last_error = msg
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
