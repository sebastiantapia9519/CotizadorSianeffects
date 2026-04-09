import json
import io
import zipfile
import requests 
from functools import wraps 
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file, current_app
from db import get_db_connection
from helpers import guardar_pase_bd, obtener_estado_mesas
from utils.datetime_utils import hoy_sqlite, hoy_local, ahora_sql

# ==============================================================================
# INICIALIZACION DEL BLUEPRINT
# ==============================================================================
clientes_bp = Blueprint('invitaciones_clientes', __name__)

# Variable estandarizada para comparaciones de fecha a nivel de dia (SQLite)
hoy = hoy_sqlite()

# ==============================================================================
# DECORADORES DE SEGURIDAD
# ==============================================================================
def planner_required(f):
    """
    Decorador que protege las rutas exclusivas para Planners (Socios B2B).
    Verifica no solo la sesion, sino tambien el estado en tiempo real en la BD.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # 1. Verificacion de la cookie de sesion
        if session.get('user_type') != 'planner' or 'planner_id' not in session:
            flash("Debes iniciar sesion como Planner para acceder.", "warning")
            return redirect(url_for('invitaciones_clientes.login_cliente'))
        
        # 2. Verificacion en Base de Datos (Seguridad en tiempo real)
        conn = get_db_connection()
        planner = conn.execute("SELECT estado FROM planners WHERE id = ?", (session['planner_id'],)).fetchone()
        conn.close()

        # Si el administrador suspendio al planner mientras estaba logueado, lo expulsa
        if not planner or planner['estado'] != 'activo':
            session.clear() 
            flash("Tu cuenta no esta activa. Contacta al administrador.", "danger")
            return redirect(url_for('invitaciones_clientes.login_cliente'))

        return f(*args, **kwargs)
    return decorated_function

# ==============================================================================
# RUTAS DE ACCESO (EL EMBUDO UNIFICADO)
# ==============================================================================
@clientes_bp.route('/mi-evento', methods=['GET', 'POST'], strict_slashes=False)
def login_cliente():
    """
    Portal de acceso unico. Separa el trafico dependiendo del formato del codigo:
    - PLAN-XXXXX redirige al Dashboard B2B.
    - SIA-XXXXX redirige al Dashboard del Cliente Final (Novios/Quinceanera).
    """
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
                    if planner['estado'] != 'activo':
                        flash("Esta cuenta se encuentra suspendida.", "danger")
                        return render_template('clientes/login.html')

                    session.clear()
                    session['planner_id'] = planner['id']
                    session['planner_nombre'] = planner['nombre_contacto']
                    session['user_type'] = 'planner'
                    
                    current_app.logger.info(f"PLANNER_LOGIN: Agencia '{planner['nombre_empresa']}' (ID: {planner['id']}) inició sesión.")
                    return redirect(url_for('invitaciones_clientes.dashboard_planner'))
                else:
                    flash("Codigo de Planner no encontrado.", "danger")

            # --- CASO 2: ES UN CLIENTE FINAL (SIA-XXXXX) ---
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
                    
                    # Extraemos el nombre para saludarlo en el dashboard
                    datos = json.loads(inv['datos_cliente_json']) if inv['datos_cliente_json'] else {}
                    session['cliente_nombre'] = datos.get('novios', 'Nuestro Evento')
                    
                    current_app.logger.info(f"CLIENT_LOGIN: Los clientes '{session['cliente_nombre']}' entraron a ver su invitación {inv['slug']}.")
                    return redirect(url_for('invitaciones_clientes.dashboard_cliente'))
                else:
                    flash("Codigo de acceso no valido.", "danger")
        finally:
            conn.close()
            
    return render_template('clientes/login.html')

# ==============================================================================
# DASHBOARD DEL PLANNER (B2B)
# ==============================================================================
@clientes_bp.route('/socio/panel')
@planner_required
def dashboard_planner():
    """
    Calcula en tiempo real los creditos, vigencias y eventos del socio comercial.
    """
    planner_id = session['planner_id']
    conn = get_db_connection()
    
    try:
        # 1. Calculo de Saldo Neto Vigente
        creditos_info = conn.execute("""
            SELECT SUM(cantidad_total - cantidad_usada) as saldo
            FROM planner_paquetes 
            WHERE planner_id = ? 
            AND activo = 1 
            AND fecha_vencimiento > ?
        """, (planner_id, hoy)).fetchone()
        
        saldo = creditos_info['saldo'] if creditos_info['saldo'] else 0

        # 2. Historial de compras/recargas
        paquetes_raw = conn.execute("""
            SELECT id, cantidad_total, cantidad_usada, fecha_vencimiento, notas, fecha_compra
            FROM planner_paquetes
            WHERE planner_id = ? AND activo = 1
            ORDER BY fecha_compra ASC
        """, (planner_id,)).fetchall()

        # Blindaje: Calculo de fechas directo en Python para evitar conflictos Postgres/SQLite
        fecha_hoy = hoy_local()[:10] 
        fecha_limite = ahora_sql(dias=15)[:10] 

        # Alertas de vencimiento proximo (15 dias)
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
        
        # Generacion del estado de cuenta paso a paso
        for p in paquetes_raw:
            cambio = p['cantidad_total'] - p['cantidad_usada']
            saldo_acumulado += cambio
            
            p_dict = dict(p)
            p_dict['cambio'] = cambio
            p_dict['saldo_posterior'] = saldo_acumulado
            
            if not p['fecha_vencimiento'] or cambio < 0:
                p_dict['vence_display'] = "---"
            else:
                p_dict['vence_display'] = p['fecha_vencimiento'][:10]
            
            paquetes_procesados.append(p_dict)

        paquetes_procesados.reverse()

        # 3. Historial de Eventos (Consumo)
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

        # Verificacion del cupon de Demo
        demo_db = conn.execute("SELECT id FROM invitaciones WHERE planner_id = ? AND es_demo = 1", (planner_id,)).fetchone()
        tiene_demo = True if demo_db else False

        return render_template('clientes/dashboard_planner.html', 
                               saldo=saldo, 
                               paquetes=paquetes_procesados,
                               invitaciones=invitaciones,
                               alertas_vencimiento=proximos_vencimientos,
                               hoy_local=hoy_local()[:10],
                               tiene_demo=tiene_demo)
    except Exception as e:
        flash(f"Error cargando dashboard: {e}", "danger")
        return redirect(url_for('invitaciones_clientes.login_cliente'))
    finally:
        conn.close()

# ==============================================================================
# DASHBOARD DEL CLIENTE FINAL (NOVIOS / QUINCEANERA)
# ==============================================================================
@clientes_bp.route('/mi-evento/panel')
def dashboard_cliente():
    """
    Panel donde el cliente puede gestionar a sus propios invitados, ver sus fotos
    y leer los mensajes de buenos deseos.
    """
    if 'cliente_inv_id' not in session:
        return redirect(url_for('invitaciones_clientes.login_cliente'))
        
    inv_id = session['cliente_inv_id']
    conn = get_db_connection()

    # Paginacion para la galeria de fotos (evita sobrecargar el navegador)
    PER_PAGE = 12
    page = request.args.get('page', 1, type=int)
    offset = (page - 1) * PER_PAGE
    
    try:
        # 1. Blindaje: Verificar que el evento no haya sido borrado
        inv = conn.execute("SELECT * FROM invitaciones WHERE id = ?", (inv_id,)).fetchone()
        if not inv:
            session.clear()
            flash("Evento no encontrado. Ingresa tu codigo de nuevo.", "danger")
            return redirect(url_for('invitaciones_clientes.login_cliente'))
        
        config_modulos = json.loads(inv['config_json']) if inv['config_json'] else []
        
        # 2. Modulo de Invitados (RSVP)
        invitados = []
        if inv['tiene_modulo_invitados']:
             invitados = conn.execute("SELECT * FROM pases_invitados WHERE invitacion_id = ? ORDER BY nombre_familia ASC", (inv_id,)).fetchall()
        
        # 3. Modulo de Camara (Fotos)
        fotos = []
        total_fotos = 0
        if inv['camara_premium']:
            total_fotos = conn.execute("SELECT COUNT(*) FROM fotos_invitados WHERE invitacion_id = ?", (inv_id,)).fetchone()[0]
            fotos = conn.execute("""
                SELECT url FROM fotos_invitados 
                WHERE invitacion_id = ? ORDER BY fecha_creacion DESC LIMIT ? OFFSET ?
            """, (inv_id, PER_PAGE, offset)).fetchall()

        # 4. Modulo de Buenos Deseos
        buenos_deseos = []
        if 'deseos' in config_modulos:
            buenos_deseos = conn.execute("""
                SELECT nombre, mensaje, fecha 
                FROM buenos_deseos 
                WHERE invitacion_id = ? 
                ORDER BY fecha DESC
            """, (inv_id,)).fetchall()

        # 5. Estado de mesas
        estado_mesas = obtener_estado_mesas(inv_id)

    except Exception as e:
        current_app.logger.error(f"CLIENT_DASHBOARD_ERROR: Fallo al cargar panel para invitación ID {inv_id} - {e}")
        flash("Ocurrio un error al cargar la informacion de tu evento.", "danger")
        return redirect(url_for('invitaciones_clientes.login_cliente'))
    finally:
        conn.close()

    # El render_template ocurre fuera del bloque try/except para evitar enmascarar errores de Jinja
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

# ==============================================================================
# LOGICA DE CONTROL DE INVITADOS (RSVP CLIENTE)
# ==============================================================================
def edicion_permitida(inv_id, conn):
    """
    Verifica si el Planner/Admin bloqueo la modificacion de la lista de invitados.
    Retorna False si el evento no existe o si la edicion esta bloqueada.
    """
    inv = conn.execute("SELECT bloquear_edicion_invitados FROM invitaciones WHERE id = ?", (inv_id,)).fetchone()
    if not inv: 
        return False
    return not inv['bloquear_edicion_invitados']

@clientes_bp.route('/mi-evento/agregar-invitado', methods=['POST'])
def agregar_invitado_cliente():
    if 'cliente_inv_id' not in session: return redirect(url_for('invitaciones_clientes.login_cliente'))
    
    inv_id = session['cliente_inv_id']
    conn = get_db_connection()
    permitido = edicion_permitida(inv_id, conn)
    conn.close()

    if not permitido:
        flash("La edicion esta bloqueada para este evento.", "danger")
        return redirect(url_for('invitaciones_clientes.dashboard_cliente'))

    # Usamos la funcion maestra unificada
    exito, msj = guardar_pase_bd(inv_id, request.form)
    flash(msj, "success" if exito else "danger")
    
    return redirect(url_for('invitaciones_clientes.dashboard_cliente'))

@clientes_bp.route('/mi-evento/editar-invitado/<int:pase_id>', methods=['POST'])
def editar_invitado_cliente(pase_id):
    if 'cliente_inv_id' not in session: return redirect(url_for('invitaciones_clientes.login_cliente'))
    
    inv_id = session['cliente_inv_id']
    conn = get_db_connection()
    permitido = edicion_permitida(inv_id, conn)
    conn.close()

    if not permitido:
        flash("La edicion esta bloqueada para este evento.", "danger")
        return redirect(url_for('invitaciones_clientes.dashboard_cliente'))

    exito, msj = guardar_pase_bd(inv_id, request.form, pase_id)
    flash(msj, "success" if exito else "danger")
    
    return redirect(url_for('invitaciones_clientes.dashboard_cliente'))

@clientes_bp.route('/mi-evento/eliminar-invitado/<int:pase_id>', methods=['POST'])
def eliminar_invitado_cliente(pase_id):
    if 'cliente_inv_id' not in session: return redirect(url_for('invitaciones_clientes.login_cliente'))
    
    inv_id = session['cliente_inv_id']
    conn = get_db_connection()
    try:
        if not edicion_permitida(inv_id, conn):
            flash("La edicion esta bloqueada para este evento.", "danger")
            return redirect(url_for('invitaciones_clientes.dashboard_cliente'))

        conn.execute("DELETE FROM pases_invitados WHERE id = ? AND invitacion_id = ?", (pase_id, inv_id))
        conn.commit()
        flash("Invitado eliminado correctamente.", "success")
    except Exception as e:
        flash("Error al eliminar.", "danger")
    finally:
        conn.close()
    return redirect(url_for('invitaciones_clientes.dashboard_cliente'))


# ==============================================================================
# DESCARGA DE FOTOS (ZERO DEPENDENCIES)
# ==============================================================================
@clientes_bp.route('/mi-evento/descargar-fotos')
def descargar_fotos_cliente():
    """
    Arma un archivo ZIP en RAM descargando las fotos via HTTP publico.
    Esto evita la dependencia ciclica con boto3/s3_client y asegura el servidor.
    """
    if 'cliente_inv_id' not in session:
        return redirect(url_for('invitaciones_clientes.login_cliente'))
        
    inv_id = session['cliente_inv_id']
    conn = get_db_connection()
    try:
        inv = conn.execute("SELECT slug FROM invitaciones WHERE id = ?", (inv_id,)).fetchone()
        
        # Blindaje: Si la invitacion fue purgada, aborta limpiamente
        if not inv:
            flash("Evento no encontrado.", "danger")
            return redirect(url_for('invitaciones_clientes.dashboard_cliente'))
            
        fotos = conn.execute("SELECT url FROM fotos_invitados WHERE invitacion_id = ?", (inv_id,)).fetchall()

        if not fotos:
            flash("No hay fotos para descargar.", "warning")
            return redirect(url_for('invitaciones_clientes.dashboard_cliente'))

        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for i, foto in enumerate(fotos):
                try:
                    # Descargamos la imagen directo desde la URL final
                    url_descarga = foto['url']
                    respuesta = requests.get(url_descarga, timeout=10)
                    
                    if respuesta.status_code == 200:
                        zf.writestr(f"recuerdo_{i+1}.jpg", respuesta.content)
                except Exception as e:
                    current_app.logger.warning(f"CLIENT_ZIP_WARNING: Fallo HTTP extrayendo foto index {i} para el cliente - {e}")
                    continue

        memory_file.seek(0)
        return send_file(
            memory_file, 
            mimetype='application/zip', 
            as_attachment=True, 
            download_name=f"fotos_{inv['slug']}.zip"
        )
    except Exception as e:
        flash(f"Error general al descargar fotos: {str(e)}", "danger")
        return redirect(url_for('invitaciones_clientes.dashboard_cliente'))
    finally:
        conn.close()

# ==============================================================================
# SALIDA
# ==============================================================================
@clientes_bp.route('/mi-evento/salir')
def logout_cliente():
    """Destruye la sesion del cliente o planner."""
    usuario = session.get('planner_nombre') or session.get('cliente_nombre') or 'Usuario Desconocido'
    current_app.logger.info(f"PORTAL_LOGOUT: '{usuario}' cerró su sesión externa.")
    
    session.clear()
    return redirect(url_for('invitaciones_clientes.login_cliente'))

# ==============================================================================
# CENTRO DE AYUDA B2B
# ==============================================================================
@clientes_bp.route('/socio/ayuda')
@planner_required
def ayuda_planner():
    """Manual de usuario exclusivo para socios comerciales."""
    return render_template('clientes/ayuda_planner.html')