import os
import csv
from io import StringIO
from flask import Response
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, send_from_directory, abort
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta, timezone

# IMPORTACIÓN DE BASE DE DATOS
from db import get_db_connection as get_db
from helpers import admin_required
from utils.datetime_utils import now_utc, utc_to_local 

admin_bp = Blueprint('admin', __name__)

@admin_bp.route('/admin')
@admin_required
def dashboard():
    # 1. Datos de la base de datos
    conn = get_db()
    users = conn.execute('SELECT * FROM usuarios ORDER BY created_at DESC').fetchall()
    conn.close()
    
    # 2. Configuración de tiempos LOCALES (Tu hora de Monterrey/CDMX)
    ahora_utc = now_utc()
    ahora_local = utc_to_local(ahora_utc) 
    
    # Definimos la ventana de 24 horas en tu tiempo local
    hace_24h_local = ahora_local - timedelta(days=1)
    
    # Formateamos para las etiquetas del HTML
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
                # BLINDAJE SQLITE/POSTGRES: Si ya es datetime (Postgres), lo usamos. Si es texto (SQLite), lo parseamos.
                f_end = u['subscription_end']
                if isinstance(f_end, str):
                    f_utc = datetime.strptime(f_end[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                else:
                    # Asumimos que es objeto datetime nativo (Postgres)
                    f_utc = f_end if f_end.tzinfo else f_end.replace(tzinfo=timezone.utc)
                    
                f_local = utc_to_local(f_utc)
                
                if f_local > ahora_local:
                    stats['activos'] += 1
                else:
                    stats['vencidos'] += 1
                    # Riesgo de borrado (12 meses)
                    if u['role'] == 0 and f_local < proximos_a_borrar_local:
                        stats['en_riesgo'] += 1
            except Exception as e:
                print(f"Error parseando subscription_end: {e}")
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
                
                # Comparamos contra la variable correcta: hace_24h_local
                if log_local > hace_24h_local:
                    stats['online_hoy'] += 1
            except Exception as e:
                print(f"Error parseando last_login: {e}")
                pass

    # 5. Envío a la plantilla
    return render_template('admin.html', 
                           users=users, 
                           now=ahora_local, # El badge superior ahora dirá tu fecha local
                           stats=stats, 
                           my_role=session.get('role'),
                           hace_24h_str=hace_24h_str,
                           limite_riesgo=fecha_limite_riesgo)

                           
# --- ACCIONES PROTEGIDAS (SOLO DUEÑO - ROL 2) ---

@admin_bp.route('/admin/renovar/<int:user_id>/<int:meses>')
@admin_required
def renovar(user_id, meses):
    # BLOQUEO DE SEGURIDAD: Si no es el dueño (2), fuera.
    if session.get('role') != 2:
        flash('Solo el Dueño puede realizar acciones.', 'error')
        return redirect(url_for('admin.dashboard'))
    
    nueva_fecha_fin = now_utc() + timedelta(days=meses*30)
    conn = get_db()
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
    
    conn = get_db()
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
    
    conn = get_db()
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
    
    # CORRECCIÓN: Evita el BuildError si el usuario no existe
    return redirect(url_for('admin.dashboard'))

@admin_bp.route('/delete_user', methods=['POST'])
@admin_required
def delete_user():
    user_id = request.form.get('user_id')
    
    # 1. BLINDAJE DE SEGURIDAD (Evitar auto-borrado o borrado de dueños)
    if not user_id or str(user_id) == str(session['user_id']):
        flash('No puedes borrarte a ti mismo.', 'danger')
        return redirect(url_for('admin.dashboard')) 
        
    conn = get_db()
    
    # Verificamos a quién intentan borrar
    target_user = conn.execute("SELECT role FROM usuarios WHERE id = ?", (user_id,)).fetchone()
    if not target_user:
        conn.close()
        flash('El usuario no existe.', 'danger')
        return redirect(url_for('admin.dashboard'))
        
    # Si el que borra NO es dueño (rol 2), y la víctima es Admin (1) o Dueño (2), lo bloqueamos
    if session.get('role') < 2 and target_user['role'] >= 1:
        conn.close()
        flash('No tienes permisos para borrar a este nivel de usuario.', 'danger')
        return redirect(url_for('admin.dashboard'))

    # 2. PROCESO DE BORRADO EN CASCADA
    cursor = conn.cursor()
    try:
        # Borrar Configuración
        cursor.execute("DELETE FROM configuracion WHERE user_id = ?", (user_id,))
        
        # Borrar Detalles de Ventas y Ventas
        cursor.execute("DELETE FROM venta_detalles WHERE venta_id IN (SELECT id FROM ventas WHERE user_id = ?)", (user_id,))
        cursor.execute("DELETE FROM ventas WHERE user_id = ?", (user_id,))
        
        # Borrar Inventario y Materiales
        cursor.execute("DELETE FROM movimientos_inventario WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM materiales WHERE user_id = ?", (user_id,))
        
        # Borrar Maquinaria y Productos
        cursor.execute("DELETE FROM producto_detalles WHERE producto_id IN (SELECT id FROM productos WHERE user_id = ?)", (user_id,))
        cursor.execute("DELETE FROM producto_maquinaria WHERE producto_id IN (SELECT id FROM productos WHERE user_id = ?)", (user_id,))
        cursor.execute("DELETE FROM maquinaria WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM productos WHERE user_id = ?", (user_id,))
        
        # Borrar Logs de Envíos
        cursor.execute("DELETE FROM shipping_rates WHERE zone_id IN (SELECT id FROM shipping_zones WHERE user_id = ?)", (user_id,))
        cursor.execute("DELETE FROM shipping_zones WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM shipping_configs WHERE user_id = ?", (user_id,))

        # Finalmente borrar al usuario
        cursor.execute("DELETE FROM usuarios WHERE id = ?", (user_id,))
        
        conn.commit()
        flash('✅ Usuario y todos sus datos eliminados correctamente.', 'success')
        
    except Exception as e:
        conn.rollback()
        print(f"Error borrando cascada user {user_id}: {e}")
        flash('Error interno al eliminar el usuario. Intenta de nuevo.', 'danger')
    finally:
        conn.close()

    return redirect(url_for('admin.dashboard'))

@admin_bp.route('/descargar-log')
@admin_required
def descargar_log():
    # Solo permitimos a Admins (role >= 1)
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
        
        return redirect(url_for('admin.dashboard'))
    
    # Si algo falla gravemente, te saca
    session.clear()
    return redirect(url_for('auth.login'))

@admin_bp.route('/exportar-usuarios')
@admin_required
def exportar_usuarios():
    if session.get('role') < 1:
        abort(403)

    conn = get_db()
    usuarios = conn.execute("""
        SELECT 
            id, username, email, company_name, 
            role, created_at, subscription_end, last_login 
        FROM usuarios 
        ORDER BY created_at DESC
    """).fetchall()
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