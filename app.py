from flask import Flask
from flask_apscheduler import APScheduler
from datetime import timedelta, datetime
from db import init_db, get_db_connection

# 1. INITIALIZE APP FIRST
app = Flask(__name__)
app.secret_key = 'sianeffects_master_key_final'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=31)

# 2. DATABASE & TASKS
init_db()

scheduler = APScheduler()
def tarea_limpieza():
    with app.app_context():
        try:
            conn = get_db_connection()
            r = conn.execute("DELETE FROM ventas WHERE estado='cotizacion' AND fecha_vencimiento < ?", (datetime.now(),)).rowcount
            conn.commit(); conn.close()
            if r > 0: print(f"🧹 [AUTO] {r} cotizaciones borradas.")
        except: pass
scheduler.add_job(id='Limpieza', func=tarea_limpieza, trigger='interval', minutes=60)
scheduler.init_app(app); scheduler.start()

# 3. ANTI-CACHE
@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

# 4. REGISTER BLUEPRINTS (IMPORT HERE TO AVOID CIRCULAR IMPORT)
from routes.auth import auth_bp
from routes.main import main_bp
from routes.inventory import inventory_bp
from routes.admin import admin_bp
from routes.api import api_bp

app.register_blueprint(auth_bp)
app.register_blueprint(main_bp)
# IMPORTANT: URL PREFIXES
app.register_blueprint(inventory_bp, url_prefix='/inventory')
app.register_blueprint(admin_bp, url_prefix='/admin')
app.register_blueprint(api_bp, url_prefix='/api')

if __name__ == '__main__':
    app.run(debug=True)