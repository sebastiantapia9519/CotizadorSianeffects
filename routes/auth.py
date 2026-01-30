from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
from db import get_db_connection as get_db
from datetime import datetime, timedelta  
import re

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # 1. Usamos el nombre EXACTO que tienes en tu HTML
        login_input = request.form['email_or_user']
        password = request.form['password']
        
        conn = get_db()
        
        # 2. Buscamos si ese texto coincide con el email O con el username
        # Pasamos la variable 'login_input' dos veces para llenar los dos '?'
        user = conn.execute('''
            SELECT * FROM usuarios 
            WHERE email = ? OR username = ?
        ''', (login_input, login_input)).fetchone()
        
        conn.close()
        
        # 3. Verificamos contraseña
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            
            conn = get_db()
            conn.execute('UPDATE usuarios SET last_login = ? WHERE id = ?', (datetime.now(), user['id']))
            conn.commit()
            conn.close()
            
            return redirect(url_for('main.index')) 
            
        else:
            flash('Usuario/Correo o contraseña incorrectos.', 'error')
            
    return render_template('login.html')


@auth_bp.route('/registro', methods=['GET', 'POST'])
def registro():
    if request.method == 'POST':
        # 1. Obtener datos
        nombre = request.form['username']
        email = request.form['email']
        password = request.form['password']
        # Usamos .get() porque estos son nuevos en el form
        empresa = request.form.get('company_name', 'Sin Empresa') 
        telefono = request.form.get('phone', '') # Si viene vacío, guarda cadena vacía
        
        # --- 2. VALIDACIONES DE SEGURIDAD (Python) ---
        
        # A. Validar Contraseña (mínimo 6 caracteres)
        if len(password) < 6:
            flash('La contraseña es muy débil. Usa al menos 6 caracteres.', 'error')
            return render_template('registro.html')

        # B. Validar Email (Formato básico)
        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            flash('El correo electrónico no es válido.', 'error')
            return render_template('registro.html')

        # C. Validar Teléfono (Solo números, opcional)
        # Si el usuario escribió algo en teléfono...
        if telefono:
            # Quitamos espacios o guiones que haya puesto
            telefono_limpio = re.sub(r'\D', '', telefono) 
            if len(telefono_limpio) < 10:
                flash('El teléfono parece incompleto. Ingresa 10 dígitos.', 'error')
                return render_template('registro.html')
            telefono = telefono_limpio # Guardamos solo los números

        # --- 3. GUARDADO EN BASE DE DATOS ---
        hashed_pw = generate_password_hash(password)
        fecha_fin_prueba = datetime.now() + timedelta(days=7)
        fecha_creacion = datetime.now()

        conn = get_db()
        try:
            # ¡OJO! Aquí agregué 'company_name' y 'telefono' que faltaban
            conn.execute('''
                INSERT INTO usuarios (
                    username, email, password, role, subscription_end, created_at, last_login, 
                    company_name, telefono
                ) 
                VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
            ''', (nombre, email, hashed_pw, fecha_fin_prueba, fecha_creacion, datetime.now(), empresa, telefono))
            
            conn.commit()
            flash('Cuenta creada con éxito. ¡Tienes 7 días de prueba!', 'success')
            return redirect(url_for('auth.login'))
            
        except Exception as e:
            # Tip Pro: Si el error es de "UNIQUE constraint", es que el correo ya existe
            if "UNIQUE" in str(e).upper():
                flash('Ese usuario o correo ya está registrado.', 'error')
            else:
                print(f"Error registro: {e}")
                flash('Ocurrió un error al crear la cuenta.', 'error')
        finally:
            conn.close()
            
    return render_template('registro.html')

@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))