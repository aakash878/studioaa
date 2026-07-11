#!/usr/bin/env python3
"""
Local dev server for the HQ project.

Serves index.html/login.html as static files (same as `python3 -m http.server`)
and adds:
  - POST /api/route     asks Claude whether a Whiteboard line starts a new
                         section or links to an existing one.
  - GET  /oauth/start    kicks off "Sign in with Google" (real OAuth 2.0).
  - GET  /oauth/callback Google redirects here with a code; we exchange it,
                         verify it, and start a session.
  - GET  /api/me         who's currently signed in (or 401).
  - POST /api/logout     clears the session.
  - GET  /api/calendar/upcoming  next 7 days of the signed-in user's primary
                                 Google Calendar (live, via calendar.readonly).
  - GET  /api/workspaces                       workspaces the signed-in user belongs to.
  - POST /api/workspaces                       create a new workspace (caller becomes owner).
  - PATCH /api/workspaces/<id>                 rename a workspace (owner-only).
  - GET  /PUT /api/workspaces/<id>/data         read/write a workspace's pages+categories+widgets.
  - GET  /POST /api/workspaces/<id>/members     list / invite members (owner-only).
  - DELETE /api/workspaces/<id>/members/<email> remove a member or pending invite (owner-only).
  - POST /api/workspaces/<id>/files             upload a file (base64 JSON body, 8MB cap); returns {id, filename, size}.
  - GET  /api/files/<id>                        download a previously uploaded file (workspace members only).
  - DELETE /api/files/<id>                      delete an uploaded file (workspace members only).
index.html/login.html gate on that session: visiting index.html while signed
out bounces you to login.html, and vice versa.

Both the Claude key and the Google client secret stay server-side — neither is
ever embedded in the static HTML/JS shipped to the browser.

Data (users, workspaces, memberships, and each workspace's pages/categories)
lives in Postgres — see init_db() for the schema. Every account is scoped to
one or more workspaces instead of one shared global dataset, so different
clients/collaborators don't see each other's data.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    export GOOGLE_CLIENT_ID=...apps.googleusercontent.com
    export GOOGLE_CLIENT_SECRET=...
    export GOOGLE_REDIRECT_URI=http://127.0.0.1:8080/oauth/callback   # or your deployed URL
    export ALLOWED_EMAILS=aakash@studioaamgmt.com   # bootstrap allowlist, see note below
    export DATABASE_URL=postgres://user:pass@host:5432/dbname   # Render sets this automatically
    export ALLOW_DEV_LOGIN=1   # optional, re-enables the no-auth /dev-login bypass locally
    python3 server.py            # serves on http://127.0.0.1:8080 locally, or $PORT on a host

ALLOWED_EMAILS is only a *bootstrap* gate: an allowlisted email with no
workspace yet gets a brand-new workspace (as owner) on first sign-in. Anyone
else can only sign in if they already belong to a workspace or have a pending
invite waiting for their email — see _oauth_callback.

Uses the Python standard library for HTTP (http.server for routing,
urllib.request for outbound calls to the Claude and Google APIs,
http.cookies for the session cookie) plus psycopg2 for Postgres.
"""
import base64
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import psycopg2
import psycopg2.extras

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
# Claude Haiku 4.5 — this is a cheap, high-frequency, single-decision classification
# call (new section vs. link existing), so the fast/cheap model is the right default.
# Override with CLAUDE_MODEL=claude-opus-4-6 (or claude-sonnet-4-6) if you want a
# smarter router instead.
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5")

SYSTEM_PROMPT = (
    "You are the router for a freeform whiteboard in a lightweight workspace tool. "
    "The user types short, casual lines of text. Your only job is to decide where "
    "each line goes: does it belong in a section that already exists, or does it "
    "need a brand new section created for it? Sections are loose, informal topic "
    "buckets (e.g. \"Moodboard\", \"Shipping\", \"Notes\") — prefer linking to an "
    "existing section whenever the line reasonably fits one, and fall back to "
    "\"Notes\" for a stray thought that doesn't fit any topic. Only choose "
    "new_section when the line clearly introduces a topic that isn't covered by "
    "any existing section. New section names should be short, Title Case, 1-2 words."
)

ROUTE_TOOL = {
    "name": "route_line",
    "description": "File this line of text into a section.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["new_section", "link_existing"],
            },
            "section": {
                "type": "string",
                "description": "New short section name, or the exact name of the existing section to link to.",
            },
        },
        "required": ["action", "section"],
    },
}

# ---------- Google sign-in ----------
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
PORT = int(os.environ.get("PORT", 8080))
# 0.0.0.0 so the process is reachable from outside its container on a host
# like Render; still works fine locally via http://127.0.0.1:PORT or http://localhost:PORT.
HOST = os.environ.get("HOST", "0.0.0.0")
REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", f"http://127.0.0.1:{PORT}/oauth/callback")
# openid/email/profile identify who signed in; gmail.readonly is the scope
# you'll need once the app actually reads mail, not just logs you in;
# calendar.readonly powers the live "this week" home widget.
GOOGLE_SCOPES = (
    "openid email profile "
    "https://www.googleapis.com/auth/gmail.readonly "
    "https://www.googleapis.com/auth/calendar.readonly"
)
# Bootstrap allowlist: an email here with zero workspaces gets a brand-new
# workspace on first sign-in. Anyone else needs an existing membership or a
# pending invite (see _oauth_callback). Comma-separated, case-insensitive.
# Empty/unset = allow nobody in as a fresh bootstrap (fail closed, not open).
ALLOWED_EMAILS = {
    e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()
}

# ---------- Postgres (users, workspaces, membership, per-workspace data) ----------
DATABASE_URL = os.environ.get("DATABASE_URL")


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                name TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS workspaces (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                created_by INTEGER REFERENCES users(id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS workspace_members (
                workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT NOT NULL DEFAULT 'member',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (workspace_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS workspace_invites (
                workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                email TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                invited_by INTEGER REFERENCES users(id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (workspace_id, email)
            );
            CREATE TABLE IF NOT EXISTS workspace_data (
                workspace_id INTEGER PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
                pages JSONB,
                categories JSONB,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            ALTER TABLE workspace_data ADD COLUMN IF NOT EXISTS widgets JSONB;
            CREATE TABLE IF NOT EXISTS sessions (
                sid TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                email TEXT NOT NULL,
                name TEXT,
                access_token TEXT,
                refresh_token TEXT,
                token_expiry DOUBLE PRECISION,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS page_shares (
                id SERIAL PRIMARY KEY,
                workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                page_key TEXT NOT NULL,
                email TEXT,
                token TEXT UNIQUE,
                role TEXT NOT NULL DEFAULT 'viewer',
                invited_by INTEGER REFERENCES users(id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS page_shares_email_idx ON page_shares(email);
            CREATE TABLE IF NOT EXISTS files (
                id SERIAL PRIMARY KEY,
                workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                content_type TEXT,
                size INTEGER NOT NULL,
                data BYTEA NOT NULL,
                uploaded_by INTEGER REFERENCES users(id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        conn.commit()


def upsert_user(email, name):
    email = email.lower()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (email, name) VALUES (%s, %s)
            ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
            """,
            (email, name),
        )
        user_id = cur.fetchone()[0]
        conn.commit()
        return user_id


def apply_pending_invites(user_id, email):
    """Turn any invites addressed to this email into real memberships."""
    email = email.lower()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT workspace_id, role FROM workspace_invites WHERE email = %s", (email,))
        invites = cur.fetchall()
        for workspace_id, role in invites:
            cur.execute(
                """
                INSERT INTO workspace_members (workspace_id, user_id, role)
                VALUES (%s, %s, %s)
                ON CONFLICT (workspace_id, user_id) DO NOTHING
                """,
                (workspace_id, user_id, role),
            )
        cur.execute("DELETE FROM workspace_invites WHERE email = %s", (email,))
        conn.commit()


def user_workspace_count(user_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM workspace_members WHERE user_id = %s", (user_id,))
        return cur.fetchone()[0]


def create_workspace(name, owner_user_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workspaces (name, created_by) VALUES (%s, %s) RETURNING id",
            (name, owner_user_id),
        )
        workspace_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s, %s, 'owner')",
            (workspace_id, owner_user_id),
        )
        cur.execute(
            "INSERT INTO workspace_data (workspace_id, pages, categories) VALUES (%s, NULL, NULL)",
            (workspace_id,),
        )
        conn.commit()
        return workspace_id


def workspace_role(cur, user_id, workspace_id):
    cur.execute(
        "SELECT role FROM workspace_members WHERE workspace_id = %s AND user_id = %s",
        (workspace_id, user_id),
    )
    row = cur.fetchone()
    return row[0] if row else None


PENDING_STATES = {}  # oauth "state" csrf token -> issued_at, so callback can't be spoofed


def _create_session(user_id, email, name, access_token, refresh_token, token_expiry):
    """Sessions live in Postgres, not an in-process dict — Render restarts the
    process on every deploy and free-tier services also cold-start after idle
    spin-down, which would otherwise silently log everyone out and make every
    subsequent fetch() 401 (looking like "nothing saves after refresh")."""
    sid = secrets.token_urlsafe(32)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sessions (sid, user_id, email, name, access_token, refresh_token, token_expiry)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (sid, user_id, email, name, access_token, refresh_token, token_expiry),
        )
        conn.commit()
    return sid


def _get_session(sid):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT user_id, email, name, access_token, refresh_token, token_expiry FROM sessions WHERE sid = %s",
            (sid,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "sid": sid, "user_id": row[0], "email": row[1], "name": row[2],
        "access_token": row[3], "refresh_token": row[4], "token_expiry": row[5],
    }


def _update_session_tokens(sid, access_token, token_expiry):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE sessions SET access_token = %s, token_expiry = %s WHERE sid = %s",
            (access_token, token_expiry, sid),
        )
        conn.commit()


def _delete_session(sid):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE sid = %s", (sid,))
        conn.commit()


def _find_page(pages, page_key):
    for p in (pages or []):
        if p.get("key") == page_key:
            return p
    return None


def _replace_page(pages, page_key, updates):
    """Returns a new pages array with the one page matching page_key merged
    with `updates` (e.g. {"blocks": [...]} or {"rows": [...], "cols": [...]}).
    Every other page is left untouched — this lets a page-guest (viewer/editor
    scoped to a single page, not the whole workspace) save an edit without
    ever reading or writing the rest of the workspace's data."""
    out = []
    for p in (pages or []):
        if p.get("key") == page_key:
            merged = dict(p)
            merged.update(updates)
            out.append(merged)
        else:
            out.append(p)
    return out


def _page_share_access(cur, user_id, email, workspace_id, page_key):
    """Returns 'owner' (via workspace_role) or the page_shares.role for a
    page-guest, or None if this user has no access to this page at all."""
    role = workspace_role(cur, user_id, workspace_id)
    if role:
        return role
    cur.execute(
        "SELECT role FROM page_shares WHERE workspace_id = %s AND page_key = %s AND email = %s",
        (workspace_id, page_key, email),
    )
    row = cur.fetchone()
    return row[0] if row else None


WORKSPACE_RE = re.compile(r"^/api/workspaces/(\d+)$")
WORKSPACE_DATA_RE = re.compile(r"^/api/workspaces/(\d+)/data$")
WORKSPACE_MEMBERS_RE = re.compile(r"^/api/workspaces/(\d+)/members$")
WORKSPACE_MEMBER_RE = re.compile(r"^/api/workspaces/(\d+)/members/([^/]+)$")
PAGE_SHARES_RE = re.compile(r"^/api/workspaces/(\d+)/pages/([^/]+)/shares$")
PAGE_SHARE_RE = re.compile(r"^/api/workspaces/(\d+)/pages/([^/]+)/shares/(\d+)$")
SHARED_PAGE_RE = re.compile(r"^/api/workspaces/(\d+)/pages/([^/]+)/view$")
SHARED_TOKEN_RE = re.compile(r"^/api/shared/([A-Za-z0-9_-]+)$")
FILES_RE = re.compile(r"^/api/workspaces/(\d+)/files$")
FILE_RE = re.compile(r"^/api/files/(\d+)$")
MAX_FILE_BYTES = 8 * 1024 * 1024  # 8MB per upload


def _refresh_access_token(session):
    """Mint a fresh access_token from the stored refresh_token. Mutates
    session in place and returns True on success, False if we can't refresh
    (no refresh_token on file, or Google rejected it — e.g. revoked access)."""
    refresh_token = session.get("refresh_token")
    if not refresh_token:
        return False
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=urllib.parse.urlencode({
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode("utf-8"),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            tokens = json.loads(resp.read())
    except urllib.error.HTTPError:
        return False
    except urllib.error.URLError:
        return False
    session["access_token"] = tokens.get("access_token")
    session["token_expiry"] = time.time() + int(tokens.get("expires_in", 3600))
    _update_session_tokens(session["sid"], session["access_token"], session["token_expiry"])
    return True


def _fetch_calendar_events(session):
    """Returns (connected: bool, reauth_required: bool, events: list). Refreshes
    the access token first if it's missing or about to expire."""
    if not session.get("access_token") or not session.get("refresh_token"):
        return False, True, []
    if not session.get("token_expiry") or time.time() > session["token_expiry"] - 60:
        if not _refresh_access_token(session):
            return False, True, []

    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=7)).isoformat()
    params = {
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "20",
    }
    url = "https://www.googleapis.com/calendar/v3/calendars/primary/events?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {session['access_token']}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, True, []
        return False, False, []
    except urllib.error.URLError:
        return False, False, []

    events = []
    for item in data.get("items", []):
        start = item.get("start", {})
        end = item.get("end", {})
        all_day = "date" in start
        events.append({
            "id": item.get("id"),
            "summary": item.get("summary") or "(untitled event)",
            "start": start.get("date") if all_day else start.get("dateTime"),
            "end": end.get("date") if all_day else end.get("dateTime"),
            "allDay": all_day,
            "location": item.get("location"),
        })
    return True, False, events


def _session_from_cookie(handler):
    raw = handler.headers.get("Cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    jar.load(raw)
    if "hq_session" not in jar:
        return None
    return _get_session(jar["hq_session"].value)


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # Force revalidation on every load for the app shell so a plain
        # refresh (not just a hard refresh) always picks up the latest
        # deployed index.html/login.html instead of a browser-cached copy.
        if self.path in ("/", "/index.html", "/login.html"):
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        super().end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/oauth/start":
            self._oauth_start()
            return
        if path == "/oauth/callback":
            self._oauth_callback(urllib.parse.parse_qs(parsed.query))
            return
        if path == "/dev-login":
            self._dev_login()
            return
        if path == "/api/me":
            session = _session_from_cookie(self)
            if not session:
                self._reply(401, {"error": "not signed in"})
            else:
                self._reply(200, {"email": session["email"], "name": session["name"]})
            return
        if path == "/api/workspaces":
            session = self._require_session()
            if not session:
                return
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT w.id, w.name, m.role FROM workspaces w
                    JOIN workspace_members m ON m.workspace_id = w.id
                    WHERE m.user_id = %s ORDER BY w.id
                    """,
                    (session["user_id"],),
                )
                rows = cur.fetchall()
            self._reply(200, [{"id": r[0], "name": r[1], "role": r[2]} for r in rows])
            return
        if path == "/api/calendar/upcoming":
            session = self._require_session()
            if not session:
                return
            connected, reauth_required, events = _fetch_calendar_events(session)
            self._reply(200, {
                "connected": connected,
                "reauth_required": reauth_required,
                "events": events,
            })
            return
        m = WORKSPACE_DATA_RE.match(path)
        if m:
            session = self._require_session()
            if not session:
                return
            workspace_id = int(m.group(1))
            with get_conn() as conn, conn.cursor() as cur:
                if not workspace_role(cur, session["user_id"], workspace_id):
                    self._reply(403, {"error": "not a member of this workspace"})
                    return
                cur.execute(
                    "SELECT pages, categories, widgets FROM workspace_data WHERE workspace_id = %s",
                    (workspace_id,),
                )
                row = cur.fetchone()
            self._reply(200, {
                "pages": row[0] if row else None,
                "categories": row[1] if row else None,
                "widgets": row[2] if row else None,
            })
            return
        m = WORKSPACE_MEMBERS_RE.match(path)
        if m:
            session = self._require_session()
            if not session:
                return
            workspace_id = int(m.group(1))
            with get_conn() as conn, conn.cursor() as cur:
                if workspace_role(cur, session["user_id"], workspace_id) != "owner":
                    self._reply(403, {"error": "only the workspace owner can view members"})
                    return
                cur.execute(
                    """
                    SELECT u.email, u.name, m.role FROM workspace_members m
                    JOIN users u ON u.id = m.user_id
                    WHERE m.workspace_id = %s ORDER BY m.created_at
                    """,
                    (workspace_id,),
                )
                members = [{"email": r[0], "name": r[1], "role": r[2]} for r in cur.fetchall()]
                cur.execute(
                    "SELECT email, role FROM workspace_invites WHERE workspace_id = %s ORDER BY created_at",
                    (workspace_id,),
                )
                invites = [{"email": r[0], "role": r[1]} for r in cur.fetchall()]
            self._reply(200, {"members": members, "invites": invites})
            return
        m = FILE_RE.match(path)
        if m:
            session = self._require_session()
            if not session:
                return
            file_id = int(m.group(1))
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT workspace_id, filename, content_type, data FROM files WHERE id = %s",
                    (file_id,),
                )
                row = cur.fetchone()
                if not row or not workspace_role(cur, session["user_id"], row[0]):
                    self.send_error(404)
                    return
            _, filename, content_type, data = row
            self.send_response(200)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", "attachment; filename=\"" + filename.replace('"', "") + "\"")
            self.end_headers()
            self.wfile.write(bytes(data))
            return
        if path in ("/", "/index.html") and not _session_from_cookie(self):
            self._redirect("/login.html")
            return
        if path == "/login.html" and _session_from_cookie(self):
            self._redirect("/index.html")
            return
        super().do_GET()

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        m = WORKSPACE_DATA_RE.match(path)
        if not m:
            self.send_error(404)
            return
        session = self._require_session()
        if not session:
            return
        workspace_id = int(m.group(1))
        body = self._read_json_body()
        if body is None:
            return
        with get_conn() as conn, conn.cursor() as cur:
            if not workspace_role(cur, session["user_id"], workspace_id):
                self._reply(403, {"error": "not a member of this workspace"})
                return
            cur.execute(
                """
                UPDATE workspace_data SET pages = %s, categories = %s, widgets = %s, updated_at = now()
                WHERE workspace_id = %s
                """,
                (
                    psycopg2.extras.Json(body.get("pages")),
                    psycopg2.extras.Json(body.get("categories")),
                    psycopg2.extras.Json(body.get("widgets")),
                    workspace_id,
                ),
            )
            conn.commit()
        self._reply(204, None)

    def do_PATCH(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        m = WORKSPACE_RE.match(path)
        if not m:
            self.send_error(404)
            return
        session = self._require_session()
        if not session:
            return
        workspace_id = int(m.group(1))
        body = self._read_json_body()
        if body is None:
            return
        name = (body.get("name") or "").strip()
        if not name:
            self._reply(400, {"error": "missing name"})
            return
        with get_conn() as conn, conn.cursor() as cur:
            if workspace_role(cur, session["user_id"], workspace_id) != "owner":
                self._reply(403, {"error": "only the workspace owner can rename it"})
                return
            cur.execute("UPDATE workspaces SET name = %s WHERE id = %s", (name, workspace_id))
            conn.commit()
        self._reply(200, {"id": workspace_id, "name": name})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        m = FILE_RE.match(path)
        if m:
            session = self._require_session()
            if not session:
                return
            file_id = int(m.group(1))
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT workspace_id FROM files WHERE id = %s", (file_id,))
                row = cur.fetchone()
                if not row or not workspace_role(cur, session["user_id"], row[0]):
                    self.send_error(404)
                    return
                cur.execute("DELETE FROM files WHERE id = %s", (file_id,))
                conn.commit()
            self._reply(204, None)
            return
        m = WORKSPACE_MEMBER_RE.match(path)
        if not m:
            self.send_error(404)
            return
        session = self._require_session()
        if not session:
            return
        workspace_id = int(m.group(1))
        target_email = urllib.parse.unquote(m.group(2)).lower()
        with get_conn() as conn, conn.cursor() as cur:
            if workspace_role(cur, session["user_id"], workspace_id) != "owner":
                self._reply(403, {"error": "only the workspace owner can remove members"})
                return
            cur.execute(
                "SELECT COUNT(*) FROM workspace_members WHERE workspace_id = %s AND role = 'owner'",
                (workspace_id,),
            )
            owner_count = cur.fetchone()[0]
            cur.execute(
                """
                SELECT m.role FROM workspace_members m JOIN users u ON u.id = m.user_id
                WHERE m.workspace_id = %s AND u.email = %s
                """,
                (workspace_id, target_email),
            )
            row = cur.fetchone()
            if row and row[0] == "owner" and owner_count <= 1:
                self._reply(400, {"error": "can't remove the last owner of a workspace"})
                return
            cur.execute(
                """
                DELETE FROM workspace_members WHERE workspace_id = %s AND user_id = (
                    SELECT id FROM users WHERE email = %s
                )
                """,
                (workspace_id, target_email),
            )
            cur.execute(
                "DELETE FROM workspace_invites WHERE workspace_id = %s AND email = %s",
                (workspace_id, target_email),
            )
            conn.commit()
        self._reply(204, None)

    def do_POST(self):
        if self.path == "/api/logout":
            raw = self.headers.get("Cookie")
            if raw:
                jar = SimpleCookie()
                jar.load(raw)
                if "hq_session" in jar:
                    _delete_session(jar["hq_session"].value)
            self.send_response(204)
            self.send_header("Set-Cookie", "hq_session=; Path=/; Max-Age=0")
            self.end_headers()
            return
        if self.path == "/api/workspaces":
            session = self._require_session()
            if not session:
                return
            body = self._read_json_body()
            if body is None:
                return
            name = (body.get("name") or "").strip() or "Untitled Workspace"
            workspace_id = create_workspace(name, session["user_id"])
            self._reply(200, {"id": workspace_id, "name": name, "role": "owner"})
            return
        m = WORKSPACE_MEMBERS_RE.match(self.path)
        if m:
            session = self._require_session()
            if not session:
                return
            workspace_id = int(m.group(1))
            body = self._read_json_body()
            if body is None:
                return
            email = (body.get("email") or "").strip().lower()
            role = body.get("role") or "member"
            if not email:
                self._reply(400, {"error": "missing email"})
                return
            with get_conn() as conn, conn.cursor() as cur:
                if workspace_role(cur, session["user_id"], workspace_id) != "owner":
                    self._reply(403, {"error": "only the workspace owner can invite members"})
                    return
                cur.execute("SELECT id FROM users WHERE email = %s", (email,))
                existing = cur.fetchone()
                if existing:
                    cur.execute(
                        """
                        INSERT INTO workspace_members (workspace_id, user_id, role)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (workspace_id, user_id) DO UPDATE SET role = EXCLUDED.role
                        """,
                        (workspace_id, existing[0], role),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO workspace_invites (workspace_id, email, role, invited_by)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (workspace_id, email) DO UPDATE SET role = EXCLUDED.role
                        """,
                        (workspace_id, email, role, session["user_id"]),
                    )
                conn.commit()
            self._reply(204, None)
            return
        m = FILES_RE.match(self.path)
        if m:
            session = self._require_session()
            if not session:
                return
            workspace_id = int(m.group(1))
            body = self._read_json_body()
            if body is None:
                return
            filename = (body.get("filename") or "upload").strip()
            content_type = body.get("contentType") or "application/octet-stream"
            data_b64 = body.get("dataBase64") or ""
            try:
                data = base64.b64decode(data_b64)
            except Exception:
                self._reply(400, {"error": "invalid file data"})
                return
            if len(data) > MAX_FILE_BYTES:
                self._reply(413, {"error": "file too large (max 8MB)"})
                return
            with get_conn() as conn, conn.cursor() as cur:
                if not workspace_role(cur, session["user_id"], workspace_id):
                    self._reply(403, {"error": "not a member of this workspace"})
                    return
                cur.execute(
                    """
                    INSERT INTO files (workspace_id, filename, content_type, size, data, uploaded_by)
                    VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
                    """,
                    (workspace_id, filename, content_type, len(data), psycopg2.Binary(data), session["user_id"]),
                )
                file_id = cur.fetchone()[0]
                conn.commit()
            self._reply(200, {"id": file_id, "filename": filename, "size": len(data)})
            return
        if self.path != "/api/route":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._reply(400, {"error": "invalid JSON body"})
            return

        text = (body.get("text") or "").strip()
        sections = body.get("sections") or ["Notes"]
        if not text:
            self._reply(400, {"error": "missing text"})
            return
        if not API_KEY:
            self._reply(500, {"error": "ANTHROPIC_API_KEY is not set on the server"})
            return

        payload = {
            "model": MODEL,
            "max_tokens": 200,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": "Existing sections: " + json.dumps(sections) + "\nLine: " + text,
                }
            ],
            "tools": [ROUTE_TOOL],
            "tool_choice": {"type": "tool", "name": "route_line"},
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": API_KEY,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            self._reply(e.code, {"error": e.read().decode("utf-8", "replace")})
            return
        except urllib.error.URLError as e:
            self._reply(502, {"error": str(e.reason)})
            return

        tool_use = next((b for b in data.get("content", []) if b.get("type") == "tool_use"), None)
        if not tool_use:
            self._reply(502, {"error": "no tool_use block in Claude's response"})
            return
        self._reply(200, tool_use["input"])

    def _oauth_start(self):
        if not GOOGLE_CLIENT_ID:
            self._redirect("/login.html?error=" + urllib.parse.quote("GOOGLE_CLIENT_ID is not set on the server"))
            return
        state = secrets.token_urlsafe(16)
        PENDING_STATES[state] = time.time()
        params = {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": GOOGLE_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        self._redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params))

    def _oauth_callback(self, query):
        error = query.get("error", [None])[0]
        if error:
            self._redirect("/login.html?error=" + urllib.parse.quote(error))
            return
        state = query.get("state", [None])[0]
        code = query.get("code", [None])[0]
        if not code or not state or PENDING_STATES.pop(state, None) is None:
            self._redirect("/login.html?error=" + urllib.parse.quote("invalid or expired oauth state"))
            return

        token_req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=urllib.parse.urlencode({
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
            }).encode("utf-8"),
            method="POST",
        )
        try:
            with urllib.request.urlopen(token_req, timeout=20) as resp:
                tokens = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            self._redirect("/login.html?error=" + urllib.parse.quote(e.read().decode("utf-8", "replace")[:200]))
            return

        # Verify the id_token with Google itself instead of checking the JWT
        # signature locally (would need a crypto library we don't have) — this
        # endpoint validates it server-side and hands back the trusted claims.
        verify_url = "https://oauth2.googleapis.com/tokeninfo?" + urllib.parse.urlencode(
            {"id_token": tokens.get("id_token", "")}
        )
        try:
            with urllib.request.urlopen(verify_url, timeout=20) as resp:
                claims = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            self._redirect("/login.html?error=" + urllib.parse.quote("id_token verification failed"))
            return
        if claims.get("aud") != GOOGLE_CLIENT_ID:
            self._redirect("/login.html?error=" + urllib.parse.quote("token audience mismatch"))
            return
        email = (claims.get("email") or "").lower()
        name = claims.get("name") or claims.get("email")

        # Every account is scoped to workspace membership, not a flat allowlist:
        # upsert the user, absorb any invite waiting for this email, and — only
        # if they still belong to nothing — bootstrap a first workspace, but
        # only for emails on ALLOWED_EMAILS. Anyone else with no membership and
        # no invite is turned away (fail closed).
        user_id = upsert_user(email, name)
        apply_pending_invites(user_id, email)
        if user_workspace_count(user_id) == 0:
            if email not in ALLOWED_EMAILS:
                self._redirect("/login.html?error=" + urllib.parse.quote(
                    "This Google account isn't allowed to sign in to this workspace."))
                return
            create_workspace(f"{name.split(' ')[0]}\u2019s Workspace" if name else "My Workspace", user_id)

        sid = _create_session(
            user_id, claims.get("email"), name,
            tokens.get("access_token"), tokens.get("refresh_token"),
            time.time() + int(tokens.get("expires_in", 3600)),
        )
        self.send_response(302)
        self.send_header("Location", "/index.html")
        self.send_header("Set-Cookie", f"hq_session={sid}; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()

    def _dev_login(self):
        # Preview-only bypass: no Google account needed, just look around.
        # Off by default — this holds real client/financial data once deployed,
        # so an unauthenticated "log in as anyone" route can't be reachable in
        # production. Set ALLOW_DEV_LOGIN=1 locally if you want it back.
        if os.environ.get("ALLOW_DEV_LOGIN") != "1":
            self.send_error(404)
            return
        user_id = upsert_user("dev-preview@local", "Dev Preview")
        if user_workspace_count(user_id) == 0:
            create_workspace("Dev Preview Workspace", user_id)
        sid = _create_session(user_id, "dev-preview@local", "Dev Preview", None, None, None)
        self.send_response(302)
        self.send_header("Location", "/index.html")
        self.send_header("Set-Cookie", f"hq_session={sid}; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()

    def _require_session(self):
        session = _session_from_cookie(self)
        if not session:
            self._reply(401, {"error": "not signed in"})
            return None
        return session

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._reply(400, {"error": "invalid JSON body"})
            return None

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _reply(self, code, obj):
        if obj is None:
            self.send_response(code)
            self.end_headers()
            return
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # keep the default access log; override only if you want it quieter
        super().log_message(fmt, *args)


if __name__ == "__main__":
    if not API_KEY:
        print("warning: ANTHROPIC_API_KEY is not set — /api/route will return 500 "
              "until you `export ANTHROPIC_API_KEY=sk-ant-...` and restart this server.")
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        print("warning: GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET are not set — sign-in "
              "will fail until you set both and restart this server.")
    if not DATABASE_URL:
        print("warning: DATABASE_URL is not set — sign-in and workspace data will fail "
              "until you `export DATABASE_URL=postgres://...` and restart this server.")
    else:
        init_db()
    print(f"serving {os.getcwd()} on http://{HOST}:{PORT} (model: {MODEL})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
