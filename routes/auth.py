from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from db import get_db_connection as get_db
from datetime import timedelta  
import re
import secrets

# UTILIDADES Y SERVICIOS LOCALES
from utils.email_validators import is_disposable_email
from services.mail_service import enviar_correo_sian
from utils.auth_utils import generate_verification_code
from utils.datetime_utils import now_utc

auth_bp = Blueprint('auth', __name__)

# =========================================================
# LOGIN (ESTRICTAMENTE POR EMAIL)
# =========================================================
@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # Normalizamos el input
        email_input = request.form['email_or_user'].strip().lower()
        password = request.form['password']
        
        current_app.logger.info(f"LOGIN_ATTEMPT: Intento de acceso para {email_input}")

        conn = get_db()
        cursor = conn.cursor()
        
        # Buscamos solo por email para evitar ambigüedad con usernames repetidos
        cursor.execute('SELECT * FROM usuarios WHERE LOWER(email) = %s', (email_input,))
        user = cursor.fetchone()
        
        cursor.close()
        conn.close()

        if user and check_password_hash(user['password'], password):
            # Configuración de sesión
            session.permanent = True 
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']

            # BITÁCORA: Registro de inicio de sesión exitoso
            try:
                conn_log = get_db()
                cursor_log = conn_log.cursor()
                cursor_log.execute("""
                    INSERT INTO logs_actividad (user_id, accion, modulo) 
                    VALUES (%s, %s, %s)
                """, (user['id'], "Inició sesión en el sistema", "Acceso"))
                conn_log.commit()
                cursor_log.close()
                conn_log.close()
            except Exception as e:
                current_app.logger.warning(f"LOG_ERROR: No se pudo registrar login en bitácora: {e}")

            current_app.logger.info(f"LOGIN_SUCCESS: Usuario '{user['username']}' autenticado.")
            return redirect(url_for('main.index'))
        else:
            current_app.logger.warning(f"LOGIN_FAILED: Credenciales inválidas para {email_input}")
            flash('Correo o contraseña incorrectos.', 'error')

    return render_template('login.html')


# =========================================================
# REGISTRO (EMAIL ÚNICO / USERNAME REPETIBLE)
# =========================================================
@auth_bp.route('/registro', methods=['GET', 'POST'])
def registro():
    if request.method == 'GET':
        # Captura de UTMs para marketing
        if request.args.get('utm_source'):
            session['utm_source'] = request.args.get('utm_source').lower()
        if request.args.get('utm_campaign'):
            session['utm_campaign'] = request.args.get('utm_campaign').lower()
        return render_template('registro.html')

    if request.method == 'POST':
        # 1. Extracción de datos
        username = request.form['username'].lower().strip()
        email = request.form['email'].lower().strip()
        password = request.form['password']
        confirm_password = request.form['confirm_password']
        telefono = request.form.get('phone', '')
        company_name = request.form.get('company_name', 'Mi Negocio')

        origen_registro = session.pop('utm_source', 'desconocido')
        utm_campaign = session.pop('utm_campaign', None)

        # 2. Validaciones de Negocio
        if request.cookies.get('has_free_trial'):
            flash('Este dispositivo ya utilizó una prueba gratuita.', 'warning')
            return redirect(url_for('main.plan_vencido'))

        if len(password) < 6 or password != confirm_password:
            flash('Revisa que la contraseña sea válida y coincida.', 'error')
            return render_template('registro.html')

        if not re.match(r"[^@]+@[^@]+\.[^@]+", email) or is_disposable_email(email):
            flash('Por favor usa un correo electrónico real y válido.', 'error')
            return render_template('registro.html')

        # 3. Preparación de datos (Seguridad y Tiempos)
        hashed_pw = generate_password_hash(password)
        created_at = now_utc()       
        subscription_end = created_at + timedelta(days=7)

        conn = get_db()
        cursor = conn.cursor()
        
        try:
            # 4. Verificación de Email Único (El username ya no se valida aquí)
            cursor.execute('SELECT id FROM usuarios WHERE email = %s', (email,))
            if cursor.fetchone():
                flash('Este correo ya está registrado.', 'error')
                return render_template('registro.html')

            # 5. Inserción de Usuario
            cursor.execute('''
                INSERT INTO usuarios (
                    username, email, password, telefono, company_name,
                    role, subscription_end, created_at, last_login, terms_accepted,
                    origen_registro, utm_campaign
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (username, email, hashed_pw, telefono, company_name,
                  0, subscription_end, created_at, created_at, True, 
                  origen_registro, utm_campaign))

            user_id = cursor.fetchone()['id']

            # 6. Inicialización de Servicios (Configuraciones Default)
            cursor.execute('''
                INSERT INTO configuracion (user_id, margen_ganancia, inventario_activo, ticket_bw, nombre_empresa)
                VALUES (%s, %s, %s, %s, %s)
            ''', (user_id, 100, False, False, company_name))

            cursor.execute('''
                INSERT INTO shipping_configs (user_id, local_base_rate, local_km_rate, safety_margin_percent)
                VALUES (%s, %s, %s, %s)
            ''', (user_id, 35.00, 8.00, 10))

            cursor.execute("INSERT INTO logs_actividad (user_id, accion, modulo) VALUES (%s, %s, %s)", 
                           (user_id, "Registro inicial completo", "Cuenta"))

            # 7. Generación de Código de Verificación
            v_code = generate_verification_code()
            expires_at = now_utc() + timedelta(minutes=10)

            cursor.execute('''
                INSERT INTO auth_codes (user_id, email, code, expires_at)
                VALUES (%s, %s, %s, %s)
            ''', (user_id, email, v_code, expires_at))

            # 8. Envío de Email (Alias: accounts)
            enviar_correo_sian(
                subject="Verifica tu cuenta - Sianeffects",
                recipient=email,
                template="auth_code",
                sender_alias="accounts",
                code=v_code
            )

            # --- COMMIT FINAL: Si llegamos aquí sin errores, se guarda TODO ---
            conn.commit() 
            
            current_app.logger.info(f"REGISTER_SUCCESS: Usuario '{username}' ({email}) registrado. Código enviado.")
            session['email_por_verificar'] = email
            
            flash('Te enviamos un código de verificación a tu correo.', 'info')
            resp = redirect(url_for('auth.verificar_email'))
            resp.set_cookie('has_free_trial', 'true', max_age=31536000)
            return resp

        except Exception as e:
            conn.rollback()
            current_app.logger.error(f"REGISTER_ERROR: Fallo crítico en registro de {email}: {str(e)}")
            flash('Error al procesar el registro. Intenta de nuevo.', 'error')
        finally:
            cursor.close()
            conn.close()

    return render_template('registro.html')


# =========================================================
# VERIFICACIÓN DE EMAIL
# =========================================================
@auth_bp.route('/verificar-email', methods=['GET', 'POST'])
def verificar_email():
    email = session.get('email_por_verificar')
    if not email:
        return redirect(url_for('auth.registro'))

    if request.method == 'POST':
        codigo_usuario = request.form.get('codigo').strip()
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Validación de código: debe coincidir, no estar usado y no estar expirado
        cursor.execute('''
            SELECT * FROM auth_codes 
            WHERE email = %s AND code = %s AND used = FALSE AND expires_at > %s
            ORDER BY created_at DESC LIMIT 1
        ''', (email, codigo_usuario, now_utc()))
        
        record = cursor.fetchone()
        
        if record:
            # 1. Marcar código como procesado
            cursor.execute('UPDATE auth_codes SET used = TRUE WHERE id = %s', (record['id'],))
            
            # 2. Log de actividad
            cursor.execute("INSERT INTO logs_actividad (user_id, accion, modulo) VALUES (%s, %s, %s)", 
                           (record['user_id'], "Email verificado correctamente", "Seguridad"))
            
            conn.commit()
            session.pop('email_por_verificar', None) # Limpiamos sesión
            flash('¡Correo verificado! Te damos la bienvenida a Sianeffects.', 'success')
            
            # 3. Email de Bienvenida (Alias: hola)
            enviar_correo_sian(
                subject="¡Bienvenido/a a Sianeffects!",
                recipient=email,
                template="bienvenida",
                sender_alias="hola"
            )
            
            return redirect(url_for('auth.login'))
        else:
            flash('Código incorrecto o ha expirado.', 'error')
            
        cursor.close()
        conn.close()

    return render_template('verificar_email.html', email=email)


# =========================================================
# REENVIAR CÓDIGO (CON RATE LIMITING)
# =========================================================
@auth_bp.route('/reenviar-codigo', methods=['POST'])
def reenviar_codigo():
    email = session.get('email_por_verificar')
    if not email:
        return jsonify({"status": "error", "message": "Sesión expirada."}), 400

    conn = get_db()
    cursor = conn.cursor()

    try:
        # Rate Limit: 1 minuto entre reenvíos
        un_minuto_atras = now_utc() - timedelta(minutes=1)
        cursor.execute('SELECT id FROM auth_codes WHERE email = %s AND created_at > %s', (email, un_minuto_atras))
        
        if cursor.fetchone():
            return jsonify({"status": "error", "message": "Espera un minuto para solicitar otro código."}), 429

        # Invalida códigos previos
        cursor.execute('UPDATE auth_codes SET used = TRUE WHERE email = %s', (email,))
        
        # Obtener ID para el log
        cursor.execute('SELECT id FROM usuarios WHERE email = %s', (email,))
        user = cursor.fetchone()
        
        new_code = generate_verification_code()
        expires_at = now_utc() + timedelta(minutes=10)

        cursor.execute('''
            INSERT INTO auth_codes (user_id, email, code, expires_at)
            VALUES (%s, %s, %s, %s)
        ''', (user['id'], email, new_code, expires_at))

        enviar_correo_sian(
            subject="Nuevo código de verificación - Sianeffects",
            recipient=email,
            template="auth_code",
            sender_alias="accounts",
            code=new_code
        )

        conn.commit()
        return jsonify({"status": "success", "message": "Código reenviado con éxito."})

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"RETRY_ERROR: {str(e)}")
        return jsonify({"status": "error", "message": "Error al reenviar."}), 500
    finally:
        cursor.close()
        conn.close()


# =========================================================
# SOLICITAR RESET DE CONTRASEÑA
# =========================================================
@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email').lower().strip()
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT id, username FROM usuarios WHERE email = %s', (email,))
        user = cursor.fetchone()

        if user:
            # 1. Generar token único y expiración (30 min)
            token = secrets.token_urlsafe(32)
            expires_at = now_utc() + timedelta(minutes=30)

            # 2. Guardar en la tabla password_resets
            cursor.execute('''
                INSERT INTO password_resets (user_id, token, expires_at)
                VALUES (%s, %s, %s)
            ''', (user['id'], token, expires_at))
            conn.commit()

            # 3. Mandar el correo con el link
            # El link debe apuntar a tu dominio real o localhost en dev
            reset_link = url_for('auth.reset_password', token=token, _external=True)
            
            enviar_correo_sian(
                subject="Restablece tu contraseña - Sianeffects",
                recipient=email,
                template="reset_password_mail", # Crea este HTML simple
                sender_alias="accounts",
                reset_link=reset_link
            )

        # Por seguridad, mostramos el mismo mensaje aunque el correo no exista
        # para que no puedan "pescar" correos registrados.
        flash('Si el correo está registrado, recibirás un link para restablecer tu contraseña en unos minutos.', 'info')
        return redirect(url_for('auth.login'))

    return render_template('forgot_password.html')

# =========================================================
# ESTABLECER NUEVA CONTRASEÑA
# =========================================================
@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    conn = get_db()
    cursor = conn.cursor()

    # Validar que el token sea real, no usado y no expirado
    cursor.execute('''
        SELECT * FROM password_resets 
        WHERE token = %s AND used = FALSE AND expires_at > %s
    ''', (token, now_utc()))
    reset_request = cursor.fetchone()

    if not reset_request:
        flash('El link es inválido o ha expirado.', 'error')
        return redirect(url_for('auth.forgot_password'))

    if request.method == 'POST':
        new_password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if len(new_password) < 6:
            flash('La contraseña debe tener al menos 6 caracteres.', 'error')
        elif new_password != confirm_password:
            flash('Las contraseñas no coinciden.', 'error')
        else:
            # 1. Hashear nueva clave
            hashed_pw = generate_password_hash(new_password)
            
            # 2. Actualizar usuario y marcar token como usado
            cursor.execute('UPDATE usuarios SET password = %s WHERE id = %s', 
                           (hashed_pw, reset_request['user_id']))
            cursor.execute('UPDATE password_resets SET used = TRUE WHERE id = %s', 
                           (reset_request['id'],))
            
            # 3. Log de seguridad
            cursor.execute("INSERT INTO logs_actividad (user_id, accion, modulo) VALUES (%s, %s, %s)", 
                           (reset_request['user_id'], "Cambio de contraseña mediante reset link", "Seguridad"))
            current_app.logger.info(f"PASSWORD_RESET: El usuario {reset_request['user_id']} cambió su contraseña.")
            
            conn.commit()
            flash('Tu contraseña ha sido actualizada. Ya puedes entrar.', 'success')
            return redirect(url_for('auth.login'))

    return render_template('reset_password_form.html', token=token)


# =========================================================
# LOGOUT
# =========================================================
@auth_bp.route('/logout')
def logout():
    usuario = session.get('username', 'Usuario_Desconocido')
    current_app.logger.info(f"LOGOUT: '{usuario}' cerró sesión.")
    session.clear()
    return redirect(url_for('auth.login'))