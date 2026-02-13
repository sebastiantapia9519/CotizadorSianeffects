import boto3 # Librería estándar para R2/S3
from flask import current_app

def upload_to_cloudflare(file, folder="invitaciones"):
    s3 = boto3.client('s3',
        # Pega tu URL completa aquí directamente entre comillas
        endpoint_url="https://5dad301112cb3db90de60278e5d4e101.r2.cloudflarestorage.com",
        
        # Pega tus claves directamente aquí (luego las pasamos a variables de entorno para seguridad)
        aws_access_key_id="8d6b5dc8d9b01a8196b9e1a7d3e425f600cefad5e189bf42f026...", 
        aws_secret_access_key="TU_SECRET_KEY_AQUI_LA_QUE_EMPIEZA_CON_NUMEROS_O_LETRAS",
        
        region_name='auto'
    )
    
    filename = f"{folder}/{file.filename}"
    # Extra: Definimos el ContentType para que el navegador sepa si es imagen o audio
    content_type = file.content_type
    s3.upload_fileobj(
        file, 
        current_app.config['sianeffectscatalogo'],
        filename, 
        ExtraArgs={'ContentType': content_type}
        )
    
    return f"https://media.sianeffects.com/{filename}"