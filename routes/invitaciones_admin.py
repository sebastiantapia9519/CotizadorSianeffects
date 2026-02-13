import json
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from helpers import admin_required
from db import get_db_connection  # Usamos tu conexión principal
from services.cloudflare_service import upload_to_cloudflare # Importamos el servicio de subida

# Definimos el Blueprint
invitaciones_bp = Blueprint('invitaciones_admin', __name__)

# --- RUTA 1: CONSTRUCTOR DE INVITACIONES ---
@invitaciones_bp.route('/admin/nueva-invitacion', methods=['GET', 'POST'])
@admin_required
def crear_invitacion():
    conn = get_db_connection()
    
    if request.method == 'POST':
        try:
            # 1. Datos del formulario
            slug = request.form.get('slug')
            musica_id = request.form.get('musica_id')
            fecha_evento = request.form.get('fecha_evento')
            vigencia = request.form.get('vigencia')
            
            # 2. Datos del cliente (JSON)
            datos_cliente = {
                "novios": request.form.get('nombres_novios'),
                "frase": request.form.get('frase'),
                "maps_misa": request.form.get('maps_misa'),
                "maps_fiesta": request.form.get('maps_fiesta'),
                "cuenta_bancaria": request.form.get('cuenta_bancaria'),
                "telefono_rsvp": request.form.get('telefono_rsvp')
            }
            
            # 3. Orden de ítems (JSON)
            orden_items = request.form.getlist('orden_items[]')
            
            # 4. Manejo de fotos (Subida a Cloudflare)
            fotos = request.files.getlist('fotos')
            urls_fotos = []
            
            for f in fotos:
                if f.filename != '':
                    # Subimos a carpeta específica del evento
                    url = upload_to_cloudflare(f, folder=f"invitaciones/{slug}")
                    urls_fotos.append(url)

            # 5. Guardar en la Base de Datos Principal
            conn.execute("""
                INSERT INTO invitaciones 
                (slug, config_json, musica_id, fecha_evento, vigencia, datos_cliente_json, fotos_json) 
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                slug, 
                json.dumps(orden_items), 
                musica_id or None, # Si viene vacío, guarda NULL
                fecha_evento, 
                vigencia, 
                json.dumps(datos_cliente), 
                json.dumps(urls_fotos)
            ))
            conn.commit()
            flash(f"¡Invitación para {datos_cliente['novios']} creada con éxito!", "success")
            
            # Redirigir a la misma página (o a una lista si la creamos después)
            return redirect(url_for('invitaciones_admin.crear_invitacion'))
            
        except Exception as e:
            conn.rollback()
            flash(f"Error al crear invitación: {str(e)}", "danger")
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
        import json
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