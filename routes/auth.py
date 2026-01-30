from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
from db import get_db_connection as get_db
from datetime import datetime, timedelta  # <--- 1. AGREGAMOS timedelta AQUÍ

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
            
            # RECUERDA: Cambia 'index' por el nombre real de tu función principal
            # si se llama diferente (ej: 'dashboard', 'home', etc.)
            return redirect(url_for('main.index')) 
            
        else:
            flash('Usuario/Correo o contraseña incorrectos.', 'error')
            
    return render_template('login.html')


@auth_bp.route('/registro', methods=['GET', 'POST'])
def registro():
    if request.method == 'POST':
        nombre = request.form['username']
        email = request.form['email']
        password = request.form['password']
        hashed_pw = generate_password_hash(password)
        
        # --- 2. CALCULAMOS LOS 7 DÍAS DE PRUEBA ---
        fecha_prueba = datetime.now() + timedelta(days=7)
        
        conn = get_db()
        try:
            # --- 3. AGREGAMOS 'subscription_end' AL INSERT ---
            # Nota: Agregué subscription_end en los campos y el valor fecha_prueba al final
            conn.execute('''
                INSERT INTO usuarios (username, email, password, role, subscription_end) 
                VALUES (?, ?, ?, 1, ?)
            ''', (nombre, email, hashed_pw, fecha_prueba))
            
            conn.commit()
            flash('Cuenta creada con éxito. ¡Tienes 7 días de prueba!', 'success')
            return redirect(url_for('auth.login'))
        except Exception as e:
            # Imprimimos el error en consola para que veas si pasa algo raro
            print(f"Error registro: {e}")
            flash('Ese correo o usuario ya está registrado.', 'error')
        finally:
            conn.close()
            
    return render_template('registro.html')

@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))