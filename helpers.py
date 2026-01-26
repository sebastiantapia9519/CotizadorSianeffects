import functools
from flask import session, redirect, url_for, flash

def login_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if 'user_id' not in session: return redirect(url_for('auth.login'))
        return view(**kwargs)
    return wrapped_view

def admin_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if 'user_id' not in session or session.get('role', 0) < 1:
            flash('Acceso denegado.', 'danger')
            return redirect(url_for('main.cotizador'))
        return view(**kwargs)
    return wrapped_view