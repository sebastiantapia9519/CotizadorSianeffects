import os
import json
import uuid
import boto3
from botocore.config import Config
from datetime import timezone
from dateutil import parser
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# Importes de tu ecosistema
from db import get_db_connection as get_db
from helpers import login_required
from utils.datetime_utils import utc_to_local
from utils.tutorial_utils import debe_mostrar_tutorial, obtener_version_tutorial # <-- NUEVO: Funciones del tutorial

# Carga de variables de entorno
load_dotenv()

config_bp = Blueprint('configuracion', __name__)

# =========================================================
# CONFIGURACIÓN DE CLOUDFLARE R2
# =========================================================
ACCESS_KEY = os.getenv('R2_ACCESS_KEY')
SECRET_KEY = os.getenv('R2_SECRET_KEY')
ENDPOINT_URL = os.getenv('R2_ENDPOINT_URL')
BUCKET_NAME = os.getenv('R2_BUCKET_NAME')
PUBLIC_URL = os.getenv('R2_PUBLIC_URL')

# Cliente Boto3 para R2
s3_client = boto3.client(
    service_name='s3',
    endpoint_url=ENDPOINT_URL,
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    region_name='auto',
    config=Config(signature_version='s3v4')
)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'svg', 'webp'}

def allowed_file(filename):
    """Valida que el archivo subido sea una imagen permitida"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# =========================================================
# HELPER LOCAL: FORMATEO DE FECHAS
# =========================================================
def procesar_fila_fechas(fila_db):
    """
    Formatea las fechas de la BD (UTC) a la zona horaria local del usuario.
    Usa tu función 'utc_to_local' para no romper tu arquitectura.
    """
    if not fila_db: return None
    item = dict(fila_db)
    campos_fecha = ['fecha', 'fecha_vencimiento', 'created_at']
    
    for campo in campos_fecha:
        valor_original = item.get(campo)
        if valor_original:
            try:
                # Limpieza de string
                str_fecha = str(valor_original).replace('T', ' ')[:19]
                dt_utc = parser.parse(str_fecha)
                
                # Asignar zona UTC si no la trae
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                
                dt_local = utc_to_local(dt_utc)
                
                # Regla visual: Vencimientos van sin hora
                if campo == 'fecha_vencimiento':
                    item[campo] = dt_local.strftime('%d/%m/%Y') 
                else:
                    item[campo] = dt_local.strftime('%d/%m/%Y %H:%M') 
            except Exception as e:
                current_app.logger.warning(f"DATE_FORMAT_WARNING: Fallo procesando {campo} - {e}")
                pass 
    return item

# ==============================================================================
# 1. SERVICIO DE LECTURA (GET) - CARGA TODA LA VISTA DE CONFIGURACIÓN
# ==============================================================================
@config_bp.route('/configuracion', methods=['GET'])
@login_required
def configuracion():
    conn = get_db()
    uid = session['user_id']
    
    try:
        # --- 1. Datos del Usuario (Fuente de la verdad del nombre de empresa) ---
        user_raw = conn.execute('SELECT * FROM usuarios WHERE id=?', (uid,)).fetchone()
        user_display = procesar_fila_fechas(user_raw)
        
        # --- 2. Configuración del Negocio ---
        config_row = conn.execute('SELECT * FROM configuracion WHERE user_id=?', (uid,)).fetchone()
        if config_row:
            config = dict(config_row) 
        else:
            config = {
                'margen_ganancia': 100, 
                'slogan': '', 
                'website': '', 
                'inventario_activo': False,
                'ticket_bw': False,
                'icono_empresa': '🎨',
                'logo_empresa': ''
            }
        # Aseguramos que el nombre de la empresa empate con la tabla usuarios
        config['nombre_empresa'] = user_raw['company_name'] if user_raw['company_name'] else ''

        # --- 3. Zonas de Envío ---
        zones_db = conn.execute("SELECT * FROM shipping_zones WHERE user_id=?", (uid,)).fetchall()
        zones = []
        for z in zones_db:
            z_dict = dict(z)
            rates_db = conn.execute("SELECT * FROM shipping_rates WHERE zone_id=? ORDER BY max_weight_kg ASC", (z['id'],)).fetchall()
            z_dict['rates'] = [dict(r) for r in rates_db]
            try:
                z_dict['states_str'] = ", ".join(json.loads(z['states_included']))
            except:
                z_dict['states_str'] = z['states_included']
            zones.append(z_dict)

        # --- 4. Configuración Base Logística ---
        shipping_config_row = conn.execute("SELECT * FROM shipping_configs WHERE user_id = ?", (uid,)).fetchone()
        shipping_config = dict(shipping_config_row) if shipping_config_row else None

    except Exception as e:
        current_app.logger.error(f"CONFIG_LOAD_ERROR: Usuario {uid} experimentó error al cargar vista - {e}")
        flash("Hubo un problema al cargar tu configuración.", "danger")
        config, user_display, shipping_config, zones = {}, {}, None, []
    finally:
        conn.close()

    # 💡 NUEVO: LÓGICA DEL TUTORIAL
    mostrar_tour = debe_mostrar_tutorial(uid, 'configuracion')
    version_tour = obtener_version_tutorial('configuracion')
    
    return render_template('configuracion.html', 
                           config=config, 
                           usuario=user_display, 
                           shipping_config=shipping_config,
                           zones=zones,
                           mostrar_tour=mostrar_tour,  # <-- Se pasa a Jinja
                           version_tour=version_tour)  # <-- Se pasa a Jinja

# ==============================================================================
# 2. SERVICIO: ACTUALIZAR PERFIL Y CONTACTO
# ==============================================================================
@config_bp.route('/configuracion/perfil', methods=['POST'])
@login_required
def actualizar_perfil():
    conn = get_db()
    uid = session['user_id']
    
    new_username = request.form.get('username')
    new_email = request.form.get('email')
    new_phone = request.form.get('telefono')
    new_country = request.form.get('country_code', 'MX')
    
    try:
        conn.execute('''
            UPDATE usuarios SET username=?, email=?, telefono=?, country_code=? WHERE id=?
        ''', (new_username, new_email, new_phone, new_country, uid))
        session['username'] = new_username
        conn.commit()
        current_app.logger.info(f"PROFILE_UPDATE: Usuario {uid} actualizó su información de contacto.")
        flash('Perfil actualizado correctamente.', 'success')
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"PROFILE_UPDATE_ERROR: Usuario {uid} intentó usar email/user duplicado - {e}")
        flash('Error: El nombre de usuario o correo ya está en uso.', 'danger')
    finally:
        conn.close()

    return redirect(url_for('configuracion.configuracion') + '#list-perfil')

# ==============================================================================
# 3. SERVICIO: SEGURIDAD (CAMBIAR CONTRASEÑA)
# ==============================================================================
@config_bp.route('/configuracion/password', methods=['POST'])
@login_required
def actualizar_password():
    conn = get_db()
    uid = session['user_id']
    new_password = request.form.get('password')

    if new_password and len(new_password) >= 6:
        hashed_pw = generate_password_hash(new_password)
        conn.execute('UPDATE usuarios SET password=? WHERE id=?', (hashed_pw, uid))
        conn.commit()
        current_app.logger.info(f"SECURITY_UPDATE: Usuario {uid} cambió su contraseña.")
        flash('Contraseña actualizada. Por favor inicia sesión de nuevo.', 'success')
    else:
        current_app.logger.warning(f"SECURITY_WARNING: Usuario {uid} intentó guardar una contraseña muy corta.")
        flash('La contraseña es muy corta. Mínimo 6 caracteres.', 'danger')

    conn.close()
    return redirect(url_for('configuracion.configuracion') + '#list-seguridad')

# ==============================================================================
# 4. SERVICIO: ACTUALIZAR NEGOCIO (EXCLUSIVIDAD ÍCONO VS LOGO)
# ==============================================================================
@config_bp.route('/configuracion/negocio', methods=['POST'])
@login_required
def actualizar_negocio():
    conn = get_db()
    uid = session['user_id']
    
    try:
        try:
            margen = float(request.form.get('margen') or 0)
        except ValueError:
            margen = 0.0

        empresa = request.form.get('nombre_empresa', '')
        slogan = request.form.get('slogan', '')
        website = request.form.get('website', '')
        
        inventario_activo = True if request.form.get('inventario_activo') else False
        ticket_bw = True if request.form.get('ticket_bw') else False
        mostrar_ayuda = True if request.form.get('mostrar_ayuda') else False

        # --- LÓGICA DE EXCLUSIVIDAD (ÍCONO VS LOGO) ---
        tipo_identidad = request.form.get('tipo_identidad', 'emoji')
        icono_empresa = request.form.get('icono_empresa', '🎨')
        logo_url_final = request.form.get('current_logo', '') 

        if tipo_identidad == 'emoji':
            # Si eligió emoji, DESTRUIMOS el logo en R2 para ahorrar espacio
            if logo_url_final and logo_url_final.startswith(PUBLIC_URL):
                try:
                    old_key = logo_url_final.replace(f"{PUBLIC_URL}/", "")
                    s3_client.delete_object(Bucket=BUCKET_NAME, Key=old_key)
                    current_app.logger.info(f"R2_CLEANUP: Logo borrado al cambiar a emoji (Usuario {uid})")
                except Exception as e:
                    current_app.logger.warning(f"R2_DELETE_WARNING: Fallo al borrar logo viejo {old_key} de R2 - {e}")
            
            logo_url_final = '' # Limpiamos la BD
        
        elif tipo_identidad == 'logo':
            # Solo subimos a R2 si explícitamente eligió "logo" y mandó archivo
            if 'logo_file' in request.files:
                file = request.files['logo_file']
                if file and file.filename != '' and allowed_file(file.filename):
                    
                    # Borramos el logo viejo si lo está reemplazando
                    if logo_url_final and logo_url_final.startswith(PUBLIC_URL):
                        try:
                            old_key = logo_url_final.replace(f"{PUBLIC_URL}/", "")
                            s3_client.delete_object(Bucket=BUCKET_NAME, Key=old_key)
                        except: pass

                    base_filename = secure_filename(file.filename)
                    unique_filename = f"logos/{uid}/{uuid.uuid4().hex}_{base_filename}"
                    
                    try:
                        s3_client.upload_fileobj(
                            file,
                            BUCKET_NAME,
                            unique_filename,
                            ExtraArgs={'ContentType': file.content_type}
                        )
                        logo_url_final = f"{PUBLIC_URL}/{unique_filename}"
                        current_app.logger.info(f"R2_LOGO_UPLOAD_SUCCESS: Usuario {uid} subió su logo: {unique_filename}")
                    except Exception as e:
                        current_app.logger.error(f"R2_LOGO_UPLOAD_ERROR: Usuario {uid} - {e}")
                        flash("Error al subir el logo a la nube.", "danger")

        # --- ACTUALIZACIÓN EN BASE DE DATOS ---
        conn.execute('UPDATE usuarios SET company_name=? WHERE id=?', (empresa, uid))

        config_existente = conn.execute('SELECT id FROM configuracion WHERE user_id=?', (uid,)).fetchone()
        if config_existente:
            conn.execute('''
                UPDATE configuracion
                SET margen_ganancia=?, nombre_empresa=?, slogan=?, website=?, 
                    inventario_activo=?, ticket_bw=?, icono_empresa=?, logo_empresa=?, mostrar_ayuda=?
                WHERE user_id=?
            ''', (margen, empresa, slogan, website, inventario_activo, ticket_bw, icono_empresa, logo_url_final, mostrar_ayuda, uid))
        else:
            conn.execute('''
                INSERT INTO configuracion (user_id, margen_ganancia, nombre_empresa, slogan, website, 
                                           inventario_activo, ticket_bw, icono_empresa, logo_empresa, mostrar_ayuda)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (uid, margen, empresa, slogan, website, inventario_activo, ticket_bw, icono_empresa, logo_url_final, mostrar_ayuda))

        conn.commit()
        flash('Datos del negocio guardados correctamente.', 'success')

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"BUSINESS_UPDATE_ERROR: Usuario {uid} - {e}")
        flash('Error al guardar la configuración.', 'danger')
    finally:
        conn.close()

    return redirect(url_for('configuracion.configuracion') + '#list-negocio')

# ==============================================================================
# 5. SERVICIO: LOGÍSTICA (TARIFAS BASE)
# ==============================================================================
@config_bp.route('/configuracion/logistica/base', methods=['POST'])
@login_required
def actualizar_logistica_base():
    conn = get_db()
    uid = session['user_id']
    
    origin_address = request.form.get('origin_address') 
    origin_lat = request.form.get('origin_lat')
    origin_lng = request.form.get('origin_lng')
    
    try:
        local_base = float(request.form.get('local_base_rate') or 0)
        local_km = float(request.form.get('local_km_rate') or 0)
        safety_margin = int(request.form.get('safety_margin') or 10)
        
        existing = conn.execute("SELECT id FROM shipping_configs WHERE user_id=?", (uid,)).fetchone()

        if existing:
            conn.execute("""
                UPDATE shipping_configs 
                SET origin_address=?, origin_lat=?, origin_lng=?, local_base_rate=?, local_km_rate=?, safety_margin_percent=?
                WHERE user_id=?
            """, (origin_address, origin_lat, origin_lng, local_base, local_km, safety_margin, uid))
        else:
            conn.execute("""
                INSERT INTO shipping_configs (user_id, origin_address, origin_lat, origin_lng, local_base_rate, local_km_rate, safety_margin_percent)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (uid, origin_address, origin_lat, origin_lng, local_base, local_km, safety_margin))

        conn.commit()
        current_app.logger.info(f"SHIPPING_BASE_UPDATE: Usuario {uid} actualizó tarifas logísticas locales.")
        flash('Configuración de envíos actualizada.', 'success')
    except ValueError:
        current_app.logger.warning(f"SHIPPING_BASE_WARNING: Usuario {uid} metió caracteres inválidos en costo logístico.")
        flash('Los costos y márgenes de envío deben ser numéricos.', 'danger')
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"SHIPPING_CONFIG_ERROR: Usuario {uid} - {e}")
        flash(f'Error al guardar envíos: {e}', 'danger')
    finally:
        conn.close()

    return redirect(url_for('configuracion.configuracion') + '#list-envios')

# ==============================================================================
# 6. SERVICIOS: GESTIÓN DE ZONAS Y TARIFAS
# ==============================================================================
@config_bp.route('/configuracion/logistica/zona/crear', methods=['POST'])
@login_required
def crear_zona():
    conn = get_db()
    uid = session['user_id']
    try:
        nombre = request.form.get('zone_name')
        estados_str = request.form.get('zone_states', '').upper()
        
        if 'TODOS' in estados_str or 'ALL' in estados_str:
            estados_json = json.dumps(['ALL'])
        else:
            estados_lista = [x.strip() for x in estados_str.split(',') if x.strip()]
            estados_json = json.dumps(estados_lista)

        conn.execute("INSERT INTO shipping_zones (user_id, zone_name, states_included) VALUES (?, ?, ?)",
                     (uid, nombre, estados_json))
        conn.commit()
        current_app.logger.info(f"SHIPPING_ZONE_CREATE: Usuario {uid} creó zona '{nombre}'.")
        flash('Zona de envío creada con éxito.', 'success')
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"SHIPPING_ZONE_ERROR: Usuario {uid} - {e}")
        flash('Error al crear zona.', 'danger')
    finally:
        conn.close()
    return redirect(url_for('configuracion.configuracion') + '#list-envios')

@config_bp.route('/configuracion/logistica/zona/eliminar', methods=['POST'])
@login_required
def eliminar_zona():
    conn = get_db()
    uid = session['user_id']
    raw_zone_id = request.form.get('zone_id')
    
    if not raw_zone_id or not str(raw_zone_id).isdigit():
        flash("ID de zona inválido.", "danger")
        return redirect(url_for('configuracion.configuracion') + '#list-envios')
        
    try:
        zone_id = int(raw_zone_id)
        conn.execute("DELETE FROM shipping_rates WHERE zone_id=?", (zone_id,))
        conn.execute("DELETE FROM shipping_zones WHERE id=? AND user_id=?", (zone_id, uid))
        conn.commit()
        current_app.logger.info(f"SHIPPING_ZONE_DELETE: Usuario {uid} borró zona ID {zone_id}.")
        flash('Zona y sus tarifas eliminadas.', 'warning')
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"SHIPPING_ZONE_DELETE_ERROR: Usuario {uid} - {e}")
        flash('Error al eliminar zona.', 'danger')
    finally:
        conn.close()
    return redirect(url_for('configuracion.configuracion') + '#list-envios')

@config_bp.route('/configuracion/logistica/tarifa/crear', methods=['POST'])
@login_required
def crear_tarifa():
    conn = get_db()
    uid = session['user_id']
    raw_zone_id = request.form.get('zone_id')
    
    if not raw_zone_id or raw_zone_id == 'None':
        flash("El ID de la zona no se cargó correctamente.", "danger")
        return redirect(url_for('configuracion.configuracion') + '#list-envios')

    try:
        zone_id = int(raw_zone_id) 
        peso = float(request.form.get('max_weight') or 0)
        precio = float(request.form.get('price') or 0)
        
        conn.execute("INSERT INTO shipping_rates (zone_id, max_weight_kg, price) VALUES (?, ?, ?)",
                     (zone_id, peso, precio))
        conn.commit()
        current_app.logger.info(f"SHIPPING_RATE_CREATE: Usuario {uid} agregó tarifa de ${precio} a zona {zone_id}.")
        flash('Tarifa agregada correctamente.', 'success')
    except ValueError:
        flash("El peso y el precio deben ser numéricos.", "danger")
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"SHIPPING_RATE_ERROR: Usuario {uid} - {e}")
        flash('No se pudo agregar la tarifa.', 'danger')
    finally:
        conn.close()
    return redirect(url_for('configuracion.configuracion') + '#list-envios')

@config_bp.route('/configuracion/logistica/tarifa/eliminar', methods=['POST'])
@login_required
def eliminar_tarifa():
    conn = get_db()
    uid = session['user_id']
    raw_rate_id = request.form.get('rate_id')
    
    if not raw_rate_id or not str(raw_rate_id).isdigit():
        flash("ID de tarifa inválido.", "danger")
        return redirect(url_for('configuracion.configuracion') + '#list-envios')
        
    try:
        rate_id = int(raw_rate_id) 
        conn.execute("DELETE FROM shipping_rates WHERE id=?", (rate_id,))
        conn.commit()
        current_app.logger.info(f"SHIPPING_RATE_DELETE: Usuario {uid} eliminó tarifa ID {rate_id}.")
        flash('Tarifa eliminada.', 'warning')
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"SHIPPING_RATE_DELETE_ERROR: Usuario {uid} - {e}")
        flash('Error al eliminar tarifa.', 'danger')
    finally:
        conn.close()
    return redirect(url_for('configuracion.configuracion') + '#list-envios')