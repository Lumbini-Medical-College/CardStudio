"""
Windows: prints through Entrust's XPS Card Printer Driver (install it from entrust.com first).

The card is sent as one portrait page. The barcode and QR areas are drawn a
second time as 1-bit (pure black/white) bitmaps on top, which the XPS driver
can print with the black K panel when "K panel" printing of monochrome
bitmaps is enabled in the printer's Printing Preferences.
"""

import os

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
    """The printer named in CARD_PRINTER (run.py --printer or the app's printer menu),
    else the first Entrust/SD160 printer."""
    if os.environ.get("CARD_PRINTER"):
        return os.environ["CARD_PRINTER"]
    names = list_printers()
    for hint in NAME_HINTS:
        for n in names:
            if hint.lower() in n.lower():
                return n
    return None


def status():
    result = {"link": False, "status": "Offline", "ribbon": None, "model": None, "serial": None,
              "queue": [], "message": ""}
    if win32print is None:
        result["message"] = "pywin32 is not installed. Run: python run.py --setup"
        return result
    name = find_printer()
    if not name:
        result["message"] = "No Entrust card printer found. Install the XPS Card Printer Driver."
        return result
    result["model"] = name
    try:
        h = win32print.OpenPrinter(name)
    except Exception:
        result["message"] = f"Printer \"{name}\" isn't installed. Pick another in the printer menu."
        return result
    try:
        info = win32print.GetPrinter(h, 2)
        jobs = win32print.EnumJobs(h, 0, 50, 1)
    finally:
        win32print.ClosePrinter(h)
    st, attrs = info["Status"], info["Attributes"]
    result["queue"] = [str(j["JobId"]) for j in jobs]
    problems = [msg for key, msg in MESSAGES.items() if st & S[key]]
    if attrs & WORK_OFFLINE or st & (S["OFFLINE"] | S["NOT_AVAILABLE"]):
        result["message"] = "Printer is offline. Check it is on and the USB cable is connected."
    elif st & (S["ERROR"] | S["PAPER_JAM"] | S["PAPER_OUT"] | S["NO_TONER"] | S["DOOR_OPEN"] | S["USER_INTERVENTION"]):
        result.update(link=True, status="Error", message="; ".join(problems) or "Printer error")
    elif st & (S["BUSY"] | S["PRINTING"] | S["PROCESSING"]) or result["queue"]:
        result.update(link=True, status="Busy", message="; ".join(problems))
    else:
        result.update(link=True, status="Ready", message="; ".join(problems))
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
        raise RuntimeError("No Entrust card printer found. Install the XPS Card Printer Driver.")

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

    page, boxes = job["page"], list(job.get("boxes", []))
    dc = win32ui.CreateDCFromHandle(win32gui.CreateDC("WINSPOOL", name, devmode))
    try:
        pw, ph = dc.GetDeviceCaps(win32con.PHYSICALWIDTH), dc.GetDeviceCaps(win32con.PHYSICALHEIGHT)
        ox, oy = dc.GetDeviceCaps(win32con.PHYSICALOFFSETX), dc.GetDeviceCaps(win32con.PHYSICALOFFSETY)
        design_w, design_h = card.DESIGN
        if pw > ph:  # driver insists on landscape: turn the card to match
            page = page.rotate(90, expand=True)
            boxes = [_rotate_box(b, design_w) for b in boxes]
            design_w, design_h = design_h, design_w

        job_id = dc.StartDoc(title)
        dc.StartPage()
        out = dc.GetHandleOutput()
        ImageWin.Dib(page).draw(out, (-ox, -oy, pw - ox, ph - oy))   # full card, edge to edge
        sx, sy = page.width / design_w, page.height / design_h
        for b in boxes:
            crop = page.crop((round(b[0] * sx), round(b[1] * sy), round(b[2] * sx), round(b[3] * sy)))
            mono = crop.convert("L").point(lambda v: 0 if v < 140 else 255).convert("1")
            ImageWin.Dib(mono).draw(out, (round(b[0] * pw / design_w) - ox, round(b[1] * ph / design_h) - oy,
                                          round(b[2] * pw / design_w) - ox, round(b[3] * ph / design_h) - oy))
        dc.EndPage()
        dc.EndDoc()
    finally:
        dc.DeleteDC()
    return str(job_id)
