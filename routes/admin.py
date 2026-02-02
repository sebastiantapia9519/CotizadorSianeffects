from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta, timezone # Importamos timezone
from db import get_db_connection
from helpers import admin_required
# Importamos tu utilidad para asegurar consistencia
from utils.datetime_utils import now_utc 

admin_bp = Blueprint('admin', __name__)

@admin_bp.route('/admin')
@admin_required
def dashboard():
    conn = get_db_connection()
    users = conn.execute('SELECT * FROM usuarios ORDER BY created_at DESC').fetchall()
    conn.close()
    
    stats = {'total': len(users), 'activos': 0, 'vencidos': 0, 'admins': 0}
    
    # Referencia actual en UTC para comparaciones
    ahora_utc = now_utc()

    # 2. Calcular estadísticas
    for u in users:
        es_activo = False

        if u['subscription_end']:
            try:
                # Convertimos string de BD a objeto datetime
                # NOTA: strptime crea un objeto "ingenuo" (sin zona horaria)
                fecha_fin_naive = datetime.strptime(str(u['subscription_end'])[:19], '%Y-%m-%d %H:%M:%S')
                
                # Le asignamos UTC explícitamente para poder comparar con 'ahora_utc'
                fecha_fin = fecha_fin_naive.replace(tzinfo=timezone.utc)
                
                if fecha_fin > ahora_utc:
                    stats['activos'] += 1
                    es_activo = True
                else:
                    stats['vencidos'] += 1
            except ValueError:
                # Si falla el parseo, asumimos vencido
                stats['vencidos'] += 1
        else:
            # Si no tiene fecha fin (y no es admin perpetuo), cuenta como vencido en stats
            stats['vencidos'] += 1

        # Contamos admins aparte
        if u['role'] > 0:  # 1=Admin, 2=Dueño
            stats['admins'] += 1
            # (Opcional) Si quieres que los admins cuenten siempre como activos en la gráfica:
            # if not es_activo: stats['activos'] += 1; stats['vencidos'] -= 1

    return render_template('admin.html', users=users, now=ahora_utc, stats=stats)

@admin_bp.route('/admin/renovar/<int:user_id>/<int:meses>')
@admin_required
def renovar(user_id, meses):
    if session.get('role') < 2: 
        return redirect(url_for('admin.dashboard'))
    
    # CAMBIO CRÍTICO: Usamos now_utc() en lugar de datetime.now()
    nueva_fecha_fin = now_utc() + timedelta(days=meses*30)
    
    conn = get_db_connection()
    conn.execute('UPDATE usuarios SET subscription_end = ? WHERE id = ?', (nueva_fecha_fin, user_id))
    conn.commit()
    conn.close()
    
    flash(f'Suscripción renovada por {meses} meses.', 'success')
    return redirect(url_for('admin.dashboard'))

@admin_bp.route('/admin/cambiar_rol/<int:user_id>/<int:nuevo_rol>')
@admin_required
def cambiar_rol(user_id, nuevo_rol):
    if session.get('role') < 2: 
        return redirect(url_for('admin.dashboard'))
    
    conn = get_db_connection()
    conn.execute('UPDATE usuarios SET role = ? WHERE id = ?', (nuevo_rol, user_id))
    conn.commit()
    conn.close()
    
    return redirect(url_for('admin.dashboard'))

@admin_bp.route('/admin/reset_password', methods=['POST'])
@admin_required
def reset_password():
    if session.get('role') < 2: 
        return redirect(url_for('admin.dashboard'))
    
    conn = get_db_connection()
    conn.execute('UPDATE usuarios SET password = ? WHERE id = ?', 
                 (generate_password_hash(request.form['new_password']), request.form['user_id']))
    conn.commit()
    conn.close()
    
    flash('Password actualizada.', 'success')
    return redirect(url_for('admin.dashboard'))