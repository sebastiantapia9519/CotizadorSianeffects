import json
import io
import zipfile
import uuid
from helpers import guardar_pase_bd, obtener_estado_mesas
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file
from db import get_db_connection
from utils.datetime_utils import hoy_sqlite, hoy_local, ahora_sql
from functools import wraps # Necesario para los decoradores
from routes.invitaciones_publicas import s3_client, BUCKET_NAME

clientes_bp = Blueprint('invitaciones_clientes', __name__)

hoy = hoy_sqlite()

# ==========================================
# DECORADORES DE SEGURIDAD
# ==========================================
def planner_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('user_type') != 'planner' or 'planner_id' not in session:
            flash("Debes iniciar sesión como Planner para acceder.", "warning")
            return redirect(url_for('invitaciones_clientes.login_cliente'))
        
        # VERIFICACIÓN EN BASE DE DATOS CONTRA EL ESTADO
        conn = get_db_connection()
        planner = conn.execute("SELECT estado FROM planners WHERE id = ?", (session['planner_id'],)).fetchone()
        conn.close()

        # Si no existe o su estado no es 'activo', lo expulsamos
        if not planner or planner['estado'] != 'activo':
            session.clear() 
            flash("Tu cuenta no está activa. Contacta al administrador.", "danger")
            return redirect(url_for('invitaciones_clientes.login_cliente'))

        return f(*args, **kwargs)
    return decorated_function

# ==========================================
# RUTAS DE ACCESO (EL EMBUDO)
# ==========================================

@clientes_bp.route('/mi-evento', methods=['GET', 'POST'], strict_slashes=False)
def login_cliente():
    if request.method == 'POST':
        codigo = request.form.get('codigo', '').strip().upper()
        conn = get_db_connection()
        try:
            # --- CASO 1: ES UN PLANNER (PLAN-XXXXX) ---
            if codigo.startswith('PLAN-'):
                planner = conn.execute("""
                    SELECT id, nombre_contacto, nombre_empresa, estado 
                    FROM planners WHERE codigo_acceso_planner = ?
                """, (codigo,)).fetchone()
                
                if planner:
                    # Bloqueo de entrada
                    if planner['estado'] != 'activo':
                        flash("Esta cuenta se encuentra suspendida.", "danger")
                        return render_template('clientes/login.html')

                    session.clear()
                    session['planner_id'] = planner['id']
                    session['planner_nombre'] = planner['nombre_contacto']
                    session['user_type'] = 'planner'
                    return redirect(url_for('invitaciones_clientes.dashboard_planner'))
                else:
                    flash("Código de Planner no encontrado.", "danger")

            # --- CASO 2: ES UN CLIENTE/NOVIOS (SIA-XXXXX) ---
            else:
                inv = conn.execute("""
                    SELECT id, slug, datos_cliente_json 
                    FROM invitaciones WHERE codigo_acceso_cliente = ?
                """, (codigo,)).fetchone()
                
                if inv:
                    session.clear()
                    session['cliente_inv_id'] = inv['id']
                    session['cliente_slug'] = inv['slug']
                    session['user_type'] = 'novios'
                    datos = json.loads(inv['datos_cliente_json']) if inv['datos_cliente_json'] else {}
                    session['cliente_nombre'] = datos.get('novios', 'Nuestro Evento')
                    return redirect(url_for('invitaciones_clientes.dashboard_cliente'))
                else:
                    flash("Código de acceso no válido.", "danger")
        finally:
            conn.close()
            
    return render_template('clientes/login.html')

# ==========================================
# DASHBOARD DEL PLANNER (B2B)
# ==========================================

@clientes_bp.route('/socio/panel')
@planner_required
def dashboard_planner():
    planner_id = session['planner_id']
    conn = get_db_connection()
    
    try:
        # 1. Obtener Saldo Total Neto (Solo lo vigente y activo)
        creditos_info = conn.execute("""
            SELECT SUM(cantidad_total - cantidad_usada) as saldo
            FROM planner_paquetes 
            WHERE planner_id = ? 
            AND activo = 1 
            AND fecha_vencimiento > ?
        """, (planner_id, hoy)).fetchone()
        
        saldo = creditos_info['saldo'] if creditos_info['saldo'] else 0

        # 2. Obtener Movimientos (Ordenados por fecha de compra para calcular el histórico)
        paquetes_raw = conn.execute("""
            SELECT id, cantidad_total, cantidad_usada, fecha_vencimiento, notas, fecha_compra
            FROM planner_paquetes
            WHERE planner_id = ? AND activo = 1
            ORDER BY fecha_compra ASC
        """, (planner_id,)).fetchall()

        # Buscar créditos que vencen en los próximos 15 días
        # Calculamos ambas fechas en Python. Esto es 100% compatible con SQLite y PostgreSQL.
        fecha_hoy = hoy_local()[:10] # Ej: 2026-03-10
        fecha_limite = ahora_sql(dias=15)[:10] # Cuantos dias antes te avisará que expiran tus creditos

        proximos_vencimientos = conn.execute("""
            SELECT id, (cantidad_total - cantidad_usada) as remanente, fecha_vencimiento
            FROM planner_paquetes
            WHERE planner_id = ? 
            AND activo = 1 
            AND (cantidad_total - cantidad_usada) > 0
            AND substring(fecha_vencimiento, 1, 10) BETWEEN ? AND ?
            ORDER BY fecha_vencimiento ASC
        """, (planner_id, fecha_hoy, fecha_limite)).fetchall()

        paquetes_procesados = []
        saldo_acumulado = 0
        
        for p in paquetes_raw:
            # El cambio es la diferencia entre lo que entró y lo que se marcó como usado en ese registro
            cambio = p['cantidad_total'] - p['cantidad_usada']
            saldo_acumulado += cambio
            
            p_dict = dict(p)
            p_dict['cambio'] = cambio
            p_dict['saldo_posterior'] = saldo_acumulado
            
            # Lógica de Expiración clara: Si es una resta (cambio negativo), no expira.
            if not p['fecha_vencimiento'] or cambio < 0:
                p_dict['vence_display'] = "---"
            else:
                p_dict['vence_display'] = p['fecha_vencimiento'][:10]
            
            paquetes_procesados.append(p_dict)

        # Invertimos para que el último movimiento (el más nuevo) salga arriba
        paquetes_procesados.reverse()

        # 3. Obtener Invitaciones (Historial de consumo)
        invitaciones_db = conn.execute("""
            SELECT id, slug, datos_cliente_json, fecha_evento, created_at, 
                   codigo_acceso_cliente, tipo_evento, tiene_modulo_invitados, camara_premium
            FROM invitaciones 
            WHERE planner_id = ? 
            ORDER BY created_at DESC
        """, (planner_id,)).fetchall()

        invitaciones = []
        for inv in invitaciones_db:
            item = dict(inv)
            try:
                item['datos_cliente'] = json.loads(inv['datos_cliente_json'])
            except:
                item['datos_cliente'] = {"novios": "Evento sin nombre"}
            invitaciones.append(item)

        # Revisar si ya gastó su carta del Demo
        demo_db = conn.execute("SELECT id FROM invitaciones WHERE planner_id = ? AND es_demo = 1", (planner_id,)).fetchone()
        tiene_demo = True if demo_db else False

        return render_template('clientes/dashboard_planner.html', 
                               saldo=saldo, 
                               paquetes=paquetes_procesados,
                               invitaciones=invitaciones,
                               alertas_vencimiento=proximos_vencimientos,
                               hoy_local=hoy_local()[:10],
                               tiene_demo=tiene_demo)
    finally:
        conn.close()

@clientes_bp.route('/mi-evento/panel')
def dashboard_cliente():
    if 'cliente_inv_id' not in session:
        return redirect(url_for('invitaciones_clientes.login_cliente'))
        
    inv_id = session['cliente_inv_id']
    conn = get_db_connection()

    PER_PAGE = 12
    page = request.args.get('page', 1, type=int)
    offset = (page - 1) * PER_PAGE
    
    try:
        inv = conn.execute("SELECT * FROM invitaciones WHERE id = ?", (inv_id,)).fetchone()
        
        # 1. Leer qué módulos están prendidos
        config_modulos = json.loads(inv['config_json']) if inv['config_json'] else []
        
        # 2. Traer invitados (si aplica)
        invitados = []
        if inv['tiene_modulo_invitados']:
             invitados = conn.execute("SELECT * FROM pases_invitados WHERE invitacion_id = ? ORDER BY nombre_familia ASC", (inv_id,)).fetchall()
        
        # 3. Traer fotos (si aplica)
        fotos = []
        total_fotos = 0
        if inv['camara_premium']:
            total_fotos = conn.execute("SELECT COUNT(*) FROM fotos_invitados WHERE invitacion_id = ?", (inv_id,)).fetchone()[0]
            fotos = conn.execute("""
                SELECT url FROM fotos_invitados 
                WHERE invitacion_id = ? ORDER BY fecha_creacion DESC LIMIT ? OFFSET ?
            """, (inv_id, PER_PAGE, offset)).fetchall()

        # 4. NUEVO: Traer Buenos Deseos (Si el módulo está activo)
        buenos_deseos = []
        if 'deseos' in config_modulos:
            buenos_deseos = conn.execute("""
                SELECT nombre, mensaje, fecha 
                FROM buenos_deseos 
                WHERE invitacion_id = ? 
                ORDER BY fecha DESC
            """, (inv_id,)).fetchall()

        estado_mesas = obtener_estado_mesas(inv_id)

        return render_template(
            'clientes/dashboard.html',
            inv=inv,
            invitados=invitados,
            fotos=fotos,
            deseos=buenos_deseos,
            modulos=config_modulos,
            nombre_evento=session.get('cliente_nombre'),
            page=page,
            per_page=PER_PAGE,
            total_fotos=total_fotos,
            mesas_status=estado_mesas
        )

    finally:
        conn.close()

# --- Función Auxiliar para checar el candado ---
def edicion_permitida(inv_id, conn):
    inv = conn.execute("SELECT bloquear_edicion_invitados FROM invitaciones WHERE id = ?", (inv_id,)).fetchone()
    return not inv['bloquear_edicion_invitados']

# 1. AGREGAR (Actualizado con función unificada)
@clientes_bp.route('/mi-evento/agregar-invitado', methods=['POST'])
def agregar_invitado_cliente():
    if 'cliente_inv_id' not in session: return redirect(url_for('invitaciones_clientes.login_cliente'))
    
    inv_id = session['cliente_inv_id']
    conn = get_db_connection()
    permitido = edicion_permitida(inv_id, conn)
    conn.close()

    if not permitido:
        flash("La edición está bloqueada para este evento.", "danger")
        return redirect(url_for('invitaciones_clientes.dashboard_cliente'))

    # Llamamos a la función maestra (Modo Crear)
    exito, msj = guardar_pase_bd(inv_id, request.form)
    flash(msj, "success" if exito else "danger")
    
    return redirect(url_for('invitaciones_clientes.dashboard_cliente'))

# 2. EDITAR INVITADO (Actualizado con función unificada)
@clientes_bp.route('/mi-evento/editar-invitado/<int:pase_id>', methods=['POST'])
def editar_invitado_cliente(pase_id):
    if 'cliente_inv_id' not in session: return redirect(url_for('invitaciones_clientes.login_cliente'))
    
    inv_id = session['cliente_inv_id']
    conn = get_db_connection()
    permitido = edicion_permitida(inv_id, conn)
    conn.close()

    if not permitido:
        flash("La edición está bloqueada para este evento.", "danger")
        return redirect(url_for('invitaciones_clientes.dashboard_cliente'))

    # Llamamos a la función maestra pasándole el pase_id (Modo Editar)
    exito, msj = guardar_pase_bd(inv_id, request.form, pase_id)
    flash(msj, "success" if exito else "danger")
    
    return redirect(url_for('invitaciones_clientes.dashboard_cliente'))

    
# 3. ELIMINAR INVITADO (NUEVO)
@clientes_bp.route('/mi-evento/eliminar-invitado/<int:pase_id>', methods=['POST'])
def eliminar_invitado_cliente(pase_id):
    if 'cliente_inv_id' not in session: return redirect(url_for('invitaciones_clientes.login_cliente'))
    
    inv_id = session['cliente_inv_id']
    conn = get_db_connection()
    try:
        if not edicion_permitida(inv_id, conn):
            flash("La edición está bloqueada para este evento.", "danger")
            return redirect(url_for('invitaciones_clientes.dashboard_cliente'))

        conn.execute("DELETE FROM pases_invitados WHERE id = ? AND invitacion_id = ?", (pase_id, inv_id))
        conn.commit()
        flash("Invitado eliminado correctamente.", "success")
    except Exception as e:
        flash("Error al eliminar.", "danger")
    finally:
        conn.close()
    return redirect(url_for('invitaciones_clientes.dashboard_cliente'))


@clientes_bp.route('/mi-evento/descargar-fotos')
def descargar_fotos_cliente():
    if 'cliente_inv_id' not in session:
        return redirect(url_for('invitaciones_clientes.login_cliente'))
        
    inv_id = session['cliente_inv_id']
    conn = get_db_connection()
    try:
        inv = conn.execute("SELECT slug FROM invitaciones WHERE id = ?", (inv_id,)).fetchone()
        fotos = conn.execute("SELECT url FROM fotos_invitados WHERE invitacion_id = ?", (inv_id,)).fetchall()

        if not fotos:
            flash("No hay fotos para descargar.", "warning")
            return redirect(url_for('invitaciones_clientes.dashboard_cliente'))

        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for i, foto in enumerate(fotos):
                try:
                    key = foto['url'].split('.dev/')[-1]
                    obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=key)
                    zf.writestr(f"recuerdo_{i+1}.jpg", obj['Body'].read())
                except:
                    continue

        memory_file.seek(0)
        return send_file(memory_file, mimetype='application/zip', as_attachment=True, download_name=f"fotos_{inv['slug']}.zip")
    finally:
        conn.close()

@clientes_bp.route('/mi-evento/salir')
def logout_cliente():
    session.clear()
    return redirect(url_for('invitaciones_clientes.login_cliente'))

# ==========================================
# CENTRO DE AYUDA DEL PLANNER
# ==========================================
@clientes_bp.route('/socio/ayuda')
@planner_required
def ayuda_planner():
    return render_template('clientes/ayuda_planner.html')