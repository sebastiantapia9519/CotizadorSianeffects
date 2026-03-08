from datetime import datetime, timezone, timedelta
from dateutil.relativedelta import relativedelta
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

# Generar fecha local para mostrar en el template
def local_to_template(local_dt, timezone_str='America/Mexico_City'):
    if local_dt is None:
        return None

    tz = pytz.timezone(timezone_str)
    local_dt = local_dt.astimezone(tz)
    return local_dt.strftime('%Y-%m-%d %H:%M')

def sumar_dias_a_fecha(fecha_str, dias, formato_entrada='%Y-%m-%dT%H:%M', formato_salida='%Y-%m-%d'):
    dt = datetime.strptime(fecha_str, formato_entrada)
    return (dt + timedelta(days=dias)).strftime(formato_salida)

def hoy_local(timezone_str='America/Mexico_City'):
    return utc_to_local(now_utc(), timezone_str).strftime('%Y-%m-%d')

def hoy_sqlite():
    return now_utc().strftime('%Y-%m-%d')

def ahora_sql(dias=0, meses=0):
    dt = datetime.now(timezone.utc)
    dt = dt + relativedelta(days=dias, months=meses)
    return dt.strftime('%Y-%m-%d %H:%M:%S')

def fecha_sql(days=0):
    return (now_utc() + timedelta(days=days)).strftime('%Y-%m-%d')

def fecha_mas_dias(dias, timezone_str='America/Mexico_City'):
    """
    Devuelve una fecha YYYY-MM-DD sumando días desde hoy (UTC → local)
    """
    return (utc_to_local(now_utc(), timezone_str) + timedelta(days=dias)).strftime('%Y-%m-%d')