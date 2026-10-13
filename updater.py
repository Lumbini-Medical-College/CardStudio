"""
Keeps the Windows desktop app (CardStudio.exe) up to date from GitHub Releases.

The page asks for check() now and then; when a newer release is out it offers
"Update and restart". install() downloads CardStudio-windows.zip into data\\update,
then a small batch script waits for the app to close, copies the new files over
the program folder (leaving data\\ alone) and starts the new version.

Only the packaged .exe updates itself; running from source, use git pull.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from paths import DATA, FROZEN
from version import VERSION

REPO = "Lumbini-Medical-College/CardStudio"
ASSET = "CardStudio-windows.zip"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_EVERY = 6 * 3600
APP_DIR = Path(sys.executable).resolve().parent
STAGING = DATA / "update"

_cache = {"at": 0.0, "info": None}
progress = {"state": "idle", "done": 0, "total": 0, "error": None}   # idle|downloading|installing|restarting|failed
_lock = threading.Lock()


def parse(version):
    """'v1.2.3' -> (1, 2, 3); None for anything else (e.g. 'dev')."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", (version or "").strip())
    return tuple(int(n) for n in m.group(1).split(".")) if m else None


def why_not():
    """Reason this copy can't update itself, or None if it can."""
    if not (FROZEN and sys.platform == "win32"):
        return "Only the Windows app updates itself. From source, use git pull."
    if parse(VERSION) is None:
        return "This is a test build without a version number."
    if DATA.resolve() != (APP_DIR / "data").resolve():
        return "The program folder isn't writable. Download the new version from the release page."
    return None


def _get(url, accept="application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"CardStudio/{VERSION}"})
    return urllib.request.urlopen(req, timeout=30)


def check(force=False):
    """What's the newest release, and is it newer than this one?"""
    info = {"current": VERSION, "latest": None, "available": False, "url": None, "notes": "",
            "can_install": why_not() is None, "reason": why_not()}
    if parse(VERSION) is None:
        return info
    if not force and _cache["info"] and time.time() - _cache["at"] < CHECK_EVERY:
        return _cache["info"]
    try:
        with _get(LATEST) as r:
            rel = json.loads(r.read())
        asset = next((a for a in rel.get("assets", []) if a["name"] == ASSET), None)
        latest = parse(rel.get("tag_name"))
        info.update(latest=rel.get("tag_name"), url=rel.get("html_url"), notes=(rel.get("body") or "")[:2000],
                    available=bool(asset and latest and latest > parse(VERSION)),
                    download=asset and asset["browser_download_url"], size=asset and asset["size"])
    except Exception as e:  # offline, rate-limited, repo private...
        info["error"] = f"Couldn't check for updates: {e}"
    _cache.update(at=time.time(), info=info)
    return info


def start_install():
    with _lock:
        if progress["state"] in ("downloading", "installing", "restarting"):
            return dict(progress)
        reason = why_not()
        info = check(force=True)
        if reason or not info.get("available"):
            raise ValueError(reason or info.get("error") or "You already have the latest version.")
        progress.update(state="downloading", done=0, total=info.get("size") or 0, error=None)
    threading.Thread(target=_install, args=(info,), daemon=True).start()
    return dict(progress)


def _install(info):
    try:
        shutil.rmtree(STAGING, ignore_errors=True)
        STAGING.mkdir(parents=True)
        zip_path = STAGING / ASSET
        with _get(info["download"], accept="application/octet-stream") as r, open(zip_path, "wb") as f:
            progress["total"] = int(r.headers.get("Content-Length") or progress["total"])
            while chunk := r.read(1 << 20):
                f.write(chunk)
                progress["done"] += len(chunk)

        progress["state"] = "installing"
        new = STAGING / "new"
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(new)
        exe = next(new.rglob("CardStudio.exe"), None)
        if not exe:
            raise RuntimeError("The download doesn't contain CardStudio.exe.")
        _swap_and_restart(exe.parent)
    except Exception as e:
        progress.update(state="failed", error=str(e))


def _swap_and_restart(src):
    """Hand over to a batch script: wait for us to exit, copy the new files in, start the new exe."""
    pid, app, log = os.getpid(), APP_DIR, DATA / "update.log"
    script = DATA / "update.bat"
    script.write_text(f"""@echo off
chcp 65001 >nul
:wait
tasklist /NH /FI "PID eq {pid}" 2>nul | find " {pid} " >nul && (ping -n 2 127.0.0.1 >nul & goto wait)
robocopy "{src}" "{app}" /MIR /XD "{app}\\data" /R:10 /W:1 /NP /NFL /NDL >> "{log}"
start "" "{app}\\CardStudio.exe"
rmdir /s /q "{STAGING}"
""", encoding="utf-8")
    subprocess.Popen(["cmd", "/c", str(script)], cwd=str(DATA), close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    progress["state"] = "restarting"
    time.sleep(1.5)   # let the page see "restarting"
    os._exit(0)
