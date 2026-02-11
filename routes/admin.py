from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta, timezone
from db import get_db_connection
from helpers import admin_required
from db import get_db_connection as get_db
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

@admin_bp.route('/impersonate/<int:user_id>')
@admin_required # Solo admins pueden hacer esto
def impersonate(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM usuarios WHERE id=?", (user_id,)).fetchone()
    conn.close()
    
    if user:
        # Guardamos quién eras antes (por si quieres poner un botón de "Volver a Admin")
        session['original_admin_id'] = session['user_id']
        
        # Suplantamos la identidad
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['role'] = user['role']
        
        flash(f'👻 Modo Fantasma: Ahora estás viendo el sistema como {user["username"]}', 'info')
        return redirect(url_for('main.index'))
    
    return redirect(url_for('admin.panel'))

@admin_bp.route('/delete_user', methods=['POST'])
@admin_required
def delete_user():
    user_id = request.form.get('user_id')
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        # 1. Borrar Configuración
        cursor.execute("DELETE FROM configuracion WHERE user_id = ?", (user_id,))
        
        # 2. Borrar Detalles de Ventas y Ventas
        cursor.execute("DELETE FROM venta_detalles WHERE venta_id IN (SELECT id FROM ventas WHERE user_id = ?)", (user_id,))
        cursor.execute("DELETE FROM ventas WHERE user_id = ?", (user_id,))
        
        # 3. Borrar Inventario y Materiales
        cursor.execute("DELETE FROM materiales WHERE user_id = ?", (user_id,))
        try: cursor.execute("DELETE FROM movimientos_inventario WHERE user_id = ?", (user_id,))
        except: pass
        
        # 4. Borrar Maquinaria y Productos
        cursor.execute("DELETE FROM maquinaria WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM productos WHERE user_id = ?", (user_id,))
        
        # 5. Borrar Logs de Envíos
        try:
            cursor.execute("DELETE FROM shipping_configs WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM shipping_zones WHERE user_id = ?", (user_id,))
        except: pass

        # 6. Finalmente borrar al usuario
        cursor.execute("DELETE FROM usuarios WHERE id = ?", (user_id,))
        
        conn.commit()
        flash('✅ Usuario y todos sus datos eliminados correctamente.', 'success')
        
    except Exception as e:
        conn.rollback()
        flash(f'Error al eliminar: {str(e)}', 'danger')
    finally:
        conn.close()

    return redirect(url_for('admin.panel'))


@admin_bp.route('/stop_impersonate')
def stop_impersonate():
    # Solo funciona si hay un admin "escondido" en la sesión
    if 'original_admin_id' not in session:
        return redirect(url_for('main.index'))

    # Recuperamos tu ID real
    original_id = session['original_admin_id']

    conn = get_db()
    admin_user = conn.execute('SELECT * FROM usuarios WHERE id = ?', (original_id,)).fetchone()
    conn.close()

    if admin_user:
        # 1. Restauramos tu sesión de Admin/Dueño
        session['user_id'] = admin_user['id']
        session['username'] = admin_user['username']
        session['role'] = admin_user['role']

        # 2. Borramos el rastro del modo fantasma
        session.pop('original_admin_id', None)

        flash('👻 Modo Fantasma finalizado. Bienvenido de vuelta, Jefe.', 'success')
        
        # --- AQUÍ ESTABA EL ERROR ---
        return redirect(url_for('admin.dashboard'))
    
    # Si algo falla gravemente, te saca
    session.clear()
    return redirect(url_for('auth.login'))