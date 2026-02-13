import os
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, send_from_directory, abort
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
    # 1. Obtener datos básicos de la base de datos
    conn = get_db_connection()
    # Traemos todos los usuarios ordenados por fecha de creación (más recientes primero)
    users = conn.execute('SELECT * FROM usuarios ORDER BY created_at DESC').fetchall()
    conn.close()
    
    # 2. Configuración de tiempos (Consistencia UTC)
    ahora_utc = now_utc()
    hace_24h = ahora_utc - timedelta(days=1)
    # Definimos el umbral de "Peligro" (vencidos hace más de 350 días, casi el año)
    proximos_a_borrar = ahora_utc - timedelta(days=350)

    # 3. Inicialización del diccionario de estadísticas
    stats = {
        'total': len(users),
        'activos': 0,
        'vencidos': 0,
        'admins': 0,
        'online_hoy': 0,      # Usuarios que iniciaron sesión en las últimas 24h
        'en_riesgo': 0        # Cuentas a punto de cumplir 12 meses para borrado
    }

    # 4. Procesamiento de cada usuario para calcular métricas
    for u in users:
        # --- Lógica de Roles ---
        # Si el rol es 1 (Admin) o 2 (Dueño)
        if u['role'] > 0:
            stats['admins'] += 1
        
        # --- Lógica de Suscripción ---
        if u['subscription_end']:
            try:
                # Convertimos el string de la BD a objeto datetime con zona horaria UTC
                # Tomamos los primeros 19 caracteres para evitar microsegundos si existen
                fecha_fin = datetime.strptime(str(u['subscription_end'])[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                
                if fecha_fin > ahora_utc:
                    stats['activos'] += 1
                else:
                    stats['vencidos'] += 1
                    
                    # REGLA DE BORRADO: Si es usuario normal (role 0) y su fecha es menor al umbral de 350 días
                    if u['role'] == 0 and fecha_fin < proximos_a_borrar:
                        stats['en_riesgo'] += 1
            except (ValueError, TypeError):
                stats['vencidos'] += 1
        else:
            # Si no tiene fecha, lo contamos como vencido o sin plan
            stats['vencidos'] += 1

        # --- Lógica de Actividad (Last Login) ---
        if u['last_login']:
            try:
                # Convertimos la fecha del último login
                fecha_login = datetime.strptime(str(u['last_login'])[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                # Si entró en las últimas 24 horas
                if fecha_login > hace_24h:
                    stats['online_hoy'] += 1
            except (ValueError, TypeError):
                pass

    # 5. Renderizar la plantilla pasando el rol actual para control de interfaz
    return render_template('admin.html', 
                           users=users, 
                           now=ahora_utc, 
                           stats=stats, 
                           my_role=session.get('role'))

                           
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

@admin_bp.route('/descargar-log')
@admin_required
def descargar_log():
    # Solo permitimos a Admins (role >= 1)
    # Usamos current_user.role porque es más seguro que la sesión
    if session.get('role') < 1:
        abort(403)

    # Definimos la ruta del archivo. 
    # Si configuraste el logging como lo hicimos antes, el archivo está en la raíz.
    log_path = os.path.join(current_app.root_path, 'limpieza.log')
    
    # Verificamos si el archivo existe antes de intentar enviarlo
    if os.path.exists(log_path):
        return send_from_directory(
            directory=current_app.root_path, 
            path='limpieza.log', 
            as_attachment=True
        )
    else:
        # Si el log no existe aún (porque la tarea no ha borrado a nadie), 
        # devolvemos un mensaje amigable.
        return "El archivo de historial (limpieza.log) aún no se ha generado.", 404

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