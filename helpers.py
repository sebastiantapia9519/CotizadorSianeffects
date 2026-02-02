from functools import wraps
from flask import session, redirect, url_for, flash
from db import get_db_connection
from datetime import datetime, timezone

# Función para usuarios normales
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Por favor inicia sesión.', 'warning')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # CAMBIO: Ahora permitimos acceso si es rol 1 (Admin) o 2 (Dueño)
        # para que puedan VER el dashboard.
        if 'user_id' not in session or session.get('role', 0) < 1:
            flash('Acceso denegado.', 'danger')
            return redirect(url_for('main.cotizador'))
        return f(*args, **kwargs)
    return decorated_function

def subscription_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # 1. Primero verificamos si está logueado
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        
        # 2. Consultamos la base de datos para ver su fecha y rol
        conn = get_db_connection()
        user = conn.execute('SELECT subscription_end, role FROM usuarios WHERE id = ?', (session['user_id'],)).fetchone()
        conn.close()

        if not user:
            session.clear()
            return redirect(url_for('auth.login'))

        # 3. Regla de Oro: Si es Admin (1) o Dueño (2), PATA LIBRE (no expira)
        if user['role'] >= 1:
            return f(*args, **kwargs)

        # 4. Verificamos la fecha
        # Si subscription_end es NULL o la fecha ya pasó...
        if not user['subscription_end']:
            flash('Tu periodo de prueba ha terminado. Por favor suscríbete.', 'warning')
            return redirect(url_for('main.plan_vencido')) # <--- Redirigimos a una pagina de aviso
            
        fecha_fin = datetime.strptime(str(user['subscription_end'])[:19],'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)        
        
        if datetime.now(timezone.utc) > fecha_fin:
            flash('Tu suscripción ha vencido. Renueva para continuar.', 'error')
            return redirect(url_for('main.plan_vencido')) # <--- Redirigimos a una pagina de aviso

        return f(*args, **kwargs)
    return decorated_function