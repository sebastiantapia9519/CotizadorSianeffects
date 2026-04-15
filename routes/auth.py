from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from db import get_db_connection as get_db
from datetime import timedelta  
from utils.email_validators import is_disposable_email
import re

# IMPORTAMOS LA UTILIDAD CENTRALIZADA
from utils.datetime_utils import now_utc

auth_bp = Blueprint('auth', __name__)

# =========================================================
# LOGIN
# =========================================================
@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        login_input = request.form['email_or_user'].strip().lower()
        password = request.form['password']
        
        current_app.logger.info(f"Intento de login para: {login_input}") # Monitor!

        conn = get_db()
        cursor = conn.cursor() # 1. ABRIMOS CURSOR
        
        # 2. CAMBIAMOS ? POR %s
        cursor.execute('''
            SELECT * FROM usuarios
            WHERE LOWER(email) = %s OR LOWER(username) = %s
        ''', (login_input, login_input))
        
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if user:
            # Si encuentra al usuario, validamos la clave
            if check_password_hash(user['password'], password):
                session.permanent = True 
                session['user_id'] = user['id']
                session['username'] = user['username']
                session['role'] = user['role']

                # Log de éxito
                current_app.logger.info(f"LOGIN_SUCCESS: User '{user['username']}' authenticated.")
                return redirect(url_for('main.index'))
            else:
                current_app.logger.warning(f"LOGIN_FAILED: Invalid password for user '{login_input}'.")
                flash('Usuario/Correo o contraseña incorrectos.', 'error')
        else:
            current_app.logger.warning(f"LOGIN_FAILED: User not found '{login_input}'.")
            flash('Usuario/Correo o contraseña incorrectos.', 'error')

    return render_template('login.html')


# =========================================================
# REGISTRO
# =========================================================
@auth_bp.route('/registro', methods=['GET', 'POST'])
def registro():
    if request.method == 'GET':
        # --- ATRAPAR UTMS EN LA SESIÓN ---
        # Si el link trae parámetros, los guardamos discretamente
        if request.args.get('utm_source'):
            session['utm_source'] = request.args.get('utm_source').lower()
        if request.args.get('utm_campaign'):
            session['utm_campaign'] = request.args.get('utm_campaign').lower()
        
        return render_template('registro.html')

    if request.method == 'POST':

        # -----------------------------
        # 1. DATOS DEL FORM
        # -----------------------------
        username = request.form['username'].lower().strip()
        email = request.form['email'].lower().strip()
        password = request.form['password']
        confirm_password = request.form['confirm_password']
        telefono = request.form.get('phone', '')
        company_name = request.form.get('company_name', 'Mi Negocio')

        # --- SACAR UTMS DE LA SESIÓN ---
        # pop() las saca y las borra de la sesión para no contaminar futuros registros
        origen_registro = session.pop('utm_source', 'desconocido')
        utm_campaign = session.pop('utm_campaign', None)

        # -----------------------------
        # 2. VALIDACIONES
        # -----------------------------
        if request.cookies.get('has_free_trial'):
            flash('Tu dispositivo ya ha utilizado una prueba gratuita anteriormente.', 'warning')
            return redirect(url_for('main.plan_vencido'))

        if len(password) < 6:
            flash('La contraseña debe tener al menos 6 caracteres.', 'error')
            return render_template('registro.html')

        if password != confirm_password:
            flash('Las contraseñas no coinciden.', 'error')
            return render_template('registro.html')

        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            flash('Correo electrónico no válido.', 'error')
            return render_template('registro.html')

        if is_disposable_email(email):
            flash('Por seguridad, no aceptamos correos temporales. Usa un correo real (Gmail, Outlook, Empresa).', 'error')
            return render_template('registro.html')

        if telefono:
            telefono_limpio = re.sub(r'\D', '', telefono)
            if len(telefono_limpio) < 10:
                flash('El teléfono debe tener al menos 10 dígitos.', 'error')
                return render_template('registro.html')
            telefono = telefono_limpio

        # -----------------------------
        # 3. PREPARAR DATOS (TODO EN UTC)
        # -----------------------------
        hashed_pw = generate_password_hash(password)
        created_at = now_utc()       
        last_login = now_utc()       
        subscription_end = created_at + timedelta(days=7)

        conn = get_db()
        cursor = conn.cursor()
        
        try:
            # -----------------------------
            # 4. INSERTAR USUARIO (CON RETURNING id Y %s)
            # -----------------------------
            cursor.execute('''
                INSERT INTO usuarios (
                    username, email, password, telefono, company_name,
                    role, subscription_end, created_at, last_login, terms_accepted,
                    origen_registro, utm_campaign
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (
                username, email, hashed_pw, telefono, company_name,
                0, 
                subscription_end, created_at, last_login,
                True, # Boolean en lugar de 1
                origen_registro, utm_campaign 
            ))

            # 3. EXTRAER EL ID RETORNADO
            user_id = cursor.fetchone()['id']

            # -----------------------------
            # 5. CREAR CONFIGURACIÓN DEFAULT (CON %s)
            # -----------------------------
            cursor.execute('''
                INSERT INTO configuracion (
                    user_id, margen_ganancia, inventario_activo, ticket_bw, nombre_empresa
                )
                VALUES (%s, %s, %s, %s, %s)
            ''', (user_id, 100, False, False, company_name)) # Booleans

            cursor.execute('''
                INSERT INTO shipping_configs (
                    user_id, local_base_rate, local_km_rate, safety_margin_percent
                )
                VALUES (%s, %s, %s, %s)
            ''', (user_id, 35.00, 8.00, 10))

            conn.commit()
            
            # Log de negocio: ¡Un cliente nuevo!
            current_app.logger.info(f"REGISTER_SUCCESS: Nuevo usuario registrado -> '{username}' ({email}) de la empresa '{company_name}'. UTM: {utm_campaign}")

            flash('Cuenta creada con éxito. ¡Tienes 7 días de prueba!', 'success')
            resp = redirect(url_for('auth.login'))
            resp.set_cookie('has_free_trial', 'true', max_age=31536000)
            return resp

        except Exception as e:
            conn.rollback()
            if "UNIQUE" in str(e).upper() or "DUPLICATE" in str(e).upper():
                flash('Ese usuario o correo ya existe.', 'error')
            else:
                current_app.logger.error(f"REGISTER_ERROR: Fallo al crear cuenta para '{email}' - {str(e)}")
                flash('Error al crear la cuenta.', 'error')
        finally:
            cursor.close()
            conn.close()

    # Esto asegura que si hay un error y no entra al try, de todos modos regrese el form
    return render_template('registro.html')


# =========================================================
# LOGOUT
# =========================================================
@auth_bp.route('/logout')
def logout():
    # Sacamos el nombre antes de limpiar la sesión para saber quién se fue
    usuario = session.get('username', 'Usuario_Desconocido')
    current_app.logger.info(f"LOGOUT: '{usuario}' cerró su sesión.")
    
    session.clear()
    return redirect(url_for('auth.login'))