# services/mail_service.py
from flask_mail import Message
from flask import current_app, render_template
from threading import Thread
from extensions import mail  # Importamos la instancia limpia

def send_async_email(app, msg):
    """Envía el correo usando el contexto de la aplicación en un hilo aparte."""
    with app.app_context():
        try:
            mail.send(msg)
        except Exception as e:
            app.logger.error(f"Error enviando correo SMTP: {str(e)}")

def enviar_correo_sian(subject, recipient, template, sender_alias="hola", **kwargs):
    """
    Función maestra para enviar correos.
    sender_alias: puede ser 'accounts', 'notifications', 'hola', 'dianareyes', etc.
    """
    # Obtenemos la instancia real de la app
    app = current_app._get_current_object()
    
    # Construimos el remitente con tu dominio de Namecheap
    # Ejemplo: Sianeffects <notifications@sianeffects.com>
    sender_email = f"{sender_alias}@sianeffects.com"
    full_sender = f"Sianeffects <{sender_email}>"
    
    msg = Message(subject, sender=full_sender, recipients=[recipient])
    
    # Renderiza el HTML desde la carpeta templates/emails/
    try:
        msg.html = render_template(f"emails/{template}.html", **kwargs)
    except Exception as e:
        app.logger.error(f"Error renderizando template de email: {str(e)}")
        return False

    # Disparamos el hilo para no bloquear la ejecución principal
    Thread(target=send_async_email, args=(app, msg)).start()
    return True