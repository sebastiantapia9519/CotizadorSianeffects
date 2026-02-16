import base64
import io
import uuid
from flask import Blueprint, render_template, request, jsonify
from db import get_db_connection as get_db
import boto3
from botocore.config import Config

invitaciones_publicas_bp = Blueprint('invitaciones_publicas', __name__)

# =========================================================
# CONFIGURACIÓN CLOUDFLARE R2
# =========================================================
ACCESS_KEY = '5dad301112cb3db90de60278e5d4e101'
SECRET_KEY = '8d6b5dc8d9b01a8196b9e1a7d3e425f600cefad5e189bf42f0264edde035ab70'
ENDPOINT_URL = 'https://e063cc1ad223c0544aee7a03d9f0f9a6.r2.cloudflarestorage.com'
BUCKET_NAME = 'sianeffectscatalogo' # Usamos tu mismo bucket
PUBLIC_URL = 'https://pub-d954f01e33ff457ba37d3ede2d956690.r2.dev'

s3_client = boto3.client(
    service_name='s3',
    endpoint_url=ENDPOINT_URL,
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    region_name='auto',
    config=Config(signature_version='s3v4')
)

# =========================================================
# RUTAS PÚBLICAS PARA LA CÁMARA DESECHABLE
# =========================================================

@invitaciones_publicas_bp.route('/rollo-invitados/<int:invitacion_id>')
def abrir_camara(invitacion_id):
    # Aquí simplemente mostramos el HTML de la cámara al invitado
    # Le pasamos el ID para que sepa a qué boda subir las fotos
    return render_template('invitaciones/camara.html', inv_id=invitacion_id)

@invitaciones_publicas_bp.route('/api/upload_rollo/<int:invitacion_id>', methods=['POST'])
def upload_rollo(invitacion_id):
    try:
        data = request.get_json()
        imagen_b64 = data.get('imagen')
        
        if not imagen_b64:
            return jsonify({'success': False, 'error': 'No se recibió ninguna imagen'}), 400

        # 1. Limpiar el encabezado del base64 (data:image/jpeg;base64,....)
        if ',' in imagen_b64:
            imagen_b64 = imagen_b64.split(',')[1]

        # 2. Convertir el texto Base64 a bytes (el archivo real)
        img_bytes = base64.b64decode(imagen_b64)
        
        # 3. Crear un archivo en memoria (RAM) para que R2 lo pueda leer
        archivo_memoria = io.BytesIO(img_bytes)
        
        # 4. Generar un nombre único y ordenado en carpetas
        nombre_archivo = f"bodas/boda_{invitacion_id}/rollo_invitados/foto_{uuid.uuid4().hex[:8]}.jpg"
        
        # 5. Subir a Cloudflare R2 (indicando que es una imagen JPEG)
        s3_client.upload_fileobj(
            archivo_memoria, 
            BUCKET_NAME, 
            nombre_archivo,
            ExtraArgs={'ContentType': 'image/jpeg'} # ¡Súper importante para que se vea en el navegador!
        )
        
        # 6. Generar el link público final
        url_final = f"{PUBLIC_URL}/{nombre_archivo}"
        
        # 7. (Opcional pero RECOMENDADO) Guardar el link en tu base de datos
        conn = get_db()
        # Asumiendo que crearás una tabla llamada 'fotos_invitados'
        conn.execute('INSERT INTO fotos_invitados (invitacion_id, url) VALUES (?, ?)', (invitacion_id, url_final))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'mensaje': '¡Foto revelada!', 'url': url_final})
        
    except Exception as e:
        print(f"Error procesando foto del rollo: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500