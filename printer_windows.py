"""
Windows: prints through Entrust's XPS Card Printer Driver (install it from entrust.com first).

The card is sent as one portrait page. The barcode and QR areas are drawn a
second time as 1-bit (pure black/white) bitmaps on top, which the XPS driver
can print with the black K panel when "K panel" printing of monochrome
bitmaps is enabled in the printer's Printing Preferences.
"""

import os

from PIL import Image

import card

try:
    import win32con
    import win32gui
    import win32print
    import win32ui
    from PIL import ImageWin
except ImportError:  # pywin32 missing: the app still runs, printing reports why it can't
    win32print = None

NAME_HINTS = ("SD160", "XPS Card Printer", "Entrust", "Datacard")

# Printer status bits (winspool.h)
S = {"PAUSED": 0x1, "ERROR": 0x2, "PAPER_JAM": 0x8, "PAPER_OUT": 0x10, "PAPER_PROBLEM": 0x40,
     "OFFLINE": 0x80, "BUSY": 0x200, "PRINTING": 0x400, "OUTPUT_BIN_FULL": 0x800,
     "NOT_AVAILABLE": 0x1000, "PROCESSING": 0x4000, "TONER_LOW": 0x20000, "NO_TONER": 0x40000,
     "USER_INTERVENTION": 0x100000, "DOOR_OPEN": 0x400000}
MESSAGES = {"PAPER_JAM": "Card jam", "PAPER_OUT": "Out of cards", "PAPER_PROBLEM": "Card feed problem",
            "OUTPUT_BIN_FULL": "Output hopper full", "TONER_LOW": "Ribbon low", "NO_TONER": "Ribbon out",
            "DOOR_OPEN": "Cover open", "USER_INTERVENTION": "Printer needs attention", "PAUSED": "Printer paused"}
WORK_OFFLINE = 0x400  # PRINTER_ATTRIBUTE_WORK_OFFLINE


def list_printers():
    """Every printer installed on this PC."""
    if win32print is None:
        return []
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    return sorted(p[2] for p in win32print.EnumPrinters(flags))


def find_printer():
    """The printer chosen in the app's printer list (or run.py / agent.py --printer)."""
    return os.environ.get("CARD_PRINTER") or None


def suggest_printer():
    """First installed printer that looks like the Entrust/SD160, for the agent's first run."""
    names = list_printers()
    for hint in NAME_HINTS:
        for n in names:
            if hint.lower() in n.lower():
                return n
    return None


def status():
    result = {"link": False, "can_print": False, "status": "Offline", "ribbon": None, "model": None,
              "serial": None, "queue": [], "message": ""}
    if win32print is None:
        result["message"] = "pywin32 is not installed. Run: python run.py --setup"
        return result
    name = find_printer()
    if not name:
        result.update(status="Not selected", message="Choose the card printer from the printer list.")
        return result
    result["model"] = name
    try:
        h = win32print.OpenPrinter(name)
    except Exception:
        result["message"] = f"Printer \"{name}\" isn't installed. Choose another from the printer list."
        return result
    try:
        info = win32print.GetPrinter(h, 2)
        jobs = win32print.EnumJobs(h, 0, 50, 1)
    finally:
        win32print.ClosePrinter(h)
    st, attrs = info["Status"], info["Attributes"]
    result.update(link=True, can_print=True, queue=[str(j["JobId"]) for j in jobs])
    problems = [msg for key, msg in MESSAGES.items() if st & S[key]]
    # The card printer driver often tells Windows "offline" while the printer is fine (e.g. asleep),
    # so that is only a note: the person at the printer can see whether it is really on.
    if attrs & WORK_OFFLINE or st & (S["OFFLINE"] | S["NOT_AVAILABLE"]):
        problems.append("Windows reports it offline; you can still print")
    if st & (S["ERROR"] | S["PAPER_JAM"] | S["PAPER_OUT"] | S["NO_TONER"] | S["DOOR_OPEN"] | S["USER_INTERVENTION"]):
        result.update(status="Error", message="; ".join(problems) or "Printer error")
    elif st & (S["BUSY"] | S["PRINTING"] | S["PROCESSING"]) or result["queue"]:
        result.update(status="Busy", message="; ".join(problems))
    else:
        result.update(status="Ready", message="; ".join(problems))
    return result


def _rotate_box(b, width):
    """Box after rotating the design 90° counter-clockwise (PIL rotate(90))."""
    return (b[1], width - b[2], b[3], width - b[0])


def print_card(job, title):
    """Print one side. Returns the Windows print job id."""
    if win32print is None:
        raise RuntimeError("pywin32 is not installed. Run: python run.py --setup")
    name = find_printer()
    if not name:
        raise RuntimeError("Choose the card printer from the printer list first.")

    h = win32print.OpenPrinter(name)
    try:
        devmode = win32print.GetPrinter(h, 2)["pDevMode"]
        devmode.Orientation = win32con.DMORIENT_PORTRAIT
        devmode.Duplex = win32con.DMDUP_SIMPLEX          # SD160 prints one side; the card is flipped by hand
        devmode.Fields |= win32con.DM_ORIENTATION | win32con.DM_DUPLEX
        win32print.DocumentProperties(0, h, name, devmode, devmode,
                                      win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER)
    finally:
        win32print.ClosePrinter(h)

    page, boxes = card.device_image(job["page"]), list(job.get("boxes", []))   # 300 dpi, lossless
    dc = win32ui.CreateDCFromHandle(win32gui.CreateDC("WINSPOOL", name, devmode))
    try:
        pw, ph = dc.GetDeviceCaps(win32con.PHYSICALWIDTH), dc.GetDeviceCaps(win32con.PHYSICALHEIGHT)
        ox, oy = dc.GetDeviceCaps(win32con.PHYSICALOFFSETX), dc.GetDeviceCaps(win32con.PHYSICALOFFSETY)
        design_w, design_h = card.DESIGN
        if pw > ph:  # driver insists on landscape: turn the card to match
            page = page.rotate(90, expand=True)
            boxes = [_rotate_box(b, design_w) for b in boxes]
            design_w, design_h = design_h, design_w

        # Hand Windows an image that already has the printer's own size, so it is copied
        # dot for dot instead of being stretched (GDI's stretching drops pixels).
        if page.size != (pw, ph):
            page = page.resize((pw, ph), Image.LANCZOS)

        job_id = dc.StartDoc(title)
        dc.StartPage()
        out = dc.GetHandleOutput()
        ImageWin.Dib(page).draw(out, (-ox, -oy, pw - ox, ph - oy))   # full card, edge to edge
        sx, sy = pw / design_w, ph / design_h
        for b in boxes:
            r = (round(b[0] * sx), round(b[1] * sy), round(b[2] * sx), round(b[3] * sy))
            mono = page.crop(r).convert("L").point(lambda v: 0 if v < 140 else 255).convert("1")
            ImageWin.Dib(mono).draw(out, (r[0] - ox, r[1] - oy, r[2] - ox, r[3] - oy))
        dc.EndPage()
        dc.EndDoc()
    finally:
        dc.DeleteDC()
    return str(job_id)
