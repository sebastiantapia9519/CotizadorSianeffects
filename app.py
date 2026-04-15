import os
from flask import Flask, session
from flask_apscheduler import APScheduler
import logging
from logging.handlers import RotatingFileHandler
import json
from datetime import timedelta
from dotenv import load_dotenv
from services.cloudflare_service import delete_from_cloudflare
import pytz

# Cargar variables de entorno desde el archivo .env
load_dotenv()

# Utilidad centralizada para fechas (UTC)
from utils.datetime_utils import now_utc

from db import init_db, get_db_connection

app = Flask(__name__)

# --- ESCUDO DE SEGURIDAD ---
# Limita la subida máxima por petición a 15 MB (Evita ataques DDoS o colapsos)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

# Leemos la llave del archivo .env. Si no existe, usa la cadena 'dev_key...' como respaldo
app.secret_key = os.getenv('SECRET_KEY', 'dev_key_fallback_insegura')

# Configuración de Debug (Opcional, si quieres controlarlo desde .env)
# Si en .env FLASK_DEBUG es 1, será True. Si no, False.
app.config['DEBUG'] = os.getenv('FLASK_DEBUG') == '1'

# Configuración del log
base_dir = os.path.abspath(os.path.dirname(__file__))
log_path = os.path.join(base_dir, 'limpieza.log')

logging.basicConfig(
    filename=log_path,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)
logging.info("SianEffects System Monitor: Iniciando registro de eventos...")

# Configuración del Log
log_path = os.path.join(app.root_path, 'limpieza.log')

# Formato: [Fecha] [Nivel] Mensaje
formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', '%d/%m/%Y %H:%M:%S')

# Manejador: Archivo de max 1MB, mantiene hasta 3 copias viejas
file_handler = RotatingFileHandler(log_path, maxBytes=1024 * 1024, backupCount=3)
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.INFO)

app.logger.addHandler(file_handler)
app.logger.info("Sistema de monitoreo iniciado correctamente")

# =========================
# SESIÓN
# =========================
# Duración de sesión (no depende de timezone)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=31)

# --- NUEVO: Blindaje para navegadores móviles (iOS/Android) ---
app.config['SESSION_COOKIE_SECURE'] = True      # Solo manda la cookie por HTTPS (PythonAnywhere ya tiene HTTPS)
app.config['SESSION_COOKIE_HTTPONLY'] = True    # Evita que el JavaScript de la página lea la cookie
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'   # Crucial para celulares: evita que se pierda la sesión al cambiar de app o de red (LTE/WiFi)

# =========================
# TAREAS AUTOMÁTICAS (JOBS)
# =========================
def tarea_limpieza():
    with app.app_context():
        try:
            conn = get_db_connection()
            cursor = conn.cursor() # ABRIMOS CURSOR
            ahora = now_utc() 
            str_ahora = ahora.strftime('%Y-%m-%d %H:%M:%S')
            
            # 1. Limpieza de cotizaciones vencidas
            cursor.execute("SELECT id FROM ventas WHERE estado = 'cotizacion' AND fecha_vencimiento < %s", (str_ahora,))
            vencidas = cursor.fetchall()

            if vencidas:
                ids_ven = [v['id'] for v in vencidas]
                placeholders = ', '.join(['%s'] * len(ids_ven)) # CAMBIO A %s
                ids_tuple = tuple(ids_ven)
                
                cursor.execute(f"DELETE FROM venta_detalles WHERE venta_id IN ({placeholders})", ids_tuple)
                cursor.execute(f"DELETE FROM ventas WHERE id IN ({placeholders})", ids_tuple)
                logging.info(f"Cotizaciones expiradas eliminadas de raíz: {len(ids_ven)}")

            # 2. Identificar usuarios (role 0) inactivos
            limite_12_meses = ahora - timedelta(days=365)
            str_limite_12 = limite_12_meses.strftime('%Y-%m-%d %H:%M:%S')
            
            cursor.execute("SELECT id, email FROM usuarios WHERE role = 0 AND subscription_end < %s", (str_limite_12,))
            usuarios_out = cursor.fetchall()

            if usuarios_out:
                ids = [u['id'] for u in usuarios_out]
                emails = [u['email'] for u in usuarios_out]
                ph = ', '.join(['%s'] * len(ids)) # CAMBIO A %s
                ids_tuple = tuple(ids)

                cursor.execute(f"DELETE FROM venta_detalles WHERE venta_id IN (SELECT id FROM ventas WHERE user_id IN ({ph}))", ids_tuple)
                cursor.execute(f"DELETE FROM producto_detalles WHERE producto_id IN (SELECT id FROM productos WHERE user_id IN ({ph}))", ids_tuple)
                cursor.execute(f"DELETE FROM producto_maquinaria WHERE producto_id IN (SELECT id FROM productos WHERE user_id IN ({ph}))", ids_tuple)
                cursor.execute(f"DELETE FROM shipping_rates WHERE zone_id IN (SELECT id FROM shipping_zones WHERE user_id IN ({ph}))", ids_tuple)

                tablas_directas = ['configuracion', 'maquinaria', 'materiales', 'movimientos_inventario', 'productos', 'ventas', 'shipping_configs', 'shipping_zones']
                for tabla in tablas_directas:
                    cursor.execute(f"DELETE FROM {tabla} WHERE user_id IN ({ph})", ids_tuple)

                cursor.execute(f"DELETE FROM usuarios WHERE id IN ({ph})", ids_tuple)
                for email in emails:
                    logging.warning(f"CUENTA ELIMINADA POR INACTIVIDAD: {email}")

            # 3. PURGA DE INVITACIONES OBSOLETAS
            fecha_limite_purga = ahora - timedelta(days=15)
            str_fecha_purga = fecha_limite_purga.strftime('%Y-%m-%d')

            cursor.execute("SELECT id, slug, foto_portada_url, url_fondo, fotos_json FROM invitaciones WHERE vigencia < %s", (str_fecha_purga,))
            invitaciones_basura = cursor.fetchall()

            if invitaciones_basura:
                for inv in invitaciones_basura:
                    inv_id = inv['id']
                    
                    if inv['foto_portada_url']: delete_from_cloudflare(inv['foto_portada_url'])
                    if inv['url_fondo']: delete_from_cloudflare(inv['url_fondo'])
                    if inv['fotos_json']:
                        try:
                            fotos_galeria = json.loads(inv['fotos_json'])
                            for foto_url in fotos_galeria: delete_from_cloudflare(foto_url)
                        except Exception as e:
                            logging.error(f"Error borrando galería R2 inv {inv_id}: {e}")
                            current_app.logger.error(f"Error borrando galería R2 inv {inv_id}: {e}")
                            
                    cursor.execute("SELECT url FROM fotos_invitados WHERE invitacion_id = %s", (inv_id,))
                    fotos_camara = cursor.fetchall()
                    for fc in fotos_camara:
                        if fc['url']: delete_from_cloudflare(fc['url'])

                    cursor.execute("DELETE FROM fotos_invitados WHERE invitacion_id = %s", (inv_id,))
                    cursor.execute("DELETE FROM pases_invitados WHERE invitacion_id = %s", (inv_id,))
                    cursor.execute("DELETE FROM buenos_deseos WHERE invitacion_id = %s", (inv_id,))
                    cursor.execute("DELETE FROM invitaciones WHERE id = %s", (inv_id,))

            conn.commit()
            cursor.close()

        except Exception as e:
            logging.error(f"Error en tarea_limpieza general: {str(e)}")
            current_app.logger.error(f"Error en tarea_limpieza general: {str(e)}")
        finally:
            conn.close()

def tarea_canceladas():
    with app.app_context():
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM ventas WHERE estado = 'cancelada'")
            canceladas = cursor.fetchall()
            
            if canceladas:
                ids_canc = [c['id'] for c in canceladas]
                placeholders = ', '.join(['%s'] * len(ids_canc))
                ids_tuple = tuple(ids_canc)
                
                cursor.execute(f"DELETE FROM venta_detalles WHERE venta_id IN ({placeholders})", ids_tuple)
                cursor.execute(f"DELETE FROM ventas WHERE id IN ({placeholders})", ids_tuple)
                
                conn.commit()
                logging.info(f"Mantenimiento ejecutado. {len(ids_canc)} ventas borradas.")
            
            cursor.close()
        except Exception as e:
            logging.error(f"Error en tarea_canceladas: {str(e)}")
            current_app.logger.error(f"Error en tarea_canceladas: {str(e)}")
        finally:
            conn.close()


# =========================
# SCHEDULER
# =========================


scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()

# 1. Job Diario (Cotizaciones y Cuentas Muertas) - Corre a las 12 PM y 12 AM
if not scheduler.get_job('Limpieza'):
    scheduler.add_job(
        id='Limpieza',
        func=tarea_limpieza,
        trigger='cron',
        hour='0, 12',   
        minute=0,       
        replace_existing=True
    )

# 2. Job Mensual (Borrar Canceladas) - Corre el día 1 de cada mes a las 3:00 AM
if not scheduler.get_job('LimpiezaCanceladas'):
    scheduler.add_job(
        id='LimpiezaCanceladas',
        func=tarea_canceladas,
        trigger='cron',
        day=1,
        hour=3,
        minute=0,
        replace_existing=True
    )


# =========================
# CONTEXT PROCESSOR (GLOBAL)
# =========================
@app.context_processor
def inject_user_config():
    """
    Inyecta la configuración del negocio en TODAS las plantillas (HTML)
    """
    default_config = {
        'nombre_empresa': 'Cotizador Sianeffects',
        'slogan': '',
        'website': '',
        'margen_ganancia': 100
    }

    if 'user_id' in session:
        try:
            conn = get_db_connection()
            cursor = conn.cursor() # EN POSTGRES SIEMPRE ABRIMOS CURSOR
            
            # CAMBIO DE ? POR %s
            cursor.execute('SELECT * FROM configuracion WHERE user_id = %s', (session['user_id'],))
            user_config = cursor.fetchone()
            
            cursor.close()
            conn.close()

            if user_config:
                return {'config': dict(user_config)}
        except Exception as e:
            app.logger.error(f"CONTEXT_ERROR: Fallo al inyectar config global para usuario {session.get('user_id')} - {e}")
    
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
from routes.invitaciones_admin import invitaciones_bp
from routes.invitaciones_publicas import invitaciones_publicas_bp
from routes.invitaciones_clientes import clientes_bp
from routes.dashboard import dashboard_bp
from routes.configuracion_bp import config_bp
from routes.user_dashboard import user_dash_bp

app.register_blueprint(auth_bp)
app.register_blueprint(main_bp)
app.register_blueprint(inventory_bp)
app.register_blueprint(admin_bp, url_prefix='/admin')
app.register_blueprint(api_bp, url_prefix='/api')
app.register_blueprint(catalogo_bp)
app.register_blueprint(shipping_bp)
app.register_blueprint(invitaciones_bp)
app.register_blueprint(invitaciones_publicas_bp)
app.register_blueprint(clientes_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(config_bp)
app.register_blueprint(user_dash_bp)

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

@app.route('/health')
def health():
    return "OK", 200

# =========================
# MAIN
# =========================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)