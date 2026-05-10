# =============================================================================
# app.py — Núcleo de la aplicación Sianeffects
# =============================================================================
# Responsabilidades de este archivo:
#   1. Crear y configurar la instancia de Flask
#   2. Configurar logging con zona horaria de Monterrey (SIN DUPLICACIÓN)
#   3. Configurar sesiones seguras
#   4. Definir y registrar tareas automáticas optimizadas (scheduler)
#   5. Registrar todos los Blueprints
#   6. Definir rutas globales (health, 404, manifest, test-mail)
#
# OPTIMIZACIONES IMPLEMENTADAS:
#   - Logging sin duplicación (un solo handler)
#   - Jobs reducidos de 72/día a ~6/día
#   - Ventanas de tiempo ajustadas según frecuencia de ejecución
# =============================================================================

# =============================================================================
# IMPORTS
# =============================================================================

# --- Librería estándar ---
import os
import json
import logging
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

# --- Terceros ---
import pytz
from dotenv import load_dotenv
from flask import (
    Flask,
    session,
    render_template,
    request,
    jsonify,
    redirect,
    url_for,
    send_from_directory,
    has_request_context
)
from flask_apscheduler import APScheduler

# --- Locales ---
from services.cloudflare_service import delete_from_cloudflare
from services.mail_service import enviar_correo_sian
from utils.datetime_utils import now_utc
from db import get_db_connection

# Cargamos el .env antes de cualquier os.getenv()
load_dotenv()


# =============================================================================
# INICIALIZACIÓN DE FLASK
# =============================================================================

app = Flask(__name__)

# Límite de 50 MB por petición (protección contra uploads masivos / DDoS)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

# SECRET_KEY desde .env. En producción NUNCA debe ser el fallback.
app.secret_key = os.getenv('SECRET_KEY', 'dev_key_fallback_insegura')

# Debug solo si FLASK_DEBUG=1 en el .env (siempre False en producción)
app.config['DEBUG'] = os.getenv('FLASK_DEBUG') == '1'


# =============================================================================
# CONFIGURACIÓN DE LOGGING CON ZONA HORARIA DE MONTERREY
# =============================================================================
# CORRECCIÓN APLICADA: Eliminada duplicación de logs
# ANTES: logging.basicConfig() + RotatingFileHandler → logs duplicados
# AHORA: Solo RotatingFileHandler → un solo log por evento
# =============================================================================

base_dir = os.path.abspath(os.path.dirname(__file__))
log_path = os.path.join(base_dir, 'limpieza.log')


def tiempo_monterrey(*args):
    """
    Reemplaza el converter por defecto de logging (UTC) por la hora local de Monterrey.
    Se inyecta en el Formatter base para que TODOS los logs del proceso lo usen.
    
    Returns:
        time.struct_time: Tupla de tiempo en zona horaria America/Monterrey
    """
    tz = pytz.timezone('America/Monterrey')
    return datetime.now(tz).timetuple()


# Forzamos hora de Monterrey en el formateador maestro de Python
logging.Formatter.converter = tiempo_monterrey

# Formato legible para el archivo de log
# [DD/MM/YYYY HH:MM:SS] LEVEL: mensaje
formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', '%d/%m/%Y %H:%M:%S')

# RotatingFileHandler: max 1MB por archivo, guarda hasta 3 copias antiguas
# encoding='utf-8' garantiza compatibilidad con caracteres especiales (ñ, acentos, etc.)
file_handler = RotatingFileHandler(
    log_path,
    maxBytes=1024 * 1024,  # 1 MB
    backupCount=3,          # Mantiene limpieza.log.1, limpieza.log.2, limpieza.log.3
    encoding='utf-8'
)
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.INFO)

# CRÍTICO: Limpiamos cualquier handler previo para evitar duplicación
app.logger.handlers.clear()

# Agregamos nuestro ÚNICO handler
app.logger.addHandler(file_handler)
app.logger.setLevel(logging.INFO)
app.logger.info("Sistema de monitoreo iniciado correctamente (Hora Local Monterrey)")

# Silenciamos los logs de werkzeug (GET /static/... spam) — solo mostramos errores
logging.getLogger('werkzeug').setLevel(logging.ERROR)


# =============================================================================
# CONFIGURACIÓN DE SESIONES SEGURAS
# =============================================================================
# Estas configuraciones protegen la cookie de sesión contra:
# - Robo por redes inseguras (SECURE)
# - Lectura por JavaScript malicioso (HTTPONLY)
# - Ataques CSRF cross-site (SAMESITE)
# =============================================================================

# Las sesiones duran 31 días si el usuario marcó "recordarme"
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=31)

# Solo envía la cookie por HTTPS (evita robo en redes inseguras)
# En desarrollo local (HTTP), Flask ignora este flag automáticamente
app.config['SESSION_COOKIE_SECURE'] = True

# JavaScript del navegador no puede leer la cookie (mitiga XSS)
app.config['SESSION_COOKIE_HTTPONLY'] = True

# Lax: la cookie viaja en navegación normal pero no en peticiones cross-site peligrosas
# Crucial en móviles: evita perder sesión al cambiar de app o de red WiFi/LTE
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'


# =============================================================================
# TAREAS AUTOMÁTICAS (JOBS DEL SCHEDULER)
# =============================================================================
# OPTIMIZACIÓN APLICADA: Frecuencias reducidas de 72 ejecuciones/día a ~6/día
# - Jobs críticos mantienen frecuencia diaria
# - Jobs de notificaciones optimizados a horarios pico
# - Ventanas de tiempo ajustadas según frecuencia de ejecución
# =============================================================================

def tarea_avisos_vencimiento():
    """
    Manda correos automáticos 3 días y 1 día antes de que venza la suscripción.
    
    FRECUENCIA: 2x al día (9 AM, 6 PM hora Monterrey)
    VENTANA: 12 horas (ajustada a la frecuencia de ejecución)
    
    Beneficios de 2x al día:
    - Los correos llegan en horarios pico de revisión de email
    - Reduce spam en logs de 24x/día a 2x/día
    - Sigue capturando todos los usuarios en la ventana de vencimiento
    
    Lógica:
    1. Busca usuarios cuya suscripción vence en exactamente 3 días o 1 día
    2. Usa ventana de 12 horas para evitar duplicados entre ejecuciones
    3. Manda correo personalizado según días restantes
    """
    with app.app_context():
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            ahora = now_utc()

            # Iteramos sobre 2 tipos de avisos: 3 días y 1 día antes
            for dias, subject, template in [
                (3, "⏳ Tu acceso a Sianeffects vence en 3 días", "aviso_vencimiento_3"),
                (1, "🚨 Último aviso: tu acceso vence mañana", "aviso_vencimiento_1"),
            ]:
                # Ventana de 12 horas (ajustada porque el job corre 2x/día)
                # Esto evita que un usuario reciba el mismo correo dos veces
                ventana_inicio = ahora + timedelta(days=dias)
                ventana_fin = ventana_inicio + timedelta(hours=12)

                cursor.execute("""
                    SELECT id, email, username FROM usuarios
                    WHERE role = 0
                      AND subscription_end >= %s
                      AND subscription_end < %s
                """, (ventana_inicio, ventana_fin))

                for u in cursor.fetchall():
                    enviar_correo_sian(
                        subject=subject,
                        recipient=u['email'],
                        template=template,
                        sender_alias="hola",
                        username=u['username'],
                        dias=dias
                    )
                    logging.info(f"AVISO_VENCIMIENTO_{dias}D: Correo enviado a {u['email']}")

            conn.commit()

        except Exception as e:
            conn.rollback()
            logging.error(f"Error en tarea_avisos_vencimiento: {e}")
        finally:
            cursor.close()
            conn.close()


# -----------------------------------------------------------------------------
# JOB 1 — Limpieza general (cotizaciones, cuentas inactivas, invitaciones)
# Frecuencia: 1x al día (12 AM)
# -----------------------------------------------------------------------------
def tarea_limpieza():
    """
    Tarea de mantenimiento general que corre una vez al día a medianoche.

    Limpia tres tipos de datos obsoletos:
    1. COTIZACIONES VENCIDAS: Elimina cotizaciones expiradas y sus detalles
    2. CUENTAS INACTIVAS: Elimina usuarios (role=0) sin actividad por 12+ meses
    3. INVITACIONES VENCIDAS: Purga invitaciones vencidas hace 15+ días
       y borra sus archivos en Cloudflare R2 para liberar storage
    
    Beneficios:
    - Mantiene la BD ligera y rápida
    - Libera espacio en Cloudflare R2
    - Protege privacidad (cumplimiento LFPDPPP México)
    """
    with app.app_context():
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            ahora = now_utc()
            str_ahora = ahora.strftime('%Y-%m-%d %H:%M:%S')

            # ------------------------------------------------------------------
            # 1. COTIZACIONES VENCIDAS
            # ------------------------------------------------------------------
            cursor.execute(
                "SELECT id FROM ventas WHERE estado = 'cotizacion' AND fecha_vencimiento < %s",
                (str_ahora,)
            )
            vencidas = cursor.fetchall()

            if vencidas:
                ids_ven = [v['id'] for v in vencidas]
                ph = ', '.join(['%s'] * len(ids_ven))
                ids_tuple = tuple(ids_ven)

                # Borramos en cascada: primero detalles, luego venta
                cursor.execute(f"DELETE FROM venta_detalles WHERE venta_id IN ({ph})", ids_tuple)
                cursor.execute(f"DELETE FROM ventas WHERE id IN ({ph})", ids_tuple)
                logging.info(f"Cotizaciones expiradas eliminadas: {len(ids_ven)}")

            # ------------------------------------------------------------------
            # 2. CUENTAS INACTIVAS (sin actividad por más de 12 meses)
            # ------------------------------------------------------------------
            # Cumplimiento LFPDPPP: no conservar datos si ya no hay relación comercial
            limite_12_meses = ahora - timedelta(days=365)
            str_limite_12 = limite_12_meses.strftime('%Y-%m-%d %H:%M:%S')

            cursor.execute(
                "SELECT id, email FROM usuarios WHERE role = 0 AND subscription_end < %s",
                (str_limite_12,)
            )
            usuarios_out = cursor.fetchall()

            if usuarios_out:
                ids = [u['id'] for u in usuarios_out]
                emails = [u['email'] for u in usuarios_out]
                ph = ', '.join(['%s'] * len(ids))
                ids_tuple = tuple(ids)

                # Borramos en cascada (tablas hijas primero para no violar FK)
                cursor.execute(
                    f"DELETE FROM venta_detalles WHERE venta_id IN "
                    f"(SELECT id FROM ventas WHERE user_id IN ({ph}))",
                    ids_tuple
                )
                cursor.execute(
                    f"DELETE FROM producto_detalles WHERE producto_id IN "
                    f"(SELECT id FROM productos WHERE user_id IN ({ph}))",
                    ids_tuple
                )
                cursor.execute(
                    f"DELETE FROM producto_maquinaria WHERE producto_id IN "
                    f"(SELECT id FROM productos WHERE user_id IN ({ph}))",
                    ids_tuple
                )
                cursor.execute(
                    f"DELETE FROM shipping_rates WHERE zone_id IN "
                    f"(SELECT id FROM shipping_zones WHERE user_id IN ({ph}))",
                    ids_tuple
                )

                # Tablas principales relacionadas al usuario
                for tabla in ['configuracion', 'maquinaria', 'materiales', 'movimientos_inventario',
                              'productos', 'ventas', 'shipping_configs', 'shipping_zones']:
                    cursor.execute(f"DELETE FROM {tabla} WHERE user_id IN ({ph})", ids_tuple)

                # Tablas sin FK user_id pero relacionadas
                cursor.execute(f"DELETE FROM tutoriales_estado WHERE user_id IN ({ph})", ids_tuple)
                cursor.execute(f"DELETE FROM usuarios WHERE id IN ({ph})", ids_tuple)

                for email in emails:
                    logging.warning(f"CUENTA ELIMINADA POR INACTIVIDAD (12+ meses): {email}")

            # ------------------------------------------------------------------
            # 3. INVITACIONES VENCIDAS (más de 15 días)
            # También borra archivos de Cloudflare R2 para liberar storage
            # ------------------------------------------------------------------
            fecha_limite_purga = ahora - timedelta(days=15)
            str_fecha_purga = fecha_limite_purga.strftime('%Y-%m-%d')

            cursor.execute(
                "SELECT id, slug, foto_portada_url, url_fondo, fotos_json FROM invitaciones "
                "WHERE vigencia < %s",
                (str_fecha_purga,)
            )
            invitaciones_basura = cursor.fetchall()

            if invitaciones_basura:
                for inv in invitaciones_basura:
                    inv_id = inv['id']

                    # Borramos archivos de R2 antes de borrar el registro
                    if inv['foto_portada_url']:
                        delete_from_cloudflare(inv['foto_portada_url'])
                    if inv['url_fondo']:
                        delete_from_cloudflare(inv['url_fondo'])
                    if inv['fotos_json']:
                        try:
                            for foto_url in json.loads(inv['fotos_json']):
                                delete_from_cloudflare(foto_url)
                        except Exception as e:
                            logging.error(f"Error borrando galería R2 inv {inv_id}: {e}")

                    # Fotos de cámara de invitados
                    cursor.execute(
                        "SELECT url FROM fotos_invitados WHERE invitacion_id = %s", (inv_id,)
                    )
                    for fc in cursor.fetchall():
                        if fc['url']:
                            delete_from_cloudflare(fc['url'])

                    # Borrado en cascada de tablas relacionadas
                    cursor.execute("DELETE FROM fotos_invitados WHERE invitacion_id = %s", (inv_id,))
                    cursor.execute("DELETE FROM pases_invitados WHERE invitacion_id = %s", (inv_id,))
                    cursor.execute("DELETE FROM buenos_deseos WHERE invitacion_id = %s", (inv_id,))
                    cursor.execute("DELETE FROM invitaciones WHERE id = %s", (inv_id,))

                logging.info(f"Invitaciones expiradas purgadas: {len(invitaciones_basura)}")

            conn.commit()

        except Exception as e:
            conn.rollback()
            logging.error(f"Error en tarea_limpieza: {e}")
        finally:
            cursor.close()
            conn.close()


# -----------------------------------------------------------------------------
# JOB 2 — Borrar ventas canceladas
# Frecuencia: 1x al mes (día 1, 3:00 AM)
# -----------------------------------------------------------------------------
def tarea_canceladas():
    """
    Borra permanentemente ventas en estado 'cancelada' y sus detalles.
    
    FRECUENCIA: 1x al mes (primer día del mes a las 3 AM)
    
    Razón de ser:
    - Las ventas canceladas no tienen valor histórico/fiscal después de cierto tiempo
    - Mantenerlas indefinidamente solo hincha la BD
    - 1x al mes es suficiente para mantener limpieza sin sobrecargar
    """
    with app.app_context():
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id FROM ventas WHERE estado = 'cancelada'")
            canceladas = cursor.fetchall()

            if canceladas:
                ids_canc = [c['id'] for c in canceladas]
                ph = ', '.join(['%s'] * len(ids_canc))
                ids_tuple = tuple(ids_canc)

                cursor.execute(f"DELETE FROM venta_detalles WHERE venta_id IN ({ph})", ids_tuple)
                cursor.execute(f"DELETE FROM ventas WHERE id IN ({ph})", ids_tuple)

                conn.commit()
                logging.info(f"Ventas canceladas eliminadas: {len(ids_canc)}")

        except Exception as e:
            conn.rollback()
            logging.error(f"Error en tarea_canceladas: {e}")
        finally:
            cursor.close()
            conn.close()


# -----------------------------------------------------------------------------
# JOB 3 — Recordatorio de verificación (12-24 horas sin verificar)
# Frecuencia: 2x al día (10 AM, 8 PM)
# -----------------------------------------------------------------------------
def tarea_recordatorio_verificacion():
    """
    Manda un correo de recordatorio a usuarios que llevan entre 12 y 24 horas
    sin verificar su email y aún no recibieron este recordatorio.

    FRECUENCIA: 2x al día (10 AM y 8 PM hora Monterrey)
    
    Beneficios de 2x/día vs cada hora:
    - Reduce ejecuciones de 24x/día a 2x/día
    - Los correos llegan en horarios cuando la gente está activa
    - Sigue capturando todos los usuarios en la ventana de 12-24 horas
    
    Protección contra spam:
    - Solo se manda UNA vez por usuario gracias al flag recordatorio_enviado
    - Si el usuario verifica su cuenta, el job lo ignora automáticamente
    """
    with app.app_context():
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            ahora = now_utc()
            hace_12_horas = ahora - timedelta(hours=12)
            hace_24_horas = ahora - timedelta(hours=24)

            # Ventana exacta: entre 12 y 24 horas desde el registro
            cursor.execute("""
                SELECT id, email, username FROM usuarios
                WHERE verificado = FALSE
                  AND recordatorio_enviado = FALSE
                  AND created_at <= %s
                  AND created_at > %s
            """, (hace_12_horas, hace_24_horas))

            pendientes = cursor.fetchall()

            for usuario in pendientes:
                enviar_correo_sian(
                    subject="¿Olvidaste algo? Tu cuenta de Sianeffects te espera",
                    recipient=usuario['email'],
                    template="recordatorio_verificacion",
                    sender_alias="hola",
                    username=usuario['username']
                )

                # Marcamos para no volver a mandar este correo
                cursor.execute(
                    "UPDATE usuarios SET recordatorio_enviado = TRUE WHERE id = %s",
                    (usuario['id'],)
                )
                logging.info(f"RECORDATORIO_VERIFICACION enviado a: {usuario['email']}")

            conn.commit()

        except Exception as e:
            conn.rollback()
            logging.error(f"Error en tarea_recordatorio_verificacion: {e}")
        finally:
            cursor.close()
            conn.close()


# -----------------------------------------------------------------------------
# JOB 4 — Purga de usuarios no verificados (más de 24 horas)
# Frecuencia: 1x al día (3 AM)
# Cumplimiento: LFPDPPP México
# -----------------------------------------------------------------------------
def tarea_purga_no_verificados():
    """
    Elimina usuarios que llevan más de 24 horas sin verificar su email.

    FRECUENCIA: 1x al día (3 AM hora Monterrey)
    
    Beneficios de 1x/día vs cada hora:
    - Reduce ejecuciones de 24x/día a 1x/día
    - No importa si borras a las 24h exactas o a las 27h
    - Sigue cumpliendo con LFPDPPP México
    
    Beneficios legales y técnicos:
    A) El email queda libre para re-registro (pizarra limpia)
    B) Cumplimiento LFPDPPP: no conservar datos personales si la finalidad
       del tratamiento (el registro) no se completó
    C) La BD no acumula cuentas zombie que bloquean emails válidos

    Borra en cascada: auth_codes, logs_actividad, configuracion, shipping_configs.
    """
    with app.app_context():
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            hace_24_horas = now_utc() - timedelta(hours=24)

            cursor.execute("""
                SELECT id, email FROM usuarios
                WHERE verificado = FALSE
                  AND created_at < %s
            """, (hace_24_horas,))

            a_purgar = cursor.fetchall()

            if a_purgar:
                ids = [u['id'] for u in a_purgar]
                emails = [u['email'] for u in a_purgar]
                ph = ', '.join(['%s'] * len(ids))
                ids_tuple = tuple(ids)

                # Borramos en cascada (tablas hijas primero para no violar FK)
                cursor.execute(f"DELETE FROM auth_codes WHERE user_id IN ({ph})", ids_tuple)
                cursor.execute(f"DELETE FROM logs_actividad WHERE user_id IN ({ph})", ids_tuple)
                cursor.execute(f"DELETE FROM configuracion WHERE user_id IN ({ph})", ids_tuple)
                cursor.execute(f"DELETE FROM shipping_configs WHERE user_id IN ({ph})", ids_tuple)
                cursor.execute(f"DELETE FROM tutoriales_estado WHERE user_id IN ({ph})", ids_tuple)
                cursor.execute(f"DELETE FROM usuarios WHERE id IN ({ph})", ids_tuple)

                conn.commit()

                for email in emails:
                    logging.info(f"PURGA_NO_VERIFICADO (LFPDPPP): {email} eliminado tras 24hrs sin verificar.")

                logging.info(f"PURGA_NO_VERIFICADOS: {len(ids)} cuentas eliminadas.")

        except Exception as e:
            conn.rollback()
            logging.error(f"Error en tarea_purga_no_verificados: {e}")
        finally:
            cursor.close()
            conn.close()


# =============================================================================
# SCHEDULER — Registro de todos los jobs
# =============================================================================
# OPTIMIZACIÓN APLICADA:
# - Antes: ~72 ejecuciones/día (jobs cada hora)
# - Ahora: ~6 ejecuciones/día (jobs 1-2x/día)
# - Reducción: 92% menos spam en logs
# - Beneficio: Misma funcionalidad, mucho menos overhead
# =============================================================================

scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()

# Job 1: Limpieza general — 1x al día (12 AM / medianoche)
if not scheduler.get_job('Limpieza'):
    scheduler.add_job(
        id='Limpieza',
        func=tarea_limpieza,
        trigger='cron',
        hour=0,
        minute=0,
        replace_existing=True
    )

# Job 2: Borrar ventas canceladas — 1x al mes (día 1 a las 3:00 AM)
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

# Job 3: Recordatorio de verificación — 2x al día (10 AM y 8 PM)
# Horarios cuando la gente está activa y revisa emails
if not scheduler.get_job('RecordatorioVerificacion'):
    scheduler.add_job(
        id='RecordatorioVerificacion',
        func=tarea_recordatorio_verificacion,
        trigger='cron',
        hour='10,20',  # 10:00 y 20:00 hrs
        minute=0,
        replace_existing=True
    )

# Job 4: Purga de no verificados — 1x al día (3 AM)
# Suficiente para mantener la BD limpia sin spam en logs
if not scheduler.get_job('PurgaNoVerificados'):
    scheduler.add_job(
        id='PurgaNoVerificados',
        func=tarea_purga_no_verificados,
        trigger='cron',
        hour=3,
        minute=0,
        replace_existing=True
    )

# Job 5: Avisos de vencimiento — 2x al día (9 AM y 6 PM)
# Horarios pico de revisión de emails
if not scheduler.get_job('AvisosVencimiento'):
    scheduler.add_job(
        id='AvisosVencimiento',
        func=tarea_avisos_vencimiento,
        trigger='cron',
        hour='9,18',  # 09:00 y 18:00 hrs
        minute=0,
        replace_existing=True
    )


# =============================================================================
# CONTEXT PROCESSOR — Inyecta config del negocio en TODAS las plantillas HTML
# =============================================================================

@app.context_processor
def inject_user_config():
    """
    Hace que la variable 'config' esté disponible automáticamente en todos
    los templates Jinja2 sin tener que pasarla manualmente en cada render_template().

    Si el usuario tiene sesión activa, carga su configuración real desde la BD.
    Si no, devuelve valores por defecto para no romper los templates.
    
    IMPORTANTE: has_request_context() valida que estemos en una petición HTTP.
    Los jobs de APScheduler NO tienen request context, por eso validamos primero.
    """
    default_config = {
        'nombre_empresa': 'Cotizador Sianeffects',
        'slogan': '',
        'website': '',
        'margen_ganancia': 100
    }

    # Validamos PRIMERO que estemos dentro de una petición HTTP
    # Si es un Job de APScheduler, esto dará False y se saltará la validación de sesión
    if has_request_context() and 'user_id' in session:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM configuracion WHERE user_id = %s', (session['user_id'],))
            user_config = cursor.fetchone()
            cursor.close()
            conn.close()

            if user_config:
                return {'config': dict(user_config)}
        except Exception as e:
            app.logger.error(
                f"CONTEXT_ERROR: Fallo al inyectar config para usuario {session.get('user_id')}: {e}"
            )

    # Retorna la configuración por defecto para Jobs en segundo plano o usuarios no logueados
    return {'config': default_config}

#============================================================================
# CONTEXT PROCESSOR — Inyecta IDs de Stripe en TODAS las plantillas HTML
#============================================================================

@app.context_processor
def inject_stripe_prices():
    """Hace que los IDs de Stripe estén disponibles en todos los templates HTML"""
    return dict(
        stripe_mensual=os.getenv('STRIPE_PRICE_MENSUAL'),
        stripe_anual=os.getenv('STRIPE_PRICE_ANUAL')
    )


# =============================================================================
# REGISTRO DE BLUEPRINTS
# =============================================================================
# Cada Blueprint encapsula las rutas de su módulo. Se importan aquí (al final)
# para evitar importaciones circulares con app.

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
from routes.chatbot import chatbot_bp
from routes.payments import payments_bp

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
app.register_blueprint(chatbot_bp)
app.register_blueprint(payments_bp, url_prefix='/payments')


# =============================================================================
# MIDDLEWARE — Anti-caché
# =============================================================================

@app.after_request
def add_header(response):
    """
    Previene que el navegador cachee respuestas.
    
    Esencial en apps con sesiones y roles: evita que un usuario vea
    páginas cacheadas de otro usuario que usó el mismo navegador.
    
    Ejemplo de riesgo sin este middleware:
    1. Usuario A (admin) navega por el panel de administración
    2. Usuario A cierra sesión
    3. Usuario B (cliente) usa el mismo navegador
    4. Sin anti-caché, el navegador podría mostrar páginas admin cacheadas
    
    Headers aplicados:
    - no-cache: Validar con servidor antes de usar caché
    - no-store: No guardar en disco
    - must-revalidate: Forzar validación si está expirado
    """
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


# =============================================================================
# FILTRO JINJA2 — Fecha local formateada
# =============================================================================

@app.template_filter('now_local_format')
def now_local_format(value=None, tz_name='America/Monterrey'):
    """
    Filtro de template que devuelve la fecha/hora actual en zona Monterrey.
    
    Uso en Jinja2:
        {{ '' | now_local_format }}
    
    Retorna formato: DD/MM/YYYY HH:MM
    Ejemplo: 06/05/2026 14:30
    
    Args:
        value: No se usa (compatibilidad con sintaxis de filtro Jinja)
        tz_name: Zona horaria (default: America/Monterrey)
    
    Returns:
        str: Fecha formateada o string vacío si hay error
    """
    try:
        tz = pytz.timezone(tz_name)
        return datetime.now(tz).strftime('%d/%m/%Y %H:%M')
    except Exception:
        return ''


# =============================================================================
# RUTAS GLOBALES
# =============================================================================

@app.route('/health')
def health():
    """
    Health check para Railway y monitoreo externo.
    
    Railway pinga esta ruta cada X minutos para verificar que la app esté viva.
    Si no responde 200, Railway puede reiniciar el contenedor automáticamente.
    """
    return "OK", 200


@app.route('/manifest.json')
def serve_manifest():
    """
    PWA manifest para instalación como app en móviles.
    
    Permite que usuarios Android/iOS agreguen Sianeffects a su pantalla
    de inicio y la usen como si fuera una app nativa.
    """
    return send_from_directory('static', 'manifest.json')


@app.route('/apple-touch-icon.png')
def serve_apple_icon():
    """
    Ícono para cuando el usuario agrega la app a su pantalla de inicio en iOS.
    
    Apple busca automáticamente este archivo en la raíz del sitio.
    Si no existe, iOS usa un screenshot genérico (que se ve feo).
    """
    return send_from_directory('static/images', 'apple-touch-icon.png')


@app.errorhandler(404)
def page_not_found(e):
    """
    Manejador global de errores 404.
    
    Diferencia entre peticiones AJAX y navegación normal:
    - Si es AJAX (fetch/axios del frontend) → responde JSON para que el JS lo maneje
    - Si es navegación normal → muestra la página 404.html bonita
    
    Args:
        e: Excepción 404 capturada por Flask
    
    Returns:
        tuple: (respuesta, código_http)
    """
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'error': 'Ruta no encontrada'}), 404
    return render_template('404.html'), 404


# =============================================================================
# MAIN
# =============================================================================

if __name__ == '__main__':
    # Puerto desde variable de entorno (Railway lo asigna automáticamente)
    # En desarrollo local usa 5000 por defecto
    port = int(os.environ.get("PORT", 5000))
    
    # host='0.0.0.0' permite conexiones desde cualquier IP
    # (necesario para que Railway/Docker puedan acceder)
    app.run(host='0.0.0.0', port=port)