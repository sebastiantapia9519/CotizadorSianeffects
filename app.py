from flask import Flask
from flask_apscheduler import APScheduler
from datetime import timedelta, datetime
from db import init_db, get_db_connection

app = Flask(__name__)
app.secret_key = 'sianeffects_master_key_final'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=31)

# 1. BASE DE DATOS
init_db()

# 2. TAREAS AUTOMÁTICAS
scheduler = APScheduler()
def tarea_limpieza():
    with app.app_context():
        try:
            conn = get_db_connection()
            conn.execute("DELETE FROM ventas WHERE estado='cotizacion' AND fecha_vencimiento < ?", (datetime.now(),))
            conn.commit(); conn.close()
        except: pass
scheduler.add_job(id='Limpieza', func=tarea_limpieza, trigger='interval', minutes=60)
scheduler.init_app(app); scheduler.start()

# 3. REGISTRAR RUTAS
# Importamos aquí para evitar el "Círculo de la Muerte"
from routes.auth import auth_bp
from routes.main import main_bp
from routes.inventory import inventory_bp
from routes.admin import admin_bp
from routes.api import api_bp

app.register_blueprint(auth_bp)
app.register_blueprint(main_bp)

# --- ¡AQUÍ ESTÁ LA SOLUCIÓN DEL 404! ---
# Agregamos url_prefix para que coincida con el JavaScript
app.register_blueprint(inventory_bp, url_prefix='/inventory') 
app.register_blueprint(admin_bp, url_prefix='/admin')
app.register_blueprint(api_bp, url_prefix='/api')

# 4. ANTI-CACHÉ
@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

if __name__ == '__main__':
    app.run(debug=True)