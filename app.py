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

# Configuración del log (puedes poner esto al inicio de tu app.py)
logging.basicConfig(
    filename='limpieza.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)

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
    """
    Limpia cotizaciones expiradas, cuentas inactivas e invitaciones obsoletas.
    Se ejecuta automáticamente en los horarios programados (cron).
    """
    with app.app_context():
        try:
            conn = get_db_connection()
            ahora = now_utc() 
            str_ahora = ahora.strftime('%Y-%m-%d %H:%M:%S')
            
            # -------------------------------------------------------------
            # 1. Limpieza de cotizaciones vencidas (Borrar detalles primero)
            # -------------------------------------------------------------
            vencidas = conn.execute(
                "SELECT id FROM ventas WHERE estado = 'cotizacion' AND fecha_vencimiento < ?",
                (str_ahora,)
            ).fetchall()

            if vencidas:
                ids_ven = [v['id'] for v in vencidas]
                placeholders = ', '.join(['?'] * len(ids_ven))
                
                # POSTGRES READY: Convertimos la lista a tuple() para evitar errores de driver
                conn.execute(f"DELETE FROM venta_detalles WHERE venta_id IN ({placeholders})", tuple(ids_ven))
                conn.execute(f"DELETE FROM ventas WHERE id IN ({placeholders})", tuple(ids_ven))
                
                logging.info(f"Cotizaciones expiradas eliminadas de raíz: {len(ids_ven)}")

            # -------------------------------------------------------------
            # 2. Identificar usuarios (role 0) con 12 meses de vencimiento
            # -------------------------------------------------------------
            limite_12_meses = ahora - timedelta(days=365)
            str_limite_12 = limite_12_meses.strftime('%Y-%m-%d %H:%M:%S')
            
            usuarios_out = conn.execute(
                "SELECT id, email FROM usuarios WHERE role = 0 AND subscription_end < ?",
                (str_limite_12,)
            ).fetchall()

            if usuarios_out:
                ids = [u['id'] for u in usuarios_out]
                emails = [u['email'] for u in usuarios_out]
                ph = ', '.join(['?'] * len(ids))
                ids_tuple = tuple(ids) # POSTGRES READY

                # --- A. BORRAR DETALLES (Hijos) ---
                conn.execute(f"DELETE FROM venta_detalles WHERE venta_id IN (SELECT id FROM ventas WHERE user_id IN ({ph}))", ids_tuple)
                conn.execute(f"DELETE FROM producto_detalles WHERE producto_id IN (SELECT id FROM productos WHERE user_id IN ({ph}))", ids_tuple)
                conn.execute(f"DELETE FROM producto_maquinaria WHERE producto_id IN (SELECT id FROM productos WHERE user_id IN ({ph}))", ids_tuple)
                conn.execute(f"DELETE FROM shipping_rates WHERE zone_id IN (SELECT id FROM shipping_zones WHERE user_id IN ({ph}))", ids_tuple)

                # --- B. BORRAR TABLAS PRINCIPALES (Padres) ---
                tablas_directas = [
                    'configuracion', 'maquinaria', 'materiales', 
                    'movimientos_inventario', 'productos', 'ventas', 
                    'shipping_configs', 'shipping_zones'
                ]
                for tabla in tablas_directas:
                    conn.execute(f"DELETE FROM {tabla} WHERE user_id IN ({ph})", ids_tuple)

                # --- C. BORRAR AL USUARIO ---
                conn.execute(f"DELETE FROM usuarios WHERE id IN ({ph})", ids_tuple)
                
                for email in emails:
                    logging.warning(f"CUENTA ELIMINADA POR INACTIVIDAD (12 MESES): {email}")
                logging.info(f"JOB_CLEANUP: Limpieza UTC completada para {len(ids)} usuarios inactivos.")

            # -------------------------------------------------------------
            # 3. PURGA DE INVITACIONES OBSOLETAS
            # -------------------------------------------------------------
            fecha_limite_purga = ahora - timedelta(days=15)
            str_fecha_purga = fecha_limite_purga.strftime('%Y-%m-%d')

            invitaciones_basura = conn.execute(
                "SELECT id, slug, foto_portada_url, url_fondo, fotos_json FROM invitaciones WHERE vigencia < ?",
                (str_fecha_purga,)
            ).fetchall()

            if invitaciones_basura:
                for inv in invitaciones_basura:
                    inv_id = inv['id']
                    slug_inv = inv['slug']
                    
                    # --- A) DESTRUCCIÓN DE ARCHIVOS EN CLOUDFLARE R2 ---
                    if inv['foto_portada_url']: 
                        delete_from_cloudflare(inv['foto_portada_url'])
                    if inv['url_fondo']: 
                        delete_from_cloudflare(inv['url_fondo'])
                    
                    if inv['fotos_json']:
                        try:
                            fotos_galeria = json.loads(inv['fotos_json'])
                            for foto_url in fotos_galeria:
                                delete_from_cloudflare(foto_url)
                        except Exception as e:
                            logging.error(f"Error borrando galería R2 inv {inv_id}: {e}")
                            
                    # Borrar fotos subidas por los invitados
                    fotos_camara = conn.execute("SELECT url FROM fotos_invitados WHERE invitacion_id = ?", (inv_id,)).fetchall()
                    for fc in fotos_camara:
                        if fc['url']: delete_from_cloudflare(fc['url'])

                    # --- B) DESTRUCCIÓN LÓGICA EN BASE DE DATOS ---
                    conn.execute("DELETE FROM fotos_invitados WHERE invitacion_id = ?", (inv_id,))
                    conn.execute("DELETE FROM pases_invitados WHERE invitacion_id = ?", (inv_id,))
                    conn.execute("DELETE FROM buenos_deseos WHERE invitacion_id = ?", (inv_id,))
                    conn.execute("DELETE FROM invitaciones WHERE id = ?", (inv_id,))
                    
                    logging.info(f"JOB_CLEANUP: Invitación '{slug_inv}' (ID: {inv_id}) eliminada de BD y R2 por antigüedad.")

            conn.commit()

        except Exception as e:
            logging.error(f"Error en tarea_limpieza general: {str(e)}")
        finally:
            conn.close()


def tarea_canceladas():
    """
    Se ejecuta el día 1 de cada mes.
    Elimina permanentemente de la BD todas las ventas con estado 'cancelada'.
    """
    with app.app_context():
        try:
            conn = get_db_connection()
            canceladas = conn.execute("SELECT id FROM ventas WHERE estado = 'cancelada'").fetchall()
            
            if canceladas:
                ids_canc = [c['id'] for c in canceladas]
                placeholders = ', '.join(['?'] * len(ids_canc))
                ids_tuple = tuple(ids_canc) # POSTGRES READY
                
                conn.execute(f"DELETE FROM venta_detalles WHERE venta_id IN ({placeholders})", ids_tuple)
                conn.execute(f"DELETE FROM ventas WHERE id IN ({placeholders})", ids_tuple)
                
                conn.commit()
                logging.info(f"JOB_MAINTENANCE: Mantenimiento mensual ejecutado. {len(ids_canc)} ventas 'canceladas' borradas.")
                
        except Exception as e:
            logging.error(f"Error en tarea_canceladas: {str(e)}")
        finally:
            conn.close()


# =========================
# BASE DE DATOS Y SCHEDULER
# =========================
init_db()

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
            app.logger.error(f"CONTEXT_ERROR: Fallo al inyectar config global para usuario {session.get('user_id')} - {e}")
    
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

# =========================
# MAIN
# =========================
if __name__ == '__main__':
    app.run(debug=True)
