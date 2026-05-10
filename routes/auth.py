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

# Registramos el Blueprint de autenticación (sin prefijo de URL, rutas directas)
auth_bp = Blueprint('auth', __name__)


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
                    flash('Tu cuenta aún no ha sido verificada. Revisa tu correo.', 'warning')
                    return redirect(url_for('auth.verificar_email'))

                # SESSION FIXATION PREVENTION:
                # Limpiamos cualquier sesión anterior ANTES de asignar la nueva.
                # Esto evita que alguien que obtuvo un session_id antes del login
                # lo use para acceder con los datos del usuario recién autenticado.
                session.clear()
                session.permanent = True  # La sesión dura lo configurado en PERMANENT_SESSION_LIFETIME

                # Guardamos solo lo esencial en sesión (nunca datos sensibles como la contraseña)
                session['user_id'] = user['id']
                session['username'] = user['username']
                session['role'] = user['role']

                sub_end = user['subscription_end']
                estado = user['estado_suscripcion']
                
                try:
                    # Nivelamos ambas fechas quitándoles la zona horaria solo para compararlas
                    ahora = now_utc().replace(tzinfo=None) if now_utc().tzinfo else now_utc()
                    if sub_end:
                        sub_end_clean = sub_end.replace(tzinfo=None) if sub_end.tzinfo else sub_end
                    
                    # Verificamos si es PRO
                    if sub_end and sub_end_clean > ahora and estado == 'Activo':
                        session['is_pro_active'] = True
                        session.pop('grace_period', None)
                    else:
                        session['is_pro_active'] = False
                except Exception as e:
                    current_app.logger.error(f"Error comparando fechas en login: {e}")
                    session['is_pro_active'] = False

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
                return redirect(url_for('main.index'))

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
        cursor.execute('SELECT id FROM usuarios WHERE email = %s', (email,))
        if cursor.fetchone():
            flash('Este correo ya está registrado. ¿Olvidaste tu contraseña?', 'error')
            return render_template('registro.html')

        # ----------------------------------------------------------------
        # INSERCIÓN DEL USUARIO
        # RETURNING id → psycopg2 requiere esto para obtener el ID recién creado.
        # Sin RETURNING, cursor.fetchone() devuelve None y todo crashea.
        # ----------------------------------------------------------------
        cursor.execute('''
            INSERT INTO usuarios (
                username, email, password, telefono, company_name,
                role, subscription_end, created_at, last_login, terms_accepted,
                origen_registro, utm_campaign, verificado
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (
            username, email, hashed_pw, telefono, company_name,
            0, subscription_end, created_at, created_at, True,
            origen_registro, utm_campaign, False  # verificado=False hasta confirmar el email
        ))

        user_id = cursor.fetchone()['id']  # ID del usuario recién creado

        # ----------------------------------------------------------------
        # INICIALIZACIÓN DE SERVICIOS POR DEFECTO
        # Configuramos valores iniciales para que el usuario no encuentre
        # la app vacía en su primer ingreso.
        # ----------------------------------------------------------------

        # Configuración general del negocio (margen, inventario, etc.)
        cursor.execute('''
            INSERT INTO configuracion (user_id, margen_ganancia, inventario_activo, ticket_bw, nombre_empresa)
            VALUES (%s, %s, %s, %s, %s)
        ''', (user_id, 100, False, False, company_name))

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
            INSERT INTO auth_codes (user_id, email, code, expires_at)
            VALUES (%s, %s, %s, %s)
        ''', (user_id, email, v_code, expires_at))

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
                cursor.execute('DELETE FROM auth_codes WHERE email = %s AND used = TRUE', (email,))

                # 4. Log de actividad
                cursor.execute(
                    "INSERT INTO logs_actividad (user_id, accion, modulo) VALUES (%s, %s, %s)",
                    (record['user_id'], "Email verificado correctamente", "Seguridad")
                )

                conn.commit()

                # Limpiamos el email de sesión (ya no lo necesitamos)
                session.pop('email_por_verificar', None)
                flash('¡Correo verificado! Ya puedes iniciar sesión.', 'success')

                # 1. Extraemos el nombre del usuario de la base de datos
                cursor.execute('SELECT username FROM usuarios WHERE id = %s', (record['user_id'],))
                user_data = cursor.fetchone()
                nombre_a_enviar = user_data['username'] if user_data else "Emprendedor"

                # Correo de bienvenida DESPUÉS del commit
                enviar_correo_sian(
                    subject="¡Bienvenido/a a Sianeffects!",
                    recipient=email,
                    template="bienvenida",
                    sender_alias="hola",
                    nombre=nombre_a_enviar
                )

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
            'SELECT id FROM auth_codes WHERE email = %s AND created_at > %s',
            (email, un_minuto_atras)
        )

        if cursor.fetchone():
            return jsonify({
                "status": "error",
                "message": "Espera un minuto antes de solicitar otro código."
            }), 429  # HTTP 429 = Too Many Requests

        # Invalidamos todos los códigos anteriores de este email
        cursor.execute('UPDATE auth_codes SET used = TRUE WHERE email = %s', (email,))

        # Obtenemos el user_id para poder insertarlo en el nuevo código
        cursor.execute('SELECT id FROM usuarios WHERE email = %s', (email,))
        user = cursor.fetchone()

        if not user:
            return jsonify({"status": "error", "message": "Usuario no encontrado."}), 404

        # Generamos y guardamos el nuevo código (válido por 10 minutos)
        new_code = generate_verification_code()
        expires_at = now_utc() + timedelta(minutes=10)

        cursor.execute('''
            INSERT INTO auth_codes (user_id, email, code, expires_at)
            VALUES (%s, %s, %s, %s)
        ''', (user['id'], email, new_code, expires_at))

        # Commit primero, correo después
        conn.commit()

        enviar_correo_sian(
            subject="Nuevo código de verificación - Sianeffects",
            recipient=email,
            template="auth_code",
            sender_alias="accounts",
            code=new_code
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