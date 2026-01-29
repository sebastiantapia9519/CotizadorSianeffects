from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
from db import get_db_connection as get_db
from datetime import datetime  # <--- 1. NUEVO IMPORT

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        credencial = request.form['email_or_user']
        password = request.form['password']
        
        conn = get_db()
        # Buscamos al usuario
        user = conn.execute('SELECT * FROM usuarios WHERE email = ? OR username = ?', (credencial, credencial)).fetchone()
        
        # NOTA: No cerramos la conexión (conn.close) todavía, porque la necesitamos para el update
        
        if user and check_password_hash(user['password'], password):
            # --- 2. CÓDIGO NUEVO: Actualizar última conexión ---
            try:
                ahora = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                # Usamos 'usuarios' porque así está en tu SELECT de arriba
                conn.execute('UPDATE usuarios SET last_login = ? WHERE id = ?', (ahora, user['id']))
                conn.commit()
            except Exception as e:
                # Si falla (ej. no existe la columna) no rompemos el login, solo avisamos en consola
                print(f"⚠️ No se pudo actualizar last_login: {e}")
            # ---------------------------------------------------

            conn.close() # Ahora sí cerramos la conexión

            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            
            if user['role'] == 2:
                return redirect(url_for('admin.dashboard'))
            return redirect(url_for('main.cotizador'))
        else:
            conn.close() # Cerramos la conexión si la contraseña falló
            flash('Usuario/Correo o contraseña incorrectos', 'error')
            
    return render_template('login.html')

@auth_bp.route('/registro', methods=['GET', 'POST'])
def registro():
    if request.method == 'POST':
        nombre = request.form['username']
        email = request.form['email']
        password = request.form['password']
        hashed_pw = generate_password_hash(password)
        
        conn = get_db()
        try:
            conn.execute('INSERT INTO usuarios (username, email, password, role) VALUES (?, ?, ?, 1)', 
                         (nombre, email, hashed_pw))
            conn.commit()
            flash('Cuenta creada con éxito. ¡Inicia sesión!', 'success')
            return redirect(url_for('auth.login'))
        except Exception as e:
            flash('Ese correo o usuario ya está registrado.', 'error')
        finally:
            conn.close()
            
    return render_template('registro.html')

@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))