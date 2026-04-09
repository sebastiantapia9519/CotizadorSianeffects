from functools import wraps
from flask import session, redirect, url_for, flash
from db import get_db_connection
from datetime import datetime, timezone, timedelta
import json
import uuid
import logging

# ========================================================
# DECORADORES DE PROTECCIÓN
# ========================================================

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
        # Protege rutas sensibles (como borrar usuarios o ver métricas globales)
        # Permite acceso si es Admin (1) o Dueño (2)
        if 'user_id' not in session or session.get('role', 0) < 1:
            flash('Acceso denegado. Se requieren permisos de Administrador.', 'danger')
            return redirect(url_for('main.cotizador'))
        return f(*args, **kwargs)
    return decorated_function

def subscription_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # 1. Verificar sesión básica
        if 'user_id' not in session:
            # Quitamos el session.clear() de aquí para no ser tan destructivos 
            # si el navegador móvil tuvo un hipo con la cookie.
            flash('Por favor inicia sesión.', 'warning')
            return redirect(url_for('auth.login'))
        
        # -----------------------------------------------------------
        # BLINDAJE PARA DUEÑOS (NIVEL 2) Y ADMINS (NIVEL 1)
        # -----------------------------------------------------------
        if session.get('role', 0) >= 1:
            return f(*args, **kwargs)
        # -----------------------------------------------------------

        # 2. Si es un usuario mortal (Rol 0), entonces sí revisamos la BD
        conn = get_db_connection()
        user = conn.execute('SELECT subscription_end, role FROM usuarios WHERE id = ?', (session['user_id'],)).fetchone()
        conn.close()

        if not user:
            # Aquí SÍ es válido limpiar la sesión, porque significa que el ID existe en la cookie
            # pero el usuario ya no existe en la Base de Datos (ej. fue borrado por inactividad).
            session.clear()
            flash('Tu cuenta ya no es válida o fue eliminada.', 'error')
            return redirect(url_for('auth.login'))

        # (Doble verificación de seguridad por si la sesión falló pero en BD sí es admin)
        if user['role'] >= 1:
            return f(*args, **kwargs)

        # 3. Verificamos la fecha de suscripción para usuarios normales
        if not user['subscription_end']:
            flash('Tu periodo de prueba ha terminado. Por favor suscríbete.', 'warning')
            return redirect(url_for('main.plan_vencido')) 
            
        try:
            # BLINDAJE SQLITE/POSTGRES
            f_end = user['subscription_end']
            if isinstance(f_end, str):
                fecha_fin = datetime.strptime(f_end[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            else:
                fecha_fin = f_end if f_end.tzinfo else f_end.replace(tzinfo=timezone.utc)
            
            # Comparamos con la hora actual en UTC
            if datetime.now(timezone.utc) > fecha_fin:
                flash('Tu suscripción ha vencido. Renueva para continuar.', 'error')
                return redirect(url_for('main.plan_vencido')) 
        except Exception as e:
            logging.error(f"AUTH_DATE_ERROR: Fallo al verificar fecha de subscripción para user {session.get('user_id')} - {e}")
            # Considerar si quieres bloquear el acceso si hay un error en la fecha
            pass

        return f(*args, **kwargs)
    return decorated_function

# ========================================================
# SISTEMA DE ALERTAS (Para el menú y notificaciones)
# ========================================================
def obtener_alertas(user_id):
    """
    Revisa stock bajo y vencimiento de suscripción.
    """
    alertas = []
    conn = get_db_connection()
    
    try:
        # 1. REVISAR STOCK (Para todos los que tengan inventario activo)
        config = conn.execute("SELECT inventario_activo FROM configuracion WHERE user_id=?", (user_id,)).fetchone()
        
        if config and config['inventario_activo']:
            bajos = conn.execute("""
                SELECT nombre, stock_actual, stock_minimo 
                FROM materiales 
                WHERE user_id=? AND stock_actual <= stock_minimo
            """, (user_id,)).fetchall()
            
            for mat in bajos:
                color = 'danger' if mat['stock_actual'] <= 0 else 'warning'
                alertas.append({
                    'tipo': color,
                    'icono': 'box-open',
                    'msg': f"Stock bajo: <b>{mat['nombre']}</b> ({float(mat['stock_actual']):g} restantes)",
                    'url': '/inventario' 
                })

        # 2. REVISAR SUSCRIPCIÓN (SOLO PARA MORTALES - ROL 0)
        user = conn.execute("SELECT subscription_end, role FROM usuarios WHERE id=?", (user_id,)).fetchone()
        
        # El filtro user['role'] == 0 asegura que a TI no te salgan avisos de pago
        if user and user['role'] == 0 and user['subscription_end']:
            try:
                # BLINDAJE SQLITE/POSTGRES
                f_end = user['subscription_end']
                if isinstance(f_end, str):
                    fecha_fin = datetime.strptime(f_end[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                else:
                    fecha_fin = f_end if f_end.tzinfo else f_end.replace(tzinfo=timezone.utc)
                    
                dias_restantes = (fecha_fin - datetime.now(timezone.utc)).days
                
                if 0 <= dias_restantes <= 5:
                    alertas.append({
                        'tipo': 'danger',
                        'icono': 'clock',
                        'msg': f"Tu plan vence en <b>{dias_restantes} días</b>. Renueva pronto.",
                        'url': '/configuracion'
                    })
            except Exception as e:
                logging.warning(f"ALERT_DATE_ERROR: Fallo calculando alerta de fecha para user {user_id} - {e}")
                pass

    except Exception as e:
        logging.error(f"ALERT_FETCH_ERROR: Fallo general obteniendo alertas para user {user_id} - {e}")
    finally:
        conn.close()
        
    return alertas

# ========================================================
# GESTIÓN UNIFICADA DE INVITADOS (RSVP VIP)
# ========================================================
def guardar_pase_bd(inv_id, form_data, pase_id=None):
    """
    Función maestra para guardar o editar invitados.
    Recibe el ID de la invitación y el diccionario request.form.
    Si se envía un pase_id, hace UPDATE; si no, hace INSERT.
    """
    nombre_familia = form_data.get('nombre_familia')
    
    # BLINDAJE DE TIPOS (A prueba de capa 8 y de Postgres)
    try:
        pases = int(form_data.get('pases_totales', 2))
    except ValueError:
        pases = 2
        
    telefono = form_data.get('telefono', '')
    mesa = form_data.get('mesa') or '0'
    
    # Procesar el arreglo de nombres de acompañantes
    nombres_lista = form_data.getlist('nombres_acompanantes[]')
    # Filtrar campos vacíos y convertir a JSON solo si hay nombres
    nombres_json = json.dumps([n for n in nombres_lista if n.strip()]) if nombres_lista else None

    conn = get_db_connection()
    try:
        if pase_id:
            # MODO EDICIÓN
            conn.execute("""
                UPDATE pases_invitados 
                SET nombre_familia=?, pases_totales=?, telefono=?, mesa=?, nombres_acompanantes_json=?
                WHERE id=? AND invitacion_id=?
            """, (nombre_familia, pases, telefono, mesa, nombres_json, pase_id, inv_id))
            mensaje = f"Datos de {nombre_familia} actualizados."
        else:
            # MODO CREACIÓN
            codigo_unico = str(uuid.uuid4())[:8].upper()
            conn.execute("""
                INSERT INTO pases_invitados 
                (invitacion_id, nombre_familia, pases_totales, codigo_qr_unique, telefono, mesa, nombres_acompanantes_json) 
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (inv_id, nombre_familia, pases, codigo_unico, telefono, mesa, nombres_json))
            mensaje = f"Pase para {nombre_familia} generado con éxito."
            
        conn.commit()
        return True, str(mensaje)
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()

def obtener_estado_mesas(inv_id):
    """
    Calcula la ocupación en tiempo real de las mesas del evento.
    Retorna una lista de diccionarios con: nombre, capacidad, ocupados y disponibles.
    """
    conn = get_db_connection()
    try:
        inv = conn.execute("SELECT mesas_json FROM invitaciones WHERE id = ?", (inv_id,)).fetchone()
        
        # Leemos la configuración de mesas (si está vacío, devuelve lista vacía)
        mesas_config = []
        if inv and inv['mesas_json']:
            try:
                mesas_config = json.loads(inv['mesas_json'])
            except:
                pass
        
        # Contamos cuántos pases totales están asignados a cada mesa
        ocupacion_db = conn.execute("""
            SELECT mesa, SUM(pases_totales) as ocupados 
            FROM pases_invitados 
            WHERE invitacion_id = ? AND mesa != '0' AND mesa IS NOT NULL AND mesa != ''
            GROUP BY mesa
        """, (inv_id,)).fetchall()
        
        # Convertimos a un diccionario fácil de leer: {'1': 8, 'VIP': 10}
        ocupacion = {str(row['mesa']).strip(): row['ocupados'] for row in ocupacion_db}
        
        resultado = []
        for m in mesas_config:
            m_nombre = str(m.get('nombre', '')).strip()
            capacidad = int(m.get('capacidad', 10))
            ocupados = ocupacion.get(m_nombre, 0)
            
            resultado.append({
                'nombre': m_nombre,
                'capacidad': capacidad,
                'ocupados': ocupados,
                'disponibles': capacidad - ocupados
            })
            
        return resultado
    except Exception as e:
        logging.error(f"TABLE_CALC_ERROR: Fallo calculando estado de mesas para invitación {inv_id} - {e}")
        return []
    finally:
        conn.close()