from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta
from db import get_db_connection
from helpers import admin_required

admin_bp = Blueprint('admin', __name__)

@admin_bp.route('/admin')
@admin_required
def dashboard():
    conn = get_db_connection(); users = conn.execute('SELECT * FROM usuarios ORDER BY created_at DESC').fetchall(); conn.close()
    stats = {'total': len(users), 'activos': 0, 'vencidos': 0, 'admins': 0}
    
    # 2. Calcular estadísticas
    for u in users:
        # Verificamos si tiene fecha. Si es None, lo contamos como vencido o ignoramos
        if u['subscription_end']:
            try:
                # Intentamos convertir la fecha
                fecha_fin = datetime.strptime(str(u['subscription_end'])[:19], '%Y-%m-%d %H:%M:%S')
                
                if fecha_fin > datetime.now():
                    stats['activos'] += 1
                else:
                    stats['vencidos'] += 1
            except ValueError:
                # Si el formato de fecha está corrupto, lo contamos como vencido
                stats['vencidos'] += 1
        else:
            # Si es None (sin fecha), lo contamos como vencido (o infinito si es admin, pero para stats cuenta vencido)
            stats['vencidos'] += 1

        if u['role'] > 0:  # 1=Admin, 2=Dueño
            stats['admins'] += 1
    return render_template('admin.html', users=users, now=datetime.now(), stats=stats)

@admin_bp.route('/admin/renovar/<int:user_id>/<int:meses>')
@admin_required
def renovar(user_id, meses):
    if session.get('role') < 2: return redirect(url_for('admin.dashboard'))
    conn = get_db_connection(); conn.execute('UPDATE usuarios SET subscription_end = ? WHERE id = ?', (datetime.now() + timedelta(days=meses*30), user_id)); conn.commit(); conn.close()
    return redirect(url_for('admin.dashboard'))

@admin_bp.route('/admin/cambiar_rol/<int:user_id>/<int:nuevo_rol>')
@admin_required
def cambiar_rol(user_id, nuevo_rol):
    if session.get('role') < 2: return redirect(url_for('admin.dashboard'))
    conn = get_db_connection(); conn.execute('UPDATE usuarios SET role = ? WHERE id = ?', (nuevo_rol, user_id)); conn.commit(); conn.close()
    return redirect(url_for('admin.dashboard'))

@admin_bp.route('/admin/reset_password', methods=['POST'])
@admin_required
def reset_password():
    if session.get('role') < 2: return redirect(url_for('admin.dashboard'))
    conn = get_db_connection(); conn.execute('UPDATE usuarios SET password = ? WHERE id = ?', (generate_password_hash(request.form['new_password']), request.form['user_id'])); conn.commit(); conn.close()
    flash('Password actualizada.', 'success')
    return redirect(url_for('admin.dashboard'))