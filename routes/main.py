from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file
from werkzeug.security import generate_password_hash
from datetime import datetime
import pandas as pd
import io
from db import get_db_connection
from helpers import login_required

main_bp = Blueprint('main', __name__)

@main_bp.route('/')
def index():
    return redirect(url_for('main.cotizador'))

@main_bp.route('/cotizador')
@login_required
def cotizador():
    conn = get_db_connection(); uid = session['user_id']
    data = {
        'config': conn.execute('SELECT * FROM configuracion WHERE user_id=?', (uid,)).fetchone(),
        'materiales': conn.execute('SELECT * FROM materiales WHERE user_id=?', (uid,)).fetchall(),
        'productos': conn.execute('SELECT * FROM productos WHERE user_id=?', (uid,)).fetchall(),
        'equipos': conn.execute('SELECT * FROM maquinaria WHERE user_id=?', (uid,)).fetchall()
    }
    conn.close()
    return render_template('cotizador.html', **data)

@main_bp.route('/historial')
@login_required
def historial():
    conn = get_db_connection(); uid = session['user_id']; q = request.args.get('q')
    if q: ventas = conn.execute('SELECT * FROM ventas WHERE user_id=? AND (id=? OR cliente LIKE ?) ORDER BY id DESC', (uid, q, f'%{q}%')).fetchall()
    else: ventas = conn.execute('SELECT * FROM ventas WHERE user_id=? ORDER BY id DESC', (uid,)).fetchall()
    conn.close()
    return render_template('historial.html', ventas=ventas)

@main_bp.route('/ticket/<int:id>')
@login_required
def ver_ticket(id):
    conn = get_db_connection()
    venta = conn.execute('SELECT * FROM ventas WHERE id=? AND user_id=?', (id, session['user_id'])).fetchone()
    detalles = conn.execute('SELECT * FROM venta_detalles WHERE venta_id=?', (id,)).fetchall()
    config = conn.execute('SELECT * FROM configuracion WHERE user_id=?', (session['user_id'],)).fetchone()
    conn.close()
    if not venta: return "No encontrado"
    return render_template('ticket.html', venta=venta, detalles=detalles, config=config)

@main_bp.route('/configuracion', methods=('GET', 'POST'))
@login_required
def configuracion():
    conn = get_db_connection(); uid = session['user_id']
    if request.method == 'POST':
        if 'new_username' in request.form:
            try:
                conn.execute('UPDATE usuarios SET username=?, password=? WHERE id=?', (request.form['new_username'], generate_password_hash(request.form['new_password']), uid))
                session['username'] = request.form['new_username']
                flash('Credenciales actualizadas.', 'success')
            except: flash('Usuario ocupado.', 'danger')
        else:
            conn.execute('UPDATE configuracion SET margen_ganancia=?, nombre_empresa=?, slogan=?, website=? WHERE user_id=?', (request.form['margen'], request.form['nombre_empresa'], request.form['slogan'], request.form['website'], uid))
            flash('Datos guardados.', 'success')
        conn.commit(); conn.close(); return redirect(url_for('main.configuracion'))
    config = conn.execute('SELECT * FROM configuracion WHERE user_id=?', (uid,)).fetchone()
    user = conn.execute('SELECT * FROM usuarios WHERE id=?', (uid,)).fetchone()
    conn.close()
    return render_template('configuracion.html', config=config, usuario=user)

@main.route('/terminos')
def terminos():
    return render_template('terminos.html')

@main_bp.route('/descargar_excel')
@login_required
def descargar_excel():
    conn = get_db_connection()
    query = '''SELECT v.id as Folio, v.fecha, v.cliente, v.estado, d.concepto as Producto, d.cantidad, d.precio_unitario as Precio_Venta, d.costo_unitario as Costo_Prod, (d.precio_unitario - d.costo_unitario) as Ganancia_Unit, d.subtotal, v.total as Ticket_Total, v.monto_pagado, v.saldo_pendiente, v.costo_total as Ticket_Costo, (v.total - v.costo_total) as Ticket_Ganancia FROM ventas v JOIN venta_detalles d ON v.id = d.venta_id WHERE v.user_id = ? ORDER BY v.fecha DESC'''
    df = pd.read_sql_query(query, conn, params=(session['user_id'],))
    conn.close()
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False, sheet_name='Detalle Financiero')
    output.seek(0)
    return send_file(output, download_name="reporte_financiero_sian.xlsx", as_attachment=True)