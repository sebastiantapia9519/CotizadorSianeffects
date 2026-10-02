import re

# ==========================================
# LISTA NEGRA DE DOMINIOS TEMPORALES
# ==========================================
# TODO (deuda tecnica): reemplazar/complementar esta lista manual con una
# mantenida por la comunidad (paquete "disposable-email-domains").
# Mientras tanto, agrega aqui los dominios que vayas detectando en tus logs.
DISPOSABLE_DOMAINS = {
    # --- YOPMAIL y variantes ---
    "yopmail.com", "yopmail.fr", "yopmail.net", "cool.fr.nf", "jetable.fr.nf", "nospam.ze.tc",
    "nomail.xl.cx", "mega.zik.dj", "speed.1s.fr", "courriel.fr.nf", "moncourrier.fr.nf",
    "monemail.fr.nf", "monmail.fr.nf",

    # --- MAILINATOR y familia ---
    "mailinator.com", "binkmail.com", "bobmail.info", "chammy.info", "devnull.net.uk",
    "letthemeatspam.com", "mailinater.com", "reallymymail.com", "reconmail.com", "trashmail.net",

    # --- GUERRILLA MAIL ---
    "guerrillamail.com", "guerrillamailblock.com", "sharklasers.com", "guerrillamail.net",
    "guerrillamail.org", "grr.la", "pokemail.net",

    # --- 10 MINUTE MAIL & TEMP MAIL ---
    "10minutemail.com", "10minutemail.net", "temp-mail.org", "tempmail.com",
    "temp-mail.ru", "tempmail.net",

    # --- OTROS POPULARES ---
    "throwawaymail.com", "getnada.com", "abogo.com", "getairmail.com", "dispostable.com",
    "fake-box.com", "maildrop.cc", "tempr.email", "trashmail.com", "incognitomail.org",
    "mailpoof.com", "mintemail.com",

    # --- DETECTADOS EN LOGS ---
    "formtests.info",
}

_GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}


def is_valid_email_format(email):
    """
    Valida formato estricto (texto@texto.algo), sin espacios ni basura,
    y longitud maxima razonable (RFC: 254).
    """
    if not email:
        return False
    email = email.strip()
    if len(email) > 254:
        return False
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email))


def is_disposable_email(email):
    """
    Retorna True si el dominio del correo (o cualquiera de sus dominios raiz)
    esta en la lista negra. Bloquea subdominios trampa.
    """
    if not email or '@' not in email:
        return False

    # rpartition toma lo que esta despues del ultimo '@'; rstrip('.') evita "yopmail.com."
    domain = email.rpartition('@')[2].strip().lower().rstrip('.')
    if not domain:
        return False

    # "juan@mail.devnull.net.uk" revisa: mail.devnull.net.uk, devnull.net.uk, net.uk
    partes = domain.split('.')
    for i in range(len(partes) - 1):
        if '.'.join(partes[i:]) in DISPOSABLE_DOMAINS:
            return True
    return False


def normalize_email(email):
    """
    Normaliza el correo para detectar duplicados/alias:
    - minusculas y sin espacios
    - Gmail: quita puntos y todo lo que va despues del '+'
    Guarda el resultado en una columna aparte (ej. email_normalizado, unique)
    para impedir multiples pruebas gratis con la misma cuenta real.
    """
    email = (email or "").strip().lower()
    if '@' not in email:
        return email

    local, _, domain = email.rpartition('@')
    if domain in _GMAIL_DOMAINS:
        local = local.split('+', 1)[0].replace('.', '')
        domain = "gmail.com"
    return f"{local}@{domain}"