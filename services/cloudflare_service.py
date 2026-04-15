import os
import io
import uuid
import boto3
from botocore.client import Config
from dotenv import load_dotenv
from PIL import Image
from werkzeug.utils import secure_filename
import logging

# ==============================================================================
# INICIALIZACION Y VARIABLES DE ENTORNO
# ==============================================================================
# Carga las variables del archivo .env al entorno de ejecucion
load_dotenv()

# Configuracion protegida de Cloudflare R2
ACCESS_KEY = os.getenv('R2_ACCESS_KEY')
SECRET_KEY = os.getenv('R2_SECRET_KEY')
ENDPOINT_URL = os.getenv('R2_ENDPOINT_URL')
BUCKET_NAME = os.getenv('R2_BUCKET_NAME')
PUBLIC_URL = os.getenv('R2_PUBLIC_URL')

# ==============================================================================
# FUNCIONES AUXILIARES
# ==============================================================================
def _get_s3_client():
    """
    Inicializa y retorna el cliente boto3 configurado para Cloudflare R2.
    Se encapsula para evitar repeticion de codigo en las funciones principales.
    """
    return boto3.client(
        service_name='s3',
        endpoint_url=ENDPOINT_URL,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name='auto',
        config=Config(signature_version='s3v4')
    )

# ==============================================================================
# SERVICIO DE SUBIDA Y COMPRESION
# ==============================================================================
def upload_to_cloudflare(file, folder="invitaciones"):
    """
    Recibe un objeto FileStorage de Flask, lo comprime a formato WebP si es una 
    imagen (preservando dimensiones maximas y transparencias), y lo sube a R2.
    Retorna la URL publica del archivo.
    """
    if not BUCKET_NAME or not ENDPOINT_URL:
        raise ValueError("Error de entorno: Las credenciales de R2 (BUCKET_NAME / ENDPOINT_URL) no estan definidas.")

    s3 = _get_s3_client()
    
    # Reinicia el puntero del archivo por si fue leido previamente en validaciones
    file.seek(0)
    
    # Sanitizacion de seguridad del nombre de archivo original
    safe_original_filename = secure_filename(file.filename)
    content_type = file.content_type or 'application/octet-stream'
    
    # --- LOGICA DE COMPRESION Y OPTIMIZACION (SOLO IMAGENES RASTERIZADAS) ---
    if content_type.startswith('image/') and 'svg' not in content_type:
        try:
            # 1. Carga de imagen en memoria RAM
            img = Image.open(file)
            
            # 2. Manejo inteligente de canales de color y transparencia
            # Si tiene transparencia (RGBA, Paleta, o Luminancia+Alpha), la forzamos a RGBA
            if img.mode in ('RGBA', 'P', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                img = img.convert('RGBA')
            # Si es otro formato (ej. CMYK), lo forzamos al estandar RGB
            elif img.mode != 'RGB':
                img = img.convert('RGB')
                
            # 3. Redimensionamiento proporcional (Maximo 1200px de ancho)
            max_width = 1200
            if img.width > max_width:
                ratio = max_width / float(img.width)
                new_height = int((float(img.height) * float(ratio)))
                img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
            
            # 4. Codificacion a formato optimizado WebP
            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format='WEBP', quality=80)
            img_byte_arr.seek(0)
            
            # 5. Asignacion de nombres y preparacion del buffer
            nuevo_nombre = f"{uuid.uuid4().hex}.webp"
            filename = f"{folder}/{nuevo_nombre}"
            file_to_upload = img_byte_arr
            content_type = 'image/webp'
            
        except Exception as e:
            logging.warning(f"R2_COMPRESSION_WARNING: Error comprimiendo imagen ({e}). Procediendo con subida original de '{safe_original_filename}'.")
            # Fallback de seguridad: Si la libreria Pillow falla, subimos el archivo original
            file.seek(0)
            nuevo_nombre = f"{uuid.uuid4().hex}_{safe_original_filename}"
            filename = f"{folder}/{nuevo_nombre}"
            file_to_upload = file
    else:
        # --- LOGICA PARA ARCHIVOS NO-IMAGEN (Audio, PDF, SVG, etc.) ---
        nuevo_nombre = f"{uuid.uuid4().hex}_{safe_original_filename}"
        filename = f"{folder}/{nuevo_nombre}"
        file_to_upload = file

    # --- SUBIDA A CLOUDFLARE R2 ---
    s3.upload_fileobj(
        file_to_upload, 
        BUCKET_NAME, 
        filename,
        ExtraArgs={'ContentType': content_type}
    )
    
    # Retorna la URL concatenando el endpoint publico con la llave generada
    return f"{PUBLIC_URL}/{filename}"

# ==============================================================================
# SERVICIO DE ELIMINACION
# ==============================================================================
def delete_from_cloudflare(url_publica):
    """
    Toma la URL publica de Cloudflare devuelta previamente, extrae el Key (Ruta)
    y envia el comando de eliminacion al bucket R2 para liberar espacio.
    """
    if not url_publica:
        return False
        
    # Validacion: Nos aseguramos de extraer la ruta correctamente basados en el dominio base
    prefix = PUBLIC_URL + "/"
    
    if url_publica.startswith(prefix):
        # Aisla el Key requerido por el SDK de AWS
        file_key = url_publica[len(prefix):]
        
        try:
            s3 = _get_s3_client()
            s3.delete_object(Bucket=BUCKET_NAME, Key=file_key)
            return True
            
        except Exception as e:
            logging.error(f"R2_DELETE_ERROR: Fallo crítico al borrar el archivo '{file_key}' de R2 - {e}")
            current_app.logger.error(f"R2_DELETE_ERROR: Fallo crítico al borrar el archivo '{file_key}' de R2 - {e}")
            return False
            
    return False