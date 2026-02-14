import boto3
from botocore.client import Config

# --- CONFIGURACIÓN DE CLOUDFLARE R2 (Copiada de tu catálogo) ---
ACCESS_KEY = '5dad301112cb3db90de60278e5d4e101'
SECRET_KEY = '8d6b5dc8d9b01a8196b9e1a7d3e425f600cefad5e189bf42f0264edde035ab70'
ENDPOINT_URL = 'https://e063cc1ad223c0544aee7a03d9f0f9a6.r2.cloudflarestorage.com'
BUCKET_NAME = 'sianeffectscatalogo'
PUBLIC_URL = 'https://pub-d954f01e33ff457ba37d3ede2d956690.r2.dev'

def upload_to_cloudflare(file, folder="invitaciones"):
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