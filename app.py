import os
from flask import Flask
from flask import Flask, session
from flask_apscheduler import APScheduler
from datetime import timedelta
from dotenv import load_dotenv

# Cargar variables de entorno desde el archivo .env
load_dotenv()

# Utilidad centralizada para fechas (UTC)
from utils.datetime_utils import now_utc

from db import init_db, get_db_connection

app = Flask(__name__)

# Leemos la llave del archivo .env. Si no existe, usa la cadena 'dev_key...' como respaldo
app.secret_key = os.getenv('SECRET_KEY', 'dev_key_fallback_insegura')

# Configuración de Debug (Opcional, si quieres controlarlo desde .env)
# Si en .env FLASK_DEBUG es 1, será True. Si no, False.
app.config['DEBUG'] = os.getenv('FLASK_DEBUG') == '1'

# =========================
# SESIÓN
# =========================
# Duración de sesión (no depende de timezone)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=31)

# =========================
# TAREA AUTOMÁTICA
# =========================
def tarea_limpieza():
    """
    Limpia cotizaciones vencidas.
    IMPORTANTE:
    - now_utc() asegura que el criterio sea consistente
    - la BD debe guardar fechas en UTC
    """
    with app.app_context():
        try:
            conn = get_db_connection()

            conn.execute(
                """
                DELETE FROM ventas
                WHERE estado = 'cotizacion'
                AND fecha_vencimiento < ?
                """,
                (now_utc(),)  #UTC, no hora del servidor
            )

            conn.commit()
            conn.close()

        except Exception as e:
            print("❌ Error en tarea_limpieza:", e)

# =========================
# BASE DE DATOS
# =========================
# Se ejecuta una sola vez al arrancar la app
init_db()

# =========================
# SCHEDULER
# =========================
scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()

# Evitar duplicar el job (CRÍTICO cuando Flask recarga)
if not scheduler.get_job('Limpieza'):
    scheduler.add_job(
        id='Limpieza',
        func=tarea_limpieza,
        trigger='interval',
        minutes=60,
        replace_existing=True
    )

# =========================
# CONTEXT PROCESSOR (GLOBAL)
# =========================
@app.context_processor
def inject_user_config():
    """
    Inyecta la configuración del negocio en TODAS las plantillas (HTML)
    automáticamente. Así el nombre de la empresa sale en Recetas, Inventario, etc.
    """
    # Valores por defecto (para el Login o si algo falla)
    default_config = {
        'nombre_empresa': 'Cotizador Sianeffects', # O el nombre genérico que quieras
        'slogan': '',
        'website': '',
        'margen_ganancia': 100
    }

    # Solo buscamos si el usuario ya inició sesión
    if 'user_id' in session:
        try:
            conn = get_db_connection()
            # Buscamos la config del usuario actual
            user_config = conn.execute('SELECT * FROM configuracion WHERE user_id = ?', (session['user_id'],)).fetchone()
            conn.close()

            if user_config:
                # ¡ÉXITO! Devolvemos la config real de la base de datos
                # Convertimos a dict por si acaso
                return {'config': dict(user_config)}
        except Exception as e:
            print(f"Error inyectando config: {e}")
    
    # Si no hay usuario logueado, devolvemos los defaults para que no truene
    return {'config': default_config}


# =========================
# REGISTRO DE RUTAS
# =========================
from routes.auth import auth_bp
from routes.main import main_bp
from routes.inventory import inventory_bp
from routes.admin import admin_bp
from routes.api import api_bp
from routes.catalogo import catalogo_bp
from routes.shipping import shipping_bp

app.register_blueprint(auth_bp)
app.register_blueprint(main_bp)
app.register_blueprint(inventory_bp)
app.register_blueprint(admin_bp, url_prefix='/admin')
app.register_blueprint(api_bp, url_prefix='/api')
app.register_blueprint(catalogo_bp)
app.register_blueprint(shipping_bp)

# =========================
# ANTI-CACHÉ
# =========================
@app.after_request
def add_header(response):
    """
    Evita cacheo agresivo del navegador
    (útil en sistemas con sesiones y roles)
    """
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.template_filter('now_local_format')
def now_local_format(value=None, tz_name='America/Monterrey'):
    try:
        tz = pytz.timezone(tz_name)
        return datetime.now(tz).strftime('%d/%m/%Y %H:%M')
    except Exception:
        return ''

# =========================
# MAIN
# =========================
if __name__ == '__main__':
    app.run(debug=True)
