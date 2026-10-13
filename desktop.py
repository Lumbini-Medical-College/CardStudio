"""
LMC Card Studio desktop app (local mode in its own window).

Packaged as CardStudio.exe by build_windows.bat. Run from source with:
    python desktop.py
Settings (optional) go in data/settings.json, e.g. {"printer": "XPS Card Printer"}.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from paths import DATA

TITLE = "LMC Card Studio"
PREFERRED_PORT = 8765


def setup_logging():
    """A windowed .exe has no console: send output to data/cardstudio.log."""
    if sys.stdout is None or sys.stderr is None:
        log = open(DATA / "cardstudio.log", "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log


def load_settings():
    path = DATA / "settings.json"
    if not path.exists():
        path.write_text(json.dumps({"printer": ""}, indent=2))
    try:
        settings = json.loads(path.read_text())
    except ValueError:
        settings = {}
    if settings.get("printer"):
        os.environ["CARD_PRINTER"] = settings["printer"]


def ours(port):
    """Is Card Studio already answering on this port?"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/me", timeout=1) as r:
            return json.loads(r.read()).get("mode") == "local"
    except Exception:
        return False


def free_port(preferred):
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue


def start_server():
    if ours(PREFERRED_PORT):       # already open: just show another window
        return PREFERRED_PORT
    import server
    port = free_port(PREFERRED_PORT)
    threading.Thread(target=server.serve, kwargs={"port": port, "mode": "local"}, daemon=True).start()
    for _ in range(100):
        if ours(port):
            return port
        time.sleep(0.1)
    raise RuntimeError("The app's server did not start. See data/cardstudio.log.")


def open_window(url):
    try:
        import webview
    except ImportError:
        webview = None
    if webview:
        webview.create_window(TITLE, url, width=1380, height=920, min_size=(1000, 680))
        # Not private, so the page remembers the light/dark choice between runs.
        webview.start(private_mode=False, storage_path=str(DATA / "webview"))   # returns when the window is closed
        return
    # Fallback: Edge in app mode (no browser bars), then the default browser.
    edge = shutil.which("msedge") or next((p for p in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") if os.path.exists(p)), None)
    if edge:
        subprocess.Popen([edge, f"--app={url}", "--window-size=1380,920"])
    else:
        webbrowser.open(url)
    while True:                  # keep the server running for the browser window
        time.sleep(3600)


def main():
    setup_logging()
    load_settings()
    port = start_server()
    open_window(f"http://127.0.0.1:{port}/")


if __name__ == "__main__":
    main()
