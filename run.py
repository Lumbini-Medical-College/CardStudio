#!/usr/bin/env python3
"""
LMC Card Studio launcher (Windows and macOS).

    python run.py --setup          install the Python packages and the Poppins font (first time)
    python run.py                  local mode: this computer prints directly (http://localhost:8765)
    python run.py --check          show what is installed and which printer will be used

Server mode (shared server on the office network; agent.py prints on the printer PC):
    python run.py --add-user NAME  create a staff login (or change its password)
    python run.py --del-user NAME  remove a staff login
    python run.py --users          list staff logins
    python run.py --server         start in server mode on all network interfaces

Options:
    --port 8765                    port for the web page
    --printer "XPS Card Printer"   Windows printer name (local mode), if not found automatically
    --cert cert.pem --key key.pem  serve HTTPS with this certificate
    --no-browser                   don't open the browser
"""

import argparse
import getpass
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

FONT_URL = "https://github.com/google/fonts/raw/main/ofl/poppins/{}"
MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net_human_seg.onnx"
FONT_FILES = ["Poppins-Regular.ttf", "Poppins-Medium.ttf", "Poppins-SemiBold.ttf", "Poppins-Bold.ttf"]


def has(module):
    return importlib.util.find_spec(module) is not None


def setup():
    """Install requirements for this OS and fetch the design's font."""
    reqs = ["requirements.txt"]
    if not IS_MAC:   # macOS uses its built-in Vision tools instead
        reqs.append("requirements-extras.txt")
    if IS_WINDOWS:
        reqs.append("requirements-windows.txt")
    for req in reqs:
        print(f"Installing packages from {req} ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(HERE / req)])

    if not IS_MAC:   # photo background removal model (U^2-Net human segmentation, as used by rembg)
        models = HERE / "models"
        models.mkdir(exist_ok=True)
        target = models / "u2net_human_seg.onnx"
        if not target.exists():
            print("Downloading the photo background-removal model (~170 MB) ...")
            urllib.request.urlretrieve(MODEL_URL, target)

    fonts = HERE / "fonts"
    fonts.mkdir(exist_ok=True)
    for name in FONT_FILES:
        target = fonts / name
        if not target.exists():
            print(f"Downloading {name} (Google Fonts, Open Font License) ...")
            urllib.request.urlretrieve(FONT_URL.format(name), target)

    if IS_MAC:
        build_mac_tools()
    print("\nSetup complete. Start the app with:  python run.py")


def build_mac_tools():
    """Compile the macOS Vision helpers (barcode reader, photo cut-out) if missing."""
    if not shutil.which("swiftc"):
        print("swiftc not found: install Xcode Command Line Tools to enable the macOS helpers.")
        return
    (HERE / "bin").mkdir(exist_ok=True)
    for tool in ("scan", "cutout"):
        if not (HERE / "bin" / tool).exists():
            print(f"Building bin/{tool} ...")
            subprocess.check_call(["swiftc", "-O", "-o", str(HERE / "bin" / tool), str(HERE / f"{tool}.swift")])


def check(verbose=True):
    """Report what is available. Returns False if the app cannot start."""
    ok = True
    lines = [f"Python {platform.python_version()} on {platform.system()}"]
    if sys.version_info < (3, 9):
        lines.append("  !! Python 3.9 or newer is required")
        ok = False
    for module, label in (("PIL", "Pillow"), ("pdf417gen", "pdf417gen")):
        if not has(module):
            lines.append(f"  !! {label} missing  -> python run.py --setup")
            ok = False

    if ok:
        sys.path.insert(0, str(HERE))
        import card
        lines.append(f"  Barcode check:       {'yes' if card.can_scan() else 'no (python run.py --setup)'}")
        lines.append(f"  Photo cut-out:       {'yes' if card.can_remove_background() else 'no (python run.py --setup)'}")
        font = "Poppins" if (HERE / "fonts" / "Poppins-Bold.ttf").exists() else "system fallback"
        lines.append(f"  Font:                {font}")
        if IS_WINDOWS and "--server" not in sys.argv:
            if has("win32print"):
                import printer_windows
                lines.append(f"  Printer:             {printer_windows.find_printer() or 'NOT FOUND - install the Entrust XPS Card Printer Driver'}")
            else:
                lines.append("  Printer:             pywin32 missing -> python run.py --setup")
        elif "--server" in sys.argv:
            lines.append("  Printer:             print agent (agent.py on the printer PC)")
        else:
            lines.append("  Printer:             Docker container 'sd160-cups' (Entrust LXM driver)")
    if verbose:
        print("\n".join(lines))
    return ok


def main():
    ap = argparse.ArgumentParser(description="LMC Card Studio")
    ap.add_argument("--setup", action="store_true", help="install packages and fonts")
    ap.add_argument("--check", action="store_true", help="show what is installed")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--printer", help="Windows printer name to use")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--server", action="store_true", help="server mode: logins + print queue for agent.py")
    ap.add_argument("--host", help="address to listen on (server mode default: all interfaces)")
    ap.add_argument("--cert", help="HTTPS certificate (PEM)")
    ap.add_argument("--key", help="HTTPS private key (PEM)")
    ap.add_argument("--add-user", metavar="NAME")
    ap.add_argument("--del-user", metavar="NAME")
    ap.add_argument("--users", action="store_true", help="list staff logins")
    args = ap.parse_args()

    os.chdir(HERE)
    if args.printer:
        os.environ["CARD_PRINTER"] = args.printer
    if args.setup:
        setup()
        return
    if args.add_user or args.del_user or args.users:
        sys.path.insert(0, str(HERE))
        import db
        if args.add_user:
            pw = getpass.getpass(f"Password for {args.add_user} (min 8 characters): ")
            if pw != getpass.getpass("Repeat password: "):
                sys.exit("Passwords don't match.")
            db.set_user(args.add_user, pw)
            print(f"Saved login '{args.add_user}'.")
        if args.del_user:
            print("Removed." if db.delete_user(args.del_user) else "No such user.")
        if args.users:
            print("\n".join(db.list_users()) or "No staff logins yet.")
        return
    if not check(verbose=True) or args.check:
        return
    if IS_MAC:
        build_mac_tools()

    sys.path.insert(0, str(HERE))
    import server
    mode = "server" if args.server else "local"
    url = f"{'https' if args.cert else 'http'}://localhost:{args.port}"
    if not args.no_browser and not args.server:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve(args.port, mode=mode, host=args.host, certfile=args.cert, keyfile=args.key)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
