from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta, timezone
from db import get_db_connection
from helpers import admin_required
from utils.datetime_utils import now_utc 

admin_bp = Blueprint('admin', __name__)

@admin_bp.route('/admin')
@admin_required
def dashboard():
    # Aquí entran Rol 1 y Rol 2
    conn = get_db_connection()
    users = conn.execute('SELECT * FROM usuarios ORDER BY created_at DESC').fetchall()
    conn.close()
    
    stats = {'total': len(users), 'activos': 0, 'vencidos': 0, 'admins': 0}
    ahora_utc = now_utc()

    for u in users:
        # ... (Tu lógica de conteo de stats se mantiene igual) ...
        if u['subscription_end']:
            try:
                fecha_fin = datetime.strptime(str(u['subscription_end'])[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                if fecha_fin > ahora_utc:
                    stats['activos'] += 1
                else:
                    stats['vencidos'] += 1
            except ValueError:
                stats['vencidos'] += 1
        else:
            stats['vencidos'] += 1

        if u['role'] > 0:
            stats['admins'] += 1

    # Pasamos el rol actual a la plantilla para ocultar botones si es Rol 1
    return render_template('admin.html', users=users, now=ahora_utc, stats=stats, my_role=session.get('role'))

# --- ACCIONES PROTEGIDAS (SOLO DUEÑO - ROL 2) ---

@admin_bp.route('/admin/renovar/<int:user_id>/<int:meses>')
@admin_required
def renovar(user_id, meses):
    # BLOQUEO DE SEGURIDAD: Si no es el dueño (2), fuera.
    if session.get('role') != 2:
        flash('Solo el Dueño puede realizar acciones.', 'error')
        return redirect(url_for('admin.dashboard'))
    
    nueva_fecha_fin = now_utc() + timedelta(days=meses*30)
    conn = get_db_connection()
    conn.execute('UPDATE usuarios SET subscription_end = ? WHERE id = ?', (nueva_fecha_fin, user_id))
    conn.commit(); conn.close()
    flash(f'Suscripción renovada por {meses} meses.', 'success')
    return redirect(url_for('admin.dashboard'))

@admin_bp.route('/admin/cambiar_rol/<int:user_id>/<int:nuevo_rol>')
@admin_required
def cambiar_rol(user_id, nuevo_rol):
    # BLOQUEO DE SEGURIDAD
    if session.get('role') != 2:
        flash('Solo el Dueño puede cambiar roles.', 'error')
        return redirect(url_for('admin.dashboard'))
    
    conn = get_db_connection()
    conn.execute('UPDATE usuarios SET role = ? WHERE id = ?', (nuevo_rol, user_id))
    conn.commit(); conn.close()
    return redirect(url_for('admin.dashboard'))

@admin_bp.route('/admin/reset_password', methods=['POST'])
@admin_required
def reset_password():
    # BLOQUEO DE SEGURIDAD
    if session.get('role') != 2:
        flash('Solo el Dueño puede resetear passwords.', 'error')
        return redirect(url_for('admin.dashboard'))
    
    conn = get_db_connection()
    conn.execute('UPDATE usuarios SET password = ? WHERE id = ?', 
                 (generate_password_hash(request.form['new_password']), request.form['user_id']))
    conn.commit(); conn.close()
    flash('Password actualizada.', 'success')
    return redirect(url_for('admin.dashboard'))