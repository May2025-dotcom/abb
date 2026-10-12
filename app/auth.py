"""
Optional single-password login.

Set APP_PASSWORD to turn it on (required when the app is reachable from the
internet). With it unset the app is open, which is fine on a home network.

- Every page and API call needs a logged-in session, except /login and /static.
- Sessions last 30 days and survive restarts (the signing key is kept in /data).
- Each address gets MAX_FAILURES wrong guesses per LOCKOUT_SECONDS, and each
  wrong guess is slowed down, so the password can't be brute-forced.
"""

import hmac
import os
import secrets
import threading
import time
from datetime import timedelta

from flask import jsonify, redirect, render_template, request, session, url_for

MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
FAILURE_DELAY = 1.0

_failures = {}  # address -> list of failure times
_lock = threading.Lock()


def _load_secret_key(data_dir):
    key = os.getenv("SECRET_KEY", "").strip()
    if key:
        return key
    path = os.path.join(data_dir, "secret_key")
    try:
        with open(path) as f:
            key = f.read().strip()
    except FileNotFoundError:
        key = ""
    if not key:
        key = secrets.token_hex(32)
        with open(path, "w") as f:
            f.write(key)
        os.chmod(path, 0o600)
    return key


def _recent_failures(addr, now):
    with _lock:
        times = [t for t in _failures.get(addr, []) if now - t < LOCKOUT_SECONDS]
        _failures[addr] = times
        return times


def _record_failure(addr, now):
    with _lock:
        _failures.setdefault(addr, []).append(now)


def init_auth(app, data_dir):
    password = os.getenv("APP_PASSWORD", "")
    app.secret_key = _load_secret_key(data_dir)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Set COOKIE_SECURE=true when the app is served over HTTPS.
        SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )

    if os.getenv("TRUST_PROXY", "").lower() in ("1", "true", "yes"):
        # Behind a reverse proxy: use the visitor's real address for lockouts.
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    enabled = bool(password)
    print(f"Login: {'on' if enabled else 'OFF - anyone who can reach this app can use it'}")

    @app.context_processor
    def _auth_globals():
        return {"auth_enabled": enabled}

    if not enabled:
        return

    @app.before_request
    def _require_login():
        if request.endpoint in ("login", "static") or session.get("auth"):
            return None
        # Buttons call the API with fetch(); tell them plainly instead of redirecting.
        if request.method == "POST" and request.is_json:
            return jsonify({"message": "You've been logged out. Refresh the page to log in."}), 401
        return redirect(url_for("login", next=request.full_path if request.query_string else request.path))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        addr = request.remote_addr or "?"
        if request.method == "POST":
            now = time.time()
            recent = _recent_failures(addr, now)
            if len(recent) >= MAX_FAILURES:
                wait = int(LOCKOUT_SECONDS - (now - recent[0])) // 60 + 1
                error = f"Too many wrong attempts. Try again in {wait} min."
                return render_template("login.html", error=error), 429
            given = request.form.get("password", "")
            if hmac.compare_digest(given.encode(), password.encode()):
                session.clear()
                session["auth"] = True
                session.permanent = True
                target = request.args.get("next", "")
                # Only redirect within this app.
                if not target.startswith("/") or target.startswith("//"):
                    target = url_for("search")
                return redirect(target)
            _record_failure(addr, now)
            print(f"[WARN] Failed login from {addr}")
            time.sleep(FAILURE_DELAY)
            error = "Wrong password."
        return render_template("login.html", error=error), (401 if error else 200)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))
