# =============================================================================
# routes/auth.py — Sistema de Autenticación Sianeffects
# =============================================================================
# Cubre: Login, Registro, Verificación de Email, Reenvío de Código,
#        Recuperación de Contraseña y Logout.
#
# SEGURIDAD APLICADA:
#   - Rate limiting manual en login y forgot-password (sin dependencias externas)
#   - Session fixation prevention (limpiamos sesión antes de asignar datos nuevos)
#   - Tokens de reset invalidados en masa al cambiar contraseña
#   - Correos enviados DESPUÉS del commit (nunca antes)
#   - RETURNING id en todos los INSERTs que necesiten el ID recién creado
#   - Limpieza de códigos usados para no acumular basura en la tabla auth_codes
#   - Validación de formato de username con regex
# =============================================================================

from flask import (
    Blueprint, render_template, request,
    redirect, url_for, flash, session,
    current_app, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash
from db import get_db_connection as get_db
from datetime import timedelta
import re
import secrets

# Utilidades y servicios del proyecto
from utils.email_validators import is_disposable_email
from services.mail_service import enviar_correo_sian
from utils.auth_utils import generate_verification_code
from utils.datetime_utils import now_utc
from utils.module_activation import (
    create_activation_code,
    ensure_user_module,
    get_user_modules,
    mark_auth_code_used,
    redirect_for_module,
    user_has_module,
    validate_activation_code,
)

# Registramos el Blueprint de autenticación (sin prefijo de URL, rutas directas)
auth_bp = Blueprint('auth', __name__)


def _auth_email_profile(active_module):
    """Devuelve asunto y template de verificacion/bienvenida segun modulo."""
    module = (active_module or 'cotizador').strip().lower()
    if module == 'nails':
        return {
            "verification_subject": "Verifica tu cuenta - Sianeffects Nails",
            "verification_template": "auth_code_nails",
            "welcome_subject": "Bienvenida a Sianeffects Nails",
            "welcome_template": "bienvenida_nails",
        }
    return {
        "verification_subject": "Verifica tu cuenta - Sianeffects",
        "verification_template": "auth_code",
        "welcome_subject": "¡Bienvenido/a a Sianeffects!",
        "welcome_template": "bienvenida",
    }


def _module_label(module_key):
    return 'Nails' if module_key == 'nails' else 'Cotizador'


def _active_modules_for_user(cursor, user_id):
    modules = get_user_modules(user_id, cursor=cursor)
    return [
        module
        for module in modules
        if (module.get('status') or '').strip().lower() in {'trial', 'active'}
    ]


def _module_access_end(module):
    status = (module.get('status') or '').strip().lower()
    access_end = module.get('subscription_end') or module.get('trial_ends_at')
    if not access_end and status == 'trial' and module.get('created_at'):
        access_end = module['created_at'] + timedelta(days=7)
    return access_end


def _apply_module_access_to_session(user, module):
    if user.get('role', 0) >= 1:
        session['is_pro_active'] = True
        session.pop('grace_period', None)
        return

    status = (module.get('status') or '').strip().lower()
    if status not in {'trial', 'active'}:
        session['is_pro_active'] = False
        session.pop('grace_period', None)
        return

    access_end = _module_access_end(module)
    if not access_end:
        session['is_pro_active'] = status == 'active'
        session.pop('grace_period', None)
        return

    ahora = now_utc().replace(tzinfo=None) if now_utc().tzinfo else now_utc()
    access_end_clean = access_end.replace(tzinfo=None) if access_end.tzinfo else access_end
    session['is_pro_active'] = access_end_clean > ahora
    session.pop('grace_period', None)


def _cotizador_needs_onboarding(cursor, user_id):
    """Detecta cuentas de cotizador nuevas que aun no completan su configuracion inicial."""
    cursor.execute("""
        SELECT 1
        FROM logs_actividad
        WHERE user_id = %s
          AND accion = 'Completó onboarding cotizador'
        LIMIT 1
    """, (user_id,))
    if cursor.fetchone():
        return False

    cursor.execute("""
        SELECT
            (SELECT COUNT(*) FROM ventas WHERE user_id = %s) AS ventas_count,
            (SELECT COUNT(*) FROM materiales WHERE user_id = %s) AS materiales_count,
            (SELECT COUNT(*) FROM productos WHERE user_id = %s) AS productos_count,
            (SELECT COUNT(*) FROM maquinaria WHERE user_id = %s) AS maquinaria_count
    """, (user_id, user_id, user_id, user_id))
    activity = cursor.fetchone() or {}
    has_business_activity = any(int(activity.get(key) or 0) > 0 for key in (
        'ventas_count', 'materiales_count', 'productos_count', 'maquinaria_count'
    ))
    if has_business_activity:
        return False

    cursor.execute(
        """
        SELECT status
        FROM user_modules
        WHERE user_id = %s AND module_key = 'cotizador'
        LIMIT 1
        """,
        (user_id,)
    )
    module_row = cursor.fetchone()
    if module_row and (module_row.get('status') or '').strip().lower() == 'trial':
        return True

    cursor.execute("""
        SELECT c.slogan, c.website, c.notas_ticket, u.company_name
        FROM usuarios u
        LEFT JOIN configuracion c ON c.user_id = u.id
        WHERE u.id = %s
        LIMIT 1
    """, (user_id,))
    setup = cursor.fetchone() or {}
    return not any((setup.get(field) or '').strip() for field in (
        'slogan', 'website', 'notas_ticket'
    ))


def _module_purpose(module_key):
    return f"activate_{module_key}"


def _clear_pending_activation():
    for key in (
        'pending_activation_user_id',
        'pending_activation_email',
        'pending_activation_module',
        'pending_activation_purpose',
        'pending_activation_nails',
    ):
        session.pop(key, None)


def _set_pending_activation(user_id, email, module_key, purpose, nails_data=None):
    session['pending_activation_user_id'] = user_id
    session['pending_activation_email'] = email
    session['pending_activation_module'] = module_key
    session['pending_activation_purpose'] = purpose
    if nails_data:
        session['pending_activation_nails'] = nails_data
    else:
        session.pop('pending_activation_nails', None)


def _activation_email_profile(module_key):
    if module_key == 'nails':
        return {
            "subject": "Activa Nails en tu cuenta Sianeffects",
            "template": "auth_code_nails",
        }
    return {
        "subject": "Activa Cotizador en tu cuenta Sianeffects",
        "template": "auth_code",
    }


def _send_activation_code_email(email, code, module_key, nombre=None, salon=None):
    profile = _activation_email_profile(module_key)
    enviar_correo_sian(
        subject=profile["subject"],
        recipient=email,
        template=profile["template"],
        sender_alias="accounts",
        code=code,
        nombre=nombre,
        salon=salon,
        activation_module='Nails' if module_key == 'nails' else 'Cotizador',
    )


def _start_module_activation(cursor, user, module_key, nails_data=None):
    purpose = _module_purpose(module_key)
    activation_code = create_activation_code(user['id'], user['email'], purpose, cursor=cursor)
    _set_pending_activation(user['id'], user['email'], module_key, purpose, nails_data)
    return activation_code


def _module_status_for_user(user):
    estado = (user.get('estado_suscripcion') or '').strip().lower()
    if estado in ('activo', 'active'):
        return 'active'
    if estado in ('trial',):
        return 'trial'
    if estado in ('cancelado', 'cancelada', 'cancelled'):
        return 'cancelled'
    if estado in ('pago fallido', 'inactivo', 'inactive'):
        return 'inactive'
    return 'trial'


def _activate_session_for_user(user, active_module, module_record=None):
    session.clear()
    session.permanent = True
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['role'] = user['role']
    session['active_module'] = active_module

    if user.get('role', 0) >= 1:
        session['is_pro_active'] = True
        session.pop('grace_period', None)
        return

    if module_record:
        _apply_module_access_to_session(user, module_record)
        return

    estado = (user.get('estado_suscripcion') or '').strip().lower()
    sub_end = user.get('subscription_end')
    try:
        ahora = now_utc().replace(tzinfo=None) if now_utc().tzinfo else now_utc()
        if sub_end and estado in ['trial', 'activo']:
            sub_end_clean = sub_end.replace(tzinfo=None) if sub_end.tzinfo else sub_end
            session['is_pro_active'] = sub_end_clean > ahora
        else:
            session['is_pro_active'] = False
    except Exception:
        session['is_pro_active'] = False


def _ensure_cotizador_defaults(cursor, user_id, company_name):
    cursor.execute('SELECT id FROM configuracion WHERE user_id = %s', (user_id,))
    if not cursor.fetchone():
        cursor.execute(
            """
            INSERT INTO configuracion (
                user_id, margen_ganancia, porcentaje_gastos_operativos,
                inventario_activo, ticket_bw, nombre_empresa
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (user_id, 100, 0, False, False, company_name or 'Mi Negocio')
        )

    cursor.execute('SELECT id FROM shipping_configs WHERE user_id = %s', (user_id,))
    if not cursor.fetchone():
        cursor.execute(
            """
            INSERT INTO shipping_configs (user_id, local_base_rate, local_km_rate, safety_margin_percent)
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, 35.00, 8.00, 10)
        )


def _finish_nails_activation(cursor, user, nails_data):
    user_id = user['id']
    staff_name = (nails_data or {}).get('staff_name') or user.get('username') or 'Staff'
    phone = (nails_data or {}).get('phone') or user.get('telefono') or ''

    if (nails_data or {}).get('join_code'):
        business_id = nails_data.get('joined_business_id')
        if not business_id:
            cursor.execute(
                """
                SELECT id, name, primary_color
                FROM nails_businesses
                WHERE join_code = %s AND is_active = TRUE
                LIMIT 1
                """,
                (nails_data.get('join_code'),)
            )
            business = cursor.fetchone()
            if not business:
                raise ValueError('No encontramos un salón activo con ese código.')
            business_id = business['id']
            staff_color = business['primary_color'] or '#d946ef'
        else:
            staff_color = nails_data.get('joined_business_primary_color') or '#d946ef'

        cursor.execute(
            """
            INSERT INTO nails_staff (business_id, user_id, name, email, phone, role, color)
            VALUES (%s, %s, %s, %s, %s, 'staff', %s)
            """,
            (business_id, user_id, staff_name, user['email'], phone, staff_color)
        )
        return

    salon_name = (nails_data or {}).get('salon_name') or user.get('company_name') or 'Sianeffects Nails'
    base_slug = re.sub(r'[^a-z0-9]+', '-', salon_name.lower()).strip('-') or f'nails-{user_id}'
    slug = base_slug
    counter = 1
    while True:
        cursor.execute('SELECT id FROM nails_businesses WHERE slug = %s LIMIT 1', (slug,))
        if not cursor.fetchone():
            break
        slug = f"{base_slug}-{counter}"
        counter += 1

    join_code = secrets.token_hex(4).upper()
    while True:
        cursor.execute('SELECT id FROM nails_businesses WHERE join_code = %s LIMIT 1', (join_code,))
        if not cursor.fetchone():
            break
        join_code = secrets.token_hex(4).upper()

    cursor.execute(
        """
        INSERT INTO nails_businesses (
            user_id, name, slug, whatsapp, instagram, address,
            catalog_tagline, business_hours_json, join_code
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            user_id,
            salon_name,
            slug,
            phone,
            (nails_data or {}).get('instagram') or '',
            (nails_data or {}).get('address') or '',
            'Uñas que expresan tu estilo, hechas con amor y detalle.',
            '{}',
            join_code,
        )
    )
    business_id = cursor.fetchone()['id']
    cursor.execute(
        """
        INSERT INTO nails_staff (business_id, user_id, name, email, phone, role, color)
        VALUES (%s, %s, %s, %s, %s, 'owner', %s)
        """,
        (business_id, user_id, staff_name, user['email'], phone, '#d946ef')
    )


# =============================================================================
# LOGIN
# =============================================================================
@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """
    Ruta de inicio de sesión.

    GET  → Muestra el formulario de login.
    POST → Valida credenciales. Incluye:
           - Rate limiting: máximo 5 intentos fallidos por minuto (por IP)
           - Session fixation prevention: limpiamos la sesión anterior antes
             de asignar la nueva para evitar que alguien "robe" una sesión
             que fue creada antes del login.
    """
    if request.method == 'POST':
        # --- Normalización del input ---
        # Pasamos a minúsculas y quitamos espacios para evitar duplicados
        # como "Usuario@Gmail.com" vs "usuario@gmail.com"
        email_input = request.form['email_or_user'].strip().lower()
        password = request.form['password']

        current_app.logger.info(f"LOGIN_ATTEMPT: Intento de acceso para '{email_input}'")

        conn = get_db()
        cursor = conn.cursor()

        try:
            # ----------------------------------------------------------------
            # RATE LIMITING: Bloqueamos si hay 5+ intentos fallidos en 1 minuto
            # ----------------------------------------------------------------
            # Buscamos en la bitácora cuántos logins fallidos hubo recientemente
            # desde esta misma dirección IP. Así evitamos ataques de fuerza bruta
            # donde alguien prueba miles de contraseñas automáticamente.
            un_minuto_atras = now_utc() - timedelta(minutes=1)
            ip_cliente = request.remote_addr  # IP del visitante

            cursor.execute("""
                SELECT COUNT(*) as intentos FROM logs_actividad
                WHERE accion = 'Login fallido'
                AND modulo = 'Acceso'
                AND detalle = %s
                AND created_at > %s
            """, (ip_cliente, un_minuto_atras))

            resultado = cursor.fetchone()

            # Si superó el límite, cortamos aquí sin revisar la contraseña
            if resultado and resultado['intentos'] >= 5:
                current_app.logger.warning(
                    f"RATE_LIMIT: IP {ip_cliente} bloqueada por exceso de intentos fallidos."
                )
                flash('Demasiados intentos fallidos. Espera un momento e intenta de nuevo.', 'error')
                return render_template('login.html')

            # ----------------------------------------------------------------
            # CONSULTA DE USUARIO
            # ----------------------------------------------------------------
            # Solo buscamos por email para evitar ambigüedad con usernames.
            # LOWER() asegura comparación insensible a mayúsculas en la BD.
            cursor.execute('SELECT * FROM usuarios WHERE LOWER(email) = %s', (email_input,))
            user = cursor.fetchone()

            # ----------------------------------------------------------------
            # VERIFICACIÓN DE CREDENCIALES
            # ----------------------------------------------------------------
            if user and check_password_hash(user['password'], password):

                # PORTERO: Si el usuario no verificó su email, lo mandamos a verificar
                if not user.get('verificado', False):
                    session['email_por_verificar'] = user['email']
                    session['modulo_por_verificar'] = (user.get('active_module') or 'cotizador').strip().lower()
                    flash('Tu cuenta aún no ha sido verificada. Revisa tu correo.', 'warning')
                    return redirect(url_for('auth.verificar_email'))

                # SESSION FIXATION PREVENTION:
                # Limpiamos cualquier sesión anterior ANTES de asignar la nueva.
                # Esto evita que alguien que obtuvo un session_id antes del login
                # lo use para acceder con los datos del usuario recién autenticado.
                nails_onboarding_prefill = session.get('nails_onboarding_prefill')
                session.clear()
                session.permanent = True  # La sesión dura lo configurado en PERMANENT_SESSION_LIFETIME
                if nails_onboarding_prefill and (user.get('active_module') or '').strip().lower() == 'nails':
                    session['nails_onboarding_prefill'] = nails_onboarding_prefill

                # Guardamos solo lo esencial en sesión (nunca datos sensibles como la contraseña)
                session['user_id'] = user['id']
                session['username'] = user['username']
                session['role'] = user['role']

                active_modules = _active_modules_for_user(cursor, user['id'])
                if active_modules:
                    active_module = active_modules[0]['module_key']
                else:
                    active_module = (user.get('active_module') or 'cotizador').strip().lower()
                session['active_module'] = active_module
                session.pop('module_selection_options', None)

                module_record = next(
                    (module for module in active_modules if module['module_key'] == active_module),
                    None
                )
                if module_record:
                    _apply_module_access_to_session(user, module_record)
                else:
                    session['is_pro_active'] = False
                    session.pop('grace_period', None)

                # Registramos el login exitoso en la bitácora de actividad
                cursor.execute("""
                    INSERT INTO logs_actividad (user_id, accion, modulo, detalle)
                    VALUES (%s, %s, %s, %s)
                """, (user['id'], "Inició sesión en el sistema", "Acceso", ip_cliente))

                # Actualizamos la columna last_login del usuario para que tu Panel Master no mienta
                cursor.execute("""
                    UPDATE usuarios SET last_login = %s WHERE id = %s
                """, (now_utc(), user['id']))

                conn.commit()

                current_app.logger.info(
                    f"LOGIN_SUCCESS: Usuario '{user['username']}' autenticado desde {ip_cliente}."
                )

                if len(active_modules) > 1:
                    session['module_selection_options'] = [module['module_key'] for module in active_modules]
                    return redirect(url_for('auth.seleccionar_modulo'))

                if active_module == 'nails':
                    return redirect(url_for('nails.dashboard'))

                if _cotizador_needs_onboarding(cursor, user['id']):
                    return redirect(url_for('configuracion.cotizador_onboarding'))

                return redirect(url_for('main.cotizador'))

            else:
                # Credenciales incorrectas: registramos el intento fallido con la IP
                # para que el rate limiting funcione en el próximo intento
                cursor.execute("""
                    INSERT INTO logs_actividad (user_id, accion, modulo, detalle)
                    VALUES (%s, %s, %s, %s)
                """, (None, "Login fallido", "Acceso", ip_cliente))
                conn.commit()

                current_app.logger.warning(
                    f"LOGIN_FAILED: Credenciales inválidas para '{email_input}' desde {ip_cliente}."
                )
                # Mensaje genérico intencionalmente: no revelamos si el email existe o no
                flash('Correo o contraseña incorrectos.', 'error')

        except Exception as e:
            conn.rollback()
            current_app.logger.error(f"LOGIN_ERROR: Excepción inesperada para '{email_input}': {e}")
            flash('Error interno. Intenta de nuevo.', 'error')
        finally:
            cursor.close()
            conn.close()

    return render_template('login.html')


@auth_bp.route('/seleccionar-modulo', methods=['GET', 'POST'])
def seleccionar_modulo():
    user_id = session.get('user_id')
    if not user_id:
        flash('Inicia sesión para continuar.', 'warning')
        return redirect(url_for('auth.login'))

    conn = get_db()
    cursor = conn.cursor()
    try:
        active_modules = _active_modules_for_user(cursor, user_id)
        if len(active_modules) <= 1:
            module = active_modules[0] if active_modules else None
            module_key = module['module_key'] if module else session.get('active_module', 'cotizador')
            session['active_module'] = module_key
            if module:
                _apply_module_access_to_session({'role': session.get('role', 0)}, module)
            return redirect(redirect_for_module(module_key))

        allowed_modules = {module['module_key'] for module in active_modules}

        if request.method == 'POST':
            selected_module = (request.form.get('module_key') or '').strip().lower()
            if selected_module not in allowed_modules:
                flash('Selecciona un módulo disponible para tu cuenta.', 'warning')
                return render_template(
                    'seleccionar_modulo.html',
                    modules=active_modules,
                    module_label=_module_label,
                )

            session['active_module'] = selected_module
            session.pop('module_selection_options', None)
            selected_record = next(
                (module for module in active_modules if module['module_key'] == selected_module),
                None
            )
            if selected_record:
                _apply_module_access_to_session({'role': session.get('role', 0)}, selected_record)
            cursor.execute(
                'UPDATE usuarios SET active_module = %s WHERE id = %s',
                (selected_module, user_id)
            )
            cursor.execute(
                'INSERT INTO logs_actividad (user_id, accion, modulo, detalle) VALUES (%s, %s, %s, %s)',
                (user_id, 'Seleccionó módulo de trabajo', 'Acceso', selected_module)
            )
            conn.commit()

            if selected_module == 'cotizador' and _cotizador_needs_onboarding(cursor, user_id):
                return redirect(url_for('configuracion.cotizador_onboarding'))
            return redirect(redirect_for_module(selected_module))

        return render_template(
            'seleccionar_modulo.html',
            modules=active_modules,
            module_label=_module_label,
        )
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"MODULE_SELECTION_ERROR: Usuario {user_id} - {e}")
        flash('No pudimos cargar tus módulos. Intenta iniciar sesión de nuevo.', 'error')
        return redirect(url_for('auth.logout'))
    finally:
        cursor.close()
        conn.close()


# =============================================================================
# REGISTRO
# =============================================================================
@auth_bp.route('/registro', methods=['GET', 'POST'])
def registro():
    """
    Ruta de registro de nuevos usuarios.

    GET  → Captura UTMs de marketing y muestra el formulario.
    POST → Valida datos, crea el usuario, sus configuraciones por defecto,
           genera un código de verificación y lo envía por correo.

    IMPORTANTE: El correo se envía DESPUÉS del commit para garantizar que
    el usuario ya existe en la BD antes de mandarle instrucciones.
    """
    if request.method == 'GET':
        # Capturamos parámetros UTM para saber de dónde vienen los registros
        # (ej: utm_source=instagram, utm_campaign=promo_mayo)
        if request.args.get('utm_source'):
            session['utm_source'] = request.args.get('utm_source').lower()
        if request.args.get('utm_campaign'):
            session['utm_campaign'] = request.args.get('utm_campaign').lower()
        return render_template('registro.html')

    # --- POST: Procesamiento del registro ---

    # Extracción y limpieza de datos del formulario
    username = request.form['username'].lower().strip()
    email = request.form['email'].lower().strip()
    password = request.form['password']
    confirm_password = request.form['confirm_password']
    telefono = request.form.get('phone', '')
    company_name = request.form.get('company_name', 'Mi Negocio')

    # Tomamos los UTMs de la sesión y los eliminamos (solo se usan una vez)
    origen_registro = session.pop('utm_source', 'desconocido')
    utm_campaign = session.pop('utm_campaign', None)

    # ----------------------------------------------------------------
    # VALIDACIONES DE NEGOCIO (antes de tocar la BD)
    # ----------------------------------------------------------------

    if request.cookies.get('has_free_trial'):
        flash('¿Ya tienes una cuenta? Puedes iniciar sesión en lugar de registrarte de nuevo.', 'info')
        

    # Validación de formato de username:
    # Solo letras minúsculas, números y guion bajo. Entre 3 y 20 caracteres.
    # Validación de contraseña: mínimo 6 caracteres y que coincidan los campos
    if len(password) < 6 or password != confirm_password:
        flash('Revisa que la contraseña sea válida y coincida en ambos campos.', 'error')
        return render_template('registro.html')

    # Validación de email: formato correcto y que no sea de un dominio desechable
    # (ej: mailinator.com, tempmail.com, etc.)
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email) or is_disposable_email(email):
        flash('Por favor usa un correo electrónico real y válido.', 'error')
        return render_template('registro.html')

    # ----------------------------------------------------------------
    # PREPARACIÓN DE DATOS
    # ----------------------------------------------------------------
    hashed_pw = generate_password_hash(password)  # Nunca guardamos contraseñas en texto plano
    created_at = now_utc()
    subscription_end = created_at + timedelta(days=7)  # 7 días de prueba gratuita

    conn = get_db()
    cursor = conn.cursor()

    try:
        # Verificamos que el email no esté ya registrado
        cursor.execute('SELECT id, email, username, company_name, plan_type FROM usuarios WHERE email = %s', (email,))
        existing_row = cursor.fetchone()
        existing_user = dict(existing_row) if existing_row else None
        if existing_user:
            if user_has_module(existing_user['id'], 'cotizador', cursor=cursor):
                flash('Este correo ya tiene Cotizador activo. Inicia sesión para continuar.', 'info')
                return redirect(url_for('auth.login'))

            activation_code = _start_module_activation(cursor, existing_user, 'cotizador')
            conn.commit()
            _send_activation_code_email(
                existing_user['email'],
                activation_code['code'],
                'cotizador',
                nombre=existing_user.get('username'),
                salon=existing_user.get('company_name'),
            )
            flash('Te enviamos un código para activar Cotizador en tu cuenta Sianeffects.', 'info')
            return redirect(url_for('auth.activar_modulo'))

        # ----------------------------------------------------------------
        # INSERCIÓN DEL USUARIO
        # RETURNING id → psycopg2 requiere esto para obtener el ID recién creado.
        # Sin RETURNING, cursor.fetchone() devuelve None y todo crashea.
        # ----------------------------------------------------------------
        cursor.execute('''
            INSERT INTO usuarios (
                username, email, password, telefono, company_name,
                role, subscription_end, created_at, last_login, terms_accepted,
                origen_registro, utm_campaign, verificado,
                estado_suscripcion, plan_type
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (
            username, email, hashed_pw, telefono, company_name,
            0, subscription_end, created_at, created_at, True,
            origen_registro, utm_campaign, False,
            'Trial', 'Free'  
        ))

        user_id = cursor.fetchone()['id']  # ID del usuario recién creado
        ensure_user_module(user_id, 'cotizador', status='trial', plan_type='Free', cursor=cursor)

        # ----------------------------------------------------------------
        # INICIALIZACIÓN DE SERVICIOS POR DEFECTO
        # Configuramos valores iniciales para que el usuario no encuentre
        # la app vacía en su primer ingreso.
        # ----------------------------------------------------------------

        # Configuración general del negocio (margen, inventario, etc.)
        cursor.execute('''
            INSERT INTO configuracion (
                user_id, margen_ganancia, porcentaje_gastos_operativos,
                inventario_activo, ticket_bw, nombre_empresa
            )
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (user_id, 100, 0, False, False, company_name))

        # Configuración de envíos con tarifas base razonables
        cursor.execute('''
            INSERT INTO shipping_configs (user_id, local_base_rate, local_km_rate, safety_margin_percent)
            VALUES (%s, %s, %s, %s)
        ''', (user_id, 35.00, 8.00, 10))

        # Log de actividad: registro del evento de creación de cuenta
        cursor.execute(
            "INSERT INTO logs_actividad (user_id, accion, modulo) VALUES (%s, %s, %s)",
            (user_id, "Registro inicial completo", "Cuenta")
        )

        # ----------------------------------------------------------------
        # GENERACIÓN DEL CÓDIGO DE VERIFICACIÓN
        # ----------------------------------------------------------------
        v_code = generate_verification_code()
        expires_at = now_utc() + timedelta(minutes=10)  # Código válido por 10 minutos

        cursor.execute('''
            INSERT INTO auth_codes (user_id, email, code, expires_at, purpose)
            VALUES (%s, %s, %s, %s, %s)
        ''', (user_id, email, v_code, expires_at, 'verify_email'))

        # ----------------------------------------------------------------
        # COMMIT PRIMERO, CORREO DESPUÉS
        # ----------------------------------------------------------------
        # Hacemos el commit ANTES de mandar el correo. Si el commit falla,
        # no habremos enviado un código para un usuario que no existe.
        # Si el correo falla, el usuario puede solicitar reenvío desde la app.
        conn.commit()

        # Enviamos el correo de verificación SOLO si el commit fue exitoso
        enviar_correo_sian(
            subject="Verifica tu cuenta - Sianeffects",
            recipient=email,
            template="auth_code",
            sender_alias="accounts",
            code=v_code
        )

        current_app.logger.info(
            f"REGISTER_SUCCESS: Usuario '{username}' ({email}) registrado. Código enviado."
        )

        # Guardamos el email en sesión para usarlo en la pantalla de verificación
        session['email_por_verificar'] = email
        flash('Te enviamos un código de verificación a tu correo.', 'info')

        # Seteamos cookie anti-abuso en el navegador del usuario (dura 1 año)
        resp = redirect(url_for('auth.verificar_email'))
        resp.set_cookie('has_free_trial', 'true', max_age=31536000)
        return resp

    except Exception as e:
        # Si algo falla en medio del proceso, revertimos TODO (ningún dato queda a medias)
        conn.rollback()
        current_app.logger.error(
            f"REGISTER_ERROR: Fallo crítico en registro de '{email}': {e}"
        )
        flash('Error al procesar el registro. Intenta de nuevo.', 'error')
    finally:
        cursor.close()
        conn.close()

    return render_template('registro.html')


@auth_bp.route('/registro-nails', methods=['GET', 'POST'])
def registro_nails():
    """
    Registro especial para Sianeffects Nails.
    - Con código: crea usuario y lo une como técnica del salón.
    - Sin código: crea usuario, salón nuevo y staff owner.
    """
    if request.method == 'GET':
        if request.args.get('utm_source'):
            session['utm_source'] = request.args.get('utm_source').lower()
        if request.args.get('utm_campaign'):
            session['utm_campaign'] = request.args.get('utm_campaign').lower()
        return render_template('nails/registro_nails.html', join_code=(request.args.get('codigo') or '').strip().upper())

    username = request.form['username'].strip().upper()
    email = request.form['email'].lower().strip()
    password = request.form['password']
    confirm_password = request.form['confirm_password']
    telefono = request.form.get('phone', '').strip()
    join_code = (request.form.get('join_code') or '').strip().upper()
    salon_name = (request.form.get('salon_name') or '').strip().upper()
    instagram = (request.form.get('instagram') or '').strip()
    address = (request.form.get('address') or '').strip()
    company_name = salon_name or 'Sianeffects Nails'
    origen_registro = session.pop('utm_source', 'nails')
    utm_campaign = session.pop('utm_campaign', None)

    if len(password) < 6 or password != confirm_password:
        flash('Revisa que la contraseña sea válida y coincida en ambos campos.', 'error')
        return render_template('nails/registro_nails.html', join_code=join_code)

    if not re.match(r"[^@]+@[^@]+\.[^@]+", email) or is_disposable_email(email):
        flash('Por favor usa un correo electrónico real y válido.', 'error')
        return render_template('nails/registro_nails.html', join_code=join_code)

    if not join_code and not salon_name:
        flash('Escribe el código del salón o el nombre de tu nuevo salón.', 'error')
        return render_template('nails/registro_nails.html', join_code=join_code)

    hashed_pw = generate_password_hash(password)
    created_at = now_utc()
    subscription_end = created_at + timedelta(days=7)
    conn = get_db()
    cursor = conn.cursor()

    try:
        joined_business = None
        if join_code:
            cursor.execute(
                """
                SELECT id, name, primary_color
                FROM nails_businesses
                WHERE join_code = %s AND is_active = TRUE
                LIMIT 1
                """,
                (join_code,),
            )
            joined_business = cursor.fetchone()
            if not joined_business:
                flash('No encontramos un salón activo con ese código.', 'error')
                return render_template('nails/registro_nails.html', join_code=join_code)
            company_name = joined_business['name']

        cursor.execute(
            "SELECT id, email, username, company_name, plan_type FROM usuarios WHERE email = %s",
            (email,)
        )
        existing_row = cursor.fetchone()
        existing_user = dict(existing_row) if existing_row else None
        if existing_user:
            if user_has_module(existing_user['id'], 'nails', cursor=cursor):
                flash('Este correo ya tiene Nails activo. Inicia sesión para continuar.', 'info')
                return redirect(url_for('auth.login'))

            nails_data = {
                'join_code': join_code,
                'salon_name': salon_name,
                'instagram': instagram,
                'address': address,
                'phone': telefono,
                'staff_name': username,
            }
            if joined_business:
                nails_data['joined_business_id'] = joined_business['id']
                nails_data['joined_business_name'] = joined_business['name']
                nails_data['joined_business_primary_color'] = joined_business['primary_color'] or '#d946ef'

            activation_code = _start_module_activation(cursor, existing_user, 'nails', nails_data)
            conn.commit()
            _send_activation_code_email(
                existing_user['email'],
                activation_code['code'],
                'nails',
                nombre=existing_user.get('username'),
                salon=nails_data.get('joined_business_name') or nails_data.get('salon_name') or existing_user.get('company_name'),
            )
            flash('Te enviamos un código para activar Nails en tu cuenta Sianeffects.', 'info')
            return redirect(url_for('auth.activar_modulo'))

        cursor.execute('''
            INSERT INTO usuarios (
                username, email, password, telefono, company_name,
                role, subscription_end, created_at, last_login, terms_accepted,
                origen_registro, utm_campaign, verificado,
                estado_suscripcion, plan_type, active_module
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (
            username, email, hashed_pw, telefono, company_name,
            0, subscription_end, created_at, created_at, True,
            origen_registro, utm_campaign, False,
            'Trial', 'Free', 'nails'
        ))
        user_id = cursor.fetchone()['id']
        ensure_user_module(user_id, 'nails', status='trial', plan_type='Free', cursor=cursor)

        cursor.execute('''
            INSERT INTO configuracion (
                user_id, margen_ganancia, porcentaje_gastos_operativos,
                inventario_activo, ticket_bw, nombre_empresa
            )
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (user_id, 100, 0, False, False, company_name))

        cursor.execute('''
            INSERT INTO shipping_configs (user_id, local_base_rate, local_km_rate, safety_margin_percent)
            VALUES (%s, %s, %s, %s)
        ''', (user_id, 35.00, 8.00, 10))

        if joined_business:
            cursor.execute(
                """
                INSERT INTO nails_staff (business_id, user_id, name, email, phone, role, color)
                VALUES (%s, %s, %s, %s, %s, 'staff', %s)
                """,
                (
                    joined_business['id'],
                    user_id,
                    username,
                    email,
                    telefono,
                    joined_business['primary_color'] or '#d946ef',
                ),
            )
            log_detail = f"Registro Nails: técnica unida a {joined_business['name']}"
        else:
            session['nails_onboarding_prefill'] = {
                'name': salon_name,
                'whatsapp': telefono,
                'instagram': instagram,
                'address': address,
            }
            log_detail = f"Registro Nails: pendiente de configurar salón {salon_name}"

        cursor.execute(
            "INSERT INTO logs_actividad (user_id, accion, modulo, detalle) VALUES (%s, %s, %s, %s)",
            (user_id, "Registro Nails completo", "Cuenta", log_detail),
        )

        v_code = generate_verification_code()
        expires_at = now_utc() + timedelta(minutes=10)
        cursor.execute('''
            INSERT INTO auth_codes (user_id, email, code, expires_at, purpose)
            VALUES (%s, %s, %s, %s, %s)
        ''', (user_id, email, v_code, expires_at, 'verify_email'))

        conn.commit()
        email_profile = _auth_email_profile('nails')
        enviar_correo_sian(
            subject=email_profile["verification_subject"],
            recipient=email,
            template=email_profile["verification_template"],
            sender_alias="accounts",
            code=v_code,
            nombre=username,
            salon=company_name,
        )

        session['email_por_verificar'] = email
        session['modulo_por_verificar'] = 'nails'
        flash('Te enviamos un código de verificación a tu correo.', 'info')
        resp = redirect(url_for('auth.verificar_email'))
        resp.set_cookie('has_free_trial', 'true', max_age=31536000)
        return resp

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"NAILS_REGISTER_ERROR: Fallo en registro Nails '{email}': {e}")
        flash('Error al procesar el registro Nails. Intenta de nuevo.', 'error')
    finally:
        cursor.close()
        conn.close()

    return render_template('nails/registro_nails.html', join_code=join_code)


@auth_bp.route('/activar-modulo', methods=['GET', 'POST'])
def activar_modulo():
    user_id = session.get('pending_activation_user_id')
    email = session.get('pending_activation_email')
    module_key = session.get('pending_activation_module')
    purpose = session.get('pending_activation_purpose')

    if not user_id or not email or module_key not in {'cotizador', 'nails'} or not purpose:
        flash('No hay una activación pendiente. Inicia sesión o vuelve a solicitar el acceso.', 'warning')
        return redirect(url_for('auth.login'))

    module_label = 'Nails' if module_key == 'nails' else 'Cotizador'

    if request.method == 'POST':
        codigo = (request.form.get('codigo') or '').strip()
        conn = get_db()
        cursor = conn.cursor()

        try:
            record = validate_activation_code(user_id, email, codigo, purpose, cursor=cursor)
            if not record:
                flash('El código es incorrecto o ya venció. Intenta de nuevo.', 'error')
                return render_template('activar_modulo.html', email=email, module_key=module_key, module_label=module_label)

            cursor.execute(
                """
                SELECT id, username, email, telefono, company_name, role, subscription_end,
                       estado_suscripcion, plan_type
                FROM usuarios
                WHERE id = %s AND email = %s
                LIMIT 1
                """,
                (user_id, email)
            )
            user_row = cursor.fetchone()
            if not user_row:
                flash('No pudimos encontrar tu cuenta. Intenta iniciar sesión.', 'error')
                return redirect(url_for('auth.login'))
            user = dict(user_row)

            if user_has_module(user_id, module_key, cursor=cursor):
                flash(f'Este correo ya tiene {module_label} activo. Inicia sesión para continuar.', 'info')
                _clear_pending_activation()
                return redirect(url_for('auth.login'))

            module_record = ensure_user_module(
                user_id,
                module_key,
                status='trial',
                plan_type=user.get('plan_type') or 'Free',
                cursor=cursor
            )
            cursor.execute(
                'UPDATE usuarios SET active_module = %s, verificado = TRUE WHERE id = %s',
                (module_key, user_id)
            )

            if module_key == 'cotizador':
                _ensure_cotizador_defaults(cursor, user_id, user.get('company_name'))
            elif module_key == 'nails':
                _finish_nails_activation(cursor, user, session.get('pending_activation_nails') or {})

            mark_auth_code_used(record['id'], cursor=cursor)
            cursor.execute(
                "INSERT INTO logs_actividad (user_id, accion, modulo, detalle) VALUES (%s, %s, %s, %s)",
                (user_id, f"Activó módulo {module_label}", "Cuenta", purpose)
            )
            conn.commit()

            _activate_session_for_user(user, module_key, module_record)
            _clear_pending_activation()
            flash(f'{module_label} quedó activo en tu cuenta.', 'success')
            if module_key == 'cotizador' and _cotizador_needs_onboarding(cursor, user_id):
                return redirect(url_for('configuracion.cotizador_onboarding'))
            return redirect(redirect_for_module(module_key))

        except Exception as e:
            conn.rollback()
            current_app.logger.error(f"MODULE_ACTIVATION_ERROR: Usuario {user_id}, modulo {module_key} - {e}")
            flash('No se pudo activar el módulo. Intenta de nuevo.', 'error')
        finally:
            cursor.close()
            conn.close()

    return render_template('activar_modulo.html', email=email, module_key=module_key, module_label=module_label)


@auth_bp.route('/reenviar-codigo-activacion', methods=['POST'])
def reenviar_codigo_activacion():
    user_id = session.get('pending_activation_user_id')
    email = session.get('pending_activation_email')
    module_key = session.get('pending_activation_module')
    purpose = session.get('pending_activation_purpose')

    if not user_id or not email or module_key not in {'cotizador', 'nails'} or not purpose:
        flash('No hay una activación pendiente.', 'warning')
        return redirect(url_for('auth.login'))

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT id, username, email, company_name
            FROM usuarios
            WHERE id = %s AND email = %s
            LIMIT 1
            """,
            (user_id, email)
        )
        user_row = cursor.fetchone()
        if not user_row:
            flash('No pudimos encontrar tu cuenta. Intenta de nuevo.', 'error')
            return redirect(url_for('auth.login'))
        user = dict(user_row)

        cursor.execute(
            "UPDATE auth_codes SET used = TRUE WHERE user_id = %s AND email = %s AND purpose = %s AND used = FALSE",
            (user_id, email, purpose)
        )
        activation_code = create_activation_code(user_id, email, purpose, cursor=cursor)
        conn.commit()
        nails_data = session.get('pending_activation_nails') or {}
        _send_activation_code_email(
            email,
            activation_code['code'],
            module_key,
            nombre=user.get('username'),
            salon=nails_data.get('joined_business_name') or nails_data.get('salon_name') or user.get('company_name'),
        )
        flash('Te enviamos un nuevo código.', 'info')
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"MODULE_ACTIVATION_RESEND_ERROR: Usuario {user_id}, modulo {module_key} - {e}")
        flash('No se pudo reenviar el código. Intenta de nuevo.', 'error')
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('auth.activar_modulo'))


# =============================================================================
# VERIFICACIÓN DE EMAIL
# =============================================================================
@auth_bp.route('/verificar-email', methods=['GET', 'POST'])
def verificar_email():
    """
    Pantalla donde el usuario ingresa el código de 6 dígitos que recibió.

    GET  → Muestra el formulario de verificación.
    POST → Valida el código:
           - Debe coincidir con el registrado en auth_codes
           - No debe estar marcado como 'used'
           - No debe estar expirado (expires_at > ahora)
           Tras verificación exitosa:
           - Activa la cuenta del usuario (verificado = TRUE)
           - Limpia los códigos usados (housekeeping de la tabla)
           - Manda correo de bienvenida
    """
    # Recuperamos el email del flujo de registro/login pendiente
    email = session.get('email_por_verificar')

    # Si no hay email en sesión, el usuario llegó aquí por error
    if not email:
        return redirect(url_for('auth.registro'))

    if request.method == 'POST':
        codigo_usuario = request.form.get('codigo').strip()

        conn = get_db()
        cursor = conn.cursor()

        try:
            # Buscamos el código más reciente que:
            # 1. Coincida con el email y código ingresado
            # 2. No haya sido usado antes (used = FALSE)
            # 3. No haya expirado (expires_at > ahora)
            cursor.execute('''
                SELECT * FROM auth_codes
                WHERE email = %s
                  AND code = %s
                  AND COALESCE(purpose, 'verify_email') = 'verify_email'
                  AND used = FALSE
                  AND expires_at > %s
                ORDER BY created_at DESC
                LIMIT 1
            ''', (email, codigo_usuario, now_utc()))

            record = cursor.fetchone()

            if record:
                # Código válido: procesamos la verificación

                # 1. Marcamos el código como usado para que no pueda reutilizarse
                cursor.execute('UPDATE auth_codes SET used = TRUE WHERE id = %s', (record['id'],))

                # 2. Activamos la cuenta del usuario
                cursor.execute('UPDATE usuarios SET verificado = TRUE WHERE id = %s', (record['user_id'],))

                # 3. Limpieza de códigos viejos de este email (housekeeping)
                # Eliminamos los que ya están marcados como usados para no acumular basura
                cursor.execute(
                    "DELETE FROM auth_codes WHERE email = %s AND used = TRUE AND COALESCE(purpose, 'verify_email') = 'verify_email'",
                    (email,)
                )

                # 4. Log de actividad
                cursor.execute(
                    "INSERT INTO logs_actividad (user_id, accion, modulo) VALUES (%s, %s, %s)",
                    (record['user_id'], "Email verificado correctamente", "Seguridad")
                )

                conn.commit()

                # Limpiamos el email de sesión (ya no lo necesitamos)
                session.pop('email_por_verificar', None)
                flash('¡Correo verificado! Ya puedes iniciar sesión.', 'success')

                # 1. Extraemos datos del usuario para mandar la bienvenida correcta por modulo
                cursor.execute(
                    '''
                    SELECT username, company_name, COALESCE(active_module, 'cotizador') AS active_module
                    FROM usuarios
                    WHERE id = %s
                    ''',
                    (record['user_id'],)
                )
                user_data = cursor.fetchone()
                nombre_a_enviar = user_data['username'] if user_data else "Emprendedor"
                active_module = user_data['active_module'] if user_data else session.get('modulo_por_verificar')
                email_profile = _auth_email_profile(active_module)

                # Correo de bienvenida DESPUÉS del commit
                enviar_correo_sian(
                    subject=email_profile["welcome_subject"],
                    recipient=email,
                    template=email_profile["welcome_template"],
                    sender_alias="hola",
                    nombre=nombre_a_enviar,
                    salon=(user_data['company_name'] if user_data else None),
                )
                session.pop('modulo_por_verificar', None)

                return redirect(url_for('auth.login'))

            else:
                # Código incorrecto, ya usado o expirado
                flash('Código incorrecto o ha expirado. Solicita uno nuevo.', 'error')

        except Exception as e:
            conn.rollback()
            current_app.logger.error(f"VERIFY_ERROR: Fallo al verificar email '{email}': {e}")
            flash('Error al procesar la verificación. Intenta de nuevo.', 'error')
        finally:
            cursor.close()
            conn.close()

    return render_template('verificar_email.html', email=email)


# =============================================================================
# REENVIAR CÓDIGO DE VERIFICACIÓN
# =============================================================================
@auth_bp.route('/reenviar-codigo', methods=['POST'])
def reenviar_codigo():
    """
    Permite al usuario solicitar un nuevo código si el anterior expiró.

    Incluye rate limiting: solo se puede reenviar 1 vez por minuto.
    Invalida todos los códigos anteriores antes de generar el nuevo.
    """
    email = session.get('email_por_verificar')

    # Verificamos que haya un flujo de verificación activo
    if not email:
        return jsonify({"status": "error", "message": "Sesión expirada."}), 400

    conn = get_db()
    cursor = conn.cursor()

    try:
        # Rate limiting: no permitir más de 1 reenvío por minuto
        un_minuto_atras = now_utc() - timedelta(minutes=1)
        cursor.execute(
            """
            SELECT id
            FROM auth_codes
            WHERE email = %s
              AND created_at > %s
              AND COALESCE(purpose, 'verify_email') = 'verify_email'
            """,
            (email, un_minuto_atras)
        )

        if cursor.fetchone():
            return jsonify({
                "status": "error",
                "message": "Espera un minuto antes de solicitar otro código."
            }), 429  # HTTP 429 = Too Many Requests

        # Invalidamos todos los códigos anteriores de este email
        cursor.execute(
            "UPDATE auth_codes SET used = TRUE WHERE email = %s AND COALESCE(purpose, 'verify_email') = 'verify_email'",
            (email,)
        )

        # Obtenemos el user_id para poder insertarlo en el nuevo código
        cursor.execute(
            "SELECT id, username, company_name, COALESCE(active_module, 'cotizador') AS active_module FROM usuarios WHERE email = %s",
            (email,)
        )
        user = cursor.fetchone()

        if not user:
            return jsonify({"status": "error", "message": "Usuario no encontrado."}), 404

        # Generamos y guardamos el nuevo código (válido por 10 minutos)
        new_code = generate_verification_code()
        expires_at = now_utc() + timedelta(minutes=10)

        cursor.execute('''
            INSERT INTO auth_codes (user_id, email, code, expires_at, purpose)
            VALUES (%s, %s, %s, %s, %s)
        ''', (user['id'], email, new_code, expires_at, 'verify_email'))

        # Commit primero, correo después
        conn.commit()

        email_profile = _auth_email_profile(user['active_module'])
        enviar_correo_sian(
            subject=email_profile["verification_subject"],
            recipient=email,
            template=email_profile["verification_template"],
            sender_alias="accounts",
            code=new_code,
            nombre=user['username'],
            salon=user['company_name'],
        )

        return jsonify({"status": "success", "message": "Código reenviado con éxito."})

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"RETRY_ERROR: Fallo al reenviar código para '{email}': {e}")
        return jsonify({"status": "error", "message": "Error al reenviar. Intenta de nuevo."}), 500
    finally:
        cursor.close()
        conn.close()


# =============================================================================
# RECUPERACIÓN DE CONTRASEÑA — Solicitud del link
# =============================================================================
@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    """
    El usuario ingresa su email y recibe un link para resetear su contraseña.

    Seguridad:
    - Mostramos el mismo mensaje de éxito aunque el email no exista,
      para no filtrar si un correo está o no registrado.
    - Rate limiting: máximo 3 solicitudes por usuario cada 15 minutos,
      para evitar spam de correos de reset.
    - El token es criptográficamente seguro (secrets.token_urlsafe).
    - El token expira en 30 minutos.
    """
    if request.method == 'POST':
        email = request.form.get('email', '').lower().strip()

        conn = get_db()
        cursor = conn.cursor()

        try:
            # Buscamos al usuario (sin revelar si existe o no en el mensaje al usuario)
            cursor.execute('SELECT id, username FROM usuarios WHERE email = %s', (email,))
            user = cursor.fetchone()

            if user:
                # Rate limiting: máximo 3 solicitudes en los últimos 15 minutos
                quince_min_atras = now_utc() - timedelta(minutes=15)
                cursor.execute('''
                    SELECT COUNT(*) as total FROM password_resets
                    WHERE user_id = %s AND created_at > %s
                ''', (user['id'], quince_min_atras))

                resultado = cursor.fetchone()

                if resultado and resultado['total'] >= 3:
                    current_app.logger.warning(
                        f"RESET_RATE_LIMIT: Usuario {user['id']} superó el límite de resets."
                    )
                    # Mostramos el mensaje genérico igual (no revelamos el bloqueo)
                    flash(
                        'Si el correo está registrado, recibirás un link para restablecer tu contraseña.',
                        'info'
                    )
                    return redirect(url_for('auth.login'))

                # Generamos un token seguro usando el módulo secrets de Python
                # token_urlsafe genera caracteres URL-safe (sin +, / ni =)
                token = secrets.token_urlsafe(32)
                expires_at = now_utc() + timedelta(minutes=30)

                cursor.execute('''
                    INSERT INTO password_resets (user_id, token, expires_at)
                    VALUES (%s, %s, %s)
                ''', (user['id'], token, expires_at))

                # Commit antes de mandar el correo
                conn.commit()

                # Construimos el link completo con el token embebido
                # _external=True genera la URL absoluta (con dominio), necesaria para correos
                reset_link = url_for('auth.reset_password', token=token, _external=True)

                enviar_correo_sian(
                    subject="Restablece tu contraseña - Sianeffects",
                    recipient=email,
                    template="reset_password_mail",
                    sender_alias="accounts",
                    reset_link=reset_link
                )

                current_app.logger.info(
                    f"PASSWORD_RESET_REQUESTED: Token generado para usuario {user['id']}."
                )

        except Exception as e:
            conn.rollback()
            current_app.logger.error(f"FORGOT_PASSWORD_ERROR: Fallo para '{email}': {e}")
        finally:
            cursor.close()
            conn.close()

        # Mensaje genérico siempre (tanto si el email existe como si no)
        # Esto evita que alguien use esta ruta para descubrir qué correos están registrados
        flash(
            'Si el correo está registrado, recibirás un link para restablecer tu contraseña en unos minutos.',
            'info'
        )
        return redirect(url_for('auth.login'))

    return render_template('forgot_password.html')


# =============================================================================
# RECUPERACIÓN DE CONTRASEÑA — Formulario de nueva contraseña
# =============================================================================
@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    """
    El usuario llega desde el link del correo con su token único.

    GET  → Valida el token y muestra el formulario si es válido.
    POST → Actualiza la contraseña si las validaciones pasan.

    Seguridad:
    - El token debe existir, no estar usado y no haber expirado.
    - Al cambiar la contraseña, invalidamos TODOS los tokens activos del usuario,
      no solo el que se usó. Esto previene que tokens viejos sigan siendo válidos.
    """
    conn = get_db()
    cursor = conn.cursor()

    try:
        # Buscamos el token: debe ser real, no usado y no expirado
        cursor.execute('''
            SELECT * FROM password_resets
            WHERE token = %s
              AND used = FALSE
              AND expires_at > %s
        ''', (token, now_utc()))

        reset_request = cursor.fetchone()

        if not reset_request:
            # Token inválido, ya usado o expirado
            flash('El link es inválido o ha expirado. Solicita uno nuevo.', 'error')
            cursor.close()
            conn.close()
            return redirect(url_for('auth.forgot_password'))

        if request.method == 'POST':
            new_password = request.form.get('password', '')
            confirm_password = request.form.get('confirm_password', '')

            # Validaciones de la nueva contraseña
            if len(new_password) < 6:
                flash('La contraseña debe tener al menos 6 caracteres.', 'error')
            elif new_password != confirm_password:
                flash('Las contraseñas no coinciden.', 'error')
            else:
                # Hasheamos la nueva contraseña antes de guardarla
                hashed_pw = generate_password_hash(new_password)

                # Actualizamos la contraseña del usuario
                cursor.execute(
                    'UPDATE usuarios SET password = %s WHERE id = %s',
                    (hashed_pw, reset_request['user_id'])
                )

                # Invalidamos TODOS los tokens de reset de este usuario (no solo el actual)
                # Esto evita que alguien que tenga otro token activo lo pueda usar después
                cursor.execute(
                    'UPDATE password_resets SET used = TRUE WHERE user_id = %s',
                    (reset_request['user_id'],)
                )

                # Log de seguridad para auditoría
                cursor.execute(
                    "INSERT INTO logs_actividad (user_id, accion, modulo) VALUES (%s, %s, %s)",
                    (reset_request['user_id'], "Contraseña restablecida via reset link", "Seguridad")
                )

                conn.commit()

                current_app.logger.info(
                    f"PASSWORD_RESET_SUCCESS: Usuario {reset_request['user_id']} cambió su contraseña."
                )
                flash('Tu contraseña ha sido actualizada. Ya puedes iniciar sesión.', 'success')
                return redirect(url_for('auth.login'))

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"RESET_PASSWORD_ERROR: Fallo con token '{token[:8]}...': {e}")
        flash('Error al procesar el cambio. Intenta de nuevo.', 'error')
        return redirect(url_for('auth.forgot_password'))
    finally:
        cursor.close()
        conn.close()

    return render_template('reset_password_form.html', token=token)


# =============================================================================
# LOGOUT
# =============================================================================
@auth_bp.route('/logout')
def logout():
    """
    Cierra la sesión del usuario actual.

    Usamos session.clear() en lugar de session.pop() uno a uno,
    para asegurarnos de eliminar absolutamente todo lo que haya en sesión,
    incluidos datos que pudieran haberse agregado en otros blueprints.
    """
    usuario = session.get('username', 'Usuario_Desconocido')
    current_app.logger.info(f"LOGOUT: '{usuario}' cerró sesión.")

    # Eliminamos todos los datos de sesión de una sola vez
    session.clear()

    return redirect(url_for('auth.login'))
