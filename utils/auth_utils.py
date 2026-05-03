# utils/auth_utils.py
import secrets
import string

def generate_verification_code(length=6):
    """Genera un código numérico aleatorio de alta seguridad."""
    # Usamos secrets porque es criptográficamente seguro para contraseñas/tokens
    return ''.join(secrets.choice(string.digits) for _ in range(length))