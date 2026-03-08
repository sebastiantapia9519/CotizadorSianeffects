import boto3
import os
import io
import uuid
from botocore.client import Config
from dotenv import load_dotenv
from PIL import Image  # 👈 IMPORTANTE: Asegúrate de hacer 'pip install Pillow'

# Carga las variables del archivo .env
load_dotenv()

# --- CONFIGURACIÓN PROTEGIDA ---
ACCESS_KEY = os.getenv('R2_ACCESS_KEY')
SECRET_KEY = os.getenv('R2_SECRET_KEY')
ENDPOINT_URL = os.getenv('R2_ENDPOINT_URL')
BUCKET_NAME = os.getenv('R2_BUCKET_NAME')
PUBLIC_URL = os.getenv('R2_PUBLIC_URL')

def upload_to_cloudflare(file, folder="invitaciones"):
    if not BUCKET_NAME or not ENDPOINT_URL:
            raise ValueError("🚨 ERROR: Las credenciales del .env no se cargaron correctamente. BUCKET_NAME es None.")

    # Inicializamos el cliente
    s3 = boto3.client(
        service_name='s3',
        endpoint_url=ENDPOINT_URL,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name='auto',
        config=Config(signature_version='s3v4')
    )
    
    # Aseguramos que el puntero del archivo esté al inicio
    file.seek(0)
    content_type = file.content_type
    
    # --- 🛡️ LÓGICA INTELIGENTE DE COMPRESIÓN (SOLO IMÁGENES) ---
    if content_type and content_type.startswith('image/') and 'svg' not in content_type:
        try:
            # 1. Abrimos la imagen en RAM
            img = Image.open(file)
            
            # 2. Convertimos a RGB (Evita errores al comprimir PNGs con fondo transparente)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
                
            # 3. Redimensionamos si es gigantesca (Max 1200px de ancho)
            max_width = 1200
            if img.width > max_width:
                ratio = max_width / float(img.width)
                new_height = int((float(img.height) * float(ratio)))
                img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
            
            # 4. Guardamos optimizada en memoria como WEBP
            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format='WEBP', quality=80)
            img_byte_arr.seek(0)
            
            # 5. Variables listas para subida
            nuevo_nombre = f"{uuid.uuid4().hex}.webp"
            filename = f"{folder}/{nuevo_nombre}"
            file_to_upload = img_byte_arr
            content_type = 'image/webp'
            
        except Exception as e:
            print(f"Error al comprimir imagen, subiendo original: {e}")
            # Fallback: si falla la compresión, subimos el original de forma segura
            file.seek(0)
            nuevo_nombre = f"{uuid.uuid4().hex}_{file.filename}"
            filename = f"{folder}/{nuevo_nombre}"
            file_to_upload = file
    else:
        # --- 🎵 LÓGICA PARA ARCHIVOS NO-IMAGEN (Música, etc.) ---
        nuevo_nombre = f"{uuid.uuid4().hex}_{file.filename}"
        filename = f"{folder}/{nuevo_nombre}"
        file_to_upload = file

    # --- ☁️ SUBIDA A CLOUDFLARE R2 ---
    s3.upload_fileobj(
        file_to_upload, 
        BUCKET_NAME, 
        filename,
        ExtraArgs={'ContentType': content_type}
    )
    
    # Retornamos la URL pública lista para usarse
    return f"{PUBLIC_URL}/{filename}"


#ELIMINAR MEDIA DE INVITACIONES
def delete_from_cloudflare(url_publica):
    """
    Recibe la URL pública completa (ej. https://pub-d954.../invitaciones/boda/foto.jpg)
    Extrae la ruta del archivo y lo elimina del bucket R2.
    """
    if not url_publica:
        return False
        
    # El prefijo es tu URL pública más una diagonal
    prefix = PUBLIC_URL + "/"
    
    # Verificamos que la URL realmente pertenezca a nuestro R2
    if url_publica.startswith(prefix):
        # Extraemos solo el nombre del archivo (ej. invitaciones/boda/foto.jpg)
        file_key = url_publica[len(prefix):]
        
        try:
            s3 = boto3.client(
                service_name='s3',
                endpoint_url=ENDPOINT_URL,
                aws_access_key_id=ACCESS_KEY,
                aws_secret_access_key=SECRET_KEY,
                region_name='auto',
                config=Config(signature_version='s3v4')
            )
            
            # Comando de eliminación de S3/R2
            s3.delete_object(Bucket=BUCKET_NAME, Key=file_key)
            return True
            
        except Exception as e:
            print(f"Error al borrar {file_key} de R2: {e}")
            return False
            
    return False