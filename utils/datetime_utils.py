from datetime import datetime, timezone
import pytz

# Obtener fecha UTC para guardar en BD
def now_utc():
    return datetime.now(timezone.utc)

# Convertir fecha UTC a zona horaria del usuario
def utc_to_local(utc_dt, timezone_str='America/Mexico_City'):
    if utc_dt is None:
        return None

    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)

    tz = pytz.timezone(timezone_str)
    return utc_dt.astimezone(tz)
