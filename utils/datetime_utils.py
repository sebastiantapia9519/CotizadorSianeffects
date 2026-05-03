from datetime import datetime, timezone, timedelta
from dateutil.relativedelta import relativedelta
import pytz

# 1. Obtener fecha UTC (Objeto real, indispensable para Postgres)
def now_utc():
    return datetime.now(timezone.utc)

# 2. Convertir UTC a Local (Sigue igual, está bien hecha)
def utc_to_local(utc_dt, timezone_str='America/Mexico_City'):
    if utc_dt is None:
        return None
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    tz = pytz.timezone(timezone_str)
    return utc_dt.astimezone(tz)

# 3. Formatear para el template (Visual)
def local_to_template(local_dt, timezone_str='America/Mexico_City'):
    if local_dt is None: return None
    if isinstance(local_dt, str): return local_dt[:16] # Por si llega texto
    
    tz = pytz.timezone(timezone_str)
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=pytz.utc)
    
    return local_dt.astimezone(tz).strftime('%Y-%m-%d %H:%M')

# --- LAS FUNCIONES CLAVE PARA EL DASHBOARD ---

# NUEVA: Devuelve objeto DATETIME real (Para comparaciones en Postgres)
def ahora_objeto(dias=0, meses=0):
    dt = datetime.now(timezone.utc)
    return dt + relativedelta(days=dias, months=meses)

# MODIFICADA: ahora_sql ahora puede devolver Objeto u String
def ahora_sql(dias=0, meses=0, as_string=False):
    dt = ahora_objeto(dias=dias, meses=meses)
    if as_string:
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    return dt # Por defecto devolvemos objeto para que Postgres no truene

# 4. Otras utilidades ajustadas
def hoy_local(timezone_str='America/Mexico_City'):
    return utc_to_local(now_utc(), timezone_str).strftime('%Y-%m-%d')

def fecha_sql(days=0, as_string=True):
    dt = now_utc() + timedelta(days=days)
    return dt.strftime('%Y-%m-%d') if as_string else dt

def fecha_mas_dias(dias, timezone_str='America/Mexico_City'):
    return (utc_to_local(now_utc(), timezone_str) + timedelta(days=dias)).strftime('%Y-%m-%d')

def hoy_sqlite():
    """
    Función de compatibilidad para evitar errores de importación.
    Devuelve la fecha actual en formato YYYY-MM-DD.
    """
    return now_utc().strftime('%Y-%m-%d')