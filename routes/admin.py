import os
import csv
import math
from io import StringIO
from flask import Response
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, send_from_directory, abort, jsonify
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta, timezone

# IMPORTACIÓN DE BASE DE DATOS Y SERVICIOS
from db import get_db_connection as get_db
from helpers import admin_required
from utils.datetime_utils import now_utc, utc_to_local 
from services.cloudflare_service import delete_from_cloudflare

admin_bp = Blueprint('admin', __name__)

# --- FUNCIÓN AUXILIAR PARA TIEMPO HUMANO ---
def time_ago(dt):
    """Convierte un objeto datetime en una cadena de 'Hace X tiempo'"""
    if not dt:
        return "Nunca"
    
    # Aseguramos que ambas fechas sean 'offset-aware' (con timezone)
    now = now_utc()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
        
    diff = now - dt

    if diff.days > 365:
        return f"Hace {diff.days // 365} años"
    if diff.days > 30:
        return f"Hace {diff.days // 30} meses"
    if diff.days > 0:
        return f"Hace {diff.days} días"
    if diff.seconds > 3600:
        return f"Hace {diff.seconds // 3600} horas"
    if diff.seconds > 60:
        return f"Hace {diff.seconds // 60} minutos"
    return "Hace unos segundos"
# -------------------------------------------

@admin_bp.route('/')
@admin_required
def dashboard():
    search_query = request.args.get('q', '').strip().lower()
    page = request.args.get('page', 1, type=int)
    per_page = 20

    conn = get_db()
    cursor = conn.cursor()
    
    # 1. CONTAR TOTALES (Ultrarrápido en BD)
    if search_query:
        cursor.execute('SELECT COUNT(*) as total FROM usuarios WHERE LOWER(username) LIKE %s OR LOWER(email) LIKE %s', (f'%{search_query}%', f'%{search_query}%'))
    else:
        cursor.execute('SELECT COUNT(*) as total FROM usuarios')
    total_users = cursor.fetchone()['total']

    total_pages = math.ceil(total_users / per_page) if total_users > 0 else 1
    offset = (page - 1) * per_page

    # 2. TRAER SOLO LOS 20 USUARIOS (LIMIT y OFFSET directo en SQL)
    if search_query:
        cursor.execute('''
            SELECT u.*, (SELECT COUNT(*) FROM ventas v WHERE v.user_id = u.id) as total_cotizaciones
            FROM usuarios u 
            WHERE LOWER(u.username) LIKE %s OR LOWER(u.email) LIKE %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        ''', (f'%{search_query}%', f'%{search_query}%', per_page, offset))
    else:
        cursor.execute('''
            SELECT u.*, (SELECT COUNT(*) FROM ventas v WHERE v.user_id = u.id) as total_cotizaciones
            FROM usuarios u 
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        ''', (per_page, offset))
        
    users = [dict(row) for row in cursor.fetchall()]

    # 3. STATS GLOBALES (Calculadas en SQL en 1 milisegundo)
    ahora_utc = now_utc()
    cursor.execute('''
        SELECT 
            COUNT(CASE WHEN role > 0 THEN 1 END) as admins,
            COUNT(CASE WHEN subscription_end > %s THEN 1 END) as activos,
            COUNT(CASE WHEN subscription_end <= %s OR subscription_end IS NULL THEN 1 END) as vencidos,
            COUNT(CASE WHEN last_login > %s THEN 1 END) as online_hoy,
            COUNT(CASE WHEN role = 0 AND subscription_end < %s THEN 1 END) as en_riesgo
        FROM usuarios
    ''', (ahora_utc, ahora_utc, ahora_utc - timedelta(days=1), ahora_utc - timedelta(days=350)))
    stats_db = cursor.fetchone()

    stats = {
        'total': total_users,
        'activos': stats_db['activos'] if stats_db else 0,
        'vencidos': stats_db['vencidos'] if stats_db else 0,
        'admins': stats_db['admins'] if stats_db else 0,
        'online_hoy': stats_db['online_hoy'] if stats_db else 0,
        'en_riesgo': stats_db['en_riesgo'] if stats_db else 0
    }
    
    cursor.close()
    conn.close()

    # 4. CONFIGURACIÓN DE TIEMPOS LOCALES
    ahora_local = utc_to_local(ahora_utc) 
    hace_24h_local = ahora_local - timedelta(days=1)
    hace_24h_str = hace_24h_local.strftime('%Y-%m-%d %H:%M')
    fecha_limite_riesgo = (ahora_local - timedelta(days=350)).strftime('%Y-%m-%d')

    # 5. PROCESAMIENTO (AHORA PYTHON SOLO ITERA 20 VECES, NO 5,000)
    for u in users:
        if u['subscription_end']:
            try:
                f_end = u['subscription_end']
                if isinstance(f_end, str):
                    f_utc = datetime.strptime(f_end[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                else:
                    f_utc = f_end if f_end.tzinfo else f_end.replace(tzinfo=timezone.utc)
                u['subscription_end'] = utc_to_local(f_utc)
            except: pass

        if u['last_login']:
            try:
                l_login = u['last_login']
                if isinstance(l_login, str):
                    log_utc = datetime.strptime(l_login[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                else:
                    log_utc = l_login if l_login.tzinfo else l_login.replace(tzinfo=timezone.utc)
                u['last_login'] = utc_to_local(log_utc)
                u['tiempo_humano'] = time_ago(log_utc)
            except:
                u['tiempo_humano'] = "Error"
        else:
            u['tiempo_humano'] = "Nunca"

    # 6. Envío a la plantilla
    return render_template('admin.html', 
                           users=users, # Ya vienen solo 20 desde la BD
                           search_query=search_query, 
                           now=ahora_local,
                           stats=stats, 
                           my_role=session.get('role'),
                           hace_24h_str=hace_24h_str,
                           limite_riesgo=fecha_limite_riesgo,
                           page=page,
                           total_pages=total_pages,
                           per_page=per_page,
                           total_users=total_users)

@admin_bp.route('/renovar/<int:user_id>/<int:meses>')
@admin_required
def renovar(user_id, meses):
    if session.get('role') < 1:
        flash('No tienes permisos para renovar membresías.', 'error')
        return redirect(url_for('admin.dashboard'))
    
    admin_name = session.get('username', 'Admin_Desconocido')
    admin_id = session.get('user_id', 'N/A')
    
    nueva_fecha_fin = now_utc() + timedelta(days=meses*30)
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('SELECT username FROM usuarios WHERE id = %s', (user_id,))
    target_user = cursor.fetchone()
    target_name = target_user['username'] if target_user else f"ID {user_id}"
    
    # También reseteamos el estado a 'Activo' si estaban vencidos
    cursor.execute('UPDATE usuarios SET subscription_end = %s, estado_suscripcion = %s WHERE id = %s', (nueva_fecha_fin, 'Activo', user_id))
    
    conn.commit()
    cursor.close()
    conn.close()
    
    current_app.logger.info(f"SUB_RENEWED: Admin '{admin_name}' (ID: {admin_id}) renovo la suscripcion de '{target_name}' por {meses} meses.")
    flash(f'Suscripción renovada por {meses} meses.', 'success')
    return redirect(url_for('admin.dashboard'))

@admin_bp.route('/cambiar_rol/<int:user_id>/<int:nuevo_rol>')
@admin_required
def cambiar_rol(user_id, nuevo_rol):
    if session.get('role') < 1:
        flash('No tienes permisos para cambiar roles.', 'error')
        return redirect(url_for('admin.dashboard'))
    
    if session.get('role') == 1 and nuevo_rol > 1:
        flash('No puedes asignar un rango superior al tuyo.', 'warning')
        return redirect(url_for('admin.dashboard'))
    
    admin_name = session.get('username', 'Admin_Desconocido')
    admin_id = session.get('user_id', 'N/A')
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('SELECT username FROM usuarios WHERE id = %s', (user_id,))
    target_user = cursor.fetchone()
    target_name = target_user['username'] if target_user else f"ID {user_id}"
    
    cursor.execute('UPDATE usuarios SET role = %s WHERE id = %s', (nuevo_rol, user_id))
    
    conn.commit()
    cursor.close()
    conn.close()
    
    current_app.logger.info(f"ROLE_CHANGED: Admin '{admin_name}' (ID: {admin_id}) cambio el rol de '{target_name}' a {nuevo_rol}.")
    return redirect(url_for('admin.dashboard'))

@admin_bp.route('/reset_password', methods=['POST'])
@admin_required
def reset_password():
    MIN_ROLE_LEVEL = 1
    current_role = session.get('role', 0)
    current_uid = session.get('user_id')
    admin_name = session.get('username', 'Admin_Desconocido')

    if current_role < MIN_ROLE_LEVEL:
        current_app.logger.warning(f"AUTH_FAILURE: User {current_uid} attempted password reset without sufficient permissions.")
        flash('No tienes permisos suficientes para realizar esta acción.', 'error')
        return redirect(url_for('admin.dashboard'))
    
    user_id = request.form.get('user_id')
    new_pass = request.form.get('new_password', '').strip()

    if not user_id or not new_pass:
        current_app.logger.warning("DEBUG: Cancelled. ID or Password missing.")
        return redirect(url_for('admin.dashboard'))

    conn = get_db()
    cursor = conn.cursor()
    try:
        hashed_password = generate_password_hash(new_pass, method='pbkdf2:sha256')
        
        cursor.execute('UPDATE usuarios SET password = %s WHERE id = %s', (hashed_password, user_id))
        conn.commit()
        
        if cursor.rowcount > 0:
            cursor.execute('SELECT username FROM usuarios WHERE id = %s', (user_id,))
            target = cursor.fetchone()
            target_name = target['username'] if target else f"ID {user_id}"
            
            current_app.logger.info(f"PASSWORD_RESET: Admin '{admin_name}' (ID: {current_uid}) reseteo la contrasena de '{target_name}'.")
            flash('Contraseña actualizada correctamente.', 'success')
        else:
            flash('Error: Usuario no encontrado en la base de datos.', 'error')

    except Exception as e:
        current_app.logger.error(f"DEBUG: DATABASE ERROR -> {str(e)}")
        flash('Error en la base de datos.', 'error')
    finally:
        cursor.close()
        conn.close()
    
    return redirect(url_for('admin.dashboard'))

@admin_bp.route('/impersonate/<int:user_id>')
@admin_required 
def impersonate(user_id):
    admin_id = session.get('user_id')
    admin_name = session.get('username', 'Admin_Desconocido')
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM usuarios WHERE id=%s", (user_id,))
    user = cursor.fetchone()
    cursor.close()
    conn.close()
    
    if user:
        session['original_admin_id'] = admin_id
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['role'] = user['role']
        
        current_app.logger.info(f"IMPERSONATE_START: Admin '{admin_name}' (ID: {admin_id}) entro a la cuenta del UID {user['id']}.")
        flash(f'Modo Fantasma: Ahora estás viendo el sistema como {user["username"]}', 'info')
        return redirect(url_for('main.index'))
    
    return redirect(url_for('admin.dashboard'))

@admin_bp.route('/delete_user', methods=['POST'])
@admin_required
def delete_user():
    user_id = request.form.get('user_id')
    admin_id = session.get('user_id')
    admin_name = session.get('username', 'Admin_Desconocido')
    
    if not user_id or str(user_id) == str(admin_id):
        flash('No puedes borrarte a ti mismo.', 'danger')
        return redirect(url_for('admin.dashboard')) 
        
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT role FROM usuarios WHERE id = %s", (user_id,))
    target_user = cursor.fetchone()
    
    if not target_user:
        cursor.close()
        conn.close()
        flash('El usuario no existe.', 'danger')
        return redirect(url_for('admin.dashboard'))
        
    if session.get('role') < 2 and target_user['role'] >= session.get('role'):
        cursor.close()
        conn.close()
        flash('No puedes eliminar a un usuario de tu mismo rango o superior.', 'danger')
        return redirect(url_for('admin.dashboard'))

    try:
        # LOGICA DE BORRADO...
        cursor.execute("SELECT logo_empresa FROM configuracion WHERE user_id = %s", (user_id,))
        config_user = cursor.fetchone()
        
        if config_user and config_user['logo_empresa']:
            logo_url = config_user['logo_empresa']
            if "http" in logo_url:
                try:
                    delete_from_cloudflare(logo_url)
                    current_app.logger.info(f"R2_CLEANUP_SUCCESS: Se elimino el logo del usuario {user_id} de R2.")
                except Exception as e:
                    current_app.logger.warning(f"R2_CLEANUP_WARNING: No se pudo borrar el logo del usuario {user_id} - {e}")

        cursor.execute("DELETE FROM configuracion WHERE user_id = %s", (user_id,))
        cursor.execute("DELETE FROM venta_detalles WHERE venta_id IN (SELECT id FROM ventas WHERE user_id = %s)", (user_id,))
        cursor.execute("DELETE FROM ventas WHERE user_id = %s", (user_id,))
        cursor.execute("DELETE FROM tutoriales_estado WHERE user_id = %s", (user_id,))
        cursor.execute("DELETE FROM movimientos_inventario WHERE user_id = %s", (user_id,))
        cursor.execute("DELETE FROM materiales WHERE user_id = %s", (user_id,))
        cursor.execute("DELETE FROM producto_detalles WHERE producto_id IN (SELECT id FROM productos WHERE user_id = %s)", (user_id,))
        cursor.execute("DELETE FROM producto_maquinaria WHERE producto_id IN (SELECT id FROM productos WHERE user_id = %s)", (user_id,))
        cursor.execute("DELETE FROM maquinaria WHERE user_id = %s", (user_id,))
        cursor.execute("DELETE FROM productos WHERE user_id = %s", (user_id,))
        cursor.execute("DELETE FROM shipping_rates WHERE zone_id IN (SELECT id FROM shipping_zones WHERE user_id = %s)", (user_id,))
        cursor.execute("DELETE FROM shipping_zones WHERE user_id = %s", (user_id,))
        cursor.execute("DELETE FROM shipping_configs WHERE user_id = %s", (user_id,))
        
        # Eliminar logs de actividad si existe la tabla
        try:
             cursor.execute("DELETE FROM logs_actividad WHERE user_id = %s", (user_id,))
        except Exception:
             pass

        cursor.execute("DELETE FROM usuarios WHERE id = %s", (user_id,))
        
        conn.commit()
        
        current_app.logger.info(f"USER_DELETED: Admin '{admin_name}' (ID: {admin_id}) borro permanentemente la cuenta ID {user_id} y todos sus datos.")
        flash('Usuario y todos sus datos eliminados correctamente.', 'success')
        
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"Error borrando cascada user {user_id}: {e}")
        flash('Error interno al eliminar el usuario. Intenta de nuevo.', 'danger')
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('admin.dashboard'))

@admin_bp.route('/descargar-log')
@admin_required
def descargar_log():
    if session.get('role') < 1:
        abort(403)

    log_path = os.path.join(current_app.root_path, 'limpieza.log')
    
    if os.path.exists(log_path):
        return send_from_directory(
            directory=current_app.root_path, 
            path='limpieza.log', 
            as_attachment=True
        )
    else:
        return "El archivo de historial (limpieza.log) aún no se ha generado.", 404

@admin_bp.route('/monitor')
@admin_required
def monitor():
    log_path = os.path.join(current_app.root_path, 'limpieza.log')
    logs = []
    
    if os.path.exists(log_path):
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            logs = f.readlines()[-100:]
            logs.reverse() 
            
    return render_template('monitor.html', logs=logs)

@admin_bp.route('/stop_impersonate')
def stop_impersonate():
    if 'original_admin_id' not in session:
        return redirect(url_for('main.index'))

    original_id = session['original_admin_id']

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM usuarios WHERE id = %s', (original_id,))
    admin_user = cursor.fetchone()
    cursor.close()
    conn.close()

    if admin_user:
        session['user_id'] = admin_user['id']
        session['username'] = admin_user['username']
        session['role'] = admin_user['role']
        session.pop('original_admin_id', None)

        current_app.logger.info(f"IMPERSONATE_STOP: Admin '{admin_user['username']}' (ID: {original_id}) salio del modo fantasma.")
        flash('Modo Fantasma finalizado. Bienvenido de vuelta, Jefe.', 'success')
        
        return redirect(url_for('admin.dashboard'))
    
    session.clear()
    return redirect(url_for('auth.login'))

@admin_bp.route('/exportar-usuarios')
@admin_required
def exportar_usuarios():
    if session.get('role') < 1:
        abort(403)

    admin_name = session.get('username', 'Admin_Desconocido')
    admin_id = session.get('user_id', 'N/A')

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 
            id, username, email, company_name, 
            role, created_at, subscription_end, last_login 
        FROM usuarios 
        ORDER BY created_at DESC
    """)
    usuarios = cursor.fetchall()
    cursor.close()
    conn.close()

    si = StringIO()
    si.write('\ufeff') 
    
    cw = csv.writer(si)
    
    cw.writerow([
        'ID', 
        'Usuario', 
        'Email', 
        'Empresa', 
        'Rol (0=Usuario, 1=Admin, 2=Dueño)', 
        'Fecha de Registro', 
        'Vencimiento Suscripción', 
        'Última Vez Visto'
    ])
    
    for u in usuarios:
        cw.writerow([
            u['id'],
            u['username'],
            u['email'],
            u['company_name'],
            u['role'],
            u['created_at'],
            u['subscription_end'] if u['subscription_end'] else 'Sin fecha',
            u['last_login'] if u['last_login'] else 'Nunca'
        ])

    current_app.logger.info(f"EXPORT_DATA: Admin '{admin_name}' (ID: {admin_id}) exporto la lista completa de usuarios.")

    output = si.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment;filename=reporte_usuarios_sianeffects.csv",
            "Content-type": "text/csv; charset=utf-8"
        }
    )

@admin_bp.route('/sumar-tiempo', methods=['POST'])
@admin_required
def sumar_tiempo():
    # Verifica que al menos sea Rol 1 (Diana)
    if session.get('role') < 1:
        flash('No tienes permisos para modificar suscripciones.', 'error')
        return redirect(url_for('admin.dashboard'))

    user_id = request.form.get('user_id')
    
    try:
        dias_a_sumar = int(request.form.get('cantidad_dias', 0))
    except ValueError:
        flash('La cantidad de días debe ser un número entero.', 'error')
        return redirect(url_for('admin.dashboard'))
        
    if dias_a_sumar <= 0:
        flash('Debes sumar al menos 1 día.', 'warning')
        return redirect(url_for('admin.dashboard'))

    admin_name = session.get('username', 'Admin_Desconocido')
    admin_id = session.get('user_id', 'N/A')

    conn = get_db()
    cursor = conn.cursor()
    
    try:
        # Obtenemos la info actual del usuario
        cursor.execute("SELECT username, subscription_end FROM usuarios WHERE id = %s", (user_id,))
        target_user = cursor.fetchone()
        
        if not target_user:
            flash('Usuario no encontrado.', 'error')
            return redirect(url_for('admin.dashboard'))
            
        target_name = target_user['username']
        current_sub_end = target_user['subscription_end']
        
        ahora = now_utc()
        
        # Le agregamos la zona horaria a la fecha de la base de datos para que Python no truene
        if current_sub_end and current_sub_end.tzinfo is None:
            current_sub_end = current_sub_end.replace(tzinfo=timezone.utc)
        
        # Si la suscripción ya estaba vencida, empezamos a contar desde hoy
        if not current_sub_end or current_sub_end < ahora:
            nueva_fecha_fin = ahora + timedelta(days=dias_a_sumar)
        else:
            # Si sigue activa, le sumamos los días a la fecha que ya tenía
            nueva_fecha_fin = current_sub_end + timedelta(days=dias_a_sumar)
            
        # Actualizamos la base de datos, asegurando que vuelva a estar 'Activo'
        cursor.execute('''
            UPDATE usuarios 
            SET subscription_end = %s, 
                estado_suscripcion = %s,
                dias_regalados = COALESCE(dias_regalados, 0) + %s
            WHERE id = %s
        ''', (nueva_fecha_fin, 'Activo', dias_a_sumar, user_id))
        
        conn.commit()
        
        current_app.logger.info(f"TIME_ADDED: Admin '{admin_name}' (ID: {admin_id}) sumo {dias_a_sumar} dias a '{target_name}'.")
        flash(f'Se añadieron {dias_a_sumar} días correctamente a {target_name}.', 'success')
        
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"Error sumando días a user {user_id}: {e}")
        flash('Error interno al actualizar el tiempo.', 'danger')
    finally:
        cursor.close()
        conn.close()
        
    return redirect(url_for('admin.dashboard'))

@admin_bp.route('/api/log/<int:user_id>')
@admin_required
def api_ver_log(user_id):
    """Devuelve los últimos 20 logs de actividad de un usuario en formato JSON para el Modal Front-End."""
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT accion, modulo, fecha 
            FROM logs_actividad 
            WHERE user_id = %s 
            ORDER BY fecha DESC 
            LIMIT 20
        """, (user_id,))
        logs_db = cursor.fetchall()
        
        lista_logs = []
        for row in logs_db:
            # Aprovechamos tu función time_ago() para mandar "Hace 2 horas"
            tiempo_relativo = time_ago(row['fecha']) 
            
            lista_logs.append({
                "accion": row['accion'],
                "modulo": row['modulo'] if row['modulo'] else 'Sistema',
                "hace_tiempo": tiempo_relativo,
                "fecha_texto": row['fecha'].strftime('%d/%m/%Y %H:%M') if row['fecha'] else ''
            })
            
        return jsonify({"success": True, "logs": lista_logs})
        
    except Exception as e:
        current_app.logger.error(f"Error cargando logs JSON para {user_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
        
    finally:
        cursor.close()
        conn.close()