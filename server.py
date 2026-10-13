#!/usr/bin/env python3
"""
LMC Card Studio: fill in the employee details, add a photo (and optional
signature), get a print-ready card with a scannable PDF417, and print it on the
Datacard SD160 one side at a time.

Two modes (see run.py):
  local   one computer, prints directly, no login, listens on 127.0.0.1 only
  server  shared server for the office network: staff logins, print queue,
          and a print agent (agent.py) on the PC that has the printer
"""

import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import ssl
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image

import card
import db
import updater
from paths import DATA, RES, WORK
from version import VERSION

PORT = 8765
MAX_BODY = 40 * 1024 * 1024
COOKIE = "cs_session"

MODE = "local"         # set by serve()
SECURE = False         # HTTPS in use: mark the cookie Secure
built = {}             # owner -> side -> {"page", "mask", "boxes"} of the last build, ready to print
cutouts = {}           # photo hash -> person cut-out (background removal is slow, so reuse it)
lock = threading.Lock()
agent = {"seen": 0.0, "status": None, "host": None}   # last report from the print agent
AGENT_TIMEOUT = 20     # seconds without a report before the print station counts as offline


def agent_token():
    """Secret the print agent sends with every request (created on first use)."""
    path = DATA / "agent_token.txt"
    if not path.exists():
        DATA.mkdir(exist_ok=True)
        path.write_text(secrets.token_urlsafe(32))
    return path.read_text().strip()


# --- image helpers --------------------------------------------------------------
def data_bytes(data_url):
    return base64.b64decode(data_url.split(",", 1)[1])


def decode_image(data_url):
    return Image.open(io.BytesIO(data_bytes(data_url)))


def to_data_url(blob):
    if not blob:
        return None
    mime = "image/png" if blob[:4] == b"\x89PNG" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(blob).decode()


def png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def png_data_url(page, width=760):
    img = page.convert("RGB").resize((width, round(page.height * width / page.width)), Image.LANCZOS)
    return "data:image/png;base64," + base64.b64encode(png_bytes(img)).decode()


def photo_key(data_url):
    return hashlib.sha1(data_url.encode()).hexdigest()


def person(data_url):
    key = photo_key(data_url)
    if key not in cutouts:
        if len(cutouts) > 40:
            cutouts.clear()
        cutouts[key] = card.cutout(decode_image(data_url))
    return cutouts[key]


# --- cards --------------------------------------------------------------------------
def save_card(body):
    """Store the current form, photo, cut-out and signature. Returns the card id."""
    photo = body.get("photo")
    cut = png_bytes(person(photo)) if photo else None
    signature = data_bytes(body["signature"]) if body.get("signature") else None
    fields = body.get("fields", {})
    barcode = body.get("barcode")   # from the last preview; kept only if it belongs to these fields
    if not (isinstance(barcode, dict) and isinstance(barcode.get("text"), str)
            and barcode.get("source") == card.barcode_source(fields)):
        barcode = None
    return db.save(fields, data_bytes(photo) if photo else None, cut,
                   signature, keep_images=bool(signature), barcode=barcode)


def load_card(card_id):
    c = db.get(card_id)
    if not c:
        raise ValueError("Card not found.")
    photo = to_data_url(c.pop("photo"))
    cut = c.pop("photo_cutout")
    if photo and cut:  # reuse the stored cut-out instead of redoing it
        img = Image.open(io.BytesIO(cut)).convert("RGBA")
        img.info["cutout"] = True
        cutouts[photo_key(photo)] = img
    c["photo"] = photo
    c["signature"] = to_data_url(c.pop("signature"))
    return c


def render(body, owner):
    fields = body.get("fields", {})
    photo = person(body["photo"]) if body.get("photo") else None
    signature = decode_image(body["signature"]) if body.get("signature") else None
    front, front_mask, front_info = card.build_front(fields, photo, signature, WORK, body.get("barcode"))
    back, back_mask, back_info = card.build_back(fields)
    built[owner] = {
        "front": {"page": front, "mask": front_mask, "boxes": front_info.pop("k_boxes")},
        "back": {"page": back, "mask": back_mask, "boxes": back_info.pop("k_boxes")},
    }
    notes = []
    if photo is not None and not card.can_remove_background():
        notes.append("Background removal isn't installed, so the photo is used as-is. Run: python run.py --setup")
    return {"front": {"preview": png_data_url(front), **front_info},
            "back": {"preview": png_data_url(back), **back_info}, "notes": notes}


def decode_barcode(body):
    """Read an existing barcode image to pre-fill the fields."""
    path = WORK / f"decode_{secrets.token_hex(4)}.png"
    try:
        decode_image(body["image"]).convert("RGB").save(path)
        found = card.scan([path])
    finally:
        path.unlink(missing_ok=True)
    if not found:
        raise ValueError("No barcode could be read from that image.")
    fields, extra = {}, []
    for line in found[0]["payload"].splitlines():
        key, _, value = line.partition(":")
        k = {"ID": "employee_id", "Name": "name", "Designation": "designation"}.get(key.strip())
        if k and value:
            fields[k] = value.strip()
        else:
            extra.append(line)
    fields["extra_lines"] = extra
    # keep the original text exactly, so the card's barcode reads the same as the uploaded one
    fields["barcode"] = {"text": found[0]["payload"], "source": card.barcode_source(fields)}
    return fields


# --- printing -------------------------------------------------------------------------
def printer_status():
    if MODE == "local":
        import printer
        return printer.status()
    queue = [str(j["id"]) for j in db.active_jobs()]
    if time.time() - agent["seen"] > AGENT_TIMEOUT or not agent["status"]:
        return {"link": False, "can_print": False, "status": "Offline", "ribbon": None, "model": None, "serial": None,
                "queue": queue, "message": "Print station offline. Start agent.py on the printer PC."}
    s = dict(agent["status"])
    s["queue"] = queue
    s["can_print"] = True   # queued jobs wait for the print station
    if queue and s.get("status") == "Ready":
        s["status"] = "Busy"
    return s


def printers():
    """Installed printers for the printer menu (local mode; empty on the Mac)."""
    import printer
    return {"printers": printer.list_printers(), "selected": os.environ.get("CARD_PRINTER", "")}


def choose_printer(name):
    """Print with this printer from now on. Kept in data/settings.json."""
    name = str(name or "")
    if name and name not in printers()["printers"]:
        raise ValueError(f"Printer \"{name}\" isn't installed.")
    path = DATA / "settings.json"
    try:
        settings = json.loads(path.read_text())
    except (OSError, ValueError):
        settings = {}
    settings["printer"] = name
    path.write_text(json.dumps(settings, indent=2))
    if name:
        os.environ["CARD_PRINTER"] = name
    else:
        os.environ.pop("CARD_PRINTER", None)
    return printers()


def print_side(body, owner, user):
    side = body.get("side")
    job = built.get(owner, {}).get(side)
    if not job:
        raise ValueError("Build the card first.")
    if MODE == "local":
        import printer
        job_id = printer.print_card(job, f"Card {side}")
        if body.get("card_id"):
            db.log_print(int(body["card_id"]), side, job_id)
        return {"job": job_id}
    if not body.get("card_id"):
        raise ValueError("Save the card first.")
    job_id = db.add_job(int(body["card_id"]), side, png_bytes(job["page"]), png_bytes(job["mask"]),
                        job["boxes"], user)
    return {"queued": job_id}


# --- HTTP -------------------------------------------------------------------------------
PUBLIC = {"/login", "/api/login", "/favicon.ico"}


class Handler(BaseHTTPRequestHandler):
    server_version = "CardStudio"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    # responses
    def send_bytes(self, data, ctype, code=200, headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, code=200, headers=()):
        self.send_bytes(json.dumps(obj).encode(), "application/json", code, headers)

    def redirect(self, where):
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def page(self, name):
        self.send_bytes((RES / "static" / name).read_bytes(), "text/html; charset=utf-8")

    # auth
    def session_token(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c[COOKIE].value if COOKIE in c else None

    def user(self):
        if MODE == "local":
            return "local"
        return db.session_user(self.session_token())

    def is_agent(self):
        auth = self.headers.get("Authorization", "")
        return MODE == "server" and auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], agent_token())

    def cookie_header(self, token, max_age):
        flags = "HttpOnly; SameSite=Strict; Path=/" + ("; Secure" if SECURE else "")
        return ("Set-Cookie", f"{COOKIE}={token}; Max-Age={max_age}; {flags}")

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            raise ValueError("Upload too large.")
        return json.loads(self.rfile.read(length) or b"{}")

    # routes
    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if path.startswith("/api/agent/"):
            return self.agent_get(path)
        user = self.user()
        if path == "/login":
            return self.redirect("/") if user else self.page("login.html")
        if not user and path not in PUBLIC:
            return self.redirect("/login") if not path.startswith("/api/") else self.send_json({"error": "login"}, 401)
        if path in ("/", "/index.html"):
            return self.page("index.html")
        if path == "/api/me":
            return self.send_json({"user": user, "mode": MODE, "version": VERSION})
        if path == "/api/status":
            return self.send_json(printer_status())
        if path == "/api/printers":
            return self.send_json(printers() if MODE == "local" else {"printers": [], "selected": ""})
        if path == "/api/update":
            if MODE != "local":
                return self.send_json({"current": VERSION, "available": False})
            return self.send_json(updater.check(force="force" in parse_qs(url.query)))
        if path == "/api/update/progress":
            return self.send_json(updater.progress)
        if path == "/api/cards":
            return self.send_json(db.search(parse_qs(url.query).get("q", [""])[0]))
        if path.startswith("/api/cards/"):
            try:
                return self.send_json(load_card(int(path.rsplit("/", 1)[1])))
            except (ValueError, TypeError) as e:
                return self.send_json({"error": str(e)}, 404)
        if path.startswith("/api/jobs/"):
            job = db.get_job(int(path.rsplit("/", 1)[1]))
            return self.send_json(job) if job else self.send_json({"error": "Unknown job."}, 404)
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.read_body()
            if path.startswith("/api/agent/"):
                return self.agent_post(path, body)
            if path == "/api/login":
                if MODE == "local":
                    return self.send_json({"ok": True})
                token = db.check_login(str(body.get("username", "")), str(body.get("password", "")))
                if not token:
                    time.sleep(1)  # slow down password guessing
                    return self.send_json({"error": "Wrong username or password."}, 401)
                return self.send_json({"ok": True}, headers=[self.cookie_header(token, 12 * 3600)])
            user = self.user()
            if not user:
                return self.send_json({"error": "login"}, 401)
            owner = self.session_token() or "local"
            if path == "/api/logout":
                db.end_session(self.session_token())
                built.pop(owner, None)
                return self.send_json({"ok": True}, headers=[self.cookie_header("", 0)])
            if path == "/api/render":
                with lock:
                    return self.send_json(render(body, owner))
            if path == "/api/cards":
                with lock:
                    return self.send_json({"id": save_card(body)})
            if path == "/api/decode":
                return self.send_json(decode_barcode(body))
            if path == "/api/printer":
                if MODE != "local":
                    raise ValueError("Choose the printer on the print station (agent.py --printer).")
                return self.send_json(choose_printer(body.get("name")))
            if path == "/api/update":
                if MODE != "local":
                    raise ValueError("Update the server with git pull.")
                return self.send_json(updater.start_install())
            if path == "/api/print":
                with lock:
                    return self.send_json(print_side(body, owner, user))
            self.send_error(404)
        except Exception as e:  # report every failure to the page
            self.send_json({"error": str(e)}, 400)

    # print agent (Windows PC with the printer)
    def agent_get(self, path):
        if not self.is_agent():
            return self.send_json({"error": "agent key"}, 401)
        parts = path.strip("/").split("/")          # api/agent/jobs/<id>/page|mask
        if len(parts) == 5 and parts[2] == "jobs" and parts[4] in ("page", "mask"):
            blob = db.job_blob(int(parts[3]), "page_png" if parts[4] == "page" else "mask_png")
            return self.send_bytes(blob, "image/png") if blob else self.send_json({"error": "Unknown job."}, 404)
        self.send_error(404)

    def agent_post(self, path, body):
        if not self.is_agent():
            return self.send_json({"error": "agent key"}, 401)
        if path == "/api/agent/heartbeat":
            agent.update(seen=time.time(), status=body.get("status"), host=body.get("host"))
            ready = (body.get("status") or {}).get("status") == "Ready"
            return self.send_json({"job": db.next_job() if ready else None})
        parts = path.strip("/").split("/")          # api/agent/jobs/<id>
        if len(parts) == 4 and parts[2] == "jobs":
            status = body.get("status")
            if status not in ("printing", "done", "failed"):
                raise ValueError("Bad job status.")
            db.update_job(int(parts[3]), status, body.get("printer_job"), body.get("error"))
            return self.send_json({"ok": True})
        self.send_error(404)


def serve(port=PORT, mode="local", host=None, certfile=None, keyfile=None):
    global MODE, SECURE
    MODE = mode
    if mode == "local" and not os.environ.get("CARD_PRINTER"):   # printer chosen in the app last time
        try:
            os.environ["CARD_PRINTER"] = json.loads((DATA / "settings.json").read_text()).get("printer") or ""
        except (OSError, ValueError):
            pass
    host = host or ("0.0.0.0" if mode == "server" else "127.0.0.1")
    httpd = ThreadingHTTPServer((host, port), Handler)
    scheme = "http"
    if certfile:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        SECURE, scheme = True, "https"
    if mode == "server":
        if not db.list_users():
            print("No staff accounts yet. Create one with:  python run.py --add-user <name>")
        print(f"Agent key (for agent.py on the printer PC): {agent_token()}")
    print(f"LMC Card Studio ({mode} mode) on {scheme}://{host}:{port}  (Ctrl+C to stop)")
    httpd.serve_forever()


if __name__ == "__main__":
    serve()
