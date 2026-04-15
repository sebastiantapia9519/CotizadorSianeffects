import os
import csv
from io import StringIO
from flask import Response
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, send_from_directory, abort
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta, timezone

# IMPORTACIÓN DE BASE DE DATOS Y SERVICIOS
from db import get_db_connection as get_db
from helpers import admin_required
from utils.datetime_utils import now_utc, utc_to_local 
from services.cloudflare_service import delete_from_cloudflare

admin_bp = Blueprint('admin', __name__)

@admin_bp.route('/')
@admin_required
def dashboard():
    # 1. Datos de la base de datos
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM usuarios ORDER BY created_at DESC')
    users = cursor.fetchall()
    cursor.close()
    conn.close()
    
    # 2. Configuración de tiempos LOCALES
    ahora_utc = now_utc()
    ahora_local = utc_to_local(ahora_utc) 
    
    # Definimos la ventana de 24 horas en tu tiempo local
    hace_24h_local = ahora_local - timedelta(days=1)
    hace_24h_str = hace_24h_local.strftime('%Y-%m-%d %H:%M')
    
    # Umbral de riesgo (350 días atrás)
    proximos_a_borrar_local = ahora_local - timedelta(days=350)
    fecha_limite_riesgo = proximos_a_borrar_local.strftime('%Y-%m-%d')

    # 3. Estadísticas
    stats = {
        'total': len(users),
        'activos': 0,
        'vencidos': 0,
        'admins': 0,
        'online_hoy': 0,
        'en_riesgo': 0
    }

    # 4. Procesamiento
    for u in users:
        if u['role'] > 0:
            stats['admins'] += 1
        
        # Lógica de Suscripción
        if u['subscription_end']:
            try:
                f_end = u['subscription_end']
                if isinstance(f_end, str):
                    f_utc = datetime.strptime(f_end[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                else:
                    f_utc = f_end if f_end.tzinfo else f_end.replace(tzinfo=timezone.utc)
                    
                f_local = utc_to_local(f_utc)
                
                if f_local > ahora_local:
                    stats['activos'] += 1
                else:
                    stats['vencidos'] += 1
                    if u['role'] == 0 and f_local < proximos_a_borrar_local:
                        stats['en_riesgo'] += 1
            except Exception as e:
                current_app.logger.error(f"DATE_ERROR: Parseando subscription_end para user {u['id']} - {e}")
                stats['vencidos'] += 1
        else:
            stats['vencidos'] += 1

        # Lógica de Actividad (Last Login)
        if u['last_login']:
            try:
                l_login = u['last_login']
                if isinstance(l_login, str):
                    log_utc = datetime.strptime(l_login[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                else:
                    log_utc = l_login if l_login.tzinfo else l_login.replace(tzinfo=timezone.utc)
                    
                log_local = utc_to_local(log_utc)
                
                if log_local > hace_24h_local:
                    stats['online_hoy'] += 1
            except Exception as e:
                current_app.logger.warning(f"DATE_WARNING: Parseando last_login para user {u['id']} - {e}")
                pass

    # 5. Envío a la plantilla
    return render_template('admin.html', 
                           users=users, 
                           now=ahora_local,
                           stats=stats, 
                           my_role=session.get('role'),
                           hace_24h_str=hace_24h_str,
                           limite_riesgo=fecha_limite_riesgo)

@admin_bp.route('/renovar/<int:user_id>/<int:meses>')
@admin_required
def renovar(user_id, meses):
    if session.get('role') < 1:
        flash('No tienes permisos para renovar membresías.', 'error')
        return redirect(url_for('admin.dashboard'))
    
    nueva_fecha_fin = now_utc() + timedelta(days=meses*30)
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('SELECT username FROM usuarios WHERE id = %s', (user_id,))
    target_user = cursor.fetchone()
    target_name = target_user['username'] if target_user else f"ID {user_id}"
    
    cursor.execute('UPDATE usuarios SET subscription_end = %s WHERE id = %s', (nueva_fecha_fin, user_id))
    
    conn.commit()
    cursor.close()
    conn.close()
    
    current_app.logger.info(f"SUB_RENEWED: '{session.get('username')}' renovó la suscripción de '{target_name}' por {meses} meses.")
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
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('SELECT username FROM usuarios WHERE id = %s', (user_id,))
    target_user = cursor.fetchone()
    target_name = target_user['username'] if target_user else f"ID {user_id}"
    
    cursor.execute('UPDATE usuarios SET role = %s WHERE id = %s', (nuevo_rol, user_id))
    
    conn.commit()
    cursor.close()
    conn.close()
    
    current_app.logger.info(f"ROLE_CHANGED: '{session.get('username')}' cambió el rol de '{target_name}' a {nuevo_rol}.")
    return redirect(url_for('admin.dashboard'))

@admin_bp.route('/reset_password', methods=['POST'])
@admin_required
def reset_password():
    MIN_ROLE_LEVEL = 1
    current_role = session.get('role', 0)
    current_uid = session.get('user_id')

    if current_role < MIN_ROLE_LEVEL:
        current_app.logger.warning(f"AUTH_FAILURE: User {current_uid} attempted password reset without sufficient permissions.")
        flash('No tienes permisos suficientes para realizar esta acción.', 'error')
        return redirect(url_for('admin.dashboard'))
    
    user_id = request.form.get('user_id')
    new_pass = request.form.get('new_password', '').strip()

    current_app.logger.info(f"DEBUG: Form data received -> ID: {user_id}, Pass_Len: {len(new_pass)}")

    if not user_id or not new_pass:
        current_app.logger.warning("DEBUG: Cancelled. ID or Password missing.")
        return redirect(url_for('admin.dashboard'))

    conn = get_db()
    cursor = conn.cursor()
    try:
        hashed_password = generate_password_hash(new_pass, method='pbkdf2:sha256')
        current_app.logger.info(f"DEBUG: Generated Hash Prefix: {hashed_password[:10]}")

        cursor.execute('UPDATE usuarios SET password = %s WHERE id = %s', (hashed_password, user_id))
        conn.commit()
        
        if cursor.rowcount > 0:
            cursor.execute('SELECT username FROM usuarios WHERE id = %s', (user_id,))
            target = cursor.fetchone()
            target_name = target['username'] if target else f"ID {user_id}"
            
            current_app.logger.info(f"PASSWORD_RESET: '{session.get('username')}' reseteó la contraseña de '{target_name}'.")
            flash('Contraseña actualizada correctamente.', 'success')
        else:
            current_app.logger.warning(f"DEBUG: FAIL. No user found with UID {user_id}")
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
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM usuarios WHERE id=%s", (user_id,))
    user = cursor.fetchone()
    cursor.close()
    conn.close()
    
    if user:
        session['original_admin_id'] = session['user_id']
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['role'] = user['role']
        
        current_app.logger.info(f"IMPERSONATE_START: Admin UID {session['original_admin_id']} entró a la cuenta del UID {user['id']}.")
        flash(f'Modo Fantasma: Ahora estás viendo el sistema como {user["username"]}', 'info')
        return redirect(url_for('main.index'))
    
    return redirect(url_for('admin.dashboard'))

@admin_bp.route('/delete_user', methods=['POST'])
@admin_required
def delete_user():
    user_id = request.form.get('user_id')
    
    if not user_id or str(user_id) == str(session['user_id']):
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
        # =========================================================
        #  LIMPIEZA DEL LOGO EN CLOUDFLARE R2 
        # =========================================================
        cursor.execute("SELECT logo_empresa FROM configuracion WHERE user_id = %s", (user_id,))
        config_user = cursor.fetchone()
        
        if config_user and config_user['logo_empresa']:
            logo_url = config_user['logo_empresa']
            if "http" in logo_url:
                try:
                    delete_from_cloudflare(logo_url)
                    current_app.logger.info(f"R2_CLEANUP_SUCCESS: Se eliminó el logo del usuario {user_id} de R2.")
                except Exception as e:
                    current_app.logger.warning(f"R2_CLEANUP_WARNING: No se pudo borrar el logo del usuario {user_id} - {e}")

        # Borrar Configuración
        cursor.execute("DELETE FROM configuracion WHERE user_id = %s", (user_id,))
        
        # Borrar Detalles de Ventas y Ventas
        cursor.execute("DELETE FROM venta_detalles WHERE venta_id IN (SELECT id FROM ventas WHERE user_id = %s)", (user_id,))
        cursor.execute("DELETE FROM ventas WHERE user_id = %s", (user_id,))
        
        # Borrar Inventario y Materiales
        cursor.execute("DELETE FROM movimientos_inventario WHERE user_id = %s", (user_id,))
        cursor.execute("DELETE FROM materiales WHERE user_id = %s", (user_id,))
        
        # Borrar Maquinaria y Productos
        cursor.execute("DELETE FROM producto_detalles WHERE producto_id IN (SELECT id FROM productos WHERE user_id = %s)", (user_id,))
        cursor.execute("DELETE FROM producto_maquinaria WHERE producto_id IN (SELECT id FROM productos WHERE user_id = %s)", (user_id,))
        cursor.execute("DELETE FROM maquinaria WHERE user_id = %s", (user_id,))
        cursor.execute("DELETE FROM productos WHERE user_id = %s", (user_id,))
        
        # Borrar Logs de Envíos
        cursor.execute("DELETE FROM shipping_rates WHERE zone_id IN (SELECT id FROM shipping_zones WHERE user_id = %s)", (user_id,))
        cursor.execute("DELETE FROM shipping_zones WHERE user_id = %s", (user_id,))
        cursor.execute("DELETE FROM shipping_configs WHERE user_id = %s", (user_id,))

        # Finalmente borrar al usuario
        cursor.execute("DELETE FROM usuarios WHERE id = %s", (user_id,))
        
        conn.commit()
        
        current_app.logger.info(f"USER_DELETED: '{session.get('username')}' borró permanentemente la cuenta ID {user_id} y todos sus datos.")
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

        current_app.logger.info(f"IMPERSONATE_STOP: Admin UID {original_id} salió del modo fantasma.")
        flash('Modo Fantasma finalizado. Bienvenido de vuelta, Jefe.', 'success')
        
        return redirect(url_for('admin.dashboard'))
    
    session.clear()
    return redirect(url_for('auth.login'))

@admin_bp.route('/exportar-usuarios')
@admin_required
def exportar_usuarios():
    if session.get('role') < 1:
        abort(403)

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

    output = si.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment;filename=reporte_usuarios_sianeffects.csv",
            "Content-type": "text/csv; charset=utf-8"
        }
    )