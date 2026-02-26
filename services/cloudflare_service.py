import boto3
import os
from botocore.client import Config
from dotenv import load_dotenv

# Carga las variables del archivo .env
load_dotenv()

# --- CONFIGURACIÓN PROTEGIDA ---
# os.getenv busca el nombre exacto que pusiste en tu archivo .env
ACCESS_KEY = os.getenv('R2_ACCESS_KEY')
SECRET_KEY = os.getenv('R2_SECRET_KEY')
ENDPOINT_URL = os.getenv('R2_ENDPOINT_URL')
BUCKET_NAME = os.getenv('R2_BUCKET_NAME')
PUBLIC_URL = os.getenv('R2_PUBLIC_URL')


def upload_to_cloudflare(file, folder="invitaciones"):
    if not BUCKET_NAME or not ENDPOINT_URL:
            raise ValueError("🚨 ERROR: Las credenciales del .env no se cargaron correctamente. BUCKET_NAME es None.")

    # Inicializamos el cliente igual que en tu catálogo
    s3 = boto3.client(
        service_name='s3',
        endpoint_url=ENDPOINT_URL,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name='auto',
        config=Config(signature_version='s3v4')
    )
    
    # Definimos la ruta dentro del bucket
    filename = f"{folder}/{file.filename}"
    
    # Aseguramos que el puntero del archivo esté al inicio (buena práctica)
    file.seek(0)
    
    # Subimos el archivo
    # ExtraArgs={'ContentType': ...} ayuda a que el navegador sepa que es música o imagen
    s3.upload_fileobj(
        file, 
        BUCKET_NAME, 
        filename,
        ExtraArgs={'ContentType': file.content_type}
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