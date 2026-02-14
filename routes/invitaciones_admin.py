import json
import re
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from helpers import admin_required
from datetime import datetime
from db import get_db_connection  # Usamos tu conexión principal
from services.cloudflare_service import upload_to_cloudflare, delete_from_cloudflare

# Definimos el Blueprint
invitaciones_bp = Blueprint('invitaciones_admin', __name__)

# --- RUTA 1: CONSTRUCTOR DE INVITACIONES ---
@invitaciones_bp.route('/admin/nueva-invitacion', methods=['GET', 'POST'])
@admin_required
def crear_invitacion():
    conn = get_db_connection()
    
    if request.method == 'POST':
        try:
            # 1. Datos Básicos y Limpieza de Slug
            raw_slug = request.form.get('slug', '').strip()
            # Esto convierte "  Boda Diana & Sebastian!  " en "boda-diana-y-sebastian"
            slug = re.sub(r'[^\w\-]+', '', re.sub(r'[\s]+', '-', raw_slug.lower()))
            
            musica_id = request.form.get('musica_id')
            
            # --- NUEVOS CAMPOS ---
            dress_code = request.form.get('dress_code')
            album_url = request.form.get('album_url')
            
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

            datos_cliente = {
                "novios": request.form.get('nombres_novios'),
                "frase": request.form.get('frase'),
                "maps_misa": request.form.get('maps_misa'),
                "maps_fiesta": request.form.get('maps_fiesta'),
                "cuenta_bancaria": request.form.get('cuenta_bancaria'),
                "telefono_rsvp": request.form.get('telefono_rsvp'),
                "info_transporte": request.form.get('info_transporte') # Nuevo texto de transporte
            }
            
            # --- SUBIDA DE IMÁGENES ---
            # 1. Portada (Individual)
            foto_portada = request.files.get('foto_portada')
            url_portada = None
            if foto_portada:
                url_portada = upload_to_cloudflare(foto_portada, folder=f"invitaciones/{slug}")

            # 2. Fondo (Opcional)
            img_fondo = request.files.get('imagen_fondo')
            url_fondo = None
            if img_fondo:
                url_fondo = upload_to_cloudflare(img_fondo, folder=f"invitaciones/{slug}/bg")

            # 3. Galería (Múltiple)
            fotos_galeria = request.files.getlist('fotos_galeria')
            urls_galeria = []
            for f in fotos_galeria:
                if f.filename != '':
                    url = upload_to_cloudflare(f, folder=f"invitaciones/{slug}/galeria")
                    urls_galeria.append(url)

            # --- INSERT ---
            conn.execute("""
                INSERT INTO invitaciones 
                (slug, config_json, musica_id, fecha_evento, vigencia, datos_cliente_json, 
                 fotos_json, foto_portada_url, estilo_fuente, color_fondo, url_fondo, mesas_regalos_json,
                 dress_code, hospedaje_json, album_url) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                slug, 
                json.dumps(request.form.getlist('orden_items[]')), 
                musica_id or None, 
                request.form.get('fecha_evento'), 
                request.form.get('vigencia'), 
                json.dumps(datos_cliente), 
                json.dumps(urls_galeria),
                url_portada,
                request.form.get('estilo_fuente'),
                request.form.get('color_fondo'),
                url_fondo,
                json.dumps(mesas_regalos),
                dress_code,
                json.dumps(hoteles_sugeridos),
                album_url
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
        canciones = [] # Por si la tabla estuviera vacía o error
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
        # 1. Subimos a Cloudflare (carpeta musica)
        url_audio = upload_to_cloudflare(archivo, folder="musica")
        
        # 2. Guardamos en DB Principal
        cursor = conn.cursor()
        cursor.execute("INSERT INTO lista_musica (nombre_cancion, url_cloudflare) VALUES (?, ?)", (nombre, url_audio))
        nuevo_id = cursor.lastrowid
        conn.commit()
        
        # 3. Devolvemos éxito al Frontend
        return jsonify({
            'success': True,
            'id': nuevo_id,
            'nombre': nombre
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# --- RUTA 3: VISTA PÚBLICA DE LA INVITACIÓN ---
@invitaciones_bp.route('/invitacion/<slug>')
def ver_invitacion(slug):
    conn = get_db_connection()
    try:
        # 1. Buscar la invitación por Slug
        inv = conn.execute("""
            SELECT i.*, m.url_cloudflare as musica_url 
            FROM invitaciones i
            LEFT JOIN lista_musica m ON i.musica_id = m.id
            WHERE i.slug = ?
        """, (slug,)).fetchone()
        
        if not inv:
            return "<h1>404 - Invitación no encontrada 😢</h1>", 404

        # 2. Convertir los textos JSON a objetos de Python reales
        config = json.loads(inv['config_json'])        # El orden de los bloques
        datos = json.loads(inv['datos_cliente_json'])  # Nombres, mapas, etc
        fotos = json.loads(inv['fotos_json'])          # Lista de URLs de fotos
        
        # 3. Renderizar la plantilla mágica
        return render_template('invitaciones/base_boda.html', 
                               inv=inv, 
                               config=config, 
                               datos=datos, 
                               fotos=fotos)
    except Exception as e:
        return f"Error cargando invitación: {str(e)}", 500
    finally:
        conn.close()


@invitaciones_bp.app_template_filter('from_json')
def from_json(value):
    return json.loads(value)


# --- RUTA 4: PANEL DE GESTIÓN (DASHBOARD) ---
@invitaciones_bp.route('/admin/invitaciones')
@admin_required
def gestionar_invitaciones():
    conn = get_db_connection()
    try:
        # Traemos todas las invitaciones, las más recientes primero
        invs_db = conn.execute("SELECT * FROM invitaciones ORDER BY id DESC").fetchall()
        
        # Lista para guardar las invitaciones ya procesadas
        invitaciones = []
        for inv in invs_db:
            inv_dict = dict(inv) # Convertimos el Row de SQLite a Diccionario
            try:
                # Extraemos los nombres de los novios del JSON
                inv_dict['datos_cliente'] = json.loads(inv['datos_cliente_json'])
            except:
                inv_dict['datos_cliente'] = {"novios": "Sin Nombre"}
            
            invitaciones.append(inv_dict)
            
        # Fecha de hoy para calcular si el link sigue activo o ya venció
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
            dress_code = request.form.get('dress_code')
            album_url = request.form.get('album_url')

            # 1. Datos del cliente
            datos_cliente = {
                "novios": request.form.get('nombres_novios'),
                "frase": request.form.get('frase'),
                "maps_misa": request.form.get('maps_misa'),
                "maps_fiesta": request.form.get('maps_fiesta'),
                "cuenta_bancaria": request.form.get('cuenta_bancaria'),
                "telefono_rsvp": request.form.get('telefono_rsvp'),
                "info_transporte": request.form.get('info_transporte')
            }
            
            # 2. Mesas de regalos
            nombres_tiendas = request.form.getlist('nombre_tienda[]')
            links_tiendas = request.form.getlist('link_tienda[]')
            mesas_regalos = [{'nombre': n, 'url': l} for n, l in zip(nombres_tiendas, links_tiendas) if n and l]
            
            # 3. Hoteles (NUEVO)
            nombres_hoteles = request.form.getlist('nombre_hotel[]')
            links_hoteles = request.form.getlist('link_hotel[]')
            hoteles_sugeridos = [{'nombre': n, 'url': l} for n, l in zip(nombres_hoteles, links_hoteles) if n and l]

            orden_items = request.form.getlist('orden_items[]')

            # 4. Traemos los datos viejos para no perder fotos si no suben nuevas
            inv_old = conn.execute("SELECT foto_portada_url, fotos_json, url_fondo FROM invitaciones WHERE id=?", (id,)).fetchone()

            # 5. Procesar Fotos (Solo se sube si eligen un archivo nuevo)
            foto_portada = request.files.get('foto_portada')
            url_portada = upload_to_cloudflare(foto_portada, folder=f"invitaciones/{slug}") if foto_portada and foto_portada.filename != '' else inv_old['foto_portada_url']

            img_fondo = request.files.get('imagen_fondo')
            url_fondo = upload_to_cloudflare(img_fondo, folder=f"invitaciones/{slug}/bg") if img_fondo and img_fondo.filename != '' else inv_old['url_fondo']

            fotos_galeria = request.files.getlist('fotos_galeria')
            urls_galeria = []
            for f in fotos_galeria:
                if f.filename != '':
                    urls_galeria.append(upload_to_cloudflare(f, folder=f"invitaciones/{slug}/galeria"))
            
            # Si no subieron fotos nuevas para galería, dejamos las que ya estaban
            if not urls_galeria:
                urls_galeria = json.loads(inv_old['fotos_json']) if inv_old['fotos_json'] else []

            # 6. ACTUALIZAR EN BASE DE DATOS
            conn.execute("""
                UPDATE invitaciones SET 
                slug=?, config_json=?, musica_id=?, fecha_evento=?, vigencia=?, datos_cliente_json=?, 
                fotos_json=?, foto_portada_url=?, estilo_fuente=?, color_fondo=?, url_fondo=?, mesas_regalos_json=?,
                dress_code=?, hospedaje_json=?, album_url=?
                WHERE id=?
            """, (
                slug, json.dumps(orden_items), musica_id or None, fecha_evento, vigencia,
                json.dumps(datos_cliente), json.dumps(urls_galeria), url_portada,
                estilo_fuente, color_fondo, url_fondo, json.dumps(mesas_regalos),
                dress_code, json.dumps(hoteles_sugeridos), album_url, id
            ))
            conn.commit()
            flash("¡Invitación actualizada exitosamente! ✏️", "success")
            return redirect(url_for('invitaciones_admin.gestionar_invitaciones'))
            
        except Exception as e:
            conn.rollback()
            flash(f"Error al actualizar: {str(e)}", "danger")

    # --- MÉTODO GET: PREPARAR DATOS PARA EL FORMULARIO ---
    inv = conn.execute("SELECT * FROM invitaciones WHERE id = ?", (id,)).fetchone()
    canciones = conn.execute("SELECT * FROM lista_musica WHERE activa = 1").fetchall()
    conn.close()

    if not inv:
        flash("Invitación no encontrada.", "danger")
        return redirect(url_for('invitaciones_admin.gestionar_invitaciones'))

    datos = json.loads(inv['datos_cliente_json']) if inv['datos_cliente_json'] else {}
    mesas = json.loads(inv['mesas_regalos_json']) if inv['mesas_regalos_json'] else []
    hoteles = json.loads(inv['hospedaje_json']) if inv['hospedaje_json'] else []
    
    # Renderizamos la misma plantilla, pero le pasamos los datos para rellenar
    return render_template('invitaciones/crear.html', 
                           inv=inv, 
                           datos=datos, 
                           mesas=mesas,
                           hoteles=hoteles,
                           canciones=canciones, 
                           edit_mode=True)


# --- RUTA 6: ELIMINAR INVITACIÓN (Y SUS ARCHIVOS EN R2) ---
@invitaciones_bp.route('/admin/eliminar-invitacion/<int:id>', methods=['POST'])
@admin_required
def eliminar_invitacion(id):
    conn = get_db_connection()
    try:
        # 1. Buscamos la invitación ANTES de borrarla para saber qué fotos tiene
        inv = conn.execute("SELECT foto_portada_url, url_fondo, fotos_json FROM invitaciones WHERE id = ?", (id,)).fetchone()
        
        if inv:
            # 2. Borrar Foto de Portada de R2
            if inv['foto_portada_url']:
                delete_from_cloudflare(inv['foto_portada_url'])
            
            # 3. Borrar Imagen de Fondo de R2
            if inv['url_fondo']:
                delete_from_cloudflare(inv['url_fondo'])
                
            # 4. Borrar todas las fotos de la Galería de R2
            if inv['fotos_json']:
                fotos_galeria = json.loads(inv['fotos_json'])
                for foto_url in fotos_galeria:
                    delete_from_cloudflare(foto_url)

        # 5. Finalmente, borramos el registro de la Base de Datos
        conn.execute("DELETE FROM invitaciones WHERE id = ?", (id,))
        conn.commit()
        
        flash("Invitación y archivos multimedia eliminados correctamente 🧹", "success")
        
    except Exception as e:
        conn.rollback()
        flash(f"Error al eliminar: {str(e)}", "danger")
    finally:
        conn.close()
        
    return redirect(url_for('invitaciones_admin.gestionar_invitaciones'))