from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from db import get_db_connection

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/registro', methods=('GET', 'POST'))
def registro():
    if request.method == 'POST':
        conn = get_db_connection()
        user = conn.execute('SELECT id FROM usuarios WHERE username = ? OR email = ?', (request.form['username'], request.form['email'])).fetchone()
        if user:
            flash('Usuario o correo ya existe.', 'danger')
        else:
            hashed = generate_password_hash(request.form['password'])
            trial = datetime.now() + timedelta(days=7)
            cur = conn.execute('''INSERT INTO usuarios (username, email, password, company_name, telefono, subscription_end, created_at, terms_accepted, role, country_code) 
                                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'MX')''', 
                               (request.form['username'], request.form['email'], hashed, request.form['company_name'], request.form['phone'], trial, datetime.now(), 1))
            uid = cur.lastrowid
            conn.execute('INSERT INTO configuracion (user_id, margen_ganancia, nombre_empresa) VALUES (?, 200, ?)', (uid, request.form['company_name']))
            for n, c in [('Corte Plotter', 5.0), ('Impresión', 1.5), ('Plancha', 12.0)]:
                conn.execute('INSERT INTO maquinaria (user_id, nombre, costo_desgaste) VALUES (?, ?, ?)', (uid, n, c))
            conn.commit()
            flash('¡Bienvenido! 7 días gratis.', 'success')
            return redirect(url_for('auth.login'))
        conn.close()
    return render_template('registro.html')

@auth_bp.route('/login', methods=('GET', 'POST'))
def login():
    if request.method == 'POST':
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM usuarios WHERE username = ?', (request.form['username'],)).fetchone()
        conn.close()
        if user and check_password_hash(user['password'], request.form['password']):
            sub_end = datetime.strptime(str(user['subscription_end'])[:19], '%Y-%m-%d %H:%M:%S')
            if sub_end < datetime.now() and user['role'] == 0:
                flash('Suscripción vencida.', 'warning')
            else:
                session.clear()
                session['user_id'] = user['id']; session['username'] = user['username']; session['role'] = user['role']
                session.permanent = True
                return redirect(url_for('admin.dashboard') if user['role'] > 0 else url_for('main.cotizador'))
        else:
            flash('Credenciales incorrectas.', 'danger')
    return render_template('login.html')

@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))