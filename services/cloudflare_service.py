import boto3 # Librería estándar para R2/S3
from flask import current_app

def upload_to_cloudflare(file, folder="invitaciones"):
    # Aquí configurarás tus credenciales de Cloudflare R2
    s3 = boto3.client('s3',
        endpoint_url=f"https://e063cc1ad223c0544aee7a03d9f0f9a6.r2.cloudflarestorage.com",
        aws_access_key_id=current_app.config['5dad301112cb3db90de60278e5d4e101'],
        aws_secret_access_key=current_app.config['8d6b5dc8d9b01a8196b9e1a7d3e425f600cefad5e189bf42f0264edde035ab70'],
        region_name = 'auto'
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