# services/mail_service.py
import os
import resend
from flask import current_app
from threading import Thread

# Inicializamos Resend con la API key del entorno
resend.api_key = os.environ.get("RESEND_API_KEY")

def _send_async(app, params):
    """Envía el correo en un hilo aparte para no bloquear la app."""
    with app.app_context():
        try:
            resend.Emails.send(params)
        except Exception as e:
            app.logger.error(f"Error enviando correo Resend: {str(e)}")

def enviar_correo_sian(subject, recipient, template, sender_alias="hola", **kwargs):
    """
    Función maestra para enviar correos via Resend.
    sender_alias: 'hola', 'contacto', 'notificaciones', etc.
    """
    app = current_app._get_current_object()

    sender_email = f"{sender_alias}@sianeffects.com"
    full_sender = f"Sianeffects <{sender_email}>"

    # Renderiza el HTML desde templates/emails/
    try:
        jinja_template = app.jinja_env.get_template(f"emails/{template}.html")
        html_content = jinja_template.render(**kwargs)
    except Exception as e:
        app.logger.error(f"Error renderizando template de email: {str(e)}")
        return False

    params = {
        "from": full_sender,
        "to": [recipient],
        "subject": subject,
        "html": html_content,
    }

    Thread(target=_send_async, args=(app, params)).start()
    return True