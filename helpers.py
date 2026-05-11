from functools import wraps
from flask import session, redirect, url_for, flash, current_app, request, jsonify
from db import get_db_connection
from datetime import datetime, timezone, timedelta
import json
import uuid
import logging
import pytz

# ========================================================
# DECORADORES DE PROTECCIÓN
# ========================================================

# ========================================================
# DECORADORES DE PROTECCIÓN (Sincronización en tiempo real)
# ========================================================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Por favor inicia sesión.', 'warning')
            return redirect(url_for('auth.login'))
        
        # -----------------------------------------------------------
        # AUTO-SYNC: Sincronizamos la suscripción en CADA petición
        # -----------------------------------------------------------
        uid = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT subscription_end, role, estado_suscripcion FROM usuarios WHERE id = %s', (uid,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if not user:
            session.clear()
            return redirect(url_for('auth.login'))

        # Regla Admin: Si es Admin en DB, es PRO en sesión
        if user['role'] >= 1:
            session['is_pro_active'] = True
            session.pop('grace_period', None)
            return f(*args, **kwargs)

        # --- LÓGICA DE DÍAS CALENDARIO ---
        f_end = user['subscription_end']
        estado = (user.get('estado_suscripcion') or '').strip().lower()
        
        tz_mx = pytz.timezone('America/Mexico_City')
        hoy_mx = datetime.now(tz_mx).date()

        # Reset por defecto
        session['is_pro_active'] = False
        session.pop('grace_period', None)

        if f_end and estado not in ['expirada', 'vencida']:
            # Normalizamos el fin de suscripción a solo FECHA
            if isinstance(f_end, str):
                fecha_vence_mx = datetime.strptime(f_end[:10], '%Y-%m-%d').date()
            else:
                fecha_vence_mx = f_end.date() if hasattr(f_end, 'date') else f_end

            # REGLA DE SEBASTIÁN: Todo el día siguiente es de gracia
            dia_gracia = fecha_vence_mx + timedelta(days=1)

            if hoy_mx <= fecha_vence_mx:
                session['is_pro_active'] = True
            elif hoy_mx == dia_gracia:
                session['is_pro_active'] = True
                session['grace_period'] = True
        
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or session.get('role', 0) < 1:
            flash('Acceso denegado.', 'danger')
            return redirect(url_for('main.cotizador'))
        return f(*args, **kwargs)
    return decorated_function

def subscription_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Como login_required ya sincronizó la sesión, aquí solo decidimos
        if not session.get('user_id'):
            return redirect(url_for('auth.login'))
            
        if session.get('is_pro_active'):
            return f(*args, **kwargs)
        
        # Si llegamos aquí es que no es PRO activo
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
            return jsonify({'success': False, 'error': 'Plan vencido', 'code': 'SUBSCRIPTION_REQUIRED'}), 403
        
        return redirect(url_for('main.plan_vencido'))
    return decorated_function

# ========================================================
# SISTEMA DE ALERTAS (Para el menú y notificaciones)
# ========================================================
def obtener_alertas(user_id):
    """
    Sistema Híbrido:
    1. Alertas automáticas (Stock y Suscripción).
    2. Alertas manuales directas (buzón individual).
    3. Anuncios Globales (permanentes para todos).
    """
    alertas = []
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # --- 1. REVISAR STOCK ---
        cursor.execute("SELECT inventario_activo FROM configuracion WHERE user_id=%s", (user_id,))
        config = cursor.fetchone()
        
        if config and config['inventario_activo']:
            cursor.execute("""
                SELECT nombre, stock_actual, stock_minimo 
                FROM materiales 
                WHERE user_id=%s AND stock_actual <= stock_minimo
            """, (user_id,))
            bajos = cursor.fetchall()
            
            for mat in bajos:
                color = 'danger' if mat['stock_actual'] <= 0 else 'warning'
                alertas.append({
                    'tipo': color,
                    'icono': 'box-open',
                    'msg': f"Stock bajo: <b>{mat['nombre']}</b> ({float(mat['stock_actual']):g} restantes)",
                    'url': '/materiales' 
                })

        # --- 2. REVISAR SUSCRIPCIÓN (Solo Rol 0) ---
        cursor.execute("SELECT subscription_end, role FROM usuarios WHERE id=%s", (user_id,))
        user = cursor.fetchone()
        
        if user and user['role'] == 0 and user['subscription_end']:
            try:
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
                current_app.logger.warning(f"ALERT_DATE_ERROR: {e}")

        # --- 3. NOTIFICACIONES MANUALES (BD) ---
        cursor.execute("""
            SELECT id, titulo, mensaje, tipo, url
            FROM notificaciones_manuales 
            WHERE user_id = %s AND leida = FALSE
            ORDER BY fecha_creacion DESC
        """, (user_id,))
        
        manuales = cursor.fetchall()
        for n in manuales:
            url_destino = n['url'] if n['url'] else '#'
            
            if not n['url']:
                clase_icono = 'fas fa-bell'
            elif 'tiktok.com' in url_destino:
                clase_icono = 'fab fa-tiktok'
            elif 'instagram.com' in url_destino:
                clase_icono = 'fab fa-instagram'
            elif 'facebook.com' in url_destino:
                clase_icono = 'fab fa-facebook'
            elif 'youtube.com' in url_destino or 'youtu.be' in url_destino:
                clase_icono = 'fab fa-youtube'
            elif 'wa.me' in url_destino or 'whatsapp.com' in url_destino:
                clase_icono = 'fab fa-whatsapp'
            else:
                clase_icono = 'fas fa-external-link-alt'
            
            alertas.append({
                'id_db': n['id'],
                'es_global': False,  # <-- IMPORTANTE para el layout
                'tipo': n['tipo'],
                'icono_completo': clase_icono, 
                'msg': f"<b>{n['titulo']}</b>: {n['mensaje']}",
                'url': url_destino
            })

        # --- 4. ANUNCIOS GLOBALES (PERMANENTES) ---
        cursor.execute("""
            SELECT g.id, g.titulo, g.mensaje, g.tipo, g.url
            FROM anuncios_globales g
            LEFT JOIN anuncios_vistos v ON g.id = v.anuncio_id AND v.user_id = %s
            WHERE g.activo = TRUE AND v.anuncio_id IS NULL
            ORDER BY g.fecha_creacion DESC
        """, (user_id,))
        
        globales = cursor.fetchall()
        for g in globales:
            url_destino = g['url'] if g['url'] else '#'
            
            # Inteligencia de Iconos
            if not g['url']:
                clase_icono = 'fas fa-bullhorn' # Megáfono para anuncios globales
            elif 'tiktok.com' in url_destino:
                clase_icono = 'fab fa-tiktok'
            elif 'instagram.com' in url_destino:
                clase_icono = 'fab fa-instagram'
            elif 'facebook.com' in url_destino:
                clase_icono = 'fab fa-facebook'
            elif 'youtube.com' in url_destino or 'youtu.be' in url_destino:
                clase_icono = 'fab fa-youtube'
            elif 'wa.me' in url_destino or 'whatsapp.com' in url_destino:
                clase_icono = 'fab fa-whatsapp'
            else:
                clase_icono = 'fas fa-external-link-alt'

            alertas.append({
                'id_db': g['id'],
                'es_global': True,
                'tipo': g['tipo'],
                'icono_completo': clase_icono,
                'msg': f"<b>{g['titulo']}</b>: {g['mensaje']}",
                'url': url_destino
            })

    except Exception as e:
        current_app.logger.error(f"ALERT_FETCH_ERROR: Fallo general para user {user_id} - {e}")
    finally:
        cursor.close()
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
    cursor = conn.cursor()
    try:
        if pase_id:
            # MODO EDICIÓN
            cursor.execute("""
                UPDATE pases_invitados 
                SET nombre_familia=%s, pases_totales=%s, telefono=%s, mesa=%s, nombres_acompanantes_json=%s
                WHERE id=%s AND invitacion_id=%s
            """, (nombre_familia, pases, telefono, mesa, nombres_json, pase_id, inv_id))
            mensaje = f"Datos de {nombre_familia} actualizados."
        else:
            # MODO CREACIÓN
            codigo_unico = str(uuid.uuid4())[:8].upper()
            cursor.execute("""
                INSERT INTO pases_invitados 
                (invitacion_id, nombre_familia, pases_totales, codigo_qr_unique, telefono, mesa, nombres_acompanantes_json) 
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (inv_id, nombre_familia, pases, codigo_unico, telefono, mesa, nombres_json))
            mensaje = f"Pase para {nombre_familia} generado con éxito."
            
        conn.commit()
        return True, str(mensaje)
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        cursor.close()
        conn.close()

def obtener_estado_mesas(inv_id):
    """
    Calcula la ocupación en tiempo real de las mesas del evento.
    Retorna una lista de diccionarios con: nombre, capacidad, ocupados y disponibles.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT mesas_json FROM invitaciones WHERE id = %s", (inv_id,))
        inv = cursor.fetchone()
        
        # Leemos la configuración de mesas (si está vacío, devuelve lista vacía)
        mesas_config = []
        if inv and inv['mesas_json']:
            try:
                mesas_config = json.loads(inv['mesas_json'])
            except:
                pass
        
        # Contamos cuántos pases totales están asignados a cada mesa
        cursor.execute("""
            SELECT mesa, SUM(pases_totales) as ocupados 
            FROM pases_invitados 
            WHERE invitacion_id = %s AND mesa != '0' AND mesa IS NOT NULL AND mesa != ''
            GROUP BY mesa
        """, (inv_id,))
        ocupacion_db = cursor.fetchall()
        
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
        current_app.logger.error(f"TABLE_CALC_ERROR: Fallo calculando estado de mesas para invitacion {inv_id} - {e}")
        return []
    finally:
        cursor.close()
        conn.close()