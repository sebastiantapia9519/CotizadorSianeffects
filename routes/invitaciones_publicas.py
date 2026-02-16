import base64
import uuid
from flask import Blueprint, render_template, request, jsonify

# (Asegúrate de importar tus funciones de R2 y BD que ya usas en tu proyecto)
# from utils.r2_helper import subir_archivo_r2

@main_bp.route('/rollo-invitados/<int:invitacion_id>')
def abrir_camara(invitacion_id):
    # Solo renderizamos el HTML y le pasamos el ID de la boda
    # para que la app sepa a qué boda pertenece la foto.
    return render_template('camara.html', inv_id=invitacion_id)

@main_bp.route('/api/upload_rollo/<int:invitacion_id>', methods=['POST'])
def upload_rollo(invitacion_id):
    try:
        data = request.get_json()
        imagen_b64 = data.get('imagen')
        
        if not imagen_b64:
            return jsonify({'success': False, 'error': 'No se recibió imagen'}), 400

        # Limpiar el encabezado del base64 (data:image/jpeg;base64,....)
        if ',' in imagen_b64:
            imagen_b64 = imagen_b64.split(',')[1]

        # Decodificar la imagen a bytes
        img_bytes = base64.b64decode(imagen_b64)
        
        # Generar un nombre único para la foto
        nombre_archivo = f"boda_{invitacion_id}/rollo_invitados/foto_{uuid.uuid4().hex[:8]}.jpg"
        
        # AQUÍ ES DONDE SUBES A R2 (Usa tu función existente de Cloudflare)
        # Ejemplo: url_r2 = subir_archivo_r2(img_bytes, nombre_archivo, 'image/jpeg')
        
        # Opcional: Guardar el registro en la base de datos si quieres
        # conn = get_db()
        # conn.execute("INSERT INTO fotos_rollo (invitacion_id, url) VALUES (?, ?)", (invitacion_id, url_r2))
        # conn.commit()
        # conn.close()
        
        return jsonify({'success': True, 'mensaje': 'Foto revelada con éxito'})
        
    except Exception as e:
        print(f"Error procesando foto del rollo: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500