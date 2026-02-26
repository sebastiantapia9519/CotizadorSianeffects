import json
import re
import io
import uuid
import zipfile
import string
import random
import requests
from flask import send_file
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from helpers import admin_required
from datetime import datetime
from db import get_db_connection  # Usamos tu conexión principal
from services.cloudflare_service import upload_to_cloudflare, delete_from_cloudflare

# Definimos el Blueprint
invitaciones_bp = Blueprint('invitaciones_admin', __name__)

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

# --- FUNCIÓN HELPER PARA GENERAR CÓDIGO ---
def generar_codigo_cliente():
    suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"SIA-{suffix}"

# --- RUTA 1: CONSTRUCTOR DE INVITACIONES ---
@invitaciones_bp.route('/admin/nueva-invitacion', methods=['GET', 'POST'])
@admin_required
def crear_invitacion():
    conn = get_db_connection()
    
    if request.method == 'POST':
        try:
            # 1. Datos Básicos y Limpieza de Slug
            raw_slug = request.form.get('slug', '').strip()
            slug = re.sub(r'[^\w\-]+', '', re.sub(r'[\s]+', '-', raw_slug.lower()))
            
            musica_id = request.form.get('musica_id')
            
            # --- NUEVOS CAMPOS ---
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

            # --- PROCESAR MI HISTORIA (Línea de tiempo XV) ---
            anios_hist = request.form.getlist('anio_historia[]')
            textos_hist = request.form.getlist('texto_historia[]')
            fotos_nuevas_hist = request.files.getlist('foto_historia_nueva[]')

            historia_lista = []
            for i in range(len(anios_hist)):
                if anios_hist[i] and textos_hist[i]:
                    foto_url = ""
                    # Subir foto si existe
                    if i < len(fotos_nuevas_hist) and fotos_nuevas_hist[i].filename != '':
                        foto_url = upload_to_cloudflare(fotos_nuevas_hist[i], folder=f"invitaciones/{slug}/historia")
                    
                    historia_lista.append({
                        "anio": anios_hist[i],
                        "texto": textos_hist[i],
                        "foto": foto_url
                    })
            
            # --- PROCESAR LINKS DE TIENDAS ---
            nombres_tiendas = request.form.getlist('nombre_tienda[]')
            links_tiendas = request.form.getlist('link_tienda[]')
            mesas_regalos = []
            for nombre, link in zip(nombres_tiendas, links_tiendas):
                if nombre and link:
                    mesas_regalos.append({'nombre': nombre, 'url': link})

            # --- PROCESAR LINKS DE HOTELES (NUEVO) ---
            nombres_hoteles = request.form.getlist('nombre_hotel[]')
            links_hoteles = request.form.getlist('link_hotel[]')
            hoteles_sugeridos = []
            for nombre, link in zip(nombres_hoteles, links_hoteles):
                if nombre and link:
                    hoteles_sugeridos.append({'nombre': nombre, 'url': link})

            # --- 1. PROCESAR ITINERARIO ---
            horas_it = request.form.getlist('hora_itinerario[]')
            acts_it = request.form.getlist('actividad_itinerario[]')
            iconos_it = request.form.getlist('icono_itinerario[]')
            
            itinerario = []
            for h, a, i in zip(horas_it, acts_it, iconos_it):
                if h and a:
                    itinerario.append({'hora': h, 'actividad': a, 'icono': i})

            # --- PROCESAR PROTOCOLO FAMILIAR ---
            roles_proto = request.form.getlist('rol_protocolo[]')
            nombres_proto = request.form.getlist('nombres_protocolo[]')
            
            protocolo_familiar = []
            for rol, nombres in zip(roles_proto, nombres_proto):
                if rol and nombres: # Solo guarda si ambos campos tienen texto
                    protocolo_familiar.append({'rol': rol, 'nombres': nombres})

            # --- 2. DICCIONARIO DATOS_CLIENTE ---
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
                "no_ninos": bool(request.form.get('no_ninos')),
                "mensaje_no_ninos": request.form.get('mensaje_no_ninos', '').strip()  
            }
            
            # --- SUBIDA DE IMÁGENES ---
            foto_portada = request.files.get('foto_portada')
            url_portada = None
            if foto_portada and foto_portada.filename: # <--- Cambio aquí
                url_portada = upload_to_cloudflare(foto_portada, folder=f"invitaciones/{slug}")

            img_fondo = request.files.get('imagen_fondo')
            url_fondo = None
            if img_fondo and img_fondo.filename: # <--- Cambio aquí
                url_fondo = upload_to_cloudflare(img_fondo, folder=f"invitaciones/{slug}/bg")

            fotos_galeria = request.files.getlist('fotos_galeria')
            urls_galeria = []
            for f in fotos_galeria:
                if f and f.filename: # <--- Cambio aquí
                    url = upload_to_cloudflare(f, folder=f"invitaciones/{slug}/galeria")
                    urls_galeria.append(url)

            orden_items = request.form.getlist('orden_items[]')

            if not orden_items:
                orden_items = ['inicio', 'evento', 'galeria']

            # limpiar duplicados y basura
            orden_items = list(dict.fromkeys(orden_items))

            # si NO hay cámara, la quitamos del orden
            if camara_premium and 'camara' not in orden_items:
                orden_items.append('camara')

            if not camara_premium and 'camara' in orden_items:
                orden_items.remove('camara')



            # --- INSERT ---
            conn.execute("""
                INSERT INTO invitaciones 
                (slug, config_json, musica_id, fecha_evento, vigencia, datos_cliente_json, 
                fotos_json, foto_portada_url, estilo_fuente, color_fondo, url_fondo, mesas_regalos_json,
                dress_code, hospedaje_json, album_url, camara_premium, tiene_modulo_invitados, codigo_acceso_cliente, color_acentos,
                padres_novia, padres_novio, padrinos, frase_final, bloquear_edicion_invitados, template_id, estilo_apertura, tipo_evento, historia_json) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                slug, json.dumps(orden_items), musica_id or None, 
                request.form.get('fecha_evento'), request.form.get('vigencia'), json.dumps(datos_cliente), 
                json.dumps(urls_galeria), url_portada, request.form.get('estilo_fuente'), request.form.get('color_fondo'), 
                url_fondo, json.dumps(mesas_regalos), dress_code, json.dumps(hoteles_sugeridos), album_url, 
                camara_premium, tiene_modulo_invitados, codigo_cliente, color_acentos,
                padres_novia, padres_novio, padrinos, frase_final, bloquear_edicion, template_id, estilo_apertura,
                tipo_evento, json.dumps(historia_lista) # <--- NUEVOS
            ))
            conn.commit()
            flash("Invitación Premium Creada ✨", "success")
            return redirect(url_for('invitaciones_admin.crear_invitacion'))
            
        except Exception as e:
            conn.rollback()
            flash(f"Error: {str(e)}", "danger")
        finally:
            conn.close()

    # GET: Mostrar el formulario con las canciones cargadas
    try:
        canciones = conn.execute("SELECT * FROM lista_musica WHERE activa = 1").fetchall()
    except:
        canciones = [] 
    finally:
        conn.close()
        
    return render_template('invitaciones/crear.html', canciones=canciones)


# --- RUTA 2: API PARA SUBIR MÚSICA (POPUP) ---
@invitaciones_bp.route('/admin/api/subir-musica', methods=['POST'])
@admin_required
def api_subir_musica():
    conn = get_db_connection()
    
    nombre = request.form.get('nombre')
    archivo = request.files.get('archivo')
    
    if not archivo:
        return jsonify({'error': 'No se envió archivo'}), 400

    try:
        url_audio = upload_to_cloudflare(archivo, folder="musica")
        cursor = conn.cursor()
        cursor.execute("INSERT INTO lista_musica (nombre_cancion, url_cloudflare) VALUES (?, ?)", (nombre, url_audio))
        nuevo_id = cursor.lastrowid
        conn.commit()
        
        return jsonify({
            'success': True,
            'id': nuevo_id,
            'nombre': nombre
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@invitaciones_bp.app_template_filter('from_json')
def from_json(value):
    return json.loads(value)

# --- FILTROS INTELIGENTES PARA DISEÑO AUTOMÁTICO ---
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


# --- RUTA 4: PANEL DE GESTIÓN (DASHBOARD) ---
@invitaciones_bp.route('/admin/invitaciones')
@admin_required
def gestionar_invitaciones():
    conn = get_db_connection()
    try:
        invs_db = conn.execute("SELECT * FROM invitaciones ORDER BY id DESC").fetchall()
        
        invitaciones = []
        for inv in invs_db:
            inv_dict = dict(inv) 
            try:
                inv_dict['datos_cliente'] = json.loads(inv['datos_cliente_json'])
            except:
                inv_dict['datos_cliente'] = {"novios": "Sin Nombre"}
            
            invitaciones.append(inv_dict)
            
        hoy = datetime.now().strftime('%Y-%m-%d')
        
        return render_template('invitaciones/gestionar.html', invitaciones=invitaciones, hoy=hoy)
    except Exception as e:
        flash(f"Error cargando el panel: {str(e)}", "danger")
        return redirect(url_for('admin.dashboard'))
    finally:
        conn.close()


# --- RUTA 5: EDITAR INVITACIÓN ---
@invitaciones_bp.route('/admin/editar-invitacion/<int:id>', methods=['GET', 'POST'])
@admin_required
def editar_invitacion(id):
    conn = get_db_connection()
    
    if request.method == 'POST':
        try:
            # Limpieza de Slug
            raw_slug = request.form.get('slug', '').strip()
            slug = re.sub(r'[^\w\-]+', '', re.sub(r'[\s]+', '-', raw_slug.lower()))

            musica_id = request.form.get('musica_id')
            estilo_fuente = request.form.get('estilo_fuente')
            color_fondo = request.form.get('color_fondo')
            fecha_evento = request.form.get('fecha_evento')
            vigencia = request.form.get('vigencia')
            
            # --- NUEVOS CAMPOS ---
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
            bloquear_edicion = 1 if 'bloquear_edicion_invitados' in request.form else 0
            estilo_apertura = request.form.get('estilo_apertura', 'simple')

            # --- PROCESAR MI HISTORIA (Edición) ---
            anios_hist = request.form.getlist('anio_historia[]')
            textos_hist = request.form.getlist('texto_historia[]')
            fotos_actuales_hist = request.form.getlist('foto_historia_actual[]')
            fotos_nuevas_hist = request.files.getlist('foto_historia_nueva[]')
            
            historia_lista = []
            for i in range(len(anios_hist)):
                if anios_hist[i] and textos_hist[i]:
                    foto_url = ""
                    # Subir foto si existe
                    if i < len(fotos_nuevas_hist) and fotos_nuevas_hist[i] and fotos_nuevas_hist[i].filename: # <--- Cambio aquí
                        foto_url = upload_to_cloudflare(fotos_nuevas_hist[i], folder=f"invitaciones/{slug}/historia")
                    
                    historia_lista.append({
                        "anio": anios_hist[i],
                        "texto": textos_hist[i],
                        "foto": foto_url
                    })
            
            #Primero traemos inv_old
            inv_old = conn.execute("SELECT foto_portada_url, fotos_json, url_fondo, codigo_acceso_cliente FROM invitaciones WHERE id=?", (id,)).fetchone()

            # Ahora sí verificamos el código del cliente
            codigo_cliente = inv_old['codigo_acceso_cliente'] if inv_old else None
            if not codigo_cliente:
                codigo_cliente = generar_codigo_cliente()

            # --- PROCESAR ITINERARIO ---
            horas_it = request.form.getlist('hora_itinerario[]')
            acts_it = request.form.getlist('actividad_itinerario[]')
            iconos_it = request.form.getlist('icono_itinerario[]')
            
            itinerario = []
            for h, a, i in zip(horas_it, acts_it, iconos_it):
                if h and a:
                    itinerario.append({'hora': h, 'actividad': a, 'icono': i})

            # --- PROCESAR PROTOCOLO FAMILIAR ---
            roles_proto = request.form.getlist('rol_protocolo[]')
            nombres_proto = request.form.getlist('nombres_protocolo[]')
            
            protocolo_familiar = []
            for rol, nombres in zip(roles_proto, nombres_proto):
                if rol and nombres: # Solo guarda si ambos campos tienen texto
                    protocolo_familiar.append({'rol': rol, 'nombres': nombres})

            # --- CREAR EL DICCIONARIO DATOS_CLIENTE ---
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
                "no_ninos": bool(request.form.get('no_ninos')),
                "mensaje_no_ninos": request.form.get('mensaje_no_ninos', '').strip()
            }
            
            # Mesas de regalos y Hoteles
            nombres_tiendas = request.form.getlist('nombre_tienda[]')
            links_tiendas = request.form.getlist('link_tienda[]')
            mesas_regalos = [{'nombre': n, 'url': l} for n, l in zip(nombres_tiendas, links_tiendas) if n and l]
            
            nombres_hoteles = request.form.getlist('nombre_hotel[]')
            links_hoteles = request.form.getlist('link_hotel[]')
            hoteles_sugeridos = [{'nombre': n, 'url': l} for n, l in zip(nombres_hoteles, links_hoteles) if n and l]

            orden_items = request.form.getlist('orden_items[]')

            if not orden_items:
                orden_items = ['inicio', 'evento', 'galeria']

            orden_items = list(dict.fromkeys(orden_items))

            # --- sincronizar camara premium con orden ---
            if camara_premium and 'camara' not in orden_items:
                orden_items.append('camara')

            if not camara_premium and 'camara' in orden_items:
                orden_items.remove('camara')

            # Procesar Fotos 
            foto_portada = request.files.get('foto_portada')
            url_portada = upload_to_cloudflare(foto_portada, folder=f"invitaciones/{slug}") if foto_portada and foto_portada.filename != '' else inv_old['foto_portada_url']

            img_fondo = request.files.get('imagen_fondo')
            url_fondo = upload_to_cloudflare(img_fondo, folder=f"invitaciones/{slug}/bg") if img_fondo and img_fondo.filename != '' else inv_old['url_fondo']

            fotos_galeria = request.files.getlist('fotos_galeria')
            urls_galeria = []
            for f in fotos_galeria:
                if f.filename != '':
                    urls_galeria.append(upload_to_cloudflare(f, folder=f"invitaciones/{slug}/galeria"))
            
            if not urls_galeria:
                urls_galeria = json.loads(inv_old['fotos_json']) if inv_old['fotos_json'] else []

            # ACTUALIZAR EN BASE DE DATOS
            # ACTUALIZAR EN BASE DE DATOS
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
                slug, json.dumps(orden_items), musica_id or None, fecha_evento, vigencia, json.dumps(datos_cliente), 
                json.dumps(urls_galeria), url_portada, estilo_fuente, color_fondo, url_fondo, json.dumps(mesas_regalos),
                dress_code, json.dumps(hoteles_sugeridos), album_url, camara_premium, color_acentos,
                padres_novia, padres_novio, padrinos, frase_final, template_id, tiene_modulo_invitados,
                codigo_cliente, bloquear_edicion, estilo_apertura,
                tipo_evento, json.dumps(historia_lista),
                id               
            ))
            conn.commit()
            flash("¡Invitación actualizada exitosamente! ✏️", "success")
            return redirect(url_for('invitaciones_admin.gestionar_invitaciones'))
            
        except Exception as e:
            conn.rollback()
            flash(f"Error al actualizar: {str(e)}", "danger")

    # --- MÉTODO GET ---
    inv = conn.execute("SELECT * FROM invitaciones WHERE id = ?", (id,)).fetchone()
    canciones = conn.execute("SELECT * FROM lista_musica WHERE activa = 1").fetchall()
    conn.close()

    inv = dict(inv)

    if not inv:
        flash("Invitación no encontrada.", "danger")
        return redirect(url_for('invitaciones_admin.gestionar_invitaciones'))

    datos = json.loads(inv['datos_cliente_json']) if inv['datos_cliente_json'] else {}
    mesas = json.loads(inv['mesas_regalos_json']) if inv['mesas_regalos_json'] else []
    hoteles = json.loads(inv['hospedaje_json']) if inv['hospedaje_json'] else []
    
    return render_template('invitaciones/crear.html', 
                           inv=inv, 
                           datos=datos, 
                           mesas=mesas,
                           hoteles=hoteles,
                           canciones=canciones, 
                           edit_mode=True)


# --- RUTA 6: ELIMINAR INVITACIÓN ---
@invitaciones_bp.route('/admin/eliminar-invitacion/<int:id>', methods=['POST'])
@admin_required
def eliminar_invitacion(id):
    conn = get_db_connection()
    try:
        inv = conn.execute("SELECT foto_portada_url, url_fondo, fotos_json FROM invitaciones WHERE id = ?", (id,)).fetchone()
        
        if inv:
            if inv['foto_portada_url']:
                delete_from_cloudflare(inv['foto_portada_url'])
            
            if inv['url_fondo']:
                delete_from_cloudflare(inv['url_fondo'])
                
            if inv['fotos_json']:
                fotos_galeria = json.loads(inv['fotos_json'])
                for foto_url in fotos_galeria:
                    delete_from_cloudflare(foto_url)

            fotos_invitados = conn.execute("SELECT url FROM fotos_invitados WHERE invitacion_id = ?", (id,)).fetchall()
            for foto in fotos_invitados:
                if foto['url']:
                    delete_from_cloudflare(foto['url'])
            
            conn.execute("DELETE FROM fotos_invitados WHERE invitacion_id = ?", (id,))

        conn.execute("DELETE FROM invitaciones WHERE id = ?", (id,))
        conn.commit()
        
        flash("Invitación, galería y fotos eliminadas correctamente 🧹", "success")
        
    except Exception as e:
        conn.rollback()
        flash(f"Error al eliminar: {str(e)}", "danger")
    finally:
        conn.close()
        
    return redirect(url_for('invitaciones_admin.gestionar_invitaciones'))


@invitaciones_bp.route('/admin/ver-fotos/<int:id>')
@admin_required
def ver_fotos_invitados(id):
    conn = get_db_connection()
    try:
        inv = conn.execute("SELECT slug, datos_cliente_json FROM invitaciones WHERE id = ?", (id,)).fetchone()
        datos = json.loads(inv['datos_cliente_json'])
        
        fotos = conn.execute("""
            SELECT * FROM fotos_invitados 
            WHERE invitacion_id = ? 
            ORDER BY fecha_creacion DESC
        """, (id,)).fetchall()
        
        return render_template('invitaciones/galeria_admin.html', 
                               inv=inv, 
                               datos=datos, 
                               fotos=fotos,
                               inv_id=id)
    except Exception as e:
        flash(f"Error al cargar la galería: {str(e)}", "danger")
        return redirect(url_for('invitaciones_admin.gestionar_invitaciones'))
    finally:
        conn.close()


@invitaciones_bp.route('/admin/descargar-rollo/<int:id>')
@admin_required
def descargar_rollo_zip(id):
    conn = get_db_connection()
    try:
        inv = conn.execute("SELECT slug FROM invitaciones WHERE id = ?", (id,)).fetchone()
        fotos = conn.execute("SELECT url FROM fotos_invitados WHERE invitacion_id = ?", (id,)).fetchall()

        if not fotos:
            flash("No hay fotos para descargar.", "warning")
            return redirect(url_for('invitaciones_admin.ver_fotos_invitados', id=id))

        memory_file = io.BytesIO()
        fotos_añadidas = 0
        
        from routes.invitaciones_publicas import s3_client, BUCKET_NAME

        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for i, foto in enumerate(fotos):
                try:
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


@invitaciones_bp.route('/invitacion/<slug>')
def ver_invitacion(slug):
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


# --- RUTA PARA GESTIONAR PASES E INVITADOS ---
@invitaciones_bp.route('/admin/invitacion/<int:id>/invitados', methods=['GET', 'POST'])
@admin_required
def gestionar_pases(id):
    conn = get_db_connection()
    
    if request.method == 'POST':
        nombre_familia = request.form.get('nombre_familia')
        pases = request.form.get('pases_totales', 2)
        telefono = request.form.get('telefono')

        codigo_unico = str(uuid.uuid4())[:8].upper()
        
        try:
            conn.execute("""
                INSERT INTO pases_invitados (invitacion_id, nombre_familia, pases_totales, codigo_qr_unique, telefono)
                VALUES (?, ?, ?, ?, ?)
            """, (id, nombre_familia, pases, codigo_unico, telefono)) 
            conn.commit()
            flash(f"Pase para {nombre_familia} generado con éxito.", "success")
        except Exception as e:
            flash(f"Error: {str(e)}", "danger")

    inv = conn.execute("SELECT slug, id, codigo_acceso_cliente FROM invitaciones WHERE id = ?", (id,)).fetchone()
    invitados = conn.execute("SELECT * FROM pases_invitados WHERE invitacion_id = ? ORDER BY id DESC", (id,)).fetchall()
    conn.close()
    
    return render_template('invitaciones/pases_admin.html', inv=inv, invitados=invitados)

@invitaciones_bp.route('/admin/invitacion/<int:inv_id>/eliminar-pase/<int:pase_id>', methods=['POST'])
@admin_required
def eliminar_pase(inv_id, pase_id):
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM pases_invitados WHERE id = ? AND invitacion_id = ?", (pase_id, inv_id))
        conn.commit()
        flash("Pase de invitado eliminado correctamente 🗑️", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Error al eliminar el pase: {str(e)}", "danger")
    finally:
        conn.close()
        
    return redirect(url_for('invitaciones_admin.gestionar_pases', id=inv_id))

