"""QuickPoll — create a poll, share a link, collect votes, watch live results.

Run with:  python app.py        (serves on port 4000)
Config via env: QUICKPOLL_DB (sqlite path), PORT, SECRET_SALT
"""
import hashlib
import os
import secrets
import sqlite3
import string
import time
from datetime import datetime, timezone

from flask import (
    Flask, abort, g, jsonify, make_response, redirect,
    render_template, request, url_for,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("QUICKPOLL_DB", os.path.join(BASE_DIR, "quickpoll.db"))
PORT = int(os.environ.get("PORT", "4000"))
SALT = os.environ.get("SECRET_SALT", "quickpoll-dev-salt")

CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # unambiguous, no 0/O/1/l/i
MAX_OPTIONS = 8
MIN_OPTIONS = 2

app = Flask(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  admin_token TEXT NOT NULL UNIQUE,
  question TEXT NOT NULL,
  allow_multiple INTEGER NOT NULL DEFAULT 0,
  show_results INTEGER NOT NULL DEFAULT 1,
  require_name INTEGER NOT NULL DEFAULT 0,
  closes_at TEXT,
  closed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS options (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS votes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
  option_id INTEGER NOT NULL REFERENCES options(id) ON DELETE CASCADE,
  voter_name TEXT,
  ip_hash TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_votes_poll_ip ON votes(poll_id, ip_hash);
CREATE INDEX IF NOT EXISTS idx_options_poll ON options(poll_id);
"""


# ---------------------------------------------------------------- database

def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def new_code():
    db = get_db()
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not db.execute("SELECT 1 FROM polls WHERE code = ?", (code,)).fetchone():
            return code


def get_poll_by_code(code):
    return get_db().execute("SELECT * FROM polls WHERE code = ?", (code,)).fetchone()


def get_poll_by_token(token):
    return get_db().execute(
        "SELECT * FROM polls WHERE admin_token = ?", (token,)).fetchone()


def poll_options(poll_id):
    return get_db().execute(
        "SELECT * FROM options WHERE poll_id = ? ORDER BY position, id",
        (poll_id,)).fetchall()


def is_closed(poll):
    """A poll is closed if manually closed or its close time has passed."""
    if poll["closed"]:
        return True
    if poll["closes_at"]:
        try:
            closes = datetime.fromisoformat(poll["closes_at"])
            if closes.tzinfo is None:
                closes = closes.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= closes:
                get_db().execute("UPDATE polls SET closed = 1 WHERE id = ?",
                                 (poll["id"],))
                get_db().commit()
                return True
        except ValueError:
            pass
    return False


def vote_counts(poll_id):
    rows = get_db().execute(
        "SELECT option_id, COUNT(*) AS c FROM votes WHERE poll_id = ? GROUP BY option_id",
        (poll_id,)).fetchall()
    return {r["option_id"]: r["c"] for r in rows}


def total_ballots(poll_id):
    row = get_db().execute(
        "SELECT COUNT(DISTINCT ip_hash) AS c FROM votes WHERE poll_id = ?",
        (poll_id,)).fetchone()
    return row["c"] if row else 0


def results_payload(poll, options):
    counts = vote_counts(poll["id"])
    total = sum(counts.values())
    items = []
    for opt in options:
        c = counts.get(opt["id"], 0)
        pct = round(c / total * 100, 1) if total else 0.0
        items.append({
            "id": opt["id"],
            "text": opt["text"],
            "votes": c,
            "percent": pct,
        })
    items.sort(key=lambda x: (-x["votes"], x["id"]))
    return {"total_votes": total, "total_ballots": total_ballots(poll["id"]),
            "closed": bool(is_closed(poll)), "options": items}


# ------------------------------------------------------- rate limiting

_RATE_BUCKETS = {}  # ip -> [timestamps]


def rate_limited(ip, limit=20, window=60):
    now = time.time()
    bucket = _RATE_BUCKETS.setdefault(ip, [])
    bucket[:] = [t for t in bucket if now - t < window]
    if len(bucket) >= limit:
        return True
    bucket.append(now)
    return False


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def voter_hash(code, ip):
    return hashlib.sha256(f"{SALT}|{code}|{ip}".encode()).hexdigest()[:32]


# ---------------------------------------------------------------- routes

@app.route("/")
def home():
    db = get_db()
    recent = db.execute(
        """SELECT p.code, p.question, p.created_at,
                  (SELECT COUNT(*) FROM votes v WHERE v.poll_id = p.id) AS votes
           FROM polls p
           WHERE p.show_results = 1
           ORDER BY p.id DESC LIMIT 8""").fetchall()
    return render_template("home.html", recent=recent)


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.route("/new", methods=["GET", "POST"])
def new_poll():
    if request.method == "GET":
        return render_template("new.html")
    errors = []
    question = request.form.get("question", "").strip()
    raw_options = [o.strip() for o in request.form.getlist("options")]
    options = [o for o in raw_options if o]
    allow_multiple = request.form.get("allow_multiple") == "on"
    show_results = request.form.get("show_results", "on") == "on"
    require_name = request.form.get("require_name") == "on"
    closes_at_raw = request.form.get("closes_at", "").strip()
    closes_at = None

    if not question:
        errors.append("Please enter a question.")
    elif len(question) > 300:
        errors.append("Question is too long (max 300 characters).")
    if len(options) < MIN_OPTIONS:
        errors.append(f"Please provide at least {MIN_OPTIONS} options.")
    if len(options) > MAX_OPTIONS:
        errors.append(f"Maximum {MAX_OPTIONS} options allowed.")
    if any(len(o) > 200 for o in options):
        errors.append("Each option must be 200 characters or fewer.")
    if closes_at_raw:
        try:
            dt = datetime.fromisoformat(closes_at_raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt <= datetime.now(timezone.utc):
                errors.append("Close time must be in the future.")
            else:
                closes_at = dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
        except ValueError:
            errors.append("Invalid close date/time.")

    if errors:
        return render_template("new.html", errors=errors,
                               question=question, options=raw_options), 400

    db = get_db()
    code = new_code()
    token = secrets.token_urlsafe(32)
    cur = db.execute(
        """INSERT INTO polls (code, admin_token, question, allow_multiple,
                              show_results, require_name, closes_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (code, token, question, int(allow_multiple), int(show_results),
         int(require_name), closes_at))
    poll_id = cur.lastrowid
    for i, text in enumerate(options):
        db.execute("INSERT INTO options (poll_id, text, position) VALUES (?, ?, ?)",
                   (poll_id, text, i))
    db.commit()
    return redirect(url_for("created", token=token), code=303)


@app.route("/created/<token>")
def created(token):
    poll = get_poll_by_token(token)
    if not poll:
        abort(404)
    share_url = url_for("vote_page", code=poll["code"], _external=True)
    manage_url = url_for("manage", token=token, _external=True)
    return render_template("created.html", poll=poll,
                           share_url=share_url, manage_url=manage_url)


@app.route("/p/<code>")
def vote_page(code):
    poll = get_poll_by_code(code)
    if not poll:
        abort(404)
    options = poll_options(poll["id"])
    closed = is_closed(poll)
    counts = vote_counts(poll["id"])
    total = sum(counts.values())
    results = results_payload(poll, options) if (poll["show_results"] or closed) else None
    already_voted = request.cookies.get(f"qp_voted_{code}") == "1"
    # A cookie alone is advisory; server also checks IP hash on submit.
    return render_template("poll.html", poll=poll, options=options,
                           closed=closed, counts=counts, total=total,
                           results=results, already_voted=already_voted)


@app.route("/p/<code>/vote", methods=["POST"])
def vote(code):
    poll = get_poll_by_code(code)
    if not poll:
        return jsonify({"ok": False, "error": "Poll not found."}), 404
    if is_closed(poll):
        return jsonify({"ok": False, "error": "This poll is closed. Voting has ended."}), 410

    ip = client_ip()
    if rate_limited(ip):
        return jsonify({"ok": False,
                        "error": "Too many votes from your network. Please slow down."}), 429

    vhash = voter_hash(code, ip)
    db = get_db()
    dup = db.execute(
        "SELECT 1 FROM votes WHERE poll_id = ? AND ip_hash = ? LIMIT 1",
        (poll["id"], vhash)).fetchone()
    if dup:
        return jsonify({"ok": False,
                        "error": "You have already voted in this poll."}), 409

    valid_ids = {o["id"] for o in poll_options(poll["id"])}
    try:
        chosen = [int(x) for x in request.form.getlist("options")]
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid option selected."}), 400
    chosen = [c for c in chosen if c in valid_ids]
    if not chosen:
        return jsonify({"ok": False, "error": "Please select an option."}), 400
    if not poll["allow_multiple"] and len(chosen) != 1:
        return jsonify({"ok": False, "error": "Please select exactly one option."}), 400
    if poll["allow_multiple"] and len(chosen) != len(set(chosen)):
        return jsonify({"ok": False, "error": "Duplicate options selected."}), 400

    voter_name = request.form.get("voter_name", "").strip()[:80] or None
    for oid in set(chosen):
        db.execute(
            "INSERT INTO votes (poll_id, option_id, voter_name, ip_hash) VALUES (?, ?, ?, ?)",
            (poll["id"], oid, voter_name, vhash))
    db.commit()

    resp = make_response(jsonify({"ok": True,
                                  "results": results_payload(poll, poll_options(poll["id"]))}))
    resp.set_cookie(f"qp_voted_{code}", "1", max_age=60 * 60 * 24 * 365,
                    httponly=True, samesite="Lax")
    return resp


@app.route("/api/p/<code>/results")
def api_results(code):
    poll = get_poll_by_code(code)
    if not poll:
        return jsonify({"ok": False, "error": "Poll not found."}), 404
    options = poll_options(poll["id"])
    payload = results_payload(poll, options)
    if not poll["show_results"] and not payload["closed"]:
        return jsonify({"ok": True, "hidden": True,
                        "closed": False, "total_votes": payload["total_votes"]})
    return jsonify({"ok": True, "hidden": False, **payload})


@app.route("/m/<token>")
def manage(token):
    poll = get_poll_by_token(token)
    if not poll:
        abort(404)
    options = poll_options(poll["id"])
    results = results_payload(poll, options)
    share_url = url_for("vote_page", code=poll["code"], _external=True)
    vote_total = sum(1 for _ in
                     get_db().execute("SELECT 1 FROM votes WHERE poll_id = ?",
                                      (poll["id"],)).fetchall())
    return render_template("manage.html", poll=poll, options=options,
                           results=results, share_url=share_url,
                           vote_total=vote_total, closed=is_closed(poll))


@app.route("/m/<token>/close", methods=["POST"])
def manage_close(token):
    poll = get_poll_by_token(token)
    if not poll:
        abort(404)
    get_db().execute("UPDATE polls SET closed = 1 WHERE id = ?", (poll["id"],))
    get_db().commit()
    return redirect(url_for("manage", token=token))


@app.route("/m/<token>/reopen", methods=["POST"])
def manage_reopen(token):
    poll = get_poll_by_token(token)
    if not poll:
        abort(404)
    get_db().execute("UPDATE polls SET closed = 0, closes_at = NULL WHERE id = ?",
                     (poll["id"],))
    get_db().commit()
    return redirect(url_for("manage", token=token))


@app.route("/m/<token>/delete", methods=["POST"])
def manage_delete(token):
    poll = get_poll_by_token(token)
    if not poll:
        abort(404)
    db = get_db()
    db.execute("DELETE FROM polls WHERE id = ?", (poll["id"],))
    db.commit()
    return redirect(url_for("home"))


@app.route("/m/<token>/edit", methods=["POST"])
def manage_edit(token):
    poll = get_poll_by_token(token)
    if not poll:
        abort(404)
    db = get_db()
    votes = db.execute("SELECT COUNT(*) AS c FROM votes WHERE poll_id = ?",
                       (poll["id"],)).fetchone()["c"]
    if votes > 0:
        return redirect(url_for("manage", token=token))
    question = request.form.get("question", "").strip()
    raw_options = [o.strip() for o in request.form.getlist("options")]
    options = [o for o in raw_options if o]
    if question and 1 <= len(question) <= 300 and MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        db.execute("UPDATE polls SET question = ? WHERE id = ?", (question, poll["id"]))
        db.execute("DELETE FROM options WHERE poll_id = ?", (poll["id"],))
        for i, text in enumerate(options):
            db.execute("INSERT INTO options (poll_id, text, position) VALUES (?, ?, ?)",
                       (poll["id"], text[:200], i))
        db.commit()
    return redirect(url_for("manage", token=token))


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("500.html"), 500


# Ensure schema exists at import time too (e.g. when served by gunicorn,
# where the __main__ block below never runs). CREATE TABLE IF NOT EXISTS
# makes this safe with multiple workers.
with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
