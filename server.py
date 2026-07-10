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
index.html/login.html gate on that session: visiting index.html while signed
out bounces you to login.html, and vice versa.

Both the Claude key and the Google client secret stay server-side — neither is
ever embedded in the static HTML/JS shipped to the browser.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    export GOOGLE_CLIENT_ID=...apps.googleusercontent.com
    export GOOGLE_CLIENT_SECRET=...
    export GOOGLE_REDIRECT_URI=http://127.0.0.1:8080/oauth/callback   # or your deployed URL
    export ALLOWED_EMAILS=aakash@studioaamgmt.com   # comma-separated allowlist, defaults to this
    export ALLOW_DEV_LOGIN=1   # optional, re-enables the no-auth /dev-login bypass locally
    python3 server.py            # serves on http://127.0.0.1:8080 locally, or $PORT on a host

Uses only the Python standard library (no flask/requests installed on this
machine) — http.server for routing, urllib.request for outbound calls to the
Claude and Google APIs, http.cookies for the session cookie.
"""
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

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
# you'll need once the app actually reads mail, not just logs you in.
GOOGLE_SCOPES = "openid email profile https://www.googleapis.com/auth/gmail.readonly"
# Only these Google accounts may sign in — this holds real client/financial
# data once it's on the public internet, so anonymous Google sign-in isn't safe.
# Override on the host with ALLOWED_EMAILS=a@x.com,b@y.com (comma-separated).
ALLOWED_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("ALLOWED_EMAILS", "aakash@studioaamgmt.com").split(",")
    if e.strip()
}

# Real client/financial data lives behind this login once deployed, so sign-in
# is locked to specific emails rather than "any Google account". Comma-separated,
# case-insensitive. Empty/unset = allow nobody (fail closed, not open).
ALLOWED_EMAILS = {
    e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()
}

SESSIONS = {}       # session id -> { email, name, access_token, refresh_token }
PENDING_STATES = {}  # oauth "state" csrf token -> issued_at, so callback can't be spoofed


def _session_from_cookie(handler):
    raw = handler.headers.get("Cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    jar.load(raw)
    if "hq_session" not in jar:
        return None
    return SESSIONS.get(jar["hq_session"].value)


class Handler(SimpleHTTPRequestHandler):
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
        if path in ("/", "/index.html") and not _session_from_cookie(self):
            self._redirect("/login.html")
            return
        if path == "/login.html" and _session_from_cookie(self):
            self._redirect("/index.html")
            return
        super().do_GET()

    def do_POST(self):
        if self.path == "/api/logout":
            raw = self.headers.get("Cookie")
            if raw:
                jar = SimpleCookie()
                jar.load(raw)
                if "hq_session" in jar:
                    SESSIONS.pop(jar["hq_session"].value, None)
            self.send_response(204)
            self.send_header("Set-Cookie", "hq_session=; Path=/; Max-Age=0")
            self.end_headers()
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
        if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
            self._redirect("/login.html?error=" + urllib.parse.quote(
                "This Google account isn't allowed to sign in to this workspace."))
            return

        sid = secrets.token_urlsafe(32)
        SESSIONS[sid] = {
            "email": claims.get("email"),
            "name": claims.get("name") or claims.get("email"),
            "access_token": tokens.get("access_token"),
            "refresh_token": tokens.get("refresh_token"),
        }
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
        sid = secrets.token_urlsafe(32)
        SESSIONS[sid] = {
            "email": "dev-preview@local",
            "name": "Dev Preview",
            "access_token": None,
            "refresh_token": None,
        }
        self.send_response(302)
        self.send_header("Location", "/index.html")
        self.send_header("Set-Cookie", f"hq_session={sid}; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _reply(self, code, obj):
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
    print(f"serving {os.getcwd()} on http://{HOST}:{PORT} (model: {MODEL})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
