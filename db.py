"""
SQLite storage for cards and their print history (data/cards.db).

One row per employee (employee_id is unique); saving again updates it.
Photos are stored as the original upload plus the background-removed cut-out,
so reprinting never needs to redo the cut-out.
"""

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from pathlib import Path

from paths import DATA

DB_PATH = DATA / "cards.db"
TEXT_FIELDS = ["employee_id", "name", "designation", "valid_from", "dob", "blood_group", "fh_name",
               "citizenship_no", "nid_no", "address", "contact", "contact2", "id_type"]
NUM_FIELDS = ["photo_scale", "photo_dx", "photo_dy"]

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id             INTEGER PRIMARY KEY,
    employee_id    TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL DEFAULT '',
    designation    TEXT NOT NULL DEFAULT '',
    valid_from     TEXT NOT NULL DEFAULT '',
    dob            TEXT NOT NULL DEFAULT '',
    blood_group    TEXT NOT NULL DEFAULT '',
    fh_name        TEXT NOT NULL DEFAULT '',
    citizenship_no TEXT NOT NULL DEFAULT '',   -- or the passport number (see id_type)
    id_type        TEXT NOT NULL DEFAULT 'citizenship',   -- 'citizenship' or 'passport'
    nid_no         TEXT NOT NULL DEFAULT '',
    address        TEXT NOT NULL DEFAULT '',
    contact        TEXT NOT NULL DEFAULT '',
    contact2       TEXT NOT NULL DEFAULT '',
    extra_lines    TEXT NOT NULL DEFAULT '[]',
    photo_scale    REAL NOT NULL DEFAULT 1,
    photo_dx       REAL NOT NULL DEFAULT 0,
    photo_dy       REAL NOT NULL DEFAULT 0,
    photo          BLOB,          -- original upload
    photo_cutout   BLOB,          -- PNG with transparent background
    signature      BLOB,
    auth_signature BLOB,          -- authorised signatory's signature used on this card
    barcode        TEXT NOT NULL DEFAULT '',   -- JSON: exact barcode settings, so reprints match
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS prints (
    id          INTEGER PRIMARY KEY,
    card_id     INTEGER NOT NULL REFERENCES cards(id),
    side        TEXT NOT NULL CHECK (side IN ('front', 'back')),
    job_id      TEXT,
    printed_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS prints_card ON prints(card_id);

-- server mode: staff logins, sessions and the print queue for the Windows print agent
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    username   TEXT NOT NULL UNIQUE,
    pw_hash    TEXT NOT NULL,            -- pbkdf2_sha256$iterations$salt$hash
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY,
    card_id     INTEGER NOT NULL REFERENCES cards(id),
    side        TEXT NOT NULL CHECK (side IN ('front', 'back')),
    page_png    BLOB NOT NULL,           -- 600 dpi card image
    mask_png    BLOB NOT NULL,           -- K-panel mask (used by the Linux/Mac driver)
    boxes       TEXT NOT NULL,           -- code areas for the black panel (JSON)
    status      TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'sent', 'printing', 'done', 'failed', 'cancelled')),
    printer_job TEXT,
    error       TEXT,
    created_by  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
"""


def connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(cards)")}
    if "id_type" not in cols:   # databases from before the passport option
        con.execute("ALTER TABLE cards ADD COLUMN id_type TEXT NOT NULL DEFAULT 'citizenship'")
    if "barcode" not in cols:   # databases from before saved barcodes
        con.execute("ALTER TABLE cards ADD COLUMN barcode TEXT NOT NULL DEFAULT ''")
    if "auth_signature" not in cols:
        con.execute("ALTER TABLE cards ADD COLUMN auth_signature BLOB")
    con.commit()
    return con


_con = connect()


def save(fields, photo=None, photo_cutout=None, signature=None, keep_images=True, barcode=None,
         auth_signature=None):
    """Insert or update by employee_id. Returns the card id.

    photo/photo_cutout/signature are bytes; None keeps what is stored
    (unless keep_images is False, e.g. the signature was removed).
    barcode is the dict of barcode settings and auth_signature the authorised
    signature's bytes; None keeps what is stored.
    """
    eid = (fields.get("employee_id") or "").strip()
    if not eid:
        raise ValueError("Employee ID is required to save the card.")
    values = {k: str(fields.get(k) or "").strip() for k in TEXT_FIELDS}
    values["employee_id"] = eid
    values["id_type"] = "passport" if values["id_type"] == "passport" else "citizenship"
    values.update({k: float(fields.get(k) or (1 if k == "photo_scale" else 0)) for k in NUM_FIELDS})
    values["extra_lines"] = json.dumps([l for l in fields.get("extra_lines", []) if str(l).strip()])
    cols = list(values)
    with _lock, _con:
        _con.execute(
            f"INSERT INTO cards ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
            f"ON CONFLICT(employee_id) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "employee_id")
            + ", updated_at=datetime('now', 'localtime')",
            [values[c] for c in cols])
        card_id = _con.execute("SELECT id FROM cards WHERE employee_id=?", (eid,)).fetchone()["id"]
        if barcode is not None:
            _con.execute("UPDATE cards SET barcode=? WHERE id=?", (json.dumps(barcode), card_id))
        if auth_signature is not None:
            _con.execute("UPDATE cards SET auth_signature=? WHERE id=?", (auth_signature, card_id))
        for col, blob in (("photo", photo), ("photo_cutout", photo_cutout), ("signature", signature)):
            if blob is not None or (col == "signature" and not keep_images):
                _con.execute(f"UPDATE cards SET {col}=? WHERE id=?", (blob, card_id))
    return card_id


def search(q="", limit=20):
    """Match ID, name, designation, phone numbers, NID or citizenship/passport number."""
    like = f"%{q.strip()}%"
    with _lock:
        rows = _con.execute(
            """SELECT c.id, c.employee_id, c.name, c.designation, c.updated_at,
                      (SELECT COUNT(*) FROM prints p WHERE p.card_id = c.id AND p.side = 'front') AS times_printed,
                      (SELECT MAX(printed_at) FROM prints p WHERE p.card_id = c.id) AS last_printed
               FROM cards c
               WHERE c.employee_id LIKE ?1 OR c.name LIKE ?1 OR c.designation LIKE ?1
                  OR c.contact LIKE ?1 OR c.contact2 LIKE ?1 OR c.nid_no LIKE ?1 OR c.citizenship_no LIKE ?1
               ORDER BY c.updated_at DESC LIMIT ?2""", (like, limit)).fetchall()
    return [dict(r) for r in rows]


def get(card_id):
    with _lock:
        row = _con.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        if not row:
            return None
        history = _con.execute("SELECT side, job_id, printed_at FROM prints WHERE card_id=? "
                               "ORDER BY id DESC LIMIT 20", (card_id,)).fetchall()
    card = dict(row)
    card["extra_lines"] = json.loads(card["extra_lines"] or "[]")
    card["barcode"] = json.loads(card["barcode"]) if card.get("barcode") else None
    card["history"] = [dict(h) for h in history]
    return card


def log_print(card_id, side, job_id):
    with _lock, _con:
        _con.execute("INSERT INTO prints (card_id, side, job_id) VALUES (?, ?, ?)", (card_id, side, job_id))


# --- users and sessions (server mode) ------------------------------------------
PBKDF2_ITERATIONS = 240_000


def _hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def set_user(username, password):
    """Create a user or change their password."""
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    with _lock, _con:
        _con.execute("INSERT INTO users (username, pw_hash) VALUES (?, ?) "
                     "ON CONFLICT(username) DO UPDATE SET pw_hash=excluded.pw_hash",
                     (username.strip(), _hash_password(password)))


def delete_user(username):
    with _lock, _con:
        return _con.execute("DELETE FROM users WHERE username=?", (username.strip(),)).rowcount


def list_users():
    with _lock:
        return [r["username"] for r in _con.execute("SELECT username FROM users ORDER BY username")]


def check_login(username, password):
    """Returns a new session token, or None."""
    with _lock:
        row = _con.execute("SELECT id, pw_hash FROM users WHERE username=?", (username.strip(),)).fetchone()
    if not row:
        _hash_password(password)  # same work either way, so timing doesn't reveal valid usernames
        return None
    _, iterations, salt, digest = row["pw_hash"].split("$")
    test = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
    if not hmac.compare_digest(test, digest):
        return None
    token = secrets.token_urlsafe(32)
    with _lock, _con:
        _con.execute("DELETE FROM sessions WHERE expires_at < datetime('now', 'localtime')")
        _con.execute("INSERT INTO sessions (token, user_id, expires_at) "
                     "VALUES (?, ?, datetime('now', 'localtime', '+12 hours'))", (token, row["id"]))
    return token


def session_user(token):
    if not token:
        return None
    with _lock:
        row = _con.execute("SELECT u.username FROM sessions s JOIN users u ON u.id = s.user_id "
                           "WHERE s.token=? AND s.expires_at > datetime('now', 'localtime')", (token,)).fetchone()
    return row["username"] if row else None


def end_session(token):
    with _lock, _con:
        _con.execute("DELETE FROM sessions WHERE token=?", (token,))


# --- print queue (server mode) ----------------------------------------------------
def add_job(card_id, side, page_png, mask_png, boxes, user):
    with _lock, _con:
        cur = _con.execute("INSERT INTO jobs (card_id, side, page_png, mask_png, boxes, created_by) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (card_id, side, page_png, mask_png, json.dumps(boxes), user))
        return cur.lastrowid


def active_jobs():
    with _lock:
        return [dict(r) for r in _con.execute(
            "SELECT id, side, status FROM jobs WHERE status IN ('queued', 'sent', 'printing') ORDER BY id")]


def next_job():
    """Hand the oldest queued job to the agent (marks it 'sent')."""
    with _lock, _con:
        # a job the agent took but never confirmed within 6 minutes is failed, so the queue can't jam
        _con.execute("UPDATE jobs SET status='failed', error='The print station did not confirm this job.', "
                     "updated_at=datetime('now', 'localtime') WHERE status IN ('sent', 'printing') "
                     "AND updated_at < datetime('now', 'localtime', '-6 minutes')")
        if _con.execute("SELECT 1 FROM jobs WHERE status IN ('sent', 'printing')").fetchone():
            return None   # one card at a time: the K mask and the hopper are shared
        row = _con.execute("SELECT j.id, j.side, j.boxes, c.employee_id FROM jobs j JOIN cards c ON c.id = j.card_id "
                           "WHERE j.status='queued' ORDER BY j.id LIMIT 1").fetchone()
        if not row:
            return None
        _con.execute("UPDATE jobs SET status='sent', updated_at=datetime('now', 'localtime') WHERE id=?", (row["id"],))
    job = dict(row)
    job["boxes"] = json.loads(job["boxes"])
    return job


def job_blob(job_id, column):
    assert column in ("page_png", "mask_png")
    with _lock:
        row = _con.execute(f"SELECT {column} FROM jobs WHERE id=?", (job_id,)).fetchone()
    return row[column] if row else None


def update_job(job_id, status, printer_job=None, error=None):
    """Agent progress. A finished job is also added to the card's print history."""
    with _lock, _con:
        row = _con.execute("SELECT card_id, side, status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("Unknown job.")
        _con.execute("UPDATE jobs SET status=?, printer_job=COALESCE(?, printer_job), error=?, "
                     "updated_at=datetime('now', 'localtime') WHERE id=?", (status, printer_job, error, job_id))
        if status == "done" and row["status"] != "done":
            _con.execute("INSERT INTO prints (card_id, side, job_id) VALUES (?, ?, ?)",
                         (row["card_id"], row["side"], printer_job))


def get_job(job_id):
    with _lock:
        row = _con.execute("SELECT id, side, status, error, printer_job, created_at, updated_at FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
    return dict(row) if row else None
