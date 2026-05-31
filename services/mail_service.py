# services/mail_service.py
import os
import resend
import time
from flask import current_app
from threading import Lock, Thread

# Inicializamos Resend con la API key del entorno
resend.api_key = os.environ.get("RESEND_API_KEY")

RESEND_MIN_INTERVAL_SECONDS = float(os.environ.get("RESEND_MIN_INTERVAL_SECONDS", "0.25"))
RESEND_MAX_RETRIES = int(os.environ.get("RESEND_MAX_RETRIES", "3"))

_resend_send_lock = Lock()
_last_resend_send_at = 0.0


def _wait_for_resend_slot():
    """Espacia los envios para respetar el limite de Resend."""
    global _last_resend_send_at

    with _resend_send_lock:
        now = time.monotonic()
        elapsed = now - _last_resend_send_at
        wait_seconds = RESEND_MIN_INTERVAL_SECONDS - elapsed

        if wait_seconds > 0:
            time.sleep(wait_seconds)

        _last_resend_send_at = time.monotonic()


def _send_async(app, params):
    """Envía el correo en un hilo aparte para no bloquear la app."""
    with app.app_context():
        for attempt in range(1, RESEND_MAX_RETRIES + 1):
            try:
                _wait_for_resend_slot()
                resend.Emails.send(params)
                return
            except Exception as e:
                error_message = str(e)
                is_rate_limit = "Too many requests" in error_message or "rate limit" in error_message.lower()

                if is_rate_limit and attempt < RESEND_MAX_RETRIES:
                    time.sleep(max(1.0, RESEND_MIN_INTERVAL_SECONDS * 2))
                    continue

                app.logger.error(f"Error enviando correo Resend: {error_message}")
                return

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
