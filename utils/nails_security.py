import secrets
import time
from collections import defaultdict, deque
from functools import wraps

from flask import jsonify, redirect, request, session, url_for


NAILS_CSRF_SESSION_KEY = "_nails_csrf_token"
NAILS_CSRF_FIELD_NAME = "_nails_csrf_token"
NAILS_CSRF_HEADER_NAME = "X-Nails-CSRF-Token"

_public_booking_hits = defaultdict(deque)


def get_nails_csrf_token():
    token = session.get(NAILS_CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[NAILS_CSRF_SESSION_KEY] = token
    return token


def validate_nails_csrf():
    expected = session.get(NAILS_CSRF_SESSION_KEY)
    provided = (
        request.form.get(NAILS_CSRF_FIELD_NAME)
        or request.headers.get(NAILS_CSRF_HEADER_NAME)
    )
    return bool(expected and provided and secrets.compare_digest(expected, provided))


def nails_csrf_context():
    token = get_nails_csrf_token()
    return {
        "nails_csrf_token": token,
        "nails_csrf_field": NAILS_CSRF_FIELD_NAME,
        "nails_csrf_header": NAILS_CSRF_HEADER_NAME,
        "nails_csrf_input": (
            f'<input type="hidden" name="{NAILS_CSRF_FIELD_NAME}" value="{token}">'
        ),
    }


def nails_csrf_error_response():
    if request.accept_mimetypes.best == "application/json" or request.path.endswith("/upload-r2"):
        return jsonify({"success": False, "error": "Token de seguridad inválido. Recarga la página."}), 400
    return redirect(request.referrer or request.path or url_for("nails.dashboard"))


def require_nails_csrf(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if request.method == "POST" and not validate_nails_csrf():
            return nails_csrf_error_response()
        return view(*args, **kwargs)
    return wrapped


def require_nails_login(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            if request.accept_mimetypes.best == "application/json":
                return jsonify({"success": False, "error": "No autorizado"}), 401
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


def public_booking_rate_limited(key, limit=6, window_seconds=600):
    now = time.time()
    hits = _public_booking_hits[key]
    while hits and now - hits[0] > window_seconds:
        hits.popleft()
    if len(hits) >= limit:
        return True
    hits.append(now)
    return False
