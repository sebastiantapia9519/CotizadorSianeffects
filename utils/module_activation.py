from datetime import timedelta

from flask import url_for

from db import get_db_connection as get_db
from utils.auth_utils import generate_verification_code
from utils.datetime_utils import now_utc


ACTIVE_MODULE_STATUSES = {"trial", "active"}
DEFAULT_MODULE_STATUS = "trial"
VALID_MODULE_STATUSES = {"trial", "active", "inactive", "cancelled"}
MODULE_STATUS_MAP = {
    "trial": "trial",
    "activo": "active",
    "active": "active",
    "inactivo": "inactive",
    "inactive": "inactive",
    "cancelado": "cancelled",
    "cancelled": "cancelled",
}


def _normalize_module(module_key):
    module = (module_key or "cotizador").strip().lower()
    if module not in {"cotizador", "nails"}:
        raise ValueError("Modulo no valido.")
    return module


def normalize_module_status(status):
    normalized = MODULE_STATUS_MAP.get((status or DEFAULT_MODULE_STATUS).strip().lower(), DEFAULT_MODULE_STATUS)
    if normalized not in VALID_MODULE_STATUSES:
        return DEFAULT_MODULE_STATUS
    return normalized


def _close_if_owned(conn, cursor, owns_connection):
    if owns_connection:
        cursor.close()
        conn.close()


def _cursor_or_new(cursor=None):
    if cursor is not None:
        return None, cursor, False
    conn = get_db()
    return conn, conn.cursor(), True


def get_user_modules(user_id, cursor=None):
    conn, cur, owns_connection = _cursor_or_new(cursor)
    try:
        cur.execute(
            """
            SELECT user_id, module_key, status, plan_type, trial_start, trial_ends_at,
                   subscription_end, created_at, updated_at
            FROM user_modules
            WHERE user_id = %s
            ORDER BY module_key
            """,
            (user_id,),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        _close_if_owned(conn, cur, owns_connection)


def user_has_module(user_id, module_key, cursor=None, active_only=True):
    module = _normalize_module(module_key)
    conn, cur, owns_connection = _cursor_or_new(cursor)
    try:
        cur.execute(
            """
            SELECT status
            FROM user_modules
            WHERE user_id = %s AND module_key = %s
            LIMIT 1
            """,
            (user_id, module),
        )
        row = cur.fetchone()
        if not row:
            return False
        if not active_only:
            return True
        return (row["status"] or "").strip().lower() in ACTIVE_MODULE_STATUSES
    finally:
        _close_if_owned(conn, cur, owns_connection)


def ensure_user_module(user_id, module_key, status=DEFAULT_MODULE_STATUS, plan_type=None, cursor=None):
    module = _normalize_module(module_key)
    module_status = normalize_module_status(status)
    conn, cur, owns_connection = _cursor_or_new(cursor)
    try:
        cur.execute(
            """
            INSERT INTO user_modules (user_id, module_key, status, plan_type, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id, module_key)
            DO UPDATE SET
                status = EXCLUDED.status,
                plan_type = COALESCE(EXCLUDED.plan_type, user_modules.plan_type),
                updated_at = EXCLUDED.updated_at
            RETURNING user_id, module_key, status, plan_type, trial_start, trial_ends_at,
                      subscription_end, created_at, updated_at
            """,
            (user_id, module, module_status, plan_type, now_utc()),
        )
        row = dict(cur.fetchone())
        if owns_connection:
            conn.commit()
        return row
    except Exception:
        if owns_connection:
            conn.rollback()
        raise
    finally:
        _close_if_owned(conn, cur, owns_connection)


def create_activation_code(user_id, email, purpose, cursor=None):
    conn, cur, owns_connection = _cursor_or_new(cursor)
    code = generate_verification_code()
    expires_at = now_utc() + timedelta(minutes=10)
    try:
        cur.execute(
            """
            INSERT INTO auth_codes (user_id, email, code, expires_at, purpose)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, code
            """,
            (user_id, email, code, expires_at, purpose),
        )
        row = dict(cur.fetchone())
        if owns_connection:
            conn.commit()
        return row
    except Exception:
        if owns_connection:
            conn.rollback()
        raise
    finally:
        _close_if_owned(conn, cur, owns_connection)


def validate_activation_code(user_id, email, code, purpose, cursor=None):
    conn, cur, owns_connection = _cursor_or_new(cursor)
    try:
        cur.execute(
            """
            SELECT id, user_id, email, code, purpose
            FROM auth_codes
            WHERE user_id = %s
              AND email = %s
              AND code = %s
              AND purpose = %s
              AND used = FALSE
              AND expires_at > %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id, email, code, purpose, now_utc()),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        _close_if_owned(conn, cur, owns_connection)


def mark_auth_code_used(code_id, cursor=None):
    conn, cur, owns_connection = _cursor_or_new(cursor)
    try:
        cur.execute("UPDATE auth_codes SET used = TRUE WHERE id = %s", (code_id,))
        if owns_connection:
            conn.commit()
    except Exception:
        if owns_connection:
            conn.rollback()
        raise
    finally:
        _close_if_owned(conn, cur, owns_connection)


def redirect_for_module(module_key):
    module = _normalize_module(module_key)
    if module == "nails":
        return url_for("nails.dashboard")
    return url_for("main.cotizador")
