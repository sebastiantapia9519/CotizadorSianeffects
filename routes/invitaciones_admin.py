import json

@invitaciones_bp.route('/admin/nueva-invitacion', methods=['GET', 'POST'])
def crear_invitacion():
    if request.method == 'POST':
        # 1. Recibimos los datos básicos
        slug = request.form.get('slug')
        musica_id = request.form.get('musica_id')
        
        # 2. Guardamos el orden de los ítems (JSON)
        # Esto te permite decir: "primero el countdown, luego mapa, luego galería"
        orden_items = request.form.getlist('orden[]') 
        
        # 3. Subimos fotos a Cloudflare y guardamos URLs
        fotos = request.files.getlist('fotos')
        urls_fotos = [upload_to_cloudflare(f) for f in fotos]
        
        # 4. Guardamos en la base de datos independiente
        # ... lógica de db.execute ...
        
        return "¡Invitación creada con éxito!"
    
    # Aquí traeríamos tus 5 canciones activas para el dropdown
    canciones = db.execute("SELECT * FROM lista_musica WHERE activa = 1").fetchall()
    return render_template('invitaciones/editor.html', canciones=canciones)

@invitaciones_bp.route('/admin/musica', methods=['GET', 'POST'])
@admin_required
def gestionar_musica():
    conn = get_invitaciones_db() # Tu nueva conexión a invitaciones.db
    if request.method == 'POST':
        nombre = request.form.get('nombre')
        archivo = request.files.get('archivo')
        
        if archivo:
            url_audio = upload_to_cloudflare(archivo, folder="musica")
            conn.execute("INSERT INTO lista_musica (nombre_cancion, url_cloudflare) VALUES (?, ?)",
                         (nombre, url_audio))
            conn.commit()
            flash("Canción añadida con éxito", "success")
            
    canciones = conn.execute("SELECT * FROM lista_musica").fetchall()
    conn.close()
    return render_template('invitaciones/musica.html', canciones=canciones)

import json

@invitaciones_bp.route('/admin/nueva-invitacion', methods=['GET', 'POST'])
@admin_required
def crear_invitacion():
    conn = get_invitaciones_db()
    
    if request.method == 'POST':
        # Datos del formulario
        slug = request.form.get('slug')
        musica_id = request.form.get('musica_id')
        fecha_evento = request.form.get('fecha_evento')
        vigencia = request.form.get('vigencia')
        
        # Datos del cliente (JSON)
        datos_cliente = {
            "novios": request.form.get('nombres_novios'),
            "frase": request.form.get('frase'),
            "maps_misa": request.form.get('maps_misa'),
            "maps_fiesta": request.form.get('maps_fiesta'),
            "cuenta_bancaria": request.form.get('cuenta_bancaria')
        }
        
        # Orden de ítems (Ej: ["countdown", "galeria", "mapas"])
        orden_items = request.form.getlist('orden_items[]')
        
        # Manejo de fotos
        fotos = request.files.getlist('fotos')
        urls_fotos = []
        for f in fotos:
            if f.filename != '':
                url = upload_to_cloudflare(f, folder=f"invitaciones/{slug}")
                urls_fotos.append(url)

        # INSERT en la base de datos
        conn.execute("""
            INSERT INTO invitaciones 
            (slug, config_json, musica_id, fecha_evento, vigencia, datos_cliente_json, fotos_json) 
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            slug, 
            json.dumps(orden_items), 
            musica_id, 
            fecha_evento, 
            vigencia, 
            json.dumps(datos_cliente), 
            json.dumps(urls_fotos)
        ))
        conn.commit()
        conn.close()
        flash(f"Invitación {slug} creada correctamente", "success")
        return redirect(url_for('invitaciones_admin.gestionar_invitaciones'))

    canciones = conn.execute("SELECT * FROM lista_musica WHERE activa = 1").fetchall()
    conn.close()
    return render_template('invitaciones/crear.html', canciones=canciones)