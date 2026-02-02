from flask import Flask
from flask_apscheduler import APScheduler
from datetime import timedelta

# Utilidad centralizada para fechas (UTC)
from utils.datetime_utils import now_utc

from db import init_db, get_db_connection

app = Flask(__name__)
app.secret_key = 'sianeffects_master_key_final'

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
# REGISTRO DE RUTAS
# =========================
from routes.auth import auth_bp
from routes.main import main_bp
from routes.inventory import inventory_bp
from routes.admin import admin_bp
from routes.api import api_bp

app.register_blueprint(auth_bp)
app.register_blueprint(main_bp)
app.register_blueprint(inventory_bp)
app.register_blueprint(admin_bp, url_prefix='/admin')
app.register_blueprint(api_bp, url_prefix='/api')

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

# =========================
# MAIN
# =========================
if __name__ == '__main__':
    app.run(debug=True)
