from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
from db import get_db_connection as get_db
from datetime import datetime, timedelta  
import re

auth_bp = Blueprint('auth', __name__)

# =========================================================
# LOGIN
# =========================================================
@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # Puede ser email o username
        login_input = request.form['email_or_user']
        password = request.form['password']
        
        conn = get_db()

        # Buscar usuario por email o username
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

            # Actualizar último login
            conn = get_db()
            conn.execute(
                'UPDATE usuarios SET last_login = ? WHERE id = ?',
                (datetime.now(), user['id'])
            )
            conn.commit()
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
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        telefono = request.form.get('phone', '')
        company_name = request.form.get('company_name', 'Mi Negocio')

        # -----------------------------
        # 2. VALIDACIONES
        # -----------------------------

        # Contraseña mínima
        if len(password) < 6:
            flash('La contraseña debe tener al menos 6 caracteres.', 'error')
            return render_template('registro.html')

        # Email válido
        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            flash('Correo electrónico no válido.', 'error')
            return render_template('registro.html')

        # Teléfono opcional, solo números
        if telefono:
            telefono_limpio = re.sub(r'\D', '', telefono)
            if len(telefono_limpio) < 10:
                flash('El teléfono debe tener al menos 10 dígitos.', 'error')
                return render_template('registro.html')
            telefono = telefono_limpio

        # -----------------------------
        # 3. PREPARAR DATOS
        # -----------------------------
        hashed_pw = generate_password_hash(password)
        created_at = datetime.now()
        last_login = datetime.now()
        subscription_end = created_at + timedelta(days=7)

        conn = get_db()
        try:
            # -----------------------------
            # 4. INSERTAR USUARIO
            # -----------------------------
            cursor = conn.execute('''
                INSERT INTO usuarios (
                    username,
                    email,
                    password,
                    telefono,
                    company_name,
                    role,
                    subscription_end,
                    created_at,
                    last_login
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                username,
                email,
                hashed_pw,
                telefono,
                company_name,
                0,  # role = usuario normal
                subscription_end,
                created_at,
                last_login
            ))

            # ID del usuario recién creado
            user_id = cursor.lastrowid

            # -----------------------------
            # 5. CREAR CONFIGURACIÓN DEFAULT
            # -----------------------------
            conn.execute('''
                INSERT INTO configuracion (
                    user_id,
                    margen_ganancia
                )
                VALUES (?, ?)
            ''', (
                user_id,
                100  # Margen inicial garantizado
            ))

            conn.commit()

            flash('Cuenta creada con éxito. ¡Tienes 7 días de prueba!', 'success')
            return redirect(url_for('auth.login'))

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
