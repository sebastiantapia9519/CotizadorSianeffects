import re

# ==========================================
# LISTA NEGRA DE DOMINIOS TEMPORALES
# ==========================================
# Usamos un 'set' (conjunto) porque la busqueda es instantanea,
# mucho mas rapido que buscar en una lista normal.
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
    Retorna True si el dominio del correo (o su dominio raiz) esta en la lista negra.
    Maneja errores si el email no tiene formato correcto y bloquea subdominios trampa.
    """
    if not email or '@' not in email:
        return False
        
    try:
        # Extraemos el dominio completo y lo limpiamos
        domain = email.split('@')[1].strip().lower()
        
        # BLINDAJE: Verificamos el dominio exacto y todas sus posibles raices
        # Si el usuario manda "juan@mail.devnull.net.uk", esto revisara:
        # 1. mail.devnull.net.uk
        # 2. devnull.net.uk (AQUI LO ATRAPA Y LO BLOQUEA)
        # 3. net.uk
        partes = domain.split('.')
        for i in range(len(partes) - 1):
            subdominio_a_revisar = '.'.join(partes[i:])
            if subdominio_a_revisar in DISPOSABLE_DOMAINS:
                return True
                
        return False
        
    except IndexError:
        return False

def is_valid_email_format(email):
    """
    Valida que sea un correo real (texto@texto.algo) estricto.
    No permite espacios en blanco ni basura al final del string.
    """
    if not email:
        return False
        
    # BLINDAJE: fullmatch asegura que toda la cadena sea el correo, nada de espacios (\\s) extras
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()))