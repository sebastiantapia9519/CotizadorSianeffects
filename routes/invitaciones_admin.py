import json
import re
import io
import uuid
import zipfile
import string
import random
import requests
from flask import request, redirect, url_for, flash
from datetime import datetime, timedelta
from utils.datetime_utils import fecha_mas_dias, sumar_dias_a_fecha, hoy_local
from flask import send_file
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from helpers import admin_required
from db import get_db_connection  # Gestor centralizado de base de datos SQLite
from services.cloudflare_service import upload_to_cloudflare, delete_from_cloudflare # Integración con R2

# ==============================================================================
# INICIALIZACIÓN DEL BLUEPRINT
# ==============================================================================
# Define este archivo como un módulo conectable a la app principal de Flask
invitaciones_bp = Blueprint('invitaciones_admin', __name__)

# ==============================================================================
# CONFIGURACIÓN MAESTRA DE PLANTILLAS
# ==============================================================================
# Este diccionario actúa como la "fuente de la verdad" para los diseños rápidos.
# Si el usuario elige 'rustico', el sistema lee de aquí los colores y tipografías
# para inyectarlos en la invitación generada.
PLANTILLAS_CONFIG = {
    'rustico': {
        'fuente_titulo': 'Cormorant',
        'fuente_cuerpo': 'Proza Libre',
        'color_acento': '#5d6d5a', # Verde Eucalipto
        'color_fondo': '#f4f1ea',  # Crema Papel
        'frase_default': "Hoy celebramos el amor que nos une..."
    },
    'romantico': {
        'fuente_titulo': 'Great Vibes',
        'fuente_cuerpo': 'Montserrat',
        'color_acento': '#d48b9b', # Rosa Viejo
        'color_fondo': '#fff9f9',
        'frase_default': "Dos corazones, un mismo camino."
    }
}

# ==============================================================================
# FUNCIONES HELPER (UTILERÍAS INTERNAS)
# ==============================================================================

def generar_codigo_cliente():
    """
    Genera un código alfanumérico único para la invitación.
    Se usa para que los novios/quinceañera puedan acceder a ver su lista de invitados 
    sin necesidad de tener una cuenta formal en el sistema.
    Ejemplo de salida: SIA-X9K2A
    """
    suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"SIA-{suffix}"

def generar_codigo_planner():
    """
    Genera una contraseña única de acceso para los Planners (Socios B2B).
    Ejemplo de salida: PLAN-B7V1M
    """
    suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"PLAN-{suffix}"

def usar_credito_planner(planner_id):
    """
    Lógica de facturación de Planners.
    1. Busca en la BD el paquete de créditos que caduque más pronto y que aún tenga saldo.
    2. Si lo encuentra, le suma 1 al contador de 'cantidad_usada' (consumiendo el crédito).
    3. Retorna True si tuvo éxito, False si no tiene saldo.
    """
    conn = get_db_connection()
    paquete = conn.execute("""
        SELECT id, cantidad_total, cantidad_usada 
        FROM planner_paquetes 
        WHERE planner_id = ? AND activo = 1 
        AND datetime(fecha_vencimiento) > datetime('now')
        AND cantidad_usada < cantidad_total
        ORDER BY fecha_vencimiento ASC LIMIT 1
    """, (planner_id,)).fetchone()

    if paquete:
        conn.execute("UPDATE planner_paquetes SET cantidad_usada = cantidad_usada + 1 WHERE id = ?", (paquete['id'],))
        conn.commit()
        conn.close()
        return True 
    
    conn.close()
    return False

# ==============================================================================
# API REST: VALIDACIÓN DE URL EN TIEMPO REAL (AJAX)
# ==============================================================================
@invitaciones_bp.route('/admin/api/verificar-slug', methods=['POST'])
def verificar_slug():
    """
    Endpoint consumido por el Frontend mediante fetch().
    Recibe el string que el usuario escribe en "URL del Evento" y verifica en tiempo 
    real si esa URL ya está ocupada por otra invitación en la Base de Datos.
    """
    data = request.get_json()
    slug = data.get('slug', '').strip()
    
    # Limpiamos caracteres especiales y espacios para evitar URLs inválidas (Ej. "Mi Boda" -> "mi-boda")
    slug_limpio = re.sub(r'[^\w\-]+', '', re.sub(r'[\s]+', '-', slug.lower()))
    
    conn = get_db_connection()
    existente = conn.execute("SELECT id FROM invitaciones WHERE slug = ?", (slug_limpio,)).fetchone()
    conn.close()
    
    # Retorna JSON. 'disponible' será True si 'existente' es None (no se encontró nada)
    return jsonify({'disponible': existente is None, 'slug_sugerido': slug_limpio})


# ==============================================================================
# RUTA PRINCIPAL 1: CONSTRUCTOR DE INVITACIONES (CREAR)
# ==============================================================================
@invitaciones_bp.route('/admin/nueva-invitacion', methods=['GET', 'POST'])
def crear_invitacion():
    """
    Motor principal para ensamblar una nueva invitación. 
    Maneja subida de archivos a Cloudflare R2, armado de JSONs dinámicos y validaciones de seguridad.
    """
    
    # --- 1. SEGURIDAD DE ACCESO (Doble Rol) ---
    # Permite entrar tanto al dueño (Admin) como a los clientes B2B (Planners)
    es_admin_master = session.get('role', 0) >= 1
    es_planner = session.get('user_type') == 'planner' and 'planner_id' in session

    if not es_admin_master and not es_planner:
        flash('Acceso denegado. Se requiere una sesión válida.', 'danger')
        return redirect(url_for('invitaciones_clientes.login_cliente'))

    conn = get_db_connection()
    
    # --- PROCESAMIENTO DEL FORMULARIO (POST) ---
    if request.method == 'POST':
        try:
            id_creador_registrado = None
            tipo_creador = 'staff'
            planner_id = None
            
            # --- 2. VALIDACIÓN DE CRÉDITOS (Solo Planners) ---
            if es_planner:
                planner_id = session.get('planner_id')
                id_creador_registrado = planner_id
                tipo_creador = 'planner'
                
                # Verificamos si tiene saldo ANTES de procesar imágenes para no saturar el servidor en vano
                paquete_disp = conn.execute("""
                    SELECT id FROM planner_paquetes 
                    WHERE planner_id = ? AND activo = 1 
                    AND datetime(fecha_vencimiento) > datetime('now')
                    AND cantidad_usada < cantidad_total
                    LIMIT 1
                """, (planner_id,)).fetchone()
                
                if not paquete_disp:
                    flash("No tienes créditos disponibles o tus paquetes han vencido.", "danger")
                    conn.close()
                    return redirect(url_for('invitaciones_clientes.dashboard_planner'))

            elif es_admin_master:
                id_creador_registrado = session.get('user_id') 
                tipo_creador = 'admin'

            # --- 3. LIMPIEZA DE SLUG Y ANTI-COLISIÓN DE URLS ---
            raw_slug = request.form.get('slug', '').strip()
            slug = re.sub(r'[^\w\-]+', '', re.sub(r'[\s]+', '-', raw_slug.lower()))
            
            # Escudo de servidor: Por si falló la validación JS en el frontend
            slug_existente = conn.execute("SELECT id FROM invitaciones WHERE slug = ?", (slug,)).fetchone()
            if slug_existente:
                flash("Ese enlace ya está ocupado por otro evento. Por favor, elige uno diferente.", "danger")
                conn.close()
                return redirect(url_for('invitaciones_admin.crear_invitacion'))

            musica_id = request.form.get('musica_id')
            fecha_evento_raw = request.form.get('fecha_evento') 
            
            # --- 4. PARSEO DE FECHAS (Reparación del Bug 'T') ---
            # Los inputs <input type="datetime-local"> envían la fecha con una 'T' intermedia
            # Ej: "2026-03-07T18:00". SQLite y Python necesitan "2026-03-07 18:00:00".
            fecha_evento_limpia = None
            if fecha_evento_raw:
                fecha_str = fecha_evento_raw.replace('T', ' ')[:16] 
                try:
                    fecha_obj = datetime.strptime(fecha_str, '%Y-%m-%d %H:%M')
                    fecha_evento_limpia = fecha_obj.strftime('%Y-%m-%d %H:%M:%S')
                except ValueError:
                    print(f"Error parseando fecha: {fecha_str}")
                    fecha_evento_limpia = fecha_evento_raw
            else:
                fecha_obj = datetime.now()

            # --- 5. CÁLCULO DE EXPIRACIÓN (VIGENCIA) ---
            # La regla de negocio indica que la invitación vive 30 días después de la fiesta.
            if es_planner:
                vigencia = (fecha_obj + timedelta(days=30)).strftime('%Y-%m-%d')
            else:
                # El admin puede forzar una vigencia manual o dejar que se calcule sola
                vigencia = request.form.get('vigencia')
                if not vigencia:
                     vigencia = (fecha_obj + timedelta(days=30)).strftime('%Y-%m-%d')

            # --- 6. CAPTURA DE CONFIGURACIÓN BÁSICA ---
            tipo_evento = request.form.get('tipo_evento', 'boda')
            dress_code = request.form.get('dress_code')
            album_url = request.form.get('album_url') # Link externo a Google Drive/Photos
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

            # --- 7. CONSTRUCCIÓN DE ESTRUCTURAS DINÁMICAS (Listas a JSON) ---
            # A) Historia de XV Años (Combina texto con fotos subidas al vuelo)
            anios_hist = request.form.getlist('anio_historia[]')
            textos_hist = request.form.getlist('texto_historia[]')
            fotos_nuevas_hist = request.files.getlist('foto_historia_nueva[]')

            historia_lista = []
            for i in range(len(anios_hist)):
                if anios_hist[i] and textos_hist[i]:
                    foto_url = ""
                    # Solo sube la imagen a R2 si el usuario seleccionó un archivo válido
                    if i < len(fotos_nuevas_hist) and fotos_nuevas_hist[i].filename != '':
                        foto_url = upload_to_cloudflare(fotos_nuevas_hist[i], folder=f"invitaciones/{slug}/historia")
                    
                    historia_lista.append({
                        "anio": anios_hist[i],
                        "texto": textos_hist[i],
                        "foto": foto_url
                    })
            
            # B) Mesa de Regalos y Links Bancarios
            nombres_tiendas = request.form.getlist('nombre_tienda[]')
            links_tiendas = request.form.getlist('link_tienda[]')
            # zip() une las dos listas. Ignoramos si viene vacío.
            mesas_regalos = [{'nombre': n, 'url': l} for n, l in zip(nombres_tiendas, links_tiendas) if n and l]

            # C) Opciones de Hospedaje
            nombres_hoteles = request.form.getlist('nombre_hotel[]')
            links_hoteles = request.form.getlist('link_hotel[]')
            hoteles_sugeridos = [{'nombre': n, 'url': l} for n, l in zip(nombres_hoteles, links_hoteles) if n and l]

            # D) Itinerario de Actividades
            horas_it = request.form.getlist('hora_itinerario[]')
            acts_it = request.form.getlist('actividad_itinerario[]')
            iconos_it = request.form.getlist('icono_itinerario[]')
            itinerario = [{'hora': h, 'actividad': a, 'icono': i} for h, a, i in zip(horas_it, acts_it, iconos_it) if h and a]

            # E) Protocolo Familiar (Padres y Padrinos)
            roles_proto = request.form.getlist('rol_protocolo[]')
            nombres_proto = request.form.getlist('nombres_protocolo[]')
            protocolo_familiar = [{'rol': r, 'nombres': n} for r, n in zip(roles_proto, nombres_proto) if r and n]

            # --- 8. EMPAQUETADO MAESTRO JSON ---
            # Para evitar tener 50 columnas en SQL, guardamos toda la info del evento en un solo JSON estructurado.
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
                # Detectamos si el switch de "No Niños" estaba activado (Drag and Drop)
                "no_ninos": 'no_ninos' in request.form.getlist('orden_items[]'),
                "mensaje_no_ninos": request.form.get('mensaje_no_ninos', '').strip()  
            }
            
            # --- 9. SUBIDA DE IMÁGENES MAESTRAS A CLOUDFLARE R2 ---
            foto_portada = request.files.get('foto_portada')
            url_portada = upload_to_cloudflare(foto_portada, folder=f"invitaciones/{slug}") if foto_portada and foto_portada.filename else None

            img_fondo = request.files.get('imagen_fondo')
            url_fondo = upload_to_cloudflare(img_fondo, folder=f"invitaciones/{slug}/bg") if img_fondo and img_fondo.filename else None

            # Subida en lote (Múltiples fotos de galería)
            fotos_galeria = request.files.getlist('fotos_galeria')
            urls_galeria = [upload_to_cloudflare(f, folder=f"invitaciones/{slug}/galeria") for f in fotos_galeria if f and f.filename]

            # --- 10. ORDENAMIENTO DINÁMICO (DRAG & DROP) ---
            # Rescatamos el orden en que el usuario acomodó las tarjetas HTML
            orden_items = request.form.getlist('orden_items[]')
            if not orden_items: orden_items = ['inicio', 'evento', 'galeria'] # Fallback por defecto
            
            # Limpiamos duplicados manteniendo el orden
            orden_items = list(dict.fromkeys(orden_items))

            # Inyectamos o quitamos la cámara del arreglo visual dependiendo del Switch
            if camara_premium and 'camara' not in orden_items: orden_items.append('camara')
            if not camara_premium and 'camara' in orden_items: orden_items.remove('camara')

            # --- 11. INSERCIÓN EN BASE DE DATOS ---
            fecha_creacion_local = hoy_local() 

            conn.execute("""
                INSERT INTO invitaciones 
                (slug, config_json, musica_id, fecha_evento, vigencia, datos_cliente_json, 
                fotos_json, foto_portada_url, estilo_fuente, color_fondo, url_fondo, mesas_regalos_json,
                dress_code, hospedaje_json, album_url, camara_premium, tiene_modulo_invitados, 
                codigo_acceso_cliente, color_acentos, padres_novia, padres_novio, padrinos, 
                frase_final, bloquear_edicion_invitados, template_id, estilo_apertura, 
                tipo_evento, historia_json, planner_id, creado_por_id, tipo_creador, created_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                slug, json.dumps(orden_items), musica_id or None, 
                fecha_evento_limpia, vigencia, json.dumps(datos_cliente), 
                json.dumps(urls_galeria), url_portada, request.form.get('estilo_fuente'), 
                request.form.get('color_fondo'), url_fondo, json.dumps(mesas_regalos), 
                dress_code, json.dumps(hoteles_sugeridos), album_url, camara_premium, 
                tiene_modulo_invitados, codigo_cliente, color_acentos, padres_novia, 
                padres_novio, padrinos, frase_final, bloquear_edicion, template_id, 
                estilo_apertura, tipo_evento, json.dumps(historia_lista), planner_id, id_creador_registrado, tipo_creador,
                fecha_creacion_local
            ))
            
            # --- 12. COBRO FINAL AL PLANNER ---
            # Si el INSERT no tronó, ahora sí descontamos el crédito permanentemente.
            if es_planner:
                usar_credito_planner(planner_id)

            conn.commit()
            flash("Invitación Premium Creada ✨", "success")
            
            # Redirección dinámica según el rol
            if es_planner:
                return redirect(url_for('invitaciones_clientes.dashboard_planner'))
            return redirect(url_for('invitaciones_admin.gestionar_invitaciones'))
            
        except Exception as e:
            conn.rollback() # Revierte cambios en BD si algo falla
            flash(f"Error al crear: Verifique que todos los datos estén completos. (Detalle: {str(e)})", "danger")
            return redirect(url_for('invitaciones_admin.crear_invitacion')) # <--- ESTO EVITA EL CRASH
        finally:
            conn.close()

    # --- MÉTODO GET: RENDERIZAR FORMULARIO DE CREACIÓN ---
    saldo_real = 0
    # Obtenemos el saldo real del planner para inyectarlo en el Modal HTML de confirmación
    if es_planner:
        saldo_row = conn.execute("""
            SELECT COALESCE(SUM(cantidad_total - cantidad_usada), 0) as s 
            FROM planner_paquetes 
            WHERE planner_id = ? AND activo = 1 
            AND datetime(fecha_vencimiento) > datetime('now')
        """, (session.get('planner_id'),)).fetchone()
        if saldo_row:
            saldo_real = saldo_row['s']

    # Traemos el catálogo de música general
    canciones = conn.execute("SELECT id, nombre_cancion FROM lista_musica WHERE activa = 1 ORDER BY nombre_cancion ASC").fetchall()
    conn.close()
    
    return render_template('invitaciones/crear.html', 
                           inv=None, 
                           datos=None, 
                           mesas=[], 
                           hoteles=[], 
                           canciones=canciones, 
                           edit_mode=False,
                           saldo=saldo_real)


# ==============================================================================
# API REST: SUBIR MÚSICA AL CATÁLOGO GLOBAL
# ==============================================================================
@invitaciones_bp.route('/admin/api/subir-musica', methods=['POST'])
@admin_required
def api_subir_musica():
    """
    Recibe un archivo de audio (MP3, WAV) subido por el Admin mediante un modal,
    lo procesa en R2 y lo guarda en la tabla `lista_musica` para que esté disponible
    en el selector de canciones de todas las invitaciones.
    """
    nombre = request.form.get('nombre')
    archivo = request.files.get('archivo')
    
    if not nombre or not archivo:
        return jsonify({'success': False, 'error': 'Faltan datos o el archivo.'}), 400
        
    # Validación estricta de seguridad: Evitar inyección de scripts disfrazados
    if not archivo.content_type.startswith('audio/'):
        return jsonify({'success': False, 'error': 'Solo se permiten archivos de audio (MP3, WAV).'}), 400

    try:
        url_audio = upload_to_cloudflare(archivo, folder="musica")
        if not url_audio:
            return jsonify({'success': False, 'error': 'Error al subir a Cloudflare.'}), 500

        conn = get_db_connection()
        cursor = conn.cursor()
        # Se inserta forzando activa = 1
        cursor.execute("INSERT INTO lista_musica (nombre_cancion, url_cloudflare, activa) VALUES (?, ?, 1)", (nombre.strip(), url_audio))
        nuevo_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        # Retornamos los datos para que el JS agregue la canción al <select> al vuelo
        return jsonify({
            'success': True,
            'id': nuevo_id,
            'nombre': nombre.strip()
        })
        
    except Exception as e:
        print(f"Error en API música: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# FILTROS DE JINJA (Manipulación de datos en el HTML)
# ==============================================================================
@invitaciones_bp.app_template_filter('from_json')
def from_json(value):
    """Permite a las plantillas Jinja leer strings JSON de la base de datos como objetos reales"""
    return json.loads(value)

@invitaciones_bp.app_template_filter('color_contraste')
def color_contraste(hex_color):
    """
    Fórmula de luminancia relativa (W3C). 
    Calcula si un color hexadecimal es oscuro o claro, y retorna blanco o negro respectivamente.
    Sirve para que el texto siempre sea legible sin importar el color de fondo elegido.
    """
    if not hex_color: return '#333333'
    hex_color = hex_color.lstrip('#')
    if len(hex_color) != 6: return '#333333'
    r, g, b = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    luminance = (0.299*r + 0.587*g + 0.114*b)
    return '#fdfbf7' if luminance < 140 else '#333333'

@invitaciones_bp.app_template_filter('fondo_tarjeta')
def fondo_tarjeta(hex_color):
    """Calcula la opacidad de los componentes translúcidos basándose en el fondo"""
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
    """
    Panel central de SianEffects. Muestra una tabla con absolutamente todas 
    las invitaciones creadas, independientemente del Planner que las hizo.
    """
    conn = get_db_connection()
    try:
        # Hacemos LEFT JOIN para traer el nombre de la agencia/planner si existe
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
                # Extraemos el nombre de los novios del JSON empaquetado
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
# RUTA 5: EDITAR INVITACIÓN (Carga y procesa datos existentes)
# ==============================================================================
@invitaciones_bp.route('/admin/editar-invitacion/<int:id>', methods=['GET', 'POST'])
def editar_invitacion(id):
    """
    Renderiza el mismo HTML del creador, pero pre-llena los inputs con la información actual.
    Maneja la sustitución o eliminación de imágenes preservando las anteriores.
    """
    es_admin_master = session.get('role', 0) >= 1
    es_planner = session.get('user_type') == 'planner' and 'planner_id' in session

    if not es_admin_master and not es_planner:
        flash('Acceso denegado. Se requiere una sesión válida.', 'danger')
        return redirect(url_for('auth.login')) 

    conn = get_db_connection()
    
    # --- SEGURIDAD: EVITAR EDICIÓN CRUZADA ---
    # Un planner no puede adivinar el ID de una invitación en la URL y editarla si no es suya.
    inv_seguridad = conn.execute("SELECT planner_id FROM invitaciones WHERE id = ?", (id,)).fetchone()
    if not inv_seguridad:
        flash("Invitación no encontrada.", "danger")
        conn.close()
        return redirect(url_for('invitaciones_clientes.dashboard_planner') if es_planner else url_for('invitaciones_admin.gestionar_invitaciones'))
        
    if es_planner and str(inv_seguridad['planner_id']) != str(session.get('planner_id')):
        flash("No tienes permiso para editar esta invitación.", "danger")
        conn.close()
        return redirect(url_for('invitaciones_clientes.dashboard_planner'))

    # --- GUARDAR CAMBIOS (POST) ---
    if request.method == 'POST':
        try:
            # Rescatamos datos viejos que no pueden cambiar o que complementaremos
            inv_old = conn.execute("""
                SELECT slug, tipo_evento, datos_cliente_json, foto_portada_url, fotos_json, url_fondo, codigo_acceso_cliente 
                FROM invitaciones WHERE id=?
            """, (id,)).fetchone()
            
            datos_viejos = json.loads(inv_old['datos_cliente_json']) if inv_old['datos_cliente_json'] else {}

            # Bloqueo de Slug y Evento (Planner no lo cambia, Admin sí)
            if es_planner:
                slug = inv_old['slug']
                tipo_evento = inv_old['tipo_evento']
                nombres_novios_final = datos_viejos.get('novios', '') 
            else:
                raw_slug = request.form.get('slug', '').strip()
                slug_limpio = re.sub(r'[^\w\-]+', '', re.sub(r'[\s]+', '-', raw_slug.lower()))
                
                # Verificamos colisión EXCLUYENDO el ID actual (para que no choque consigo misma al editar)
                slug_existente = conn.execute("SELECT id FROM invitaciones WHERE slug = ? AND id != ?", (slug_limpio, id)).fetchone()
                if slug_existente:
                    flash("Ese enlace ya está ocupado.", "danger")
                    conn.close()
                    return redirect(url_for('invitaciones_admin.editar_invitacion', id=id))
                
                slug = slug_limpio
                tipo_evento = request.form.get('tipo_evento', 'boda')
                nombres_novios_final = request.form.get('nombres_novios')

            musica_id = request.form.get('musica_id')
            estilo_fuente = request.form.get('estilo_fuente')
            color_fondo = request.form.get('color_fondo')
            
            # Limpieza de fechas
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

            # Recálculo de expiración
            if es_planner:
                vigencia = (fecha_obj + timedelta(days=30)).strftime('%Y-%m-%d')
            else:
                vigencia = request.form.get('vigencia')
                if not vigencia:
                     vigencia = (fecha_obj + timedelta(days=30)).strftime('%Y-%m-%d')
            
            # Variables sueltas
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

            # Manejo avanzado de la historia de XV (Mezclando fotos viejas con nuevas)
            anios_hist = request.form.getlist('anio_historia[]')
            textos_hist = request.form.getlist('texto_historia[]')
            fotos_actuales_hist = request.form.getlist('foto_historia_actual[]') # Ocultas en HTML
            fotos_nuevas_hist = request.files.getlist('foto_historia_nueva[]')
            
            historia_lista = []
            for i in range(len(anios_hist)):
                if anios_hist[i] and textos_hist[i]:
                    foto_url = ""
                    # Si subió archivo nuevo, súbelo a R2
                    if i < len(fotos_nuevas_hist) and fotos_nuevas_hist[i] and fotos_nuevas_hist[i].filename:
                        foto_url = upload_to_cloudflare(fotos_nuevas_hist[i], folder=f"invitaciones/{slug}/historia")
                    else:
                        # Si no subió nada, respeta la URL que ya tenía la tarjeta
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

            datos_cliente = {
                "novios": nombres_novios_final,
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
                "mensaje_no_ninos": request.form.get('mensaje_no_ninos', '').strip()
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

            # --- MANEJO INTELIGENTE DE IMÁGENES ---
            # Si se envía archivo, sube a R2. Si no, respeta la URL que ya estaba en BD
            foto_portada = request.files.get('foto_portada')
            url_portada = upload_to_cloudflare(foto_portada, folder=f"invitaciones/{slug}") if foto_portada and foto_portada.filename != '' else inv_old['foto_portada_url']

            img_fondo = request.files.get('imagen_fondo')
            url_fondo = upload_to_cloudflare(img_fondo, folder=f"invitaciones/{slug}/bg") if img_fondo and img_fondo.filename != '' else inv_old['url_fondo']

            # Concatenar arreglo de fotos viejas con las fotos nuevas que subió
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
                tipo_evento=?, historia_json=? 
                WHERE id=?
            """, (
                slug, json.dumps(orden_items), musica_id or None, fecha_evento_limpia, vigencia, json.dumps(datos_cliente), 
                json.dumps(urls_finales_galeria), url_portada, estilo_fuente, color_fondo, url_fondo, json.dumps(mesas_regalos),
                dress_code, json.dumps(hoteles_sugeridos), album_url, camara_premium, color_acentos,
                padres_novia, padres_novio, padrinos, frase_final, template_id, tiene_modulo_invitados,
                codigo_cliente, bloquear_edicion, estilo_apertura,
                tipo_evento, json.dumps(historia_lista),
                id               
            ))
            conn.commit()
            flash("¡Invitación actualizada exitosamente! ✏️", "success")
            
            if es_planner:
                return redirect(url_for('invitaciones_clientes.dashboard_planner'))
            return redirect(url_for('invitaciones_admin.gestionar_invitaciones'))
            
        except Exception as e:
            conn.rollback()
            flash(f"Error al actualizar: Verifique que los datos estén completos. (Detalle: {str(e)})", "danger")
            return redirect(url_for('invitaciones_admin.editar_invitacion', id=id)) # <--- ESTO EVITA EL CRASH
        finally:
            conn.close()

    # --- MÉTODO GET: RENDERIZAR DATOS EN HTML ---
    inv = conn.execute("SELECT * FROM invitaciones WHERE id = ?", (id,)).fetchone()
    canciones = conn.execute("SELECT id, nombre_cancion FROM lista_musica WHERE activa = 1 ORDER BY nombre_cancion ASC").fetchall()
    
    saldo_real = 0
    if es_planner:
        saldo_row = conn.execute("""
            SELECT COALESCE(SUM(cantidad_total - cantidad_usada), 0) as s 
            FROM planner_paquetes 
            WHERE planner_id = ? AND activo = 1 
            AND datetime(fecha_vencimiento) > datetime('now')
        """, (session.get('planner_id'),)).fetchone()
        if saldo_row:
            saldo_real = saldo_row['s']
            
    conn.close()

    if not inv:
        flash("Invitación no encontrada.", "danger")
        if es_planner: return redirect(url_for('invitaciones_clientes.dashboard_planner'))
        return redirect(url_for('invitaciones_admin.gestionar_invitaciones'))

    inv = dict(inv)
    # Exponemos los JSONs a objetos legibles para Jinja2
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
# ELIMINAR INVITACIÓN COMPLETA (Admin Only)
# ==============================================================================
@invitaciones_bp.route('/admin/eliminar-invitacion/<int:id>', methods=['POST'])
@admin_required
def eliminar_invitacion(id):
    """
    Borra la invitación de la BD y purga TODAS sus imágenes del bucket R2.
    Fundamental para mantener el almacenamiento limpio y barato.
    """
    conn = get_db_connection()
    try:
        inv = conn.execute("SELECT foto_portada_url, url_fondo, fotos_json FROM invitaciones WHERE id = ?", (id,)).fetchone()
        
        if inv:
            # 1. Eliminar multimedia base del evento
            if inv['foto_portada_url']: delete_from_cloudflare(inv['foto_portada_url'])
            if inv['url_fondo']: delete_from_cloudflare(inv['url_fondo'])
            if inv['fotos_json']:
                fotos_galeria = json.loads(inv['fotos_json'])
                for foto_url in fotos_galeria: delete_from_cloudflare(foto_url)

            # 2. Purgar fotos subidas por invitados a la cámara
            fotos_invitados = conn.execute("SELECT url FROM fotos_invitados WHERE invitacion_id = ?", (id,)).fetchall()
            for foto in fotos_invitados:
                if foto['url']: delete_from_cloudflare(foto['url'])
            
            # Borramos registros huérfanos
            conn.execute("DELETE FROM fotos_invitados WHERE invitacion_id = ?", (id,))

        conn.execute("DELETE FROM invitaciones WHERE id = ?", (id,))
        conn.commit()
        
        flash("Invitación y fotos eliminadas permanentemente 🧹", "success")
        
    except Exception as e:
        conn.rollback()
        flash(f"Error al eliminar: {str(e)}", "danger")
    finally:
        conn.close()
        
    return redirect(url_for('invitaciones_admin.gestionar_invitaciones'))


# ==============================================================================
# RUTA PÚBLICA: RENDERIZADO DEL EVENTO (/invitacion/slug)
# ==============================================================================
@invitaciones_bp.route('/invitacion/<slug>')
def ver_invitacion(slug):
    """
    Controlador maestro que sirve la invitación pública a los usuarios finales.
    Combina la lógica de plantillas (Boda/XV), colores, música y pases VIP.
    """
    conn = get_db_connection()
    try:
        inv = conn.execute("""
            SELECT i.*, m.url_cloudflare as musica_url 
            FROM invitaciones i
            LEFT JOIN lista_musica m ON i.musica_id = m.id
            WHERE i.slug = ?
        """, (slug,)).fetchone()
        
        if not inv:
            return "<h1>404 - Invitación no encontrada 😢</h1>", 404

        # --- ESCUDO DE VIGENCIA (PROTECCIÓN DE ARCHIVO) ---
        # Evita accesos después de 30 días del evento mostrando una landing de bloqueo
        # Convertimos a string por seguridad y cortamos los primeros 10 caracteres (YYYY-MM-DD)
        hoy_str = str(hoy_local())[:10]
        vigencia_invitacion = inv['vigencia']

        # Si hay fecha de vigencia y la fecha de hoy es mayor, bloqueamos
        if vigencia_invitacion and hoy_str > str(vigencia_invitacion)[:10]:
            return render_template('invitaciones/expirada.html')
        # --------------------------------------------------

        # Lógica de Pases Personalizados (Código QR)
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

        # Resuelve la cascada de colores (Plantilla elegida VS Colores Manuales)
        template_colors = {}
        if inv['template_id'] and inv['template_id'] != 'personalizado':
            template = PLANTILLAS_CONFIG.get(inv['template_id'])
            if template:
                template_colors = {
                    'template_color_acento': template['color_acento'],
                    'template_color_fondo': template['color_fondo']
                }

        # Renderiza vista distinta según el negocio (Boda o XV)
        plantilla_render = 'invitaciones/xv.html' if inv['tipo_evento'] == 'xv' else 'invitaciones/base_boda.html'
        
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
# GESTIÓN DE ÁLBUM DE CÁMARA (DESCARGAR FOTOS)
# ==============================================================================
@invitaciones_bp.route('/admin/ver-fotos/<int:id>')
def ver_fotos_invitados(id):
    """Muestra el mosaico de fotos que los invitados capturaron durante el evento."""
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
        flash(f"Error al cargar la galería: {str(e)}", "danger")
        return redirect(url_for('invitaciones_clientes.dashboard_planner') if es_planner else url_for('invitaciones_admin.gestionar_invitaciones'))
    finally:
        conn.close()

@invitaciones_bp.route('/admin/descargar-rollo/<int:id>')
def descargar_rollo_zip(id):
    """
    Genera un archivo .zip en memoria (RAM) descargando los objetos binarios
    directamente de Cloudflare S3/R2 para entregarlos al Planner.
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

        memory_file = io.BytesIO() # Contenedor RAM para el ZIP
        fotos_añadidas = 0
        
        from routes.invitaciones_publicas import s3_client, BUCKET_NAME

        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for i, foto in enumerate(fotos):
                try:
                    # Extraer el Key exacto de la URL pública para pedirlo al SDK de boto3
                    key = foto['url'].split('.dev/')[-1]
                    objeto_s3 = s3_client.get_object(Bucket=BUCKET_NAME, Key=key)
                    contenido = objeto_s3['Body'].read()
                    
                    if contenido:
                        zf.writestr(f"foto_{i+1}.jpg", contenido)
                        fotos_añadidas += 1
                except Exception as e:
                    print(f"Error con boto3 en foto {i}: {e}")
                    continue

        if fotos_añadidas == 0:
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
# GESTIÓN DE PASES E INVITADOS VIP (RSVP)
# ==============================================================================
@invitaciones_bp.route('/admin/invitacion/<int:id>/invitados', methods=['GET', 'POST'])
def gestionar_pases(id):
    """Panel para crear boletos digitales QR por familia o invitado."""
    es_admin_master = session.get('role', 0) >= 1
    es_planner = session.get('user_type') == 'planner' and 'planner_id' in session

    if not es_admin_master and not es_planner:
        flash('Acceso denegado.', 'danger')
        return redirect(url_for('auth.login'))

    conn = get_db_connection()
    
    inv_seguridad = conn.execute("SELECT planner_id FROM invitaciones WHERE id = ?", (id,)).fetchone()
    if not inv_seguridad:
        flash("Invitación no encontrada.", "danger")
        conn.close()
        return redirect(url_for('invitaciones_clientes.dashboard_planner') if es_planner else url_for('invitaciones_admin.gestionar_invitaciones'))
        
    if es_planner and str(inv_seguridad['planner_id']) != str(session.get('planner_id')):
        flash("Permiso denegado.", "danger")
        conn.close()
        return redirect(url_for('invitaciones_clientes.dashboard_planner'))

    if request.method == 'POST':
        nombre_familia = request.form.get('nombre_familia')
        pases = request.form.get('pases_totales', 2)
        telefono = request.form.get('telefono')
        mesa = request.form.get('mesa', '0') 

        codigo_unico = str(uuid.uuid4())[:8].upper() # Token para generar el QR único
        
        try:
            conn.execute("""
                INSERT INTO pases_invitados (invitacion_id, nombre_familia, pases_totales, codigo_qr_unique, telefono, mesa)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (id, nombre_familia, pases, codigo_unico, telefono, mesa)) 
            conn.commit()
            flash(f"Pase para {nombre_familia} generado con éxito.", "success")
        except Exception as e:
            flash(f"Error: {str(e)}", "danger")

    inv = conn.execute("SELECT slug, id, codigo_acceso_cliente FROM invitaciones WHERE id = ?", (id,)).fetchone()
    invitados = conn.execute("SELECT * FROM pases_invitados WHERE invitacion_id = ? ORDER BY id DESC", (id,)).fetchall()
    conn.close()
    
    return render_template('invitaciones/pases_admin.html', inv=inv, invitados=invitados)

@invitaciones_bp.route('/admin/invitacion/<int:inv_id>/eliminar-pase/<int:pase_id>', methods=['POST'])
def eliminar_pase(inv_id, pase_id):
    """Revoca el acceso de un invitado borrándolo de la BD."""
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
# GESTIÓN B2B: SOCIOS COMERCIALES (PLANNERS)
# ==============================================================================
@invitaciones_bp.route('/admin/socios', methods=['GET', 'POST'])
@admin_required
def gestionar_socios():
    """Registra y lista cuentas de Planner y calcula su saldo en tiempo real"""
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
            flash(f"Socio {nombre} registrado. Código: {codigo_plan}", "success")
        except Exception as e:
            flash(f"Error al registrar socio: {e}", "danger")

    # Calcula saldo deduciendo cantidad_usada de cantidad_total de paquetes vigentes
    socios = conn.execute("""
        SELECT p.*, 
               COALESCE(SUM(pp.cantidad_total - pp.cantidad_usada), 0) as creditos_disponibles
        FROM planners p
        LEFT JOIN planner_paquetes pp 
          ON p.id = pp.planner_id 
          AND pp.activo = 1 
          AND datetime(pp.fecha_vencimiento) > datetime('now')
        GROUP BY p.id
        ORDER BY p.created_at DESC
    """).fetchall()
    
    conn.close()
    return render_template('invitaciones/admin_socios.html', socios=socios)

@invitaciones_bp.route('/admin/socios/cargar-paquete', methods=['POST'])
@admin_required
def cargar_paquete():
    """Recarga de inventario manual al Planner (Otorgar créditos). Tienen caducidad."""
    planner_id = request.form.get('planner_id')
    cantidad = int(request.form.get('cantidad', 0))
    vencimiento = fecha_mas_dias(60)
    
    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT INTO planner_paquetes (planner_id, cantidad_total, fecha_vencimiento)
            VALUES (?, ?, ?)
        """, (planner_id, cantidad, vencimiento))
        conn.commit()
        flash(f"Se cargaron {cantidad} créditos exitosamente.", "success")
    except Exception as e:
        flash(f"Error al cargar créditos: {e}", "danger")
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

@invitaciones_bp.route('/ajustar_saldo', methods=['POST'])
@admin_required
def ajustar_saldo():
    """Permite agregar/quitar saldo forzadamente por quejas o bonos dejando nota de auditoría"""
    planner_id = request.form.get('planner_id')
    ajuste = int(request.form.get('ajuste'))
    motivo = request.form.get('motivo')

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO planner_paquetes (planner_id, cantidad_total, fecha_compra, fecha_vencimiento, notas)
        VALUES (?, ?, CURRENT_TIMESTAMP, datetime('now', '+1 year'), ?)
    ''', (planner_id, ajuste, f"AJUSTE MANUAL: {motivo}"))
    conn.commit()
    conn.close()
    
    flash(f'Saldo ajustado.', 'warning')
    return redirect(url_for('invitaciones_admin.gestionar_socios'))

@invitaciones_bp.route('/regenerar_codigo', methods=['POST'])
@admin_required
def regenerar_codigo():
    """Restablecimiento de contraseña de acceso de Planner."""
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
    """Maneja la activación o suspensión de un planner."""
    planner_id = request.form.get('planner_id')
    accion = request.form.get('accion') # Recibimos la instrucción del botón
    
    conn = get_db_connection()
    
    if accion == 'activar':
        conn.execute("UPDATE planners SET estado = 'activo' WHERE id = ?", (planner_id,))
        flash('Socio activado correctamente.', 'success')
    else:
        conn.execute("UPDATE planners SET estado = 'suspendido' WHERE id = ?", (planner_id,))
        flash('Socio suspendido. Se le negará el acceso.', 'danger')
        
    conn.commit()
    conn.close()
    return redirect(url_for('invitaciones_admin.gestionar_socios'))

@invitaciones_bp.route('/api/socios/<int:id>/auditoria')
@admin_required
def api_auditoria_planner(id):
    """Devuelve JSON histórico con todos los paquetes recargados y las invitaciones cobradas"""
    conn = get_db_connection()
    try:
        saldo_row = conn.execute("""
            SELECT COALESCE(SUM(cantidad_total - cantidad_usada), 0) as saldo
            FROM planner_paquetes 
            WHERE planner_id = ? AND activo = 1 AND datetime(fecha_vencimiento) > datetime('now')
        """, (id,)).fetchone()
        saldo = saldo_row['saldo'] if saldo_row else 0

        movs_db = conn.execute("SELECT * FROM planner_paquetes WHERE planner_id = ? ORDER BY fecha_compra DESC", (id,)).fetchall()
        movimientos = [{'fecha_compra': str(dict(m).get('fecha_compra'))[:10], **dict(m)} for m in movs_db]

        cons_db = conn.execute("SELECT id, slug, created_at, fecha_evento, datos_cliente_json FROM invitaciones WHERE planner_id = ? ORDER BY id DESC", (id,)).fetchall()
        consumos = []
        for c in cons_db:
            c_dict = dict(c)
            c_dict['created_at'] = str(c_dict.get('created_at') or c_dict.get('fecha_evento'))[:10]
            try:
                datos = json.loads(c_dict['datos_cliente_json']) if c_dict['datos_cliente_json'] else {}
                c_dict['nombres'] = datos.get('novios', 'Sin nombre')
            except:
                c_dict['nombres'] = 'Error'
            c_dict.pop('datos_cliente_json', None)
            consumos.append(c_dict)

        return jsonify({'success': True, 'saldo': saldo, 'movimientos': movimientos, 'consumos': consumos})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
    finally:
        conn.close()

# ==============================================================================
# ELIMINAR IMÁGENES AL VUELO DESDE EL FORMULARIO (Botón Papelera)
# ==============================================================================
@invitaciones_bp.route('/admin/invitacion/<int:id>/eliminar-imagen/<string:tipo_imagen>', methods=['POST'])
def eliminar_imagen_invitacion(id, tipo_imagen):
    """
    Ruta AJAX: Si el usuario borra una foto de galería en el constructor, 
    esta ruta la purga físicamente de Cloudflare R2 y actualiza el array JSON en la Base de Datos.
    """
    es_admin_master = session.get('role', 0) >= 1
    es_planner = session.get('user_type') == 'planner' and 'planner_id' in session
    if not es_admin_master and not es_planner: return jsonify({"success": False, "error": "Acceso denegado"}), 403

    conn = get_db_connection()
    try:
        inv = conn.execute("SELECT *, planner_id FROM invitaciones WHERE id = ?", (id,)).fetchone()
        if not inv: return jsonify({"success": False, "error": "Invitación no encontrada"}), 404
        if es_planner and str(inv['planner_id']) != str(session.get('planner_id')): return jsonify({"success": False, "error": "Permiso denegado"}), 403

        from routes.invitaciones_publicas import s3_client, BUCKET_NAME
        
        # Eliminar un fragmento del arreglo de la galería
        if tipo_imagen == 'galeria':
            data = request.get_json()
            foto_url_a_borrar = data.get('foto_url')
            if not foto_url_a_borrar: return jsonify({"success": False, "error": "URL no proporcionada"}), 400

            try:
                key = foto_url_a_borrar.split('.dev/')[-1]
                s3_client.delete_object(Bucket=BUCKET_NAME, Key=key)
            except Exception as e:
                print(f"Error R2 Galeria: {e}")

            fotos_actuales = json.loads(inv['fotos_json']) if inv['fotos_json'] else []
            if foto_url_a_borrar in fotos_actuales:
                fotos_actuales.remove(foto_url_a_borrar)
                conn.execute("UPDATE invitaciones SET fotos_json = ? WHERE id = ?", (json.dumps(fotos_actuales), id))
                conn.commit()
            
            return jsonify({"success": True})
            
        # Eliminar imagen única (Fondo o Portada)
        else:
            mapeo_columnas = {'portada': 'foto_portada_url', 'fondo': 'url_fondo'}
            if tipo_imagen not in mapeo_columnas: return jsonify({"success": False, "error": "Tipo inválido"}), 400
                
            columna_db = mapeo_columnas[tipo_imagen]
            url_imagen_cloudflare = inv[columna_db]
            if not url_imagen_cloudflare: return jsonify({"success": False, "error": "Imagen ya eliminada"}), 400

            try:
                key = url_imagen_cloudflare.split('.dev/')[-1]
                s3_client.delete_object(Bucket=BUCKET_NAME, Key=key)
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