from functools import wraps
from flask import session, redirect, url_for, flash
from db import get_db_connection
from datetime import datetime, timezone, timedelta

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
            return redirect(url_for('main.plan_vencido')) 
            
        fecha_fin = datetime.strptime(str(user['subscription_end'])[:19],'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)        
        
        if datetime.now(timezone.utc) > fecha_fin:
            flash('Tu suscripción ha vencido. Renueva para continuar.', 'error')
            return redirect(url_for('main.plan_vencido')) 

        return f(*args, **kwargs)
    return decorated_function

# ========================================================
# NUEVA FUNCIÓN: SISTEMA CENTRAL DE NOTIFICACIONES (FASE 3)
# ========================================================
def obtener_alertas(user_id):
    """
    Revisa stock bajo y vencimiento de suscripción.
    Devuelve una lista de diccionarios: {'tipo': 'danger/warning', 'msg': '...', 'url': '...'}
    """
    alertas = []
    conn = get_db_connection()
    
    try:
        # 1. REVISAR STOCK (Solo si el inventario está activo)
        config = conn.execute("SELECT inventario_activo FROM configuracion WHERE user_id=?", (user_id,)).fetchone()
        
        if config and config['inventario_activo']:
            # Buscamos materiales donde stock_actual <= stock_minimo
            bajos = conn.execute("""
                SELECT nombre, stock_actual, stock_minimo 
                FROM materiales 
                WHERE user_id=? AND stock_actual <= stock_minimo
            """, (user_id,)).fetchall()
            
            for mat in bajos:
                # Calculamos qué tan grave es (Rojo si es 0, Amarillo si es bajo)
                color = 'danger' if mat['stock_actual'] <= 0 else 'warning'
                alertas.append({
                    'tipo': color,
                    'icono': 'box-open',
                    'msg': f"Stock bajo: <b>{mat['nombre']}</b> ({float(mat['stock_actual']):g} restantes)",
                    'url': '/materiales'
                })

        # 2. REVISAR SUSCRIPCIÓN (Avisar 5 días antes)
        user = conn.execute("SELECT subscription_end, role FROM usuarios WHERE id=?", (user_id,)).fetchone()
        
        # Solo avisamos a usuarios normales (rol 0) que tengan fecha definida
        if user and user['role'] == 0 and user['subscription_end']:
            fecha_fin = datetime.strptime(str(user['subscription_end'])[:19],'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            dias_restantes = (fecha_fin - datetime.now(timezone.utc)).days
            
            if 0 <= dias_restantes <= 5:
                alertas.append({
                    'tipo': 'danger',
                    'icono': 'clock',
                    'msg': f"Tu plan vence en <b>{dias_restantes} días</b>. Renueva pronto.",
                    'url': '/configuracion'
                })

    except Exception as e:
        print(f"Error obteniendo alertas: {e}")
    finally:
        conn.close()
        
    return alertas