import re

# ==========================================
# 🛑 LISTA NEGRA DE DOMINIOS TEMPORALES
# ==========================================
# Usamos un 'set' (conjunto) porque la búsqueda es instantánea,
# mucho más rápido que buscar en una lista normal.
DISPOSABLE_DOMAINS = {
    # --- YOPMAIL (El Rey) y sus variantes ---
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
    "throwawaymail.com",
    "getnada.com", "abogo.com", "getairmail.com",
    "dispostable.com",
    "fake-box.com",
    "maildrop.cc",
    "tempr.email",
    "trashmail.com",
    "incognitomail.org",
    "mailpoof.com",
    "mintemail.com"
}

def is_disposable_email(email):
    """
    Retorna True si el dominio del correo está en la lista negra.
    Maneja errores si el email no tiene formato correcto.
    """
    if not email or '@' not in email:
        return False
        
    try:
        # Separamos el usuario del dominio (ej: juan@yopmail.com -> yopmail.com)
        domain = email.split('@')[1].strip().lower()
        
        if domain in DISPOSABLE_DOMAINS:
            return True
        return False
    except IndexError:
        return False

def is_valid_email_format(email):
    """
    Valida que parezca un correo real (texto@texto.algo)
    """
    return re.match(r"[^@]+@[^@]+\.[^@]+", email)