# =============================================================================
# routes/admin.py — Panel de Administración Sianeffects
# =============================================================================
# Cubre: Dashboard de usuarios, renovación de suscripciones, cambio de roles,
#        reset de contraseñas, modo fantasma (impersonate), exportación CSV,
#        monitor de logs y bitácora de actividad por usuario.
#
# SEGURIDAD APLICADA:
#   - Todas las rutas protegidas con @admin_required
#   - Operaciones destructivas/modificadoras usan POST (no GET) → anti-CSRF
#   - Conexiones siempre cerradas en bloque finally
#   - Logging detallado con username + ID en cada acción administrativa
# =============================================================================

import os
import csv
import math
import uuid
from io import StringIO

from flask import (
    Blueprint, Response, render_template, request,
    redirect, url_for, flash, session, current_app,
    send_from_directory, abort, jsonify
)
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta, timezone

from db import get_db_connection as get_db
from helpers import admin_required
from utils.datetime_utils import now_utc, utc_to_local
from services.cloudflare_service import delete_from_cloudflare

admin_bp = Blueprint('admin', __name__)


# =============================================================================
# UTILIDAD — Tiempo relativo legible
# =============================================================================

def time_ago(dt):
    """
    Convierte un datetime en texto legible: 'Hace 2 horas', 'Hace 3 días', etc.
    Siempre trabaja con datetimes timezone-aware para evitar errores de comparación.
    """
    if not dt:
        return "Nunca"

    now = now_utc()

    # Normalizamos: si viene sin timezone, asumimos UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    diff = now - dt

    if diff.days > 365:   return f"Hace {diff.days // 365} años"
    if diff.days > 30:    return f"Hace {diff.days // 30} meses"
    if diff.days > 0:     return f"Hace {diff.days} días"
    if diff.seconds > 3600: return f"Hace {diff.seconds // 3600} horas"
    if diff.seconds > 60:   return f"Hace {diff.seconds // 60} minutos"
    return "Hace unos segundos"


# =============================================================================
# DASHBOARD PRINCIPAL (ACTUALIZADO CON FILTROS Y ORDENAMIENTO)
# =============================================================================

@admin_bp.route('/')
@admin_required
def dashboard():
    """
    Lista paginada de usuarios con búsqueda, filtros, ordenamiento dinámico y paginación.
    """
    # 1. Capturar parámetros de la URL
    search_query = request.args.get('q', '').strip().lower()
    status_filter = request.args.get('status', 'all')
    sort_by = request.args.get('sort', 'created_at_desc')
    page = request.args.get('page', 1, type=int)
    per_page = 20

    ahora_utc = now_utc()

    conn = get_db()
    cursor = conn.cursor()

    try:
        # 2. CONSTRUCCIÓN DINÁMICA DEL WHERE
        base_where = "WHERE 1=1"
        params = []

        if search_query:
            base_where += " AND (LOWER(u.username) LIKE %s OR LOWER(u.email) LIKE %s)"
            params.extend([f'%{search_query}%', f'%{search_query}%'])

        # Filtros de Estado
        if status_filter == 'expirados':
            base_where += " AND u.plan_type != 'Free' AND u.subscription_end < %s"
            params.append(ahora_utc)
        elif status_filter == 'activos':
            base_where += " AND u.plan_type != 'Free' AND u.subscription_end >= %s"
            params.append(ahora_utc)
        elif status_filter == 'trial':
            base_where += " AND u.estado_suscripcion = 'Trial'"
        elif status_filter == 'free':
            base_where += " AND u.plan_type = 'Free'"

        # 3. CONTEO TOTAL (Para la Paginación)
        # Importante: Usamos 'usuarios u' para que coincida con el alias en base_where
        count_sql = f"SELECT COUNT(*) as total FROM usuarios u {base_where}"
        cursor.execute(count_sql, params)
        total_users = cursor.fetchone()['total']
        total_pages = math.ceil(total_users / per_page) if total_users > 0 else 1
        offset = (page - 1) * per_page

        # 4. CONSTRUCCIÓN DINÁMICA DEL ORDER BY
        if sort_by == 'username_asc':
            order_clause = "ORDER BY u.username ASC"
        elif sort_by == 'username_desc':
            order_clause = "ORDER BY u.username DESC"
        elif sort_by == 'expiracion_asc':
            order_clause = "ORDER BY u.subscription_end ASC NULLS LAST"
        elif sort_by == 'expiracion_desc':
            order_clause = "ORDER BY u.subscription_end DESC NULLS LAST"
        elif sort_by == 'last_login_desc':
            order_clause = "ORDER BY u.last_login DESC NULLS LAST"
        elif sort_by == 'last_login_asc':
            order_clause = "ORDER BY u.last_login ASC NULLS FIRST"
        else:
            order_clause = "ORDER BY u.created_at DESC"

        # 5. OBTENCIÓN DE DATOS PAGINADOS
        data_sql = f"""
            SELECT u.*, (SELECT COUNT(*) FROM ventas v WHERE v.user_id = u.id) as total_cotizaciones
            FROM usuarios u
            {base_where}
            {order_clause}
            LIMIT %s OFFSET %s
        """
        data_params = params.copy()
        data_params.extend([per_page, offset])
        
        cursor.execute(data_sql, data_params)
        users = [dict(row) for row in cursor.fetchall()]

        # 6. STATS GLOBALES (Independientes de los filtros de la tabla)
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
            'total':     total_users if status_filter == 'all' and not search_query else stats_db['activos'] + stats_db['vencidos'], # Aproximación global
            'activos':   stats_db['activos']   if stats_db else 0,
            'vencidos':  stats_db['vencidos']  if stats_db else 0,
            'admins':    stats_db['admins']    if stats_db else 0,
            'online_hoy': stats_db['online_hoy'] if stats_db else 0,
            'en_riesgo': stats_db['en_riesgo'] if stats_db else 0,
        }
        
        # Corrección del total absoluto global
        cursor.execute("SELECT COUNT(*) as v FROM usuarios")
        stats['total'] = cursor.fetchone()['v']

    except Exception as e:
        current_app.logger.error(f"ADMIN_DASHBOARD_ERROR: {e}")
        stats = {'total': 0, 'activos': 0, 'vencidos': 0, 'admins': 0, 'online_hoy': 0, 'en_riesgo': 0}
        users = []
    finally:
        cursor.close()
        conn.close()

    # 7. CONVERSIÓN DE FECHAS A HORA LOCAL (Solo en los 20 resultados)
    ahora_local = utc_to_local(ahora_utc)
    for u in users:
        if u['subscription_end']:
            try:
                f = u['subscription_end']
                if isinstance(f, str):
                    f = datetime.strptime(f[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                elif not f.tzinfo:
                    f = f.replace(tzinfo=timezone.utc)
                u['subscription_end'] = utc_to_local(f)
            except: pass

        if u['last_login']:
            try:
                l = u['last_login']
                if isinstance(l, str):
                    l = datetime.strptime(l[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                elif not l.tzinfo:
                    l = l.replace(tzinfo=timezone.utc)
                u['last_login'] = utc_to_local(l)
                u['tiempo_humano'] = time_ago(l)
            except:
                u['tiempo_humano'] = "Error"
        else:
            u['tiempo_humano'] = "Nunca"

    # 8. ENVIAR TODO AL TEMPLATE
    return render_template('admin.html',
        users=users,
        search_query=search_query,
        now=ahora_local,
        stats=stats,
        my_role=session.get('role'),
        hace_24h_str=(ahora_local - timedelta(days=1)).strftime('%Y-%m-%d %H:%M'),
        limite_riesgo=(ahora_local - timedelta(days=350)).strftime('%Y-%m-%d'),
        page=page,
        total_pages=total_pages,
        per_page=per_page,
        total_users=total_users,
        status_filter=status_filter,  
        sort_by=sort_by               
    )   


# =============================================================================
# RENOVAR SUSCRIPCIÓN (POST — anti-CSRF)
# =============================================================================

@admin_bp.route('/renovar', methods=['POST'])
@admin_required
def renovar():
    """
    Renueva la suscripción de un usuario por N meses.
    Requiere role >= 1. Usa POST para protección CSRF.
    """
    if session.get('role') < 1:
        flash('No tienes permisos para renovar membresías.', 'error')
        return redirect(url_for('admin.dashboard'))

    admin_id   = session.get('user_id')
    admin_name = session.get('username', 'Admin_Desconocido')

    user_id = request.form.get('user_id')
    meses   = int(request.form.get('meses', 1))

    nueva_fecha_fin = now_utc() + timedelta(days=meses * 30)

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT username FROM usuarios WHERE id = %s', (user_id,))
        target = cursor.fetchone()
        target_name = target['username'] if target else f"ID {user_id}"

        cursor.execute(
            'UPDATE usuarios SET subscription_end = %s, estado_suscripcion = %s WHERE id = %s',
            (nueva_fecha_fin, 'Activo', user_id)
        )
        conn.commit()

        current_app.logger.info(
            f"SUB_RENEWED: Admin '{admin_name}' (ID:{admin_id}) renovó a '{target_name}' (ID:{user_id}) por {meses} mes(es)."
        )
        flash(f'Suscripción de {target_name} renovada por {meses} mes(es).', 'success')

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"SUB_RENEWED_ERROR: Admin '{admin_name}' (ID:{admin_id}) → {e}")
        flash('Error al renovar la suscripción.', 'error')
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('admin.dashboard'))


# =============================================================================
# CAMBIAR ROL (POST — anti-CSRF)
# =============================================================================

@admin_bp.route('/cambiar_rol', methods=['POST'])
@admin_required
def cambiar_rol():
    """
    Cambia el rol de un usuario. Un admin (role=1) no puede asignar roles
    superiores al suyo propio. Usa POST para protección CSRF.
    """
    if session.get('role') < 1:
        flash('No tienes permisos para cambiar roles.', 'error')
        return redirect(url_for('admin.dashboard'))

    admin_id   = session.get('user_id')
    admin_name = session.get('username', 'Admin_Desconocido')
    admin_role = session.get('role')

    user_id   = request.form.get('user_id')
    nuevo_rol = int(request.form.get('nuevo_rol', 0))

    # Un admin (role=1) no puede asignar roles iguales o superiores al suyo
    if admin_role == 1 and nuevo_rol >= admin_role:
        flash('No puedes asignar un rango igual o superior al tuyo.', 'warning')
        return redirect(url_for('admin.dashboard'))

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT username, role FROM usuarios WHERE id = %s', (user_id,))
        target = cursor.fetchone()
        target_name = target['username'] if target else f"ID {user_id}"
        old_role    = target['role'] if target else '?'

        cursor.execute('UPDATE usuarios SET role = %s WHERE id = %s', (nuevo_rol, user_id))
        conn.commit()

        current_app.logger.info(
            f"ROLE_CHANGED: Admin '{admin_name}' (ID:{admin_id}) cambió rol de "
            f"'{target_name}' (ID:{user_id}): {old_role} → {nuevo_rol}."
        )
        flash(f'Rol de {target_name} actualizado a {nuevo_rol}.', 'success')

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"ROLE_CHANGED_ERROR: Admin '{admin_name}' (ID:{admin_id}) → {e}")
        flash('Error al cambiar el rol.', 'error')
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('admin.dashboard'))


# =============================================================================
# RESET DE CONTRASEÑA (POST)
# =============================================================================

@admin_bp.route('/reset_password', methods=['POST'])
@admin_required
def reset_password():
    """
    Permite a un admin resetear la contraseña de cualquier usuario.
    Requiere role >= 1. Invalida también todos los tokens de reset activos
    del usuario afectado para evitar accesos con links viejos.
    """
    if session.get('role') < 1:
        flash('No tienes permisos para resetear contraseñas.', 'error')
        return redirect(url_for('admin.dashboard'))

    admin_id   = session.get('user_id')
    admin_name = session.get('username', 'Admin_Desconocido')

    user_id  = request.form.get('user_id')
    new_pass = request.form.get('new_password', '').strip()

    if not user_id or not new_pass:
        flash('Datos incompletos para el reset.', 'error')
        return redirect(url_for('admin.dashboard'))

    conn = get_db()
    cursor = conn.cursor()
    try:
        hashed_pw = generate_password_hash(new_pass, method='pbkdf2:sha256')

        cursor.execute('UPDATE usuarios SET password = %s WHERE id = %s', (hashed_pw, user_id))

        # Invalidamos todos los tokens de reset activos del usuario para que
        # links viejos en su correo dejen de funcionar
        cursor.execute(
            'UPDATE password_resets SET used = TRUE WHERE user_id = %s AND used = FALSE',
            (user_id,)
        )

        conn.commit()

        if cursor.rowcount >= 0:
            cursor.execute('SELECT username FROM usuarios WHERE id = %s', (user_id,))
            target = cursor.fetchone()
            target_name = target['username'] if target else f"ID {user_id}"

            current_app.logger.info(
                f"PASSWORD_RESET: Admin '{admin_name}' (ID:{admin_id}) "
                f"reseteó contraseña de '{target_name}' (ID:{user_id})."
            )
            flash(f'Contraseña de {target_name} actualizada correctamente.', 'success')

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"PASSWORD_RESET_ERROR: Admin '{admin_name}' (ID:{admin_id}) → {e}")
        flash('Error en la base de datos al resetear contraseña.', 'error')
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('admin.dashboard'))


# =============================================================================
# MODO FANTASMA — Entrar a cuenta de otro usuario
# =============================================================================

@admin_bp.route('/impersonate/<int:user_id>')
@admin_required
def impersonate(user_id):
    """
    Permite a un admin ver el sistema exactamente como lo ve otro usuario.
    Guarda el ID del admin original en sesión para poder volver.
    No requiere verificación de rol superior — permite impersonar a otros admins
    (útil para soporte entre administradores del mismo equipo).
    """
    admin_id   = session.get('user_id')
    admin_name = session.get('username', 'Admin_Desconocido')

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT id, username, role FROM usuarios WHERE id = %s', (user_id,))
        user = cursor.fetchone()

        if not user:
            flash('Usuario no encontrado.', 'error')
            return redirect(url_for('admin.dashboard'))

        # Guardamos el ID original para poder restaurar la sesión del admin al salir
        session['original_admin_id'] = admin_id
        session['user_id']   = user['id']
        session['username']  = user['username']
        session['role']      = user['role']

        current_app.logger.info(
            f"IMPERSONATE_START: Admin '{admin_name}' (ID:{admin_id}) "
            f"entró a cuenta de '{user['username']}' (ID:{user['id']}, role:{user['role']})."
        )
        flash(f'Modo Fantasma activo: estás viendo el sistema como {user["username"]}', 'info')
        return redirect(url_for('main.index'))

    except Exception as e:
        current_app.logger.error(f"IMPERSONATE_ERROR: Admin '{admin_name}' (ID:{admin_id}) → {e}")
        flash('Error al entrar al modo fantasma.', 'error')
        return redirect(url_for('admin.dashboard'))
    finally:
        cursor.close()
        conn.close()


# =============================================================================
# MODO FANTASMA — Salir y volver a la cuenta de admin
# =============================================================================

@admin_bp.route('/stop_impersonate')
def stop_impersonate():
    """
    Restaura la sesión del admin original y termina el modo fantasma.
    Si no hay ID de admin guardado, limpia la sesión y manda al login.
    """
    if 'original_admin_id' not in session:
        return redirect(url_for('main.index'))

    original_id = session['original_admin_id']

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT id, username, role FROM usuarios WHERE id = %s', (original_id,))
        admin_user = cursor.fetchone()

        if admin_user:
            # Restauramos los datos del admin original
            current_impersonated = session.get('username', 'Usuario_Desconocido')

            session['user_id']  = admin_user['id']
            session['username'] = admin_user['username']
            session['role']     = admin_user['role']
            session.pop('original_admin_id', None)

            current_app.logger.info(
                f"IMPERSONATE_STOP: Admin '{admin_user['username']}' (ID:{original_id}) "
                f"salió del modo fantasma (estaba en cuenta de '{current_impersonated}')."
            )
            flash('Modo Fantasma finalizado. Bienvenido de vuelta, Jefe.', 'success')
            return redirect(url_for('admin.dashboard'))

    except Exception as e:
        current_app.logger.error(f"IMPERSONATE_STOP_ERROR: ID:{original_id} → {e}")
    finally:
        cursor.close()
        conn.close()

    # Si algo falló al recuperar al admin, limpiamos y mandamos al login
    session.clear()
    return redirect(url_for('auth.login'))


# =============================================================================
# ELIMINAR USUARIO (POST)
# =============================================================================

@admin_bp.route('/delete_user', methods=['POST'])
@admin_required
def delete_user():
    """
    Elimina permanentemente un usuario y TODOS sus datos relacionados.
    Borra en cascada respetando el orden de FK para no violar constraints.
    También limpia archivos de Cloudflare R2 (logos).

    Restricciones:
    - No puedes borrarte a ti mismo.
    - No puedes borrar usuarios de igual o mayor rango (a menos que seas role=2).
    """
    user_id    = request.form.get('user_id')
    admin_id   = session.get('user_id')
    admin_name = session.get('username', 'Admin_Desconocido')
    admin_role = session.get('role', 0)

    if not user_id or str(user_id) == str(admin_id):
        flash('No puedes borrarte a ti mismo.', 'danger')
        return redirect(url_for('admin.dashboard'))

    conn = get_db()
    cursor = conn.cursor()

    try:
        cursor.execute('SELECT username, role FROM usuarios WHERE id = %s', (user_id,))
        target = cursor.fetchone()

        if not target:
            flash('El usuario no existe.', 'danger')
            return redirect(url_for('admin.dashboard'))

        target_name = target['username']
        target_role = target['role']

        # Verificación de jerarquía: solo role=2 puede borrar otros role=2
        if admin_role < 2 and target_role >= admin_role:
            flash('No puedes eliminar a un usuario de tu mismo rango o superior.', 'danger')
            return redirect(url_for('admin.dashboard'))

        # --- Limpieza de archivos R2 (logo del usuario) ---
        cursor.execute('SELECT logo_empresa FROM configuracion WHERE user_id = %s', (user_id,))
        config_user = cursor.fetchone()
        if config_user and config_user['logo_empresa'] and 'http' in config_user['logo_empresa']:
            try:
                delete_from_cloudflare(config_user['logo_empresa'])
                current_app.logger.info(f"R2_CLEANUP: Logo de '{target_name}' (ID:{user_id}) eliminado de R2.")
            except Exception as e:
                current_app.logger.warning(f"R2_CLEANUP_WARN: No se pudo borrar logo de '{target_name}' → {e}")

        # --- Borrado en cascada (orden crítico: hijos antes que padres) ---

        # Auth y seguridad
        cursor.execute('DELETE FROM auth_codes WHERE user_id = %s', (user_id,))
        cursor.execute('DELETE FROM password_resets WHERE user_id = %s', (user_id,))
        cursor.execute('DELETE FROM logs_actividad WHERE user_id = %s', (user_id,))

        # Ventas y cotizaciones
        cursor.execute('DELETE FROM venta_detalles WHERE venta_id IN (SELECT id FROM ventas WHERE user_id = %s)', (user_id,))
        cursor.execute('DELETE FROM ventas WHERE user_id = %s', (user_id,))

        # Inventario y productos
        cursor.execute('DELETE FROM movimientos_inventario WHERE user_id = %s', (user_id,))
        cursor.execute('DELETE FROM producto_detalles WHERE producto_id IN (SELECT id FROM productos WHERE user_id = %s)', (user_id,))
        cursor.execute('DELETE FROM producto_maquinaria WHERE producto_id IN (SELECT id FROM productos WHERE user_id = %s)', (user_id,))
        cursor.execute('DELETE FROM productos WHERE user_id = %s', (user_id,))
        cursor.execute('DELETE FROM materiales WHERE user_id = %s', (user_id,))
        cursor.execute('DELETE FROM maquinaria WHERE user_id = %s', (user_id,))

        # Envíos
        cursor.execute('DELETE FROM shipping_rates WHERE zone_id IN (SELECT id FROM shipping_zones WHERE user_id = %s)', (user_id,))
        cursor.execute('DELETE FROM shipping_zones WHERE user_id = %s', (user_id,))
        cursor.execute('DELETE FROM shipping_configs WHERE user_id = %s', (user_id,))

        # Configuración y tutoriales
        cursor.execute('DELETE FROM tutoriales_estado WHERE user_id = %s', (user_id,))
        cursor.execute('DELETE FROM configuracion WHERE user_id = %s', (user_id,))

        # Finalmente el usuario
        cursor.execute('DELETE FROM usuarios WHERE id = %s', (user_id,))

        conn.commit()

        current_app.logger.warning(
            f"USER_DELETED: Admin '{admin_name}' (ID:{admin_id}) eliminó permanentemente "
            f"la cuenta de '{target_name}' (ID:{user_id}, role:{target_role}) y todos sus datos."
        )
        flash(f'Usuario {target_name} y todos sus datos eliminados correctamente.', 'success')

    except Exception as e:
        conn.rollback()
        current_app.logger.error(
            f"USER_DELETE_ERROR: Admin '{admin_name}' (ID:{admin_id}) "
            f"falló al eliminar ID:{user_id} → {e}"
        )
        flash('Error interno al eliminar el usuario. Intenta de nuevo.', 'danger')
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('admin.dashboard'))


# =============================================================================
# SUMAR DÍAS A SUSCRIPCIÓN (POST)
# =============================================================================

@admin_bp.route('/sumar-tiempo', methods=['POST'])
@admin_required
def sumar_tiempo():
    """
    Suma días adicionales a la suscripción de un usuario.
    Si ya estaba vencida, empieza a contar desde hoy.
    Si sigue activa, suma desde la fecha actual de vencimiento.
    Registra los días acumulados en el campo dias_regalados.
    """
    if session.get('role') < 1:
        flash('No tienes permisos para modificar suscripciones.', 'error')
        return redirect(url_for('admin.dashboard'))

    admin_id   = session.get('user_id')
    admin_name = session.get('username', 'Admin_Desconocido')

    user_id = request.form.get('user_id')
    try:
        dias_a_sumar = int(request.form.get('cantidad_dias', 0))
    except ValueError:
        flash('La cantidad de dias debe ser un número entero.', 'error')
        return redirect(url_for('admin.dashboard'))

    if dias_a_sumar <= 0:
        flash('Debes sumar al menos 1 dia.', 'warning')
        return redirect(url_for('admin.dashboard'))

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'SELECT username, subscription_end FROM usuarios WHERE id = %s', (user_id,)
        )
        target = cursor.fetchone()

        if not target:
            flash('Usuario no encontrado.', 'error')
            return redirect(url_for('admin.dashboard'))

        target_name    = target['username']
        current_sub_end = target['subscription_end']
        ahora          = now_utc()

        # Normalizamos timezone de la fecha de BD
        if current_sub_end and current_sub_end.tzinfo is None:
            current_sub_end = current_sub_end.replace(tzinfo=timezone.utc)

        # Si estaba vencida: contamos desde hoy. Si sigue activa: extendemos.
        if not current_sub_end or current_sub_end < ahora:
            nueva_fecha_fin = ahora + timedelta(days=dias_a_sumar)
            base_texto = "desde hoy"
        else:
            nueva_fecha_fin = current_sub_end + timedelta(days=dias_a_sumar)
            base_texto = "desde vencimiento actual"

        cursor.execute('''
            UPDATE usuarios
            SET subscription_end   = %s,
                estado_suscripcion = %s,
                plan_type          = CASE WHEN plan_type = 'Free' THEN 'Pro' ELSE plan_type END,
                dias_regalados     = COALESCE(dias_regalados, 0) + %s
            WHERE id = %s
        ''', (nueva_fecha_fin, 'Activo', dias_a_sumar, user_id))

        conn.commit()

        current_app.logger.info(
            f"TIME_ADDED: Admin '{admin_name}' (ID:{admin_id}) sumó {dias_a_sumar} dias "
            f"({base_texto}) a '{target_name}' (ID:{user_id}). Nuevo vencimiento: {nueva_fecha_fin.date()}."
        )
        flash(f'Se sumaron {dias_a_sumar} dias a {target_name} correctamente.', 'success')

    except Exception as e:
        conn.rollback()
        current_app.logger.error(
            f"TIME_ADDED_ERROR: Admin '{admin_name}' (ID:{admin_id}) "
            f"falló al sumar dias a ID:{user_id} → {e}"
        )
        flash('Error interno al actualizar el tiempo.', 'danger')
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('admin.dashboard'))


# =============================================================================
# EXPORTAR USUARIOS A CSV
# =============================================================================

@admin_bp.route('/exportar-usuarios')
@admin_required
def exportar_usuarios():
    """
    Genera y descarga un CSV con todos los usuarios del sistema.
    Incluye BOM UTF-8 para compatibilidad con Excel en español.
    Solo accesible para role >= 1.
    """
    if session.get('role') < 1:
        abort(403)

    admin_id   = session.get('user_id')
    admin_name = session.get('username', 'Admin_Desconocido')

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, username, email, company_name, role,
                   created_at, subscription_end, last_login
            FROM usuarios
            ORDER BY created_at DESC
        """)
        usuarios = cursor.fetchall()

        si = StringIO()
        si.write('\ufeff')  # BOM UTF-8 para que Excel abra con tildes correctas
        cw = csv.writer(si)
        cw.writerow([
            'ID', 'Usuario', 'Email', 'Empresa',
            'Rol (0=Usuario, 1=Admin, 2=Dueño)',
            'Fecha de Registro', 'Vencimiento Suscripción', 'Última Vez Visto'
        ])
        for u in usuarios:
            cw.writerow([
                u['id'], u['username'], u['email'], u['company_name'], u['role'],
                u['created_at'],
                u['subscription_end'] if u['subscription_end'] else 'Sin fecha',
                u['last_login'] if u['last_login'] else 'Nunca'
            ])

        current_app.logger.info(
            f"EXPORT_DATA: Admin '{admin_name}' (ID:{admin_id}) exportó lista de {len(usuarios)} usuarios."
        )

        return Response(
            si.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition": "attachment;filename=reporte_usuarios_sianeffects.csv",
                "Content-type": "text/csv; charset=utf-8"
            }
        )
    except Exception as e:
        current_app.logger.error(f"EXPORT_ERROR: Admin '{admin_name}' (ID:{admin_id}) → {e}")
        flash('Error al generar el CSV.', 'error')
        return redirect(url_for('admin.dashboard'))
    finally:
        cursor.close()
        conn.close()


# =============================================================================
# MONITOR DE LOGS EN TIEMPO REAL
# =============================================================================

@admin_bp.route('/monitor')
@admin_required
def monitor():
    """
    Muestra las últimas 100 líneas del archivo limpieza.log en orden inverso
    (las más recientes primero). Útil para supervisar tareas automáticas.
    """
    log_path = os.path.join(current_app.root_path, 'limpieza.log')
    logs = []

    if os.path.exists(log_path):
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            logs = f.readlines()[-100:]
            logs.reverse()

    return render_template('monitor.html', logs=logs)


# =============================================================================
# DESCARGAR ARCHIVO DE LOG
# =============================================================================

@admin_bp.route('/descargar-log')
@admin_required
def descargar_log():
    """Descarga el archivo limpieza.log completo. Solo role >= 1."""
    if session.get('role') < 1:
        abort(403)

    log_path = os.path.join(current_app.root_path, 'limpieza.log')

    if os.path.exists(log_path):
        current_app.logger.info(
            f"LOG_DOWNLOAD: Admin '{session.get('username')}' (ID:{session.get('user_id')}) descargó limpieza.log."
        )
        return send_from_directory(
            directory=current_app.root_path,
            path='limpieza.log',
            as_attachment=True
        )

    return "El archivo de historial (limpieza.log) aún no se ha generado.", 404


# =============================================================================
# API — Bitácora de actividad por usuario (para Modal en el frontend)
# =============================================================================

@admin_bp.route('/api/log/<int:user_id>')
@admin_required
def api_ver_log(user_id):
    """
    Devuelve los últimos 20 eventos de actividad de un usuario en JSON.
    Usado por el modal de detalle en admin.html via fetch().
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        # También traemos el username para enriquecer el log en el frontend
        cursor.execute('SELECT username FROM usuarios WHERE id = %s', (user_id,))
        usuario = cursor.fetchone()
        username = usuario['username'] if usuario else f"ID {user_id}"

        cursor.execute("""
            SELECT accion, modulo, detalle, fecha
            FROM logs_actividad
            WHERE user_id = %s
            ORDER BY fecha DESC
            LIMIT 20
        """, (user_id,))
        logs_db = cursor.fetchall()

        lista_logs = [{
            "accion":       row['accion'],
            "modulo":       row['modulo'] if row['modulo'] else 'Sistema',
            "detalle":      row['detalle'] if row['detalle'] else '',
            "hace_tiempo":  time_ago(row['fecha']),
            "fecha_texto":  row['fecha'].strftime('%d/%m/%Y %H:%M') if row['fecha'] else ''
        } for row in logs_db]

        return jsonify({"success": True, "username": username, "logs": lista_logs})

    except Exception as e:
        current_app.logger.error(
            f"API_LOG_ERROR: Admin '{session.get('username')}' (ID:{session.get('user_id')}) "
            f"falló al cargar logs de ID:{user_id} → {e}"
        )
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# =============================================================================
# GESTIÓN DE NOTIFICACIONES MANUALES (ADMIN)
# =============================================================================
@admin_bp.route('/notificaciones-admin', methods=['GET', 'POST'])
@admin_required
def gestion_notificaciones():
    conn = get_db()
    cursor = conn.cursor()

    if request.method == 'POST':
        seleccionados = request.form.getlist('user_ids')
        segmento = request.form.get('segmento', '').strip()
        titulo = request.form.get('titulo')
        mensaje = request.form.get('mensaje')
        url_link = request.form.get('url_link', '').strip()
        tipo = request.form.get('tipo', 'info')
        
        if not url_link: url_link = None
        
        try:
            final_user_ids = set()
            ahora_utc = now_utc()
            
            # --- FUNCIÓN EXTRACCIÓN ROBUSTA ---
            def extraer_ids(registros):
                for r in registros:
                    try:
                        uid = r['id'] if hasattr(r, 'keys') else r[0]
                        final_user_ids.add(int(uid))
                    except: pass

            # --- EVALUAR SEGMENTOS ---
            if segmento == 'todos':
                cursor.execute("SELECT id FROM usuarios")
                extraer_ids(cursor.fetchall())
            elif segmento == 'activos':
                cursor.execute("SELECT id FROM usuarios WHERE subscription_end > %s", (ahora_utc,))
                extraer_ids(cursor.fetchall())
            elif segmento == 'vencidos':
                cursor.execute("SELECT id FROM usuarios WHERE subscription_end <= %s OR subscription_end IS NULL", (ahora_utc,))
                extraer_ids(cursor.fetchall())
            elif segmento == 'por_vencer':
                from datetime import timedelta
                limite = ahora_utc + timedelta(days=5)
                cursor.execute("SELECT id FROM usuarios WHERE subscription_end BETWEEN %s AND %s", (ahora_utc, limite))
                extraer_ids(cursor.fetchall())

            if seleccionados:
                for uid in seleccionados:
                    if uid.isdigit(): final_user_ids.add(int(uid))

            # --- INSERCIÓN MASIVA AGRUPADA POR LOTE ---
            if final_user_ids:
                # Generamos un ID único para este envío (ej. 'batch-8f4a2b')
                lote_id = f"batch-{uuid.uuid4().hex[:8]}" 
                
                for uid in final_user_ids:
                    cursor.execute("""
                        INSERT INTO notificaciones_manuales (user_id, titulo, mensaje, tipo, url, batch_id)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (uid, titulo, mensaje, tipo, url_link, lote_id))
                
                conn.commit()
                flash(f"¡Éxito! Notificación enviada a {len(final_user_ids)} usuario(s).", "success")
            else:
                flash("No seleccionaste ningún usuario o filtro válido.", "warning")
                
        except Exception as e:
            conn.rollback()
            current_app.logger.error(f"Error en broadcast: {e}")
            flash("Error interno al procesar el envío masivo.", "danger")
        
        return redirect(url_for('admin.gestion_notificaciones'))

    # 1. Traer Notificaciones Directas (Lotes)
    cursor.execute("""
        SELECT 
            'directo' as tipo_envio,
            COALESCE(batch_id, id::text) as lote_id,
            titulo, mensaje, tipo, url,
            MAX(fecha_creacion) as fecha_envio,
            COUNT(id) as total_enviados,
            COUNT(CASE WHEN leida = TRUE THEN 1 END) as total_leidos
        FROM notificaciones_manuales
        GROUP BY lote_id, titulo, mensaje, tipo, url
    """)
    # Convertimos a diccionario normal para poder juntarlos
    historial_directo = [dict(row) for row in cursor.fetchall()]

    # 2. Traer Anuncios Globales (Permanentes)
    cursor.execute("""
        SELECT 
            'global' as tipo_envio,
            id::text as lote_id,
            titulo, mensaje, tipo, url,
            fecha_creacion as fecha_envio,
            (SELECT COUNT(*) FROM usuarios) as total_enviados,
            (SELECT COUNT(*) FROM anuncios_vistos WHERE anuncio_id = g.id) as total_leidos
        FROM anuncios_globales g
    """)
    historial_global = [dict(row) for row in cursor.fetchall()]

    # 3. Mezclamos ambos historiales y los ordenamos por fecha (el más reciente primero)
    historial_completo = historial_directo + historial_global
    historial_completo.sort(key=lambda x: x['fecha_envio'], reverse=True)
    
    # Nos quedamos con los últimos 50 envíos en total
    historial_completo = historial_completo[:50]
    
    # Convertimos la fecha UTC de la base de datos a la hora local (Monterrey/CDMX)
    for h in historial_completo:
        if h['fecha_envio']:
            h['fecha_envio'] = utc_to_local(h['fecha_envio'])
            
    # Lista de usuarios para el selector
    cursor.execute("SELECT id, username FROM usuarios ORDER BY username ASC")
    usuarios = cursor.fetchall()
    
    cursor.close()
    conn.close()
    return render_template('dashboard/notificaciones.html', historial=historial_completo, usuarios=usuarios)

# =============================================================================
# ELIMINAR LOTE DE NOTIFICACIONES
# =============================================================================
@admin_bp.route('/eliminar-notificacion-admin/<string:lote_id>')
@admin_required
def eliminar_notificacion_admin(lote_id):
    conn = get_db()
    cursor = conn.cursor()
    # Borra todos los registros que compartan este batch_id (o el id viejo)
    cursor.execute("""
        DELETE FROM notificaciones_manuales 
        WHERE batch_id = %s OR id::text = %s
    """, (lote_id, lote_id))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Lote de notificaciones eliminado para todos los usuarios", "info")
    return redirect(url_for('admin.gestion_notificaciones'))

@admin_bp.route('/crear-anuncio-global', methods=['POST'])
@admin_required
def crear_anuncio_global():
    titulo = request.form.get('titulo')
    mensaje = request.form.get('mensaje')
    tipo = request.form.get('tipo', 'info')
    url = request.form.get('url_link')

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO anuncios_globales (titulo, mensaje, tipo, url)
        VALUES (%s, %s, %s, %s)
    """, (titulo, mensaje, tipo, url))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Anuncio global lanzado con éxito", "success")
    return redirect(url_for('admin.gestion_notificaciones'))

@admin_bp.route('/eliminar-global-admin/<int:id>')
@admin_required
def eliminar_global_admin(id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM anuncios_globales WHERE id = %s", (id,))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Anuncio global eliminado y retirado de todos los usuarios", "info")
    return redirect(url_for('admin.gestion_notificaciones'))