"""
Builds LMC ID cards from the blank templates for the Datacard SD160.

Layout coordinates are measured on the 300 dpi design (638 x 1011 px, CR80 portrait).
Everything is drawn on a 600 dpi page (1276 x 2026 px), exactly twice the printer's
own 300 dpi grid (638 x 1013 dots). For printing it is halved to that grid and sent
losslessly, so the driver passes it to the printer dot for dot: nothing is rescaled
or JPEG-compressed, and every barcode bar lands on whole printer dots.
"""

import io
import json
import re
import subprocess
import sys
import tempfile
import zlib
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from pdf417gen import encode, render_image

from paths import RES

BIN = RES / "bin"
IS_MAC = sys.platform == "darwin"
TEMPLATES = RES / "templates"
FONTS = RES / "fonts"
MODELS = RES / "models"

DESIGN = (638, 1011)       # 300 dpi design canvas
DEVICE = (638, 1013)       # the driver's 300 dpi raster for an ISO ID-1 card (front/back panels)
DEVICE_PT = (DEVICE[0] * 72 / 300, DEVICE[1] * 72 / 300)   # PDF page that maps 1:1 onto it
PAGE = (DEVICE[0] * 2, DEVICE[1] * 2)                      # 600 dpi working page
MASK = (652, 1012)         # driver's DTC K-mask canvas
SX, SY = PAGE[0] / DESIGN[0], PAGE[1] / DESIGN[1]

NAME_RED = (153, 34, 38)
TEAL = (21, 86, 90)
INK = (52, 52, 52)

# --- layout (300 dpi design pixels) ---------------------------------------
NAME_RIGHT = 612
NAME_LEFT_LIMIT = 214                       # text may not start left of this
NAME_LINES = [(398, 38), (453, 38)]         # (baseline, cap height)
DESIG = (497, 22)
VALID = (477, 987, 17)                      # centre x, baseline, cap height
PHOTO_BOX = (0, 192, 365, 1011)             # area the person cut-out may use
SIGN_BOX = (392, 808, 598, 917)             # cardholder signature area
SIGN_BAR = (303, 923, 638, 962)             # red "Cardholder's Signature" bar
BARCODE_SLOT = (362, 510, 614, 628)       # below the designation's descenders, clear of the red shape
ID_LABEL_BOX = (100, 198, 300, 234)         # "Citizenship No:" printed on the back template
ID_LABEL = (295.5, 224, 17.5)               # its right edge, baseline, cap height (Poppins SemiBold)
ID_LABELS = {"citizenship": None, "passport": "Passport No:"}   # None: keep the template's label
BACK_VALUE_X, BACK_RIGHT = 311, 622
BACK_BASELINES = [round(58 + 41.4 * i) for i in range(9)]
BACK_CAP = 19
BACK_QR_BOX = (58, 863, 166, 971)           # QR on the back template (used if no reader is available)
BACK_FIELDS = ["card_no", "dob", "blood_group", "fh_name", "citizenship_no",
               "nid_no", "address", "contact", "contact2"]
TITLES = {"dr", "dr.", "mr", "mr.", "mrs", "mrs.", "ms", "ms.", "prof", "prof.", "er", "er."}


# --- fonts ------------------------------------------------------------------
@lru_cache(maxsize=None)
def _font_file(weight):
    """Poppins if present in ./fonts (the design's font), else the closest system font."""
    poppins = {"bold": "Poppins-Bold.ttf", "semibold": "Poppins-SemiBold.ttf",
               "medium": "Poppins-Medium.ttf", "regular": "Poppins-Regular.ttf"}[weight]
    if (FONTS / poppins).exists():
        return str(FONTS / poppins), 0
    avenir = Path("/System/Library/Fonts/Avenir Next.ttc")
    if avenir.exists():
        return str(avenir), {"bold": 0, "semibold": 2, "medium": 5, "regular": 7}[weight]
    segoe = Path("C:/Windows/Fonts") / {"bold": "segoeuib.ttf", "semibold": "seguisb.ttf",
                                         "medium": "seguisb.ttf", "regular": "segoeui.ttf"}[weight]
    if segoe.exists():
        return str(segoe), 0
    raise RuntimeError("No usable font found. Put the Poppins .ttf files in card_studio/fonts "
                       "(python run.py --setup does this).")


@lru_cache(maxsize=None)
def font(weight, cap_px):
    """Font sized so capital letters are cap_px tall (600 dpi pixels)."""
    path, index = _font_file(weight)
    probe = ImageFont.truetype(path, 1000, index=index)
    box = probe.getbbox("H")
    size = round(cap_px * 1000 / (box[3] - box[1]))
    return ImageFont.truetype(path, size, index=index)


def fit_font(draw, text, weight, cap_px, max_w):
    """Shrink until the text fits max_w."""
    cap = cap_px
    while cap > 8:
        f = font(weight, round(cap))
        if draw.textlength(text, font=f) <= max_w:
            return f
        cap *= 0.96
    return font(weight, 8)


def P(x, y):
    """300 dpi design -> 600 dpi page coordinates."""
    return round(x * SX), round(y * SY)


# --- templates ----------------------------------------------------------------
def enhance(img):
    """Template (from JPEG) -> clean 600 dpi page."""
    img = img.convert("RGB").resize(DESIGN, Image.LANCZOS)
    up = img.filter(ImageFilter.GaussianBlur(0.4)).resize((DESIGN[0] * 2, DESIGN[1] * 2), Image.LANCZOS)
    up = up.filter(ImageFilter.UnsharpMask(radius=2, percent=110, threshold=2))
    return up.resize(PAGE, Image.LANCZOS)


@lru_cache(maxsize=None)
def template(side):
    return enhance(Image.open(TEMPLATES / f"{side}.png"))


# --- tools --------------------------------------------------------------------
def can_scan():
    if IS_MAC and (BIN / "scan").exists():
        return True
    try:
        import zxingcpp  # noqa: F401
        return True
    except ImportError:
        return False


def scan(paths):
    """Decode barcodes. Returns [{"symbology", "payload", "box": [x0, y0, x1, y1]}]."""
    if IS_MAC and (BIN / "scan").exists():
        out = subprocess.run([str(BIN / "scan"), *map(str, paths)], capture_output=True, text=True, timeout=60)
        try:
            return json.loads(out.stdout or "[]")
        except json.JSONDecodeError:
            return []
    try:
        import zxingcpp
    except ImportError:
        return []
    found = []
    for path in paths:
        for r in zxingcpp.read_barcodes(Image.open(path).convert("RGB")):
            pts = [r.position.top_left, r.position.top_right, r.position.bottom_right, r.position.bottom_left]
            xs, ys = [p.x for p in pts], [p.y for p in pts]
            name = r.format.name if hasattr(r.format, "name") else str(r.format)
            found.append({"symbology": "QR" if "QR" in name.upper() else name, "payload": r.text,
                          "box": [min(xs), min(ys), max(xs), max(ys)]})
    return found


HUMAN_SEG_MODEL = "u2net_human_seg.onnx"   # same model rembg uses; downloaded by run.py --setup


def can_remove_background():
    if IS_MAC and (BIN / "cutout").exists():
        return True
    if (MODELS / HUMAN_SEG_MODEL).exists():
        try:
            import onnxruntime  # noqa: F401
            return True
        except ImportError:
            pass
    try:
        import rembg  # noqa: F401
        return True
    except ImportError:
        return False


@lru_cache(maxsize=1)
def _seg_session():
    import onnxruntime as ort
    return ort.InferenceSession(str(MODELS / HUMAN_SEG_MODEL), providers=["CPUExecutionProvider"])


def _onnx_cutout(photo):
    """U^2-Net human segmentation (as in rembg) run directly with ONNX Runtime."""
    import numpy as np
    rgb = photo.convert("RGB")
    small = np.asarray(rgb.resize((320, 320), Image.LANCZOS), dtype=np.float32)
    small = small / max(small.max(), 1e-6)
    mean, std = np.array([0.485, 0.456, 0.406]), np.array([0.229, 0.224, 0.225])
    x = ((small - mean) / std).transpose(2, 0, 1)[None].astype(np.float32)
    sess = _seg_session()
    pred = sess.run(None, {sess.get_inputs()[0].name: x})[0][0, 0]
    pred = (pred - pred.min()) / max(pred.max() - pred.min(), 1e-6)
    mask = Image.fromarray((pred * 255).astype("uint8")).resize(rgb.size, Image.LANCZOS)
    out = rgb.convert("RGBA")
    out.putalpha(mask)
    return out


def cutout(photo):
    """
    Person with transparent background: macOS Vision on a Mac, rembg elsewhere.
    Photos that already have transparency are used as they are. Without a
    background remover the photo is used unchanged (rectangular).
    """
    photo = ImageOps.exif_transpose(photo)
    if photo.mode in ("RGBA", "LA") and photo.getchannel("A").getextrema()[0] < 250:
        cut = photo.convert("RGBA")
    elif not (IS_MAC and (BIN / "cutout").exists()):
        cut = None
        if (MODELS / HUMAN_SEG_MODEL).exists():
            try:
                cut = _onnx_cutout(photo)
            except ImportError:  # onnxruntime/numpy not installed
                cut = None
        if cut is None:
            try:
                from rembg import remove
                cut = remove(photo.convert("RGB")).convert("RGBA")
            except ImportError:  # no background remover: use the photo as it is
                cut = photo.convert("RGBA")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "in.png", Path(tmp) / "out.png"
            photo.convert("RGB").save(src)
            r = subprocess.run([str(BIN / "cutout"), str(src), str(dst)], capture_output=True, text=True, timeout=60)
            if r.returncode or not dst.exists():
                raise ValueError("Could not find a person in the photo. Try a clearer, front-facing photo.")
            cut = Image.open(dst).convert("RGBA")
            cut.load()
    bbox = cut.getchannel("A").point(lambda a: 255 if a > 24 else 0).getbbox()
    cut = cut.crop(bbox) if bbox else cut
    cut.info["cutout"] = True
    return cut


def signature_ink(img):
    """
    Signature -> ink on a transparent background. A digital signature PNG that is
    already transparent keeps its own ink (and colour); a scan or photo of paper
    becomes dark ink by brightness.
    """
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        alpha = rgba.getchannel("A")
        if alpha.getextrema()[0] < 250:   # transparent background (its hidden colour is often black)
            bbox = alpha.point(lambda a: 255 if a > 16 else 0).getbbox()
            return rgba.crop(bbox) if bbox else rgba
        img = rgba.convert("RGB")
    g = img.convert("L")
    g = ImageOps.autocontrast(g, cutoff=2)
    alpha = g.point(lambda v: 0 if v > 190 else min(255, (190 - v) * 2))
    ink = Image.new("RGBA", g.size, (20, 20, 20, 0))
    ink.putalpha(alpha)
    bbox = alpha.getbbox()
    return ink.crop(bbox) if bbox else ink


# --- barcode --------------------------------------------------------------------
def build_text(employee_id, name="", designation="", extra_lines=(), separator="\n"):
    """Same payload format as barcode_gen.py."""
    lines = [f"ID: {employee_id.strip()}"]
    if name.strip():
        lines.append(f"Name: {name.strip()}")
    if designation.strip():
        lines.append(f"Designation: {designation.strip()}")
    lines += [l.strip() for l in extra_lines if l.strip()]
    return separator.join(lines)


def fit_pdf417(text, slot_w, slot_h):
    """
    Most robust PDF417 that fits the slot (300 dpi px = printer dots).
    Wider bars matter most (3 dots preferred; 2 dots, 0.17 mm, is the smallest
    that prints), then taller rows (2-3 bars high), then more error correction.
    Returns (codes, module dots, row height dots, security level, columns).
    """
    for module in (3, 2):
        best = None
        for security in (2, 3, 4):
            for cols in range(1, 31):
                if (17 * (cols + 4) + 1) * module > slot_w:
                    break
                try:
                    codes = encode(text, columns=cols, security_level=security)
                except Exception:
                    continue
                row = min(slot_h // len(codes), 3 * module)
                if row >= 2 * module and (best is None or (row, security) > (best[2], best[3])):
                    best = (codes, module, row, security, cols)
        if best:
            return best
    return None


def barcode_source(fields):
    """The form values a barcode is made from: a saved barcode is reused only while these are unchanged."""
    return {"employee_id": str(fields.get("employee_id") or "").strip(),
            "name": str(fields.get("name") or "").strip(),
            "designation": str(fields.get("designation") or "").strip(),
            "extra_lines": [str(l).strip() for l in fields.get("extra_lines") or [] if str(l).strip()]}


def saved_pdf417(spec, slot_w, slot_h):
    """Rebuild a saved barcode exactly (same text, columns, error correction and bar sizes), if it still fits."""
    try:
        cols, security, module, row = (int(spec[k]) for k in ("columns", "security", "module", "row"))
        codes = encode(spec["text"], columns=cols, security_level=security)
    except Exception:  # no settings saved (e.g. read from an uploaded barcode image)
        return None
    if (17 * (cols + 4) + 1) * module > slot_w or len(codes) * row > slot_h:
        return None
    return codes, module, row, security, cols


def verify_barcode(page, box, text, workdir):
    """Simulate a 300 dpi print of the barcode area and decode it."""
    x0, y0, x1, y1 = box
    crop = page.crop((P(x0 - 12, y0 - 12) + P(x1 + 12, y1 + 12)))
    crop = crop.resize((crop.width // 2, crop.height // 2), Image.LANCZOS).filter(ImageFilter.GaussianBlur(1.0))
    path = Path(workdir) / "barcode_check.png"
    crop.resize((crop.width * 3, crop.height * 3), Image.BILINEAR).save(path)
    found = scan([path])
    return any(f["payload"] == text for f in found), (found[0]["payload"] if found else None)


# --- masks / pdf ----------------------------------------------------------------
def kmask(boxes, pad=12):
    """K-panel mask: black = print with the black (K) panel. Only code areas get K."""
    m = Image.new("RGBA", MASK, (255, 255, 255, 255))
    d = ImageDraw.Draw(m)
    sx, sy = MASK[0] / DESIGN[0], MASK[1] / DESIGN[1]
    for b in boxes:
        d.rectangle([b[0] * sx - pad, b[1] * sy - pad, b[2] * sx + pad, b[3] * sy + pad], fill=(0, 0, 0, 255))
    return m


def device_image(page):
    """The card at the printer's own 300 dpi grid (an exact 2:1 reduction of the page)."""
    page = page.convert("RGB")
    return page.reduce(2) if page.size == PAGE else page.resize(DEVICE, Image.LANCZOS)


def to_pdf(page):
    """
    One-page PDF holding the card as a lossless 300 dpi image (Pillow's own PDF
    writer would JPEG-compress it). Its page size makes the Entrust driver copy
    it dot for dot.
    """
    img = device_image(page)
    data = zlib.compress(img.tobytes(), 6)
    pw, ph = (f"{v:.2f}" for v in DEVICE_PT)
    content = f"q {pw} 0 0 {ph} 0 0 cm /Im0 Do Q".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pw} {ph}] "
        f"/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>".encode(),
        f"<< /Type /XObject /Subtype /Image /Width {img.width} /Height {img.height} /ColorSpace /DeviceRGB "
        f"/BitsPerComponent 8 /Interpolate false /Filter /FlateDecode /Length {len(data)} >>\nstream\n".encode()
        + data + b"\nendstream",
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


# --- front ----------------------------------------------------------------------
def split_name(name):
    words = name.split()
    if not words:
        return []
    title = [words.pop(0)] if words[0].lower() in TITLES and len(words) > 1 else []
    words = [w.upper() for w in words]
    if len(words) == 1:
        return [" ".join(title + words)]
    return [" ".join(title + words[:-1]), words[-1]]


def place_photo(page, cut, scale=1.0, dx=0, dy=0):
    """Person cut-out: bottom-anchored in the photo area, as in the design."""
    x0, y0, x1, y1 = PHOTO_BOX
    box_w, box_h = (x1 - x0) * SX, (y1 - y0) * SY
    k = min(box_h / cut.height, box_w / cut.width) * scale
    w, h = round(cut.width * k), round(cut.height * k)
    person = cut.resize((w, h), Image.LANCZOS)
    left = round(x0 * SX + (box_w - w) / 2 + dx * SX)
    top = round(PAGE[1] - h + dy * SY)
    page.alpha_composite(person, (left, top)) if page.mode == "RGBA" else page.paste(person, (left, top), person)


def build_front(fields, photo=None, signature=None, workdir=".", barcode=None):
    """
    barcode: the card's saved barcode ({"text", "columns", "security", "module", "row",
    "source"}). While the ID, name, designation and extra lines are unchanged it is
    rebuilt exactly as saved; otherwise a new one is made from the form.
    """
    page = template("front").copy()
    d = ImageDraw.Draw(page)
    info = {}

    if photo is not None:
        cut = photo if isinstance(photo, Image.Image) and photo.mode == "RGBA" and photo.info.get("cutout") else cutout(photo)
        place_photo(page, cut, float(fields.get("photo_scale") or 1),
                    float(fields.get("photo_dx") or 0), float(fields.get("photo_dy") or 0))
        # the signature bar sits on top of the photo, as in the design
        page.paste(template("front").crop(P(*SIGN_BAR[:2]) + P(*SIGN_BAR[2:])), P(*SIGN_BAR[:2]))

    max_w = (NAME_RIGHT - NAME_LEFT_LIMIT) * SX
    lines = split_name(fields.get("name", ""))
    if lines:
        # one size for both lines: the largest that fits the longest line
        cap = NAME_LINES[0][1] * SY
        f = min((fit_font(d, l, "bold", cap, max_w) for l in lines), key=lambda f: f.size)
        slots = NAME_LINES if len(lines) == 2 else NAME_LINES[1:]
        for line, (baseline, _) in zip(lines, slots):
            d.text(P(NAME_RIGHT, baseline), line, font=f, fill=NAME_RED, anchor="rs")
    if fields.get("designation", "").strip():
        t = fields["designation"].strip()
        d.text(P(NAME_RIGHT, DESIG[0]), t, font=fit_font(d, t, "regular", DESIG[1] * SY, max_w), fill=TEAL, anchor="rs")
    if fields.get("valid_from", "").strip():
        t = f"Valid from {fields['valid_from'].strip()}"
        d.text(P(VALID[0], VALID[1]), t, font=fit_font(d, t, "regular", VALID[2] * SY, 300 * SX), fill=INK, anchor="ms")

    if signature is not None:
        ink = signature_ink(signature)
        x0, y0, x1, y1 = SIGN_BOX
        k = min((x1 - x0) * SX / ink.width, (y1 - y0) * SY / ink.height)
        ink = ink.resize((max(1, round(ink.width * k)), max(1, round(ink.height * k))), Image.LANCZOS)
        cx, bottom = (x0 + x1) / 2 * SX, y1 * SY
        page.paste(ink, (round(cx - ink.width / 2), round(bottom - ink.height)), ink)

    # barcode
    if not fields.get("employee_id", "").strip():
        raise ValueError("Employee ID is required for the barcode.")
    source = barcode_source(fields)
    saved = barcode if isinstance(barcode, dict) and barcode.get("source") == source and barcode.get("text") else None
    text = saved["text"] if saved else build_text(source["employee_id"], source["name"], source["designation"],
                                                  source["extra_lines"])
    s = BARCODE_SLOT
    reused = saved_pdf417(saved, s[2] - s[0], s[3] - s[1]) if saved else None
    fit = reused or fit_pdf417(text, s[2] - s[0], s[3] - s[1])
    if not fit:
        raise ValueError("The barcode text is too long to fit at a scannable size. Shorten the extra lines.")
    codes, module, row, security, cols = fit
    bc = render_image(codes, scale=1, ratio=1, padding=0).convert("RGB")      # 1 px per bar and per row
    w300, h300 = bc.width * module, bc.height * row                          # printer dots
    bc = bc.resize((w300 * 2, h300 * 2), Image.NEAREST)                       # 600 dpi page
    # Even page pixels = whole printer dots, so every bar prints the same width.
    x, y = (2 * round(v / 2) for v in P(s[2] - w300, s[1]))
    page.paste(bc, (x, y))
    box = (x / SX, y / SY, (x + bc.width) / SX, (y + bc.height) / SY)

    if can_scan():
        ok, decoded = verify_barcode(page, box, text, workdir)
    else:  # no reader on this computer: the size rules above still apply
        ok, decoded = None, None
    info["k_boxes"] = [box]
    info["barcode"] = {"text": text, "columns": cols, "security": security, "module": module, "row": row,
                       "source": source}
    info.update(text=text, verified=ok, decoded=decoded, security_level=security, barcode_reused=bool(reused),
                module_mm=round(module * 25.4 / 300, 2), row_mm=round(row * 25.4 / 300, 2),
                size_mm=[round(w300 * 25.4 / 300, 1), round(h300 * 25.4 / 300, 1)])
    return page, kmask([box]), info


# --- back ------------------------------------------------------------------------
@lru_cache(maxsize=None)
def back_codes():
    """QR code(s) printed on the back template, located once."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "back.png"
        Image.open(TEMPLATES / "back.png").save(path)
        found = tuple((c["symbology"], c["payload"], tuple(c["box"])) for c in scan([path]))
    return found or (("QR", "", BACK_QR_BOX),)


def crisp_code(page, box, pad=2):
    """Redraw a code area as pure black/white with hard edges (prints on the K panel)."""
    src = Image.open(TEMPLATES / "back.png").convert("L")
    x0, y0, x1, y1 = [int(v) for v in (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)]
    code = src.crop((x0, y0, x1, y1)).point(lambda v: 0 if v < 140 else 255)
    p0, p1 = P(x0, y0), P(x1, y1)
    page.paste(code.resize((p1[0] - p0[0], p1[1] - p0[1]), Image.NEAREST).convert("RGB"), p0)
    return (x0, y0, x1, y1)


def draw_value(d, key, value, baseline):
    max_w = (BACK_RIGHT - BACK_VALUE_X) * SX
    cap = BACK_CAP * SY
    x, y = P(BACK_VALUE_X, baseline)
    m = re.fullmatch(r"\s*(AB|A|B|O)\s*([+-])\s*(ve)?\s*", value, re.I) if key == "blood_group" else None
    if m:  # "A+ve" -> A with superscript "+ve", as in the design
        main, sup = m.group(1).upper(), f"{m.group(2)}ve"
        f, fs = font("regular", round(cap)), font("regular", round(cap * 0.62))
        d.text((x, y), main, font=f, fill=INK, anchor="ls")
        d.text((x + d.textlength(main, font=f) + 2, y - cap * 0.55), sup, font=fs, fill=INK, anchor="ls")
        return
    d.text((x, y), value, font=fit_font(d, value, "regular", cap, max_w), fill=INK, anchor="ls")


def build_back(fields):
    page = template("back").copy()
    d = ImageDraw.Draw(page)
    values = dict(fields)
    values.setdefault("card_no", fields.get("employee_id", ""))
    if not values.get("card_no"):
        values["card_no"] = fields.get("employee_id", "")
    label = ID_LABELS.get(fields.get("id_type") or "citizenship")
    if label:  # e.g. a passport instead of a citizenship certificate: replace the printed label
        d.rectangle(P(*ID_LABEL_BOX[:2]) + P(*ID_LABEL_BOX[2:]), fill="white")
        d.text(P(*ID_LABEL[:2]), label, font=font("semibold", round(ID_LABEL[2] * SY)), fill=(53, 53, 53), anchor="rs")
    for key, baseline in zip(BACK_FIELDS, BACK_BASELINES):
        v = str(values.get(key, "") or "").strip()
        if v:
            draw_value(d, key, v, baseline)
    boxes = [crisp_code(page, box) for _, _, box in back_codes()]
    return page, kmask(boxes), {"codes": [{"type": t, "payload": p} for t, p, _ in back_codes()], "k_boxes": boxes}
