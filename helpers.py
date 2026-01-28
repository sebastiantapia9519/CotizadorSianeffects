from functools import wraps
from flask import session, redirect, url_for, flash

# Función para usuarios normales
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Por favor inicia sesión.', 'warning')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function

# --- ESTA ES LA QUE FALTABA ---
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Verifica si es admin (rol 2)
        if 'user_id' not in session or session.get('role', 0) < 2:
            flash('Acceso denegado.', 'danger')
            return redirect(url_for('main.cotizador'))
        return f(*args, **kwargs)
    return decorated_function