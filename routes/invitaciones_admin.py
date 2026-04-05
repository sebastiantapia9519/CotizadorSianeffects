import json
import re
import io
import uuid
import zipfile
import string
import random
import requests
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session, send_file
from datetime import datetime, timedelta, timezone
from utils.datetime_utils import hoy_local, ahora_sql
from helpers import admin_required, guardar_pase_bd, obtener_estado_mesas
from db import get_db_connection  
from services.cloudflare_service import upload_to_cloudflare, delete_from_cloudflare 

# ==============================================================================
# INICIALIZACION DEL BLUEPRINT
# ==============================================================================
invitaciones_bp = Blueprint('invitaciones_admin', __name__)

# ==============================================================================
# CONFIGURACION MAESTRA DE PLANTILLAS
# ==============================================================================
PLANTILLAS_CONFIG = {
    'rustico': {
        'fuente_titulo': 'Cormorant',
        'fuente_cuerpo': 'Proza Libre',
        'color_acento': '#5d6d5a', 
        'color_fondo': '#f4f1ea',  
        'frase_default': "Hoy celebramos el amor que nos une..."
    },
    'romantico': {
        'fuente_titulo': 'Great Vibes',
        'fuente_cuerpo': 'Montserrat',
        'color_acento': '#d48b9b', 
        'color_fondo': '#fff9f9',
        'frase_default': "Dos corazones, un mismo camino."
    }
}

# ==============================================================================
# FUNCIONES HELPER (UTILERIAS INTERNAS)
# ==============================================================================

def generar_codigo_cliente():
    """Genera un codigo alfanumerico unico para acceso del cliente final."""
    suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"SIA-{suffix}"

def generar_codigo_planner():
    """Genera una contrasena unica de acceso para los Planners (Socios B2B)."""
    suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"PLAN-{suffix}"

def usar_credito_planner(planner_id):
    """
    Logica de facturacion B2B. 
    Busca el paquete mas proximo a vencer que tenga saldo y le descuenta 1 credito.
    """
    conn = get_db_connection()
    now_str = ahora_sql() # Blindaje Postgres: Evaluamos la fecha en Python, no en SQL
    
    paquete = conn.execute("""
        SELECT id, cantidad_total, cantidad_usada 
        FROM planner_paquetes 
        WHERE planner_id = ? AND activo = 1 
        AND fecha_vencimiento > ?
        AND cantidad_usada < cantidad_total
        ORDER BY fecha_vencimiento ASC LIMIT 1
    """, (planner_id, now_str)).fetchone()

    if paquete:
        conn.execute("UPDATE planner_paquetes SET cantidad_usada = cantidad_usada + 1 WHERE id = ?", (paquete['id'],))
        conn.commit()
        conn.close()
        return True 
    
    conn.close()
    return False

# ==============================================================================
# API REST: VALIDACION DE URL EN TIEMPO REAL
# ==============================================================================
@invitaciones_bp.route('/admin/api/verificar-slug', methods=['POST'])
def verificar_slug():
    """Verifica mediante AJAX si la URL personalizada (slug) ya esta registrada."""
    data = request.get_json()
    slug = data.get('slug', '').strip()
    
    slug_limpio = re.sub(r'[^\w\-]+', '', re.sub(r'[\s]+', '-', slug.lower()))
    
    conn = get_db_connection()
    existente = conn.execute("SELECT id FROM invitaciones WHERE slug = ?", (slug_limpio,)).fetchone()
    conn.close()
    
    return jsonify({'disponible': existente is None, 'slug_sugerido': slug_limpio})

# ==============================================================================
# RUTA PRINCIPAL 1: CONSTRUCTOR DE INVITACIONES (CREAR)
# ==============================================================================
@invitaciones_bp.route('/admin/nueva-invitacion', methods=['GET', 'POST'])
def crear_invitacion():
    """Motor principal para ensamblar una nueva invitacion premium."""
    es_admin_master = session.get('role', 0) >= 1
    es_planner = session.get('user_type') == 'planner' and 'planner_id' in session

    if not es_admin_master and not es_planner:
        flash('Acceso denegado. Se requiere una sesion valida.', 'danger')
        return redirect(url_for('invitaciones_clientes.login_cliente'))

    conn = get_db_connection()
    
    if request.method == 'POST':
        try:
            id_creador_registrado = None
            tipo_creador = 'staff'
            planner_id = None
            now_str = ahora_sql()
            
            if es_planner:
                planner_id = session.get('planner_id')
                id_creador_registrado = planner_id
                tipo_creador = 'planner'
                
                paquete_disp = conn.execute("""
                    SELECT id FROM planner_paquetes 
                    WHERE planner_id = ? AND activo = 1 
                    AND fecha_vencimiento > ?
                    AND cantidad_usada < cantidad_total
                    LIMIT 1
                """, (planner_id, now_str)).fetchone()
                
                if not paquete_disp:
                    flash("No tienes creditos disponibles o tus paquetes han vencido.", "danger")
                    conn.close()
                    return redirect(url_for('invitaciones_clientes.dashboard_planner'))

            elif es_admin_master:
                id_creador_registrado = session.get('user_id') 
                tipo_creador = 'admin'

            raw_slug = request.form.get('slug', '').strip()
            slug = re.sub(r'[^\w\-]+', '', re.sub(r'[\s]+', '-', raw_slug.lower()))
            
            slug_existente = conn.execute("SELECT id FROM invitaciones WHERE slug = ?", (slug,)).fetchone()
            if slug_existente:
                flash("Ese enlace ya esta ocupado por otro evento. Por favor, elige uno diferente.", "danger")
                conn.close()
                return redirect(url_for('invitaciones_admin.crear_invitacion'))

            musica_id = request.form.get('musica_id')
            fecha_evento_raw = request.form.get('fecha_evento') 
            
            fecha_evento_limpia = None
            if fecha_evento_raw:
                fecha_str = fecha_evento_raw.replace('T', ' ')[:16] 
                try:
                    fecha_obj = datetime.strptime(fecha_str, '%Y-%m-%d %H:%M')
                    fecha_evento_limpia = fecha_obj.strftime('%Y-%m-%d %H:%M:%S')
                except ValueError:
                    fecha_evento_limpia = fecha_evento_raw
            else:
                fecha_obj = datetime.now()

            if es_planner:
                vigencia = (fecha_obj + timedelta(days=30)).strftime('%Y-%m-%d')
            else:
                vigencia = request.form.get('vigencia')
                if not vigencia:
                     vigencia = (fecha_obj + timedelta(days=30)).strftime('%Y-%m-%d')

            tipo_evento = request.form.get('tipo_evento', 'boda')
            dress_code = request.form.get('dress_code')
            album_url = request.form.get('album_url') 
            camara_premium = 1 if 'camara_premium' in request.form else 0
            color_acentos = request.form.get('color_acentos', '#D4AF37')
            padres_novia = request.form.get('padres_novia')
            padres_novio = request.form.get('padres_novio')
            padrinos = request.form.get('padrinos')
            frase_final = request.form.get('frase_final')
            template_id = request.form.get('template_id')
            tiene_modulo_invitados = 1 if 'modulo_invitados' in request.form else 0
            codigo_cliente = generar_codigo_cliente() 
            bloquear_edicion = 1 if 'bloquear_edicion_invitados' in request.form else 0
            estilo_apertura = request.form.get('estilo_apertura', 'simple')

            es_demo = 1 if request.form.get('action') == 'demo' else 0

            if es_demo and es_planner:
                planner_data = conn.execute("SELECT nombre_empresa FROM planners WHERE id = ?", (planner_id,)).fetchone()
                empresa_str = planner_data['nombre_empresa'] if planner_data else 'agencia'
                
                slug_base = re.sub(r'[^\w\-]+', '', re.sub(r'[\s]+', '-', empresa_str.lower()))
                slug = f"demo-{slug_base}"
                
                while conn.execute("SELECT id FROM invitaciones WHERE slug = ?", (slug,)).fetchone():
                    slug = f"demo-{slug_base}-{str(uuid.uuid4())[:3]}"

                fecha_obj = datetime.now() + timedelta(days=60)
                fecha_evento_limpia = fecha_obj.strftime('%Y-%m-%d %H:%M:%S')
                vigencia = (fecha_obj + timedelta(days=30)).strftime('%Y-%m-%d')

            else:
                raw_slug = request.form.get('slug', '').strip()
                slug = re.sub(r'[^\w\-]+', '', re.sub(r'[\s]+', '-', raw_slug.lower()))
                
                if conn.execute("SELECT id FROM invitaciones WHERE slug = ?", (slug,)).fetchone():
                    flash("Ese enlace ya esta ocupado por otro evento. Elige uno diferente.", "danger")
                    conn.close()
                    return redirect(url_for('invitaciones_admin.crear_invitacion'))

                fecha_evento_raw = request.form.get('fecha_evento') 
                if fecha_evento_raw:
                    fecha_str = fecha_evento_raw.replace('T', ' ')[:16] 
                    try:
                        fecha_obj = datetime.strptime(fecha_str, '%Y-%m-%d %H:%M')
                        fecha_evento_limpia = fecha_obj.strftime('%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        fecha_evento_limpia = fecha_evento_raw
                else:
                    fecha_obj = datetime.now()

                vigencia = request.form.get('vigencia')
                if not vigencia:
                    vigencia = (fecha_obj + timedelta(days=30)).strftime('%Y-%m-%d')

            anios_hist = request.form.getlist('anio_historia[]')
            textos_hist = request.form.getlist('texto_historia[]')
            fotos_nuevas_hist = request.files.getlist('foto_historia_nueva[]')

            historia_lista = []
            for i in range(len(anios_hist)):
                if anios_hist[i] and textos_hist[i]:
                    foto_url = ""
                    if i < len(fotos_nuevas_hist) and fotos_nuevas_hist[i].filename != '':
                        foto_url = upload_to_cloudflare(fotos_nuevas_hist[i], folder=f"invitaciones/{slug}/historia")
                    
                    historia_lista.append({
                        "anio": anios_hist[i],
                        "texto": textos_hist[i],
                        "foto": foto_url
                    })
            
            nombres_tiendas = request.form.getlist('nombre_tienda[]')
            links_tiendas = request.form.getlist('link_tienda[]')
            mesas_regalos = [{'nombre': n, 'url': l} for n, l in zip(nombres_tiendas, links_tiendas) if n and l]

            nombres_hoteles = request.form.getlist('nombre_hotel[]')
            links_hoteles = request.form.getlist('link_hotel[]')
            hoteles_sugeridos = [{'nombre': n, 'url': l} for n, l in zip(nombres_hoteles, links_hoteles) if n and l]

            horas_it = request.form.getlist('hora_itinerario[]')
            acts_it = request.form.getlist('actividad_itinerario[]')
            iconos_it = request.form.getlist('icono_itinerario[]')
            itinerario = [{'hora': h, 'actividad': a, 'icono': i} for h, a, i in zip(horas_it, acts_it, iconos_it) if h and a]

            roles_proto = request.form.getlist('rol_protocolo[]')
            nombres_proto = request.form.getlist('nombres_protocolo[]')
            protocolo_familiar = [{'rol': r, 'nombres': n} for r, n in zip(roles_proto, nombres_proto) if r and n]

            # [NUEVO] Extracción y subida de foto exclusiva para el STD
            activar_std = True if request.form.get('activar_std') == '1' else False
            foto_std = request.files.get('foto_std')
            url_foto_std = upload_to_cloudflare(foto_std, folder=f"invitaciones/{slug}/std") if foto_std and foto_std.filename else None

            datos_cliente = {
                "novios": request.form.get('nombres_novios'),
                "mensaje_bienvenida": request.form.get('mensaje_bienvenida', '').strip(),
                "iniciales": request.form.get('iniciales'),
                "frase": request.form.get('frase'),
                "maps_misa": request.form.get('maps_misa'),
                "maps_fiesta": request.form.get('maps_fiesta'),
                "protocolo": protocolo_familiar,
                "cuenta_bancaria": request.form.get('cuenta_bancaria'),
                "telefono_rsvp": request.form.get('telefono_rsvp'),
                "info_transporte": request.form.get('info_transporte'),
                "itinerario": itinerario,
                "no_ninos": 'no_ninos' in request.form.getlist('orden_items[]'),
                "mensaje_no_ninos": request.form.get('mensaje_no_ninos', '').strip(),
                "mensaje_envio_pases": request.form.get('mensaje_envio_pases', '').strip(),
                
                # [NUEVO] Almacenar la configuracion del STD dentro del JSON
                "activar_std": activar_std,
                "foto_std_url": url_foto_std,
                "std_frase_calendario": request.form.get('std_frase_calendario', 'Save the Date').strip(),
                "std_ubicacion": request.form.get('std_ubicacion', '').strip(),
                "std_estilo_marcador": request.form.get('std_estilo_marcador', 'circulo'),
                "std_incluir_contador": True if request.form.get('std_incluir_contador') else False,
                "std_frase_final": request.form.get('std_frase_final', '').strip()
            }
            
            foto_portada = request.files.get('foto_portada')
            url_portada = upload_to_cloudflare(foto_portada, folder=f"invitaciones/{slug}") if foto_portada and foto_portada.filename else None

            img_fondo = request.files.get('imagen_fondo')
            url_fondo = upload_to_cloudflare(img_fondo, folder=f"invitaciones/{slug}/bg") if img_fondo and img_fondo.filename else None

            fotos_galeria = request.files.getlist('fotos_galeria')
            urls_galeria = [upload_to_cloudflare(f, folder=f"invitaciones/{slug}/galeria") for f in fotos_galeria if f and f.filename]

            orden_items = request.form.getlist('orden_items[]')
            if not orden_items: orden_items = ['inicio', 'evento', 'galeria'] 
            
            orden_items = list(dict.fromkeys(orden_items))

            if camara_premium and 'camara' not in orden_items: orden_items.append('camara')
            if not camara_premium and 'camara' in orden_items: orden_items.remove('camara')

            fecha_creacion_local = hoy_local() 

            conn.execute("""
                INSERT INTO invitaciones 
                (slug, config_json, musica_id, fecha_evento, vigencia, datos_cliente_json, 
                fotos_json, foto_portada_url, estilo_fuente, color_fondo, url_fondo, mesas_regalos_json,
                dress_code, hospedaje_json, album_url, camara_premium, tiene_modulo_invitados, 
                codigo_acceso_cliente, color_acentos, padres_novia, padres_novio, padrinos, 
                frase_final, bloquear_edicion_invitados, template_id, estilo_apertura, 
                tipo_evento, historia_json, planner_id, created_at, es_demo) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                slug, json.dumps(orden_items), musica_id or None, 
                fecha_evento_limpia, vigencia, json.dumps(datos_cliente), 
                json.dumps(urls_finales_galeria if 'urls_finales_galeria' in locals() else urls_galeria), url_portada, request.form.get('estilo_fuente'), 
                request.form.get('color_fondo'), url_fondo, json.dumps(mesas_regalos), 
                dress_code, json.dumps(hoteles_sugeridos), album_url, camara_premium, 
                tiene_modulo_invitados, codigo_cliente, color_acentos, padres_novia, 
                padres_novio, padrinos, frase_final, bloquear_edicion, template_id, 
                estilo_apertura, tipo_evento, json.dumps(historia_lista), planner_id, 
                fecha_creacion_local, es_demo
            ))
            
            if es_planner and not es_demo:
                usar_credito_planner(planner_id)

            conn.commit()
            flash("Invitacion Premium Creada", "success")
            
            if es_planner:
                return redirect(url_for('invitaciones_clientes.dashboard_planner'))
            return redirect(url_for('invitaciones_admin.gestionar_invitaciones'))
            
        except Exception as e:
            conn.rollback() 
            flash(f"Error al crear: Verifique que todos los datos esten completos. (Detalle: {str(e)})", "danger")
            return redirect(url_for('invitaciones_admin.crear_invitacion')) 
        finally:
            conn.close()

    # GET REQUEST RENDERING
    saldo_real = 0
    tiene_demo = False 
    
    if es_planner:
        now_str = ahora_sql()
        saldo_row = conn.execute("""
            SELECT COALESCE(SUM(cantidad_total - cantidad_usada), 0) as s 
            FROM planner_paquetes 
            WHERE planner_id = ? AND activo = 1 
            AND fecha_vencimiento > ?
        """, (session.get('planner_id'), now_str)).fetchone()
        
        if saldo_row:
            saldo_real = saldo_row['s']
            
        demo_db = conn.execute("SELECT id FROM invitaciones WHERE planner_id = ? AND es_demo = 1", (session.get('planner_id'),)).fetchone()
        if demo_db:
            tiene_demo = True

    canciones = conn.execute("SELECT id, nombre_cancion FROM lista_musica WHERE activa = 1 ORDER BY nombre_cancion ASC").fetchall()
    conn.close()
    
    return render_template('invitaciones/crear.html', 
                           inv=None, 
                           datos={},  
                           mesas=[], 
                           hoteles=[], 
                           canciones=canciones, 
                           edit_mode=False,
                           saldo=saldo_real,
                           tiene_demo=tiene_demo)


# ==============================================================================
# API REST: SUBIR MUSICA AL CATALOGO GLOBAL
# ==============================================================================
@invitaciones_bp.route('/admin/api/subir-musica', methods=['POST'])
@admin_required
def api_subir_musica():
    """Permite al Admin subir audios globales para el selector de invitaciones."""
    nombre = request.form.get('nombre')
    archivo = request.files.get('archivo')
    
    if not nombre or not archivo:
        return jsonify({'success': False, 'error': 'Faltan datos o el archivo.'}), 400
        
    if not archivo.content_type.startswith('audio/'):
        return jsonify({'success': False, 'error': 'Solo se permiten archivos de audio.'}), 400

    try:
        url_audio = upload_to_cloudflare(archivo, folder="musica")
        if not url_audio:
            return jsonify({'success': False, 'error': 'Error al subir a la nube.'}), 500

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO lista_musica (nombre_cancion, url_cloudflare, activa) VALUES (?, ?, 1)", (nombre.strip(), url_audio))
        nuevo_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'id': nuevo_id, 'nombre': nombre.strip()})
        
    except Exception as e:
        print(f"Error en API musica: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# FILTROS DE JINJA (Manipulacion de datos en el HTML)
# ==============================================================================
@invitaciones_bp.app_template_filter('from_json')
def from_json(value):
    return json.loads(value)

@invitaciones_bp.app_template_filter('color_contraste')
def color_contraste(hex_color):
    if not hex_color: return '#333333'
    hex_color = hex_color.lstrip('#')
    if len(hex_color) != 6: return '#333333'
    r, g, b = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    luminance = (0.299*r + 0.587*g + 0.114*b)
    return '#fdfbf7' if luminance < 140 else '#333333'

@invitaciones_bp.app_template_filter('fondo_tarjeta')
def fondo_tarjeta(hex_color):
    if not hex_color: return 'rgba(255, 255, 255, 0.7)'
    hex_color = hex_color.lstrip('#')
    if len(hex_color) != 6: return 'rgba(255, 255, 255, 0.7)'
    r, g, b = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    luminance = (0.299*r + 0.587*g + 0.114*b)
    return 'rgba(0, 0, 0, 0.4)' if luminance < 140 else 'rgba(255, 255, 255, 0.7)'


# ==============================================================================
# PANEL ADMIN: GESTIONAR TODAS LAS INVITACIONES
# ==============================================================================
@invitaciones_bp.route('/admin/invitaciones')
@admin_required
def gestionar_invitaciones():
    """Panel general administrativo que lista todas las invitaciones creadas."""
    conn = get_db_connection()
    try:
        invs_db = conn.execute("""
            SELECT i.*, p.nombre_contacto as planner_nombre 
            FROM invitaciones i
            LEFT JOIN planners p ON i.planner_id = p.id
            ORDER BY i.id DESC
        """).fetchall()
        
        invitaciones = []
        for inv in invs_db:
            inv_dict = dict(inv) 
            try:
                inv_dict['datos_cliente'] = json.loads(inv['datos_cliente_json'])
            except:
                inv_dict['datos_cliente'] = {"novios": "Sin Nombre"}
            invitaciones.append(inv_dict)
            
        hoy = hoy_local()
        return render_template('invitaciones/gestionar.html', invitaciones=invitaciones, hoy=hoy)
    except Exception as e:
        flash(f"Error cargando el panel: {str(e)}", "danger")
        return redirect(url_for('admin.dashboard')) 
    finally:
        conn.close()


# ==============================================================================
# RUTA 5: EDITAR INVITACION
# ==============================================================================
@invitaciones_bp.route('/admin/editar-invitacion/<int:id>', methods=['GET', 'POST'])
def editar_invitacion(id):
    """Renderiza el constructor pre-llenado y actualiza registros y multimedia."""
    es_admin_master = session.get('role', 0) >= 1
    es_planner = session.get('user_type') == 'planner' and 'planner_id' in session

    if not es_admin_master and not es_planner:
        flash('Acceso denegado.', 'danger')
        return redirect(url_for('auth.login')) 

    conn = get_db_connection()
    
    inv_seguridad = conn.execute("SELECT planner_id FROM invitaciones WHERE id = ?", (id,)).fetchone()
    if not inv_seguridad:
        flash("Invitacion no encontrada.", "danger")
        conn.close()
        return redirect(url_for('invitaciones_clientes.dashboard_planner') if es_planner else url_for('invitaciones_admin.gestionar_invitaciones'))
        
    if es_planner and str(inv_seguridad['planner_id']) != str(session.get('planner_id')):
        flash("No tienes permiso para editar esta invitacion.", "danger")
        conn.close()
        return redirect(url_for('invitaciones_clientes.dashboard_planner'))

    if request.method == 'POST':
        try:
            inv_old = conn.execute("""
                SELECT slug, tipo_evento, datos_cliente_json, foto_portada_url, fotos_json, url_fondo, codigo_acceso_cliente 
                FROM invitaciones WHERE id=?
            """, (id,)).fetchone()
            
            datos_viejos = json.loads(inv_old['datos_cliente_json']) if inv_old['datos_cliente_json'] else {}

            if es_planner:
                slug = inv_old['slug']
                tipo_evento = inv_old['tipo_evento']
                nombres_novios_final = datos_viejos.get('novios', '') 
            else:
                raw_slug = request.form.get('slug', '').strip()
                slug_limpio = re.sub(r'[^\w\-]+', '', re.sub(r'[\s]+', '-', raw_slug.lower()))
                
                slug_existente = conn.execute("SELECT id FROM invitaciones WHERE slug = ? AND id != ?", (slug_limpio, id)).fetchone()
                if slug_existente:
                    flash("Ese enlace ya esta ocupado.", "danger")
                    conn.close()
                    return redirect(url_for('invitaciones_admin.editar_invitacion', id=id))
                
                slug = slug_limpio
                tipo_evento = request.form.get('tipo_evento', 'boda')
                nombres_novios_final = request.form.get('nombres_novios')

            musica_id = request.form.get('musica_id')
            estilo_fuente = request.form.get('estilo_fuente')
            color_fondo = request.form.get('color_fondo')
            
            fecha_evento_raw = request.form.get('fecha_evento')
            fecha_evento_limpia = None
            
            if fecha_evento_raw:
                fecha_str = fecha_evento_raw.replace('T', ' ')[:16] 
                try:
                    fecha_obj = datetime.strptime(fecha_str, '%Y-%m-%d %H:%M')
                    fecha_evento_limpia = fecha_obj.strftime('%Y-%m-%d %H:%M:%S')
                except ValueError:
                    fecha_evento_limpia = fecha_evento_raw
            else:
                fecha_obj = datetime.now()

            if es_planner:
                vigencia = (fecha_obj + timedelta(days=30)).strftime('%Y-%m-%d')
            else:
                vigencia = request.form.get('vigencia')
                if not vigencia:
                     vigencia = (fecha_obj + timedelta(days=30)).strftime('%Y-%m-%d')
            
            dress_code = request.form.get('dress_code')
            album_url = request.form.get('album_url')
            camara_premium = 1 if 'camara_premium' in request.form else 0
            color_acentos = request.form.get('color_acentos', '#D4AF37')
            padres_novia = request.form.get('padres_novia')
            padres_novio = request.form.get('padres_novio')
            padrinos = request.form.get('padrinos')
            frase_final = request.form.get('frase_final')
            template_id = request.form.get('template_id')
            tiene_modulo_invitados = 1 if 'modulo_invitados' in request.form else 0
            bloquear_edicion = 1 if 'bloquear_edicion_invitados' in request.form else 0
            estilo_apertura = request.form.get('estilo_apertura', 'simple')
            es_demo = 1 if 'es_demo' in request.form else 0

            anios_hist = request.form.getlist('anio_historia[]')
            textos_hist = request.form.getlist('texto_historia[]')
            fotos_actuales_hist = request.form.getlist('foto_historia_actual[]') 
            fotos_nuevas_hist = request.files.getlist('foto_historia_nueva[]')
            
            historia_lista = []
            for i in range(len(anios_hist)):
                if anios_hist[i] and textos_hist[i]:
                    foto_url = ""
                    if i < len(fotos_nuevas_hist) and fotos_nuevas_hist[i] and fotos_nuevas_hist[i].filename:
                        foto_url = upload_to_cloudflare(fotos_nuevas_hist[i], folder=f"invitaciones/{slug}/historia")
                    else:
                        if i < len(fotos_actuales_hist):
                            foto_url = fotos_actuales_hist[i]
                    
                    historia_lista.append({
                        "anio": anios_hist[i],
                        "texto": textos_hist[i],
                        "foto": foto_url
                    })
        
            codigo_cliente = inv_old['codigo_acceso_cliente'] if inv_old else None
            if not codigo_cliente:
                codigo_cliente = generar_codigo_cliente()

            horas_it = request.form.getlist('hora_itinerario[]')
            acts_it = request.form.getlist('actividad_itinerario[]')
            iconos_it = request.form.getlist('icono_itinerario[]')
            itinerario = []
            for h, a, i in zip(horas_it, acts_it, iconos_it):
                if h and a: itinerario.append({'hora': h, 'actividad': a, 'icono': i})

            roles_proto = request.form.getlist('rol_protocolo[]')
            nombres_proto = request.form.getlist('nombres_protocolo[]')
            protocolo_familiar = []
            for rol, nombres in zip(roles_proto, nombres_proto):
                if rol and nombres: protocolo_familiar.append({'rol': rol, 'nombres': nombres})

            # [MODIFICACION] Extraemos y procesamos la foto del STD si se subió una nueva
            activar_std = True if request.form.get('activar_std') == '1' else False
            foto_std = request.files.get('foto_std')
            if foto_std and foto_std.filename != '':
                url_foto_std = upload_to_cloudflare(foto_std, folder=f"invitaciones/{slug}/std")
            else:
                url_foto_std = datos_viejos.get('foto_std_url') # Mantenemos la que ya tenía

            datos_cliente = {
                "novios": request.form.get('nombres_novios') if not es_planner else nombres_novios_final,
                "mensaje_bienvenida": request.form.get('mensaje_bienvenida', '').strip(),
                "iniciales": request.form.get('iniciales'),
                "frase": request.form.get('frase'),
                "maps_misa": request.form.get('maps_misa'),
                "maps_fiesta": request.form.get('maps_fiesta'),
                "protocolo": protocolo_familiar,
                "cuenta_bancaria": request.form.get('cuenta_bancaria'),
                "telefono_rsvp": request.form.get('telefono_rsvp'),
                "info_transporte": request.form.get('info_transporte'),
                "itinerario": itinerario,
                "no_ninos": 'no_ninos' in request.form.getlist('orden_items[]'),
                "mensaje_no_ninos": request.form.get('mensaje_no_ninos', '').strip(),
                "mensaje_envio_pases": request.form.get('mensaje_envio_pases', '').strip(),
                
                # [NUEVO] Actualizamos los datos del STD en la base de datos
                "activar_std": activar_std,
                "foto_std_url": url_foto_std,
                "std_frase_calendario": request.form.get('std_frase_calendario', 'Save the Date').strip(),
                "std_ubicacion": request.form.get('std_ubicacion', '').strip(),
                "std_estilo_marcador": request.form.get('std_estilo_marcador', 'circulo'),
                "std_incluir_contador": True if request.form.get('std_incluir_contador') else False,
                "std_frase_final": request.form.get('std_frase_final', '').strip()
            }
            
            nombres_tiendas = request.form.getlist('nombre_tienda[]')
            links_tiendas = request.form.getlist('link_tienda[]')
            mesas_regalos = [{'nombre': n, 'url': l} for n, l in zip(nombres_tiendas, links_tiendas) if n and l]
            
            nombres_hoteles = request.form.getlist('nombre_hotel[]')
            links_hoteles = request.form.getlist('link_hotel[]')
            hoteles_sugeridos = [{'nombre': n, 'url': l} for n, l in zip(nombres_hoteles, links_hoteles) if n and l]

            orden_items = request.form.getlist('orden_items[]')
            if not orden_items: orden_items = ['inicio', 'evento', 'galeria']
            orden_items = list(dict.fromkeys(orden_items))

            if camara_premium and 'camara' not in orden_items: orden_items.append('camara')
            if not camara_premium and 'camara' in orden_items: orden_items.remove('camara')

            foto_portada = request.files.get('foto_portada')
            url_portada = upload_to_cloudflare(foto_portada, folder=f"invitaciones/{slug}") if foto_portada and foto_portada.filename != '' else inv_old['foto_portada_url']

            img_fondo = request.files.get('imagen_fondo')
            url_fondo = upload_to_cloudflare(img_fondo, folder=f"invitaciones/{slug}/bg") if img_fondo and img_fondo.filename != '' else inv_old['url_fondo']

            fotos_viejas = json.loads(inv_old['fotos_json']) if inv_old and inv_old['fotos_json'] else []
            fotos_nuevas = request.files.getlist('fotos_galeria')
            
            urls_galeria_nuevas = []
            for f in fotos_nuevas:
                if f and f.filename != '':
                    urls_galeria_nuevas.append(upload_to_cloudflare(f, folder=f"invitaciones/{slug}/galeria"))
            
            urls_finales_galeria = fotos_viejas + urls_galeria_nuevas

            conn.execute("""
                UPDATE invitaciones SET 
                slug=?, config_json=?, musica_id=?, fecha_evento=?, vigencia=?, datos_cliente_json=?, 
                fotos_json=?, foto_portada_url=?, estilo_fuente=?, color_fondo=?, url_fondo=?, mesas_regalos_json=?,
                dress_code=?, hospedaje_json=?, album_url=?, camara_premium=?, color_acentos=?,
                padres_novia=?, padres_novio=?, padrinos=?, frase_final=?, template_id=?,
                tiene_modulo_invitados=?, codigo_acceso_cliente=?, bloquear_edicion_invitados=?, estilo_apertura=?,
                tipo_evento=?, historia_json=?, es_demo=?
                WHERE id=?
            """, (
                slug, json.dumps(orden_items), musica_id or None, fecha_evento_limpia, vigencia, json.dumps(datos_cliente), 
                json.dumps(urls_finales_galeria), url_portada, estilo_fuente, color_fondo, url_fondo, json.dumps(mesas_regalos),
                dress_code, json.dumps(hoteles_sugeridos), album_url, camara_premium, color_acentos,
                padres_novia, padres_novio, padrinos, frase_final, template_id, tiene_modulo_invitados,
                codigo_cliente, bloquear_edicion, estilo_apertura,
                tipo_evento, json.dumps(historia_lista), es_demo,
                id                
            ))
            conn.commit()
            flash("Invitacion actualizada exitosamente.", "success")
            
            if es_planner:
                return redirect(url_for('invitaciones_clientes.dashboard_planner'))
            return redirect(url_for('invitaciones_admin.gestionar_invitaciones'))
            
        except Exception as e:
            conn.rollback()
            flash(f"Error al actualizar. Verifique los datos. (Detalle: {str(e)})", "danger")
            return redirect(url_for('invitaciones_admin.editar_invitacion', id=id)) 
        finally:
            conn.close()

    inv = conn.execute("SELECT * FROM invitaciones WHERE id = ?", (id,)).fetchone()
    canciones = conn.execute("SELECT id, nombre_cancion FROM lista_musica WHERE activa = 1 ORDER BY nombre_cancion ASC").fetchall()
    
    saldo_real = 0
    if es_planner:
        now_str = ahora_sql()
        saldo_row = conn.execute("""
            SELECT COALESCE(SUM(cantidad_total - cantidad_usada), 0) as s 
            FROM planner_paquetes 
            WHERE planner_id = ? AND activo = 1 
            AND fecha_vencimiento > ?
        """, (session.get('planner_id'), now_str)).fetchone()
        if saldo_row:
            saldo_real = saldo_row['s']
            
    conn.close()

    if not inv:
        flash("Invitacion no encontrada.", "danger")
        if es_planner: return redirect(url_for('invitaciones_clientes.dashboard_planner'))
        return redirect(url_for('invitaciones_admin.gestionar_invitaciones'))

    inv = dict(inv)
    datos = json.loads(inv['datos_cliente_json']) if inv['datos_cliente_json'] else {}
    mesas = json.loads(inv['mesas_regalos_json']) if inv['mesas_regalos_json'] else []
    hoteles = json.loads(inv['hospedaje_json']) if inv['hospedaje_json'] else []
    
    return render_template('invitaciones/crear.html', 
                           inv=inv, 
                           datos=datos, 
                           mesas=mesas,
                           hoteles=hoteles,
                           canciones=canciones, 
                           edit_mode=True,
                           saldo=saldo_real)


# ==============================================================================
# ELIMINAR INVITACION COMPLETA
# ==============================================================================
@invitaciones_bp.route('/admin/eliminar-invitacion/<int:id>', methods=['POST'])
@admin_required
def eliminar_invitacion(id):
    """Purga la invitacion y libera todo el almacenamiento asociado en R2."""
    conn = get_db_connection()
    try:
        # [MODIFICACION] Agregamos 'datos_cliente_json' al SELECT para purgar la foto del STD si existe.
        inv = conn.execute("SELECT foto_portada_url, url_fondo, fotos_json, datos_cliente_json FROM invitaciones WHERE id = ?", (id,)).fetchone()
        
        if inv:
            if inv['foto_portada_url']: delete_from_cloudflare(inv['foto_portada_url'])
            if inv['url_fondo']: delete_from_cloudflare(inv['url_fondo'])
            if inv['fotos_json']:
                fotos_galeria = json.loads(inv['fotos_json'])
                for foto_url in fotos_galeria: delete_from_cloudflare(foto_url)

            # [NUEVO] Eliminación de la foto exclusiva del Save the Date
            datos_cliente = json.loads(inv['datos_cliente_json']) if inv['datos_cliente_json'] else {}
            if datos_cliente.get('foto_std_url'):
                delete_from_cloudflare(datos_cliente['foto_std_url'])

            fotos_invitados = conn.execute("SELECT url FROM fotos_invitados WHERE invitacion_id = ?", (id,)).fetchall()
            for foto in fotos_invitados:
                if foto['url']: delete_from_cloudflare(foto['url'])
            
            conn.execute("DELETE FROM fotos_invitados WHERE invitacion_id = ?", (id,))

        conn.execute("DELETE FROM invitaciones WHERE id = ?", (id,))
        conn.commit()
        flash("Invitacion y fotos eliminadas permanentemente.", "success")
        
    except Exception as e:
        conn.rollback()
        flash(f"Error al eliminar: {str(e)}", "danger")
    finally:
        conn.close()
        
    return redirect(url_for('invitaciones_admin.gestionar_invitaciones'))


# ==============================================================================
# RUTA PUBLICA: RENDERIZADO DEL EVENTO (/invitacion/slug)
# ==============================================================================
@invitaciones_bp.route('/invitacion/<slug>')
@invitaciones_bp.route('/xv/<slug>')
@invitaciones_bp.route('/save-the-date/<slug>')
@invitaciones_bp.route('/std/<slug>') 
@invitaciones_bp.route('/fiesta/<slug>') 
def ver_invitacion(slug):
    """Renderiza el front-end final que ven los invitados (Boda, XV o STD)."""
    conn = get_db_connection()
    try:
        inv = conn.execute("""
            SELECT i.*, m.url_cloudflare as musica_url 
            FROM invitaciones i
            LEFT JOIN lista_musica m ON i.musica_id = m.id
            WHERE i.slug = ?
        """, (slug,)).fetchone()
        
        if not inv:
            return "<h1>404 - Invitación no encontrada</h1>", 404

        inv = dict(inv)

        # Blindaje Date String Lexicográfico (Seguro en SQLite y Postgres)
        hoy_str = str(hoy_local())[:10]
        vigencia_str = str(inv['vigencia'])[:10] if inv['vigencia'] else None

        if vigencia_str and hoy_str > vigencia_str:
            return render_template('invitaciones/expirada.html')

        codigo_pase = request.args.get('pass') 
        datos_pase = None
        if codigo_pase:
            datos_pase = conn.execute("""
                SELECT * FROM pases_invitados 
                WHERE invitacion_id = ? AND codigo_qr_unique = ?
            """, (inv['id'], codigo_pase)).fetchone()

        config = json.loads(inv['config_json'])
        datos = json.loads(inv['datos_cliente_json'])
        fotos = json.loads(inv['fotos_json'])

        buenos_deseos = conn.execute("""
            SELECT nombre, mensaje, fecha 
            FROM buenos_deseos 
            WHERE invitacion_id = ? 
            ORDER BY fecha DESC
        """, (inv['id'],)).fetchall()

        template_colors = {}
        if inv['template_id'] and inv['template_id'] != 'personalizado':
            template = PLANTILLAS_CONFIG.get(inv['template_id'])
            if template:
                template_colors = {
                    'template_color_acento': template['color_acento'],
                    'template_color_fondo': template['color_fondo']
                }

        # [MODIFICACION] --- SELECCIÓN DINÁMICA DE PLANTILLA BASADA EN LA URL ---
        if request.path.startswith('/std/') or request.path.startswith('/save-the-date/'):
            # Si entran por el link del STD, revisamos si el Planner lo tiene activado
            if not datos.get('activar_std'):
                return "<h1>404 - El Save the Date no está activo para este evento.</h1>", 404
            
            plantilla_render = 'invitaciones/std.html'
            
            # Ajustamos la foto principal a mandar a la vista (para que si hay foto_std la prefiera)
            inv['foto_portada_url'] = datos.get('foto_std_url') if datos.get('foto_std_url') else inv['foto_portada_url']
            
        elif inv['tipo_evento'] == 'xv':
            plantilla_render = 'invitaciones/xv.html'
        elif inv['tipo_evento'] == 'otro':
            plantilla_render = 'invitaciones/fiesta.html'
        else:
            plantilla_render = 'invitaciones/base_boda.html'
        
        return render_template(
            plantilla_render,
            inv=inv,
            config=config,
            datos=datos,
            fotos=fotos,
            datos_pase=datos_pase,
            buenos_deseos=buenos_deseos,
            historia_lista=json.loads(inv['historia_json']) if inv['historia_json'] else [],
            **template_colors
        )

    except Exception as e:
        return f"Error: {str(e)}", 500
    finally:
        conn.close()


# ==============================================================================
# GESTION DE ALBUM DE CAMARA (DESCARGAR FOTOS)
# ==============================================================================
@invitaciones_bp.route('/admin/ver-fotos/<int:id>')
def ver_fotos_invitados(id):
    """Galeria administrativa de las fotos que tomaron los invitados."""
    es_admin_master = session.get('role', 0) >= 1
    es_planner = session.get('user_type') == 'planner' and 'planner_id' in session

    if not es_admin_master and not es_planner:
        return redirect(url_for('auth.login'))

    conn = get_db_connection()
    try:
        inv = conn.execute("SELECT slug, datos_cliente_json, planner_id FROM invitaciones WHERE id = ?", (id,)).fetchone()
        
        if es_planner and str(inv['planner_id']) != str(session.get('planner_id')):
            flash("Permiso denegado.", "danger")
            return redirect(url_for('invitaciones_clientes.dashboard_planner'))

        datos = json.loads(inv['datos_cliente_json'])
        fotos = conn.execute("""
            SELECT * FROM fotos_invitados 
            WHERE invitacion_id = ? 
            ORDER BY fecha_creacion DESC
        """, (id,)).fetchall()
        
        return render_template('invitaciones/galeria_admin.html', inv=inv, datos=datos, fotos=fotos, inv_id=id)
    except Exception as e:
        flash(f"Error al cargar la galeria: {str(e)}", "danger")
        return redirect(url_for('invitaciones_clientes.dashboard_planner') if es_planner else url_for('invitaciones_admin.gestionar_invitaciones'))
    finally:
        conn.close()

@invitaciones_bp.route('/admin/descargar-rollo/<int:id>')
def descargar_rollo_zip(id):
    """
    Genera un archivo .zip en memoria RAM extrayendo las fotos de la URL publica.
    Blindaje: Evita usar la libreria boto3 y resuelve riesgos de importaciones circulares.
    """
    es_admin_master = session.get('role', 0) >= 1
    es_planner = session.get('user_type') == 'planner' and 'planner_id' in session

    if not es_admin_master and not es_planner:
        return redirect(url_for('auth.login'))

    conn = get_db_connection()
    try:
        inv = conn.execute("SELECT slug, planner_id FROM invitaciones WHERE id = ?", (id,)).fetchone()
        if es_planner and str(inv['planner_id']) != str(session.get('planner_id')):
            flash("Permiso denegado.", "danger")
            return redirect(url_for('invitaciones_clientes.dashboard_planner'))

        fotos = conn.execute("SELECT url FROM fotos_invitados WHERE invitacion_id = ?", (id,)).fetchall()

        if not fotos:
            flash("No hay fotos para descargar.", "warning")
            return redirect(url_for('invitaciones_admin.ver_fotos_invitados', id=id))

        memory_file = io.BytesIO() 
        fotos_anadidas = 0
        
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for i, foto in enumerate(fotos):
                try:
                    # BLINDAJE R2: Descargamos por HTTP directamente de la URL publica
                    # Evitamos usar s3_client que genera dependencias ciclicas
                    respuesta = requests.get(foto['url'], timeout=10)
                    if respuesta.status_code == 200:
                        zf.writestr(f"foto_{i+1}.jpg", respuesta.content)
                        fotos_anadidas += 1
                except Exception as e:
                    print(f"Error HTTP extrayendo foto {i}: {e}")
                    continue

        if fotos_anadidas == 0:
            flash("No se pudo extraer ninguna imagen.", "danger")
            return redirect(url_for('invitaciones_admin.ver_fotos_invitados', id=id))

        memory_file.seek(0)
        return send_file(
            memory_file,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f"fotos_{inv['slug']}.zip"
        )

    except Exception as e:
        flash(f"Error: {str(e)}", "danger")
        return redirect(url_for('invitaciones_admin.ver_fotos_invitados', id=id))
    finally:
        conn.close()

# ==============================================================================
# GESTION DE PASES E INVITADOS VIP (RSVP)
# ==============================================================================
@invitaciones_bp.route('/admin/invitacion/<int:id>/invitados', methods=['GET', 'POST'])
def gestionar_pases(id):
    """Crea o edita pases VIP y genera QRs."""
    es_admin_master = session.get('role', 0) >= 1
    es_planner = session.get('user_type') == 'planner' and 'planner_id' in session

    if not es_admin_master and not es_planner:
        flash('Acceso denegado.', 'danger')
        return redirect(url_for('auth.login'))

    conn = get_db_connection()
    
    inv_seguridad = conn.execute("SELECT planner_id FROM invitaciones WHERE id = ?", (id,)).fetchone()
    if not inv_seguridad:
        flash("Invitacion no encontrada.", "danger")
        conn.close()
        return redirect(url_for('invitaciones_clientes.dashboard_planner') if es_planner else url_for('invitaciones_admin.gestionar_invitaciones'))
        
    if es_planner and str(inv_seguridad['planner_id']) != str(session.get('planner_id')):
        flash("Permiso denegado.", "danger")
        conn.close()
        return redirect(url_for('invitaciones_clientes.dashboard_planner'))

    if request.method == 'POST':
        exito, msj = guardar_pase_bd(id, request.form)
        if exito:
            flash(msj, "success")
        else:
            flash(f"Error al guardar: {msj}", "danger")

    inv = conn.execute("SELECT slug, id, codigo_acceso_cliente, datos_cliente_json FROM invitaciones WHERE id = ?", (id,)).fetchone()
    invitados = conn.execute("SELECT * FROM pases_invitados WHERE invitacion_id = ? ORDER BY id DESC", (id,)).fetchall()
    conn.close()

    estado_mesas = obtener_estado_mesas(id)
    return render_template('invitaciones/pases_admin.html', inv=inv, invitados=invitados, mesas_status=estado_mesas)


@invitaciones_bp.route('/admin/invitacion/<int:inv_id>/eliminar-pase/<int:pase_id>', methods=['POST'])
def eliminar_pase(inv_id, pase_id):
    """Revoca un pase desde la base de datos."""
    es_admin_master = session.get('role', 0) >= 1
    es_planner = session.get('user_type') == 'planner' and 'planner_id' in session

    if not es_admin_master and not es_planner: return redirect(url_for('auth.login'))

    conn = get_db_connection()
    try:
        inv_seguridad = conn.execute("SELECT planner_id FROM invitaciones WHERE id = ?", (inv_id,)).fetchone()
        if es_planner and str(inv_seguridad['planner_id']) != str(session.get('planner_id')):
            flash("Permiso denegado.", "danger")
            return redirect(url_for('invitaciones_clientes.dashboard_planner'))

        conn.execute("DELETE FROM pases_invitados WHERE id = ? AND invitacion_id = ?", (pase_id, inv_id))
        conn.commit()
        flash("Pase revocado exitosamente.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Error al revocar: {str(e)}", "danger")
    finally:
        conn.close()
        
    return redirect(url_for('invitaciones_admin.gestionar_pases', id=inv_id))

# ==============================================================================
# GESTION B2B: SOCIOS COMERCIALES (PLANNERS)
# ==============================================================================
@invitaciones_bp.route('/admin/socios', methods=['GET', 'POST'])
@admin_required
def gestionar_socios():
    """Lista el directorio de agencias y calcula la vigencia de creditos de forma segura."""
    conn = get_db_connection()
    
    if request.method == 'POST':
        nombre = request.form.get('nombre')
        empresa = request.form.get('empresa')
        telefono = request.form.get('telefono')
        codigo_plan = generar_codigo_planner() 
        
        try:
            conn.execute("""
                INSERT INTO planners (nombre_contacto, nombre_empresa, telefono, codigo_acceso_planner)
                VALUES (?, ?, ?, ?)
            """, (nombre, empresa, telefono, codigo_plan))
            conn.commit()
            flash(f"Socio {nombre} registrado. Codigo: {codigo_plan}", "success")
        except Exception as e:
            flash(f"Error al registrar socio: {e}", "danger")

    now_str = ahora_sql()
    socios = conn.execute("""
        SELECT p.*, 
               COALESCE(SUM(pp.cantidad_total - pp.cantidad_usada), 0) as creditos_disponibles
        FROM planners p
        LEFT JOIN planner_paquetes pp 
          ON p.id = pp.planner_id 
          AND pp.activo = 1 
          AND pp.fecha_vencimiento > ?
        GROUP BY p.id
        ORDER BY p.created_at DESC
    """, (now_str,)).fetchall()
    
    conn.close()
    return render_template('invitaciones/admin_socios.html', socios=socios)

@invitaciones_bp.route('/admin/socios/cargar-paquete', methods=['POST'])
@admin_required
def cargar_paquete():
    """Recarga inventario de paquetes calculando vencimientos de forma dinamica."""
    planner_id = request.form.get('planner_id')
    cantidad = int(request.form.get('cantidad', 0))
    
    if cantidad <= 3:
        vencimiento = ahora_sql(meses=1)
    elif cantidad <= 9:
        vencimiento = ahora_sql(meses=3)
    elif cantidad <= 15:
        vencimiento = ahora_sql(meses=6)
    else:
        vencimiento = ahora_sql(meses=12) 
    
    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT INTO planner_paquetes (planner_id, cantidad_total, fecha_vencimiento)
            VALUES (?, ?, ?)
        """, (planner_id, cantidad, vencimiento))
        conn.commit()
        flash(f"Se cargaron {cantidad} creditos exitosamente. Vigencia hasta {vencimiento[:10]}.", "success")
    except Exception as e:
        flash(f"Error al cargar creditos: {e}", "danger")
    finally:
        conn.close()
        
    return redirect(url_for('invitaciones_admin.gestionar_socios'))

@invitaciones_bp.route('/editar_planner', methods=['POST'])
@admin_required
def editar_planner():
    planner_id = request.form.get('planner_id')
    nombre = request.form.get('nombre')
    empresa = request.form.get('empresa')
    telefono = request.form.get('telefono')

    conn = get_db_connection()
    conn.execute('UPDATE planners SET nombre_contacto = ?, nombre_empresa = ?, telefono = ? WHERE id = ?', (nombre, empresa, telefono, planner_id))
    conn.commit()
    conn.close()

    flash(f'Perfil de {empresa} actualizado.', 'success')
    return redirect(url_for('invitaciones_admin.gestionar_socios'))

@invitaciones_bp.route('/admin/socios/eliminar', methods=['POST'])
@admin_required
def eliminar_planner():
    """Purga completa de Planners, liberando todo su espacio en Cloudflare R2."""
    planner_id = request.form.get('planner_id')
    now_str = ahora_sql()
    
    conn = get_db_connection()
    try:
        saldo_row = conn.execute("""
            SELECT COALESCE(SUM(cantidad_total - cantidad_usada), 0) as saldo
            FROM planner_paquetes 
            WHERE planner_id = ? AND activo = 1 AND fecha_vencimiento > ?
        """, (planner_id, now_str)).fetchone()
        
        saldo = saldo_row['saldo'] if saldo_row else 0

        invitaciones_activas = conn.execute("""
            SELECT COUNT(id) as total_activas
            FROM invitaciones
            WHERE planner_id = ? AND vigencia >= ? AND es_demo = 0
        """, (planner_id, now_str[:10])).fetchone()['total_activas']

        if saldo > 0 or invitaciones_activas > 0:
            conn.execute("UPDATE planners SET estado = 'suspendido' WHERE id = ?", (planner_id,))
            conn.commit()
            flash(f"Cuenta suspendida. No se puede eliminar: tiene {saldo} creditos o {invitaciones_activas} eventos vigentes.", "warning")
            return redirect(url_for('invitaciones_admin.gestionar_socios'))

        # [MODIFICACION] Extraemos también 'datos_cliente_json' para borrar fotos del STD si existen
        invs_a_borrar = conn.execute("SELECT id, foto_portada_url, url_fondo, fotos_json, datos_cliente_json FROM invitaciones WHERE planner_id = ?", (planner_id,)).fetchall()
        
        for inv in invs_a_borrar:
            inv_id = inv['id']
            
            if inv['foto_portada_url']: delete_from_cloudflare(inv['foto_portada_url'])
            if inv['url_fondo']: delete_from_cloudflare(inv['url_fondo'])
            if inv['fotos_json']:
                fotos_galeria = json.loads(inv['fotos_json'])
                for foto_url in fotos_galeria: delete_from_cloudflare(foto_url)
                
            # [NUEVO] Purga de la foto de STD de Cloudflare
            datos_cliente = json.loads(inv['datos_cliente_json']) if inv['datos_cliente_json'] else {}
            if datos_cliente.get('foto_std_url'):
                delete_from_cloudflare(datos_cliente['foto_std_url'])
                
            fotos_invitados = conn.execute("SELECT url FROM fotos_invitados WHERE invitacion_id = ?", (inv_id,)).fetchall()
            for foto in fotos_invitados:
                if foto['url']: delete_from_cloudflare(foto['url'])

            conn.execute("DELETE FROM fotos_invitados WHERE invitacion_id = ?", (inv_id,))
            conn.execute("DELETE FROM pases_invitados WHERE invitacion_id = ?", (inv_id,))
            conn.execute("DELETE FROM buenos_deseos WHERE invitacion_id = ?", (inv_id,)) 

            conn.execute("DELETE FROM invitaciones WHERE id = ?", (inv_id,))

        conn.execute("DELETE FROM planner_paquetes WHERE planner_id = ?", (planner_id,))
        conn.execute("DELETE FROM planners WHERE id = ?", (planner_id,))
        
        conn.commit()
        flash("Planner y todo su rastro (BD e Imagenes) eliminados permanentemente.", "success")
            
    except Exception as e:
        conn.rollback() 
        flash(f"Error critico al intentar eliminar: {str(e)}", "danger")
    finally:
        conn.close()

    return redirect(url_for('invitaciones_admin.gestionar_socios'))

@invitaciones_bp.route('/ajustar_saldo', methods=['POST'])
@admin_required
def ajustar_saldo():
    """Genera ajustes de creditos arbitrarios registrando una huella de auditoria."""
    planner_id = request.form.get('planner_id')
    ajuste = int(request.form.get('ajuste'))
    motivo = request.form.get('motivo')
    
    fecha_hoy = ahora_sql()
    fecha_venc = ahora_sql(meses=12)

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO planner_paquetes (planner_id, cantidad_total, fecha_compra, fecha_vencimiento, notas)
        VALUES (?, ?, ?, ?, ?)
    ''', (planner_id, ajuste, fecha_hoy, fecha_venc, f"AJUSTE MANUAL: {motivo}"))
    conn.commit()
    conn.close()
    
    flash('Saldo ajustado.', 'warning')
    return redirect(url_for('invitaciones_admin.gestionar_socios'))

@invitaciones_bp.route('/regenerar_codigo', methods=['POST'])
@admin_required
def regenerar_codigo():
    planner_id = request.form.get('planner_id')
    nuevo_codigo = "PLAN-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
    
    conn = get_db_connection()
    conn.execute('UPDATE planners SET codigo_acceso_planner = ? WHERE id = ?', (nuevo_codigo, planner_id))
    conn.commit()
    conn.close()

    flash(f'Nuevo acceso: {nuevo_codigo}', 'success')
    return redirect(url_for('invitaciones_admin.gestionar_socios'))

@invitaciones_bp.route('/suspender_planner', methods=['POST'])
@admin_required
def suspender_planner():
    planner_id = request.form.get('planner_id')
    accion = request.form.get('accion') 
    
    conn = get_db_connection()
    
    if accion == 'activar':
        conn.execute("UPDATE planners SET estado = 'activo' WHERE id = ?", (planner_id,))
        flash('Socio activado correctamente.', 'success')
    else:
        conn.execute("UPDATE planners SET estado = 'suspendido' WHERE id = ?", (planner_id,))
        flash('Socio suspendido. Se le negara el acceso.', 'danger')
        
    conn.commit()
    conn.close()
    return redirect(url_for('invitaciones_admin.gestionar_socios'))

@invitaciones_bp.route('/api/socios/<int:id>/auditoria')
@admin_required
def api_auditoria_planner(id):
    """API para consultar el historial de compras y gastos de un socio."""
    conn = get_db_connection()
    try:
        now_str = ahora_sql() 
        
        saldo_row = conn.execute("""
            SELECT COALESCE(SUM(cantidad_total - cantidad_usada), 0) as saldo
            FROM planner_paquetes 
            WHERE planner_id = ? AND activo = 1 AND fecha_vencimiento > ?
        """, (id, now_str)).fetchone()
        saldo = saldo_row['saldo'] if saldo_row else 0

        movs_db = conn.execute("SELECT * FROM planner_paquetes WHERE planner_id = ? ORDER BY fecha_compra DESC", (id,)).fetchall()
        
        movimientos = []
        saldo_paquetes = 0
        saldo_ajustes = 0

        for m in movs_db:
            m_dict = dict(m)
            
            ct = m_dict.get('cantidad_total', 0)
            cu = m_dict.get('cantidad_usada', 0)
            restantes = ct - cu 
            
            fecha_venc = m_dict.get('fecha_vencimiento')
            
            expirado = False
            if m_dict.get('activo') == 0:
                expirado = True
            elif fecha_venc and str(fecha_venc) < now_str:
                expirado = True

            m_dict['fecha_compra'] = str(m_dict.get('fecha_compra'))[:10] if m_dict.get('fecha_compra') else ''
            m_dict['restantes'] = restantes
            m_dict['expirado'] = expirado
            
            movimientos.append(m_dict)
            
            if not expirado:
                notas = str(m_dict.get('notas', '')).lower()
                if ct < 0 or 'ajuste' in notas or 'reembolso' in notas or 'error' in notas:
                    saldo_ajustes += restantes
                else:
                    saldo_paquetes += restantes

        cons_db = conn.execute("SELECT id, slug, created_at, fecha_evento, datos_cliente_json FROM invitaciones WHERE planner_id = ? ORDER BY id DESC", (id,)).fetchall()
        consumos = []
        for c in cons_db:
            c_dict = dict(c)
            c_dict['created_at'] = str(c_dict.get('created_at') or c_dict.get('fecha_evento'))[:10]
            try:
                datos = json.loads(c_dict['datos_cliente_json']) if c_dict.get('datos_cliente_json') else {}
                c_dict['nombres'] = datos.get('novios', 'Sin nombre')
            except:
                c_dict['nombres'] = 'Error al leer datos'
                
            c_dict.pop('datos_cliente_json', None)
            consumos.append(c_dict)

        return jsonify({
            'success': True, 
            'saldo': saldo, 
            'saldo_paquetes': saldo_paquetes,
            'saldo_ajustes': saldo_ajustes,
            'movimientos': movimientos, 
            'consumos': consumos
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
    finally:
        conn.close()

# ==============================================================================
# ELIMINAR IMAGENES AL VUELO DESDE EL FORMULARIO
# ==============================================================================
@invitaciones_bp.route('/admin/invitacion/<int:id>/eliminar-imagen/<string:tipo_imagen>', methods=['POST'])
def eliminar_imagen_invitacion(id, tipo_imagen):
    """
    Elimina archivos de Cloudflare R2 usando el servicio importado para
    mantener la logica estandarizada.
    """
    es_admin_master = session.get('role', 0) >= 1
    es_planner = session.get('user_type') == 'planner' and 'planner_id' in session
    if not es_admin_master and not es_planner: return jsonify({"success": False, "error": "Acceso denegado"}), 403

    import json # Asegúrate de tener json importado arriba en tu archivo
    conn = get_db_connection()
    try:
        inv = conn.execute("SELECT *, planner_id FROM invitaciones WHERE id = ?", (id,)).fetchone()
        if not inv: return jsonify({"success": False, "error": "Invitacion no encontrada"}), 404
        if es_planner and str(inv['planner_id']) != str(session.get('planner_id')): return jsonify({"success": False, "error": "Permiso denegado"}), 403

        if tipo_imagen == 'galeria':
            data = request.get_json()
            foto_url_a_borrar = data.get('foto_url')
            if not foto_url_a_borrar: return jsonify({"success": False, "error": "URL no proporcionada"}), 400

            try:
                # BLINDAJE: Usamos el servicio oficial que tu ya creaste en services
                delete_from_cloudflare(foto_url_a_borrar)
            except Exception as e:
                print(f"Error R2 Galeria: {e}")

            fotos_actuales = json.loads(inv['fotos_json']) if inv['fotos_json'] else []
            if foto_url_a_borrar in fotos_actuales:
                fotos_actuales.remove(foto_url_a_borrar)
                conn.execute("UPDATE invitaciones SET fotos_json = ? WHERE id = ?", (json.dumps(fotos_actuales), id))
                conn.commit()
            
            return jsonify({"success": True})
            
        elif tipo_imagen == 'std':
            # AQUÍ ESTÁ LA MAGIA: Usamos el nombre real de tu columna: datos_cliente_json
            datos_cliente = json.loads(inv['datos_cliente_json']) if inv['datos_cliente_json'] else {}
            url_imagen_cloudflare = datos_cliente.get('foto_std_url')
            
            if not url_imagen_cloudflare: return jsonify({"success": False, "error": "Imagen STD ya eliminada o no existe"}), 400
            
            try:
                # Borramos de Cloudflare
                delete_from_cloudflare(url_imagen_cloudflare)
            except Exception as e:
                print(f"Error R2 STD: {e}")
                
            # Quitamos la llave 'foto_std_url' del JSON y actualizamos la base de datos con el nombre de columna correcto
            datos_cliente.pop('foto_std_url', None)
            conn.execute("UPDATE invitaciones SET datos_cliente_json = ? WHERE id = ?", (json.dumps(datos_cliente), id))
            conn.commit()
            
            return jsonify({"success": True})
            
        else:
            mapeo_columnas = {'portada': 'foto_portada_url', 'fondo': 'url_fondo'}
            if tipo_imagen not in mapeo_columnas: return jsonify({"success": False, "error": "Tipo invalido"}), 400
                
            columna_db = mapeo_columnas[tipo_imagen]
            url_imagen_cloudflare = inv[columna_db]
            if not url_imagen_cloudflare: return jsonify({"success": False, "error": "Imagen ya eliminada"}), 400

            try:
                # BLINDAJE: Usamos el servicio oficial
                delete_from_cloudflare(url_imagen_cloudflare)
            except Exception as e:
                print(f"Error R2 {tipo_imagen}: {e}")

            conn.execute(f"UPDATE invitaciones SET {columna_db} = NULL WHERE id = ?", (id,))
            conn.commit()
            return jsonify({"success": True})

    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        conn.close()

# ==============================================================================
# GESTION DE CONFIGURACION DE MESAS
# ==============================================================================
@invitaciones_bp.route('/admin/invitacion/<int:id>/mesas', methods=['POST'])
def guardar_configuracion_mesas(id):
    """Guarda la estructura dinamica del seating plan protegiendo los datos numéricos."""
    es_admin_master = session.get('role', 0) >= 1
    es_planner = session.get('user_type') == 'planner' and 'planner_id' in session

    if not es_admin_master and not es_planner:
        flash('Acceso denegado.', 'danger')
        return redirect(url_for('auth.login'))

    conn = get_db_connection()
    try:
        inv_seguridad = conn.execute("SELECT planner_id FROM invitaciones WHERE id = ?", (id,)).fetchone()
        if es_planner and str(inv_seguridad['planner_id']) != str(session.get('planner_id')):
            flash("Permiso denegado.", "danger")
            return redirect(url_for('invitaciones_clientes.dashboard_planner'))

        nombres = request.form.getlist('nombre_mesa[]')
        capacidades = request.form.getlist('capacidad_mesa[]')

        mesas_config = []
        for nombre, cap in zip(nombres, capacidades):
            if nombre.strip() and cap.strip():
                # BLINDAJE: Evita crashear si el formulario envia texto en vez de numero
                try:
                    cap_int = int(cap)
                except ValueError:
                    cap_int = 10 
                    
                mesas_config.append({
                    'nombre': nombre.strip(),
                    'capacidad': cap_int
                })

        mesas_json = json.dumps(mesas_config)

        conn.execute("UPDATE invitaciones SET mesas_json = ? WHERE id = ?", (mesas_json, id))
        conn.commit()
        
        flash(f"Distribucion actualizada. ({len(mesas_config)} mesas configuradas)", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Error al guardar configuracion de mesas: {str(e)}", "danger")
    finally:
        conn.close()

    return redirect(url_for('invitaciones_admin.gestionar_pases', id=id))

@invitaciones_bp.route('/admin/invitacion/<int:inv_id>/editar-pase/<int:pase_id>', methods=['POST'])
def editar_pase_admin(inv_id, pase_id):
    es_admin_master = session.get('role', 0) >= 1
    es_planner = session.get('user_type') == 'planner' and 'planner_id' in session

    if not es_admin_master and not es_planner: 
        return redirect(url_for('auth.login'))

    conn = get_db_connection()
    try:
        inv_seguridad = conn.execute("SELECT planner_id FROM invitaciones WHERE id = ?", (inv_id,)).fetchone()
        if es_planner and str(inv_seguridad['planner_id']) != str(session.get('planner_id')):
            flash("Permiso denegado.", "danger")
            return redirect(url_for('invitaciones_clientes.dashboard_planner'))

        from helpers import guardar_pase_bd
        exito, msj = guardar_pase_bd(inv_id, request.form, pase_id)
        
        if exito:
            flash(msj, "success")
        else:
            flash(f"Error al editar: {msj}", "danger")
    finally:
        conn.close()
        
    return redirect(url_for('invitaciones_admin.gestionar_pases', id=inv_id))