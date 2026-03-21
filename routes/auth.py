from flask import Blueprint, render_template, request, redirect, url_for, flash, session
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
        # 1. Bajamos a minúsculas en Python, no en la Base de Datos (Más rápido y seguro)
        login_input = request.form['email_or_user'].strip().lower()
        password = request.form['password']
        
        conn = get_db()

        # 2. Simplificamos el Query. La base de datos debe buscar texto exacto si ya lo normalizamos
        user = conn.execute('''
            SELECT * FROM usuarios
            WHERE email = ? OR username = ?
        ''', (login_input, login_input)).fetchone()

        conn.close()

        # Validar credenciales
        if user and check_password_hash(user['password'], password):
            # Guardar datos mínimos en sesión
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']

            # Actualizar último login con UTC
            try:
                conn = get_db()
                conn.execute(
                    'UPDATE usuarios SET last_login = ? WHERE id = ?',
                    (now_utc(), user['id'])
                )
                conn.commit()
            except Exception as e:
                print(f"Error actualizando last_login: {e}")
            finally:
                conn.close()

            return redirect(url_for('main.index'))
        else:
            flash('Usuario/Correo o contraseña incorrectos.', 'error')

    return render_template('login.html')


# =========================================================
# REGISTRO
# =========================================================
@auth_bp.route('/registro', methods=['GET', 'POST'])
def registro():
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

        # -----------------------------
        # 2. VALIDACIONES
        # -----------------------------


        if request.cookies.get('has_free_trial'):
            flash('Tu dispositivo ya ha utilizado una prueba gratuita anteriormente.', 'warning')
            return redirect(url_for('main.plan_vencido'))


        # Contraseña mínima
        if len(password) < 6:
            flash('La contraseña debe tener al menos 6 caracteres.', 'error')
            return render_template('registro.html')

        # VALIDACIÓN DE COINCIDENCIA DE CONTRASEÑAS
        if password != confirm_password:
            flash('Las contraseñas no coinciden.', 'error')
            return render_template('registro.html')

        # Email válido
        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            flash('Correo electrónico no válido.', 'error')
            return render_template('registro.html')

        # BLOQUEO DE CORREOS TEMPORALES
        if is_disposable_email(email):
            flash('Por seguridad, no aceptamos correos temporales. Usa un correo real (Gmail, Outlook, Empresa).', 'error')
            return render_template('registro.html')

# Teléfono opcional, solo números
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
        
        # Usamos now_utc() para garantizar consistencia
        created_at = now_utc()       
        last_login = now_utc()       
        subscription_end = created_at + timedelta(days=7) # Calcula sobre UTC

        conn = get_db()
        try:
            # -----------------------------
            # 4. INSERTAR USUARIO
            # -----------------------------
            cursor = conn.execute('''
                INSERT INTO usuarios (
                    username, email, password, telefono, company_name,
                    role, subscription_end, created_at, last_login, terms_accepted
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                username, email, hashed_pw, telefono, company_name,
                0,  # SIEMPRE 0 (Usuario Normal)
                subscription_end, created_at, last_login,
                1   # SIEMPRE 1 (Asumimos que el front validó el checkbox, pero idealmente se recibe por POST)
            ))

            # --- 🚨 NOTA DE MIGRACIÓN A POSTGRESQL 🚨 ---
            # SQLite usa .lastrowid. Postgres NO lo soporta así.
            # Cuando migres, el INSERT de arriba debe terminar en "RETURNING id"
            # y cambiarás esta línea a: user_id = cursor.fetchone()[0]
            user_id = cursor.lastrowid

            # -----------------------------
            # 5. CREAR CONFIGURACIÓN DEFAULT
            # -----------------------------
            # A) Configuración general del negocio
            conn.execute('''
                INSERT INTO configuracion (
                    user_id, margen_ganancia, inventario_activo, ticket_bw
                )
                VALUES (?, ?, ?, ?)
            ''', (
                user_id,
                100, # Margen inicial garantizado (100%)
                0,   # inventario_activo (Apagado por defecto)
                0    # ticket_bw (Apagado por defecto)
            ))

            # B) Configuración de logística y envíos
            conn.execute('''
                INSERT INTO shipping_configs (
                    user_id, local_base_rate, local_km_rate, safety_margin_percent
                )
                VALUES (?, ?, ?, ?)
            ''', (
                user_id, 
                35.00,  # Tarifa base (banderazo)
                8.00,   # Costo por KM
                10      # Margen de seguridad (10%)
            ))

            conn.commit()

            # 1. Primero definimos el mensaje
            flash('Cuenta creada con éxito. ¡Tienes 7 días de prueba!', 'success')
            
            # 2. Creamos la respuesta (el redirect) y la guardamos en la variable 'resp'
            resp = redirect(url_for('auth.login'))
            
            # 3. AHORA SÍ podemos pegarle la cookie a 'resp'
            resp.set_cookie('has_free_trial', 'true', max_age=31536000)

            # 4. Retornamos la respuesta modificada
            return resp

        except Exception as e:
            conn.rollback()

            if "UNIQUE" in str(e).upper():
                flash('Ese usuario o correo ya existe.', 'error')
            else:
                print(f"Error registro: {e}")
                flash('Error al crear la cuenta.', 'error')
        finally:
            conn.close()

    return render_template('registro.html')


# =========================================================
# LOGOUT
# =========================================================
@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))