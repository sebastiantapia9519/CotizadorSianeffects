from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file, jsonify
from werkzeug.security import generate_password_hash
from datetime import datetime
import pandas as pd
import io
from db import get_db_connection as get_db # Alias para facilitar
from helpers import login_required

main_bp = Blueprint('main', __name__)

@main_bp.route('/')
def index():
    return redirect(url_for('main.cotizador'))

@main_bp.route('/cotizador')
@login_required
def cotizador():
    conn = get_db()
    uid = session['user_id']
    data = {
        'config': conn.execute('SELECT * FROM configuracion WHERE user_id=?', (uid,)).fetchone(),
        'materiales': conn.execute('SELECT * FROM materiales WHERE user_id=?', (uid,)).fetchall(),
        'productos': conn.execute('SELECT * FROM productos WHERE user_id=?', (uid,)).fetchall(),
        'equipos': conn.execute('SELECT * FROM maquinaria WHERE user_id=?', (uid,)).fetchall()
    }
    conn.close()
    return render_template('cotizador.html', **data)

# --- ESTA ES LA RUTA QUE FALTABA PARA GUARDAR EL TICKET ---
@main_bp.route('/guardar_venta', methods=['POST'])
@login_required
def guardar_venta():
    data = request.get_json() # Recibimos los datos de JavaScript
    
    if not data:
        return jsonify({'error': 'No hay datos'}), 400

    conn = get_db()
    try:
        # 1. Crear la Venta General
        cursor = conn.execute('''
            INSERT INTO ventas (user_id, cliente, fecha, subtotal, descuento_porcentaje, 
                              descuento_monto, total, estado, saldo_pendiente, costo_total)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            session['user_id'],
            data['cliente'],
            datetime.now(),
            data['subtotal'],
            data['descuento_porcentaje'],
            data['descuento_monto'],
            data['total'],
            'pagado', # Asumimos pagado por defecto, o puedes recibirlo del JS
            0,
            data.get('costo_total', 0)
        ))
        
        venta_id = cursor.lastrowid # Obtenemos el ID del ticket nuevo

        # 2. Guardar cada producto del ticket
        for item in data['items']:
            conn.execute('''
                INSERT INTO venta_detalles (venta_id, concepto, cantidad, precio_unitario, costo_unitario, subtotal)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                venta_id,
                item['concepto'],
                item['cantidad'],
                item['precio'],
                item.get('costo', 0),
                item['subtotal']
            ))

        conn.commit()
        return jsonify({'success': True, 'ticket_id': venta_id})
        
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main_bp.route('/historial')
@login_required
def historial():
    conn = get_db()
    uid = session['user_id']
    q = request.args.get('q')
    if q: 
        ventas = conn.execute('SELECT * FROM ventas WHERE user_id=? AND (id=? OR cliente LIKE ?) ORDER BY id DESC', (uid, q, f'%{q}%')).fetchall()
    else: 
        ventas = conn.execute('SELECT * FROM ventas WHERE user_id=? ORDER BY id DESC', (uid,)).fetchall()
    conn.close()
    return render_template('historial.html', ventas=ventas)


# --- RUTA DEL TICKET CORREGIDA ---
# NOTA: Sin @login_required para compartir por WhatsApp
@main_bp.route('/ticket/<int:id>')
def ver_ticket(id):
    conn = get_db()
    
    # 1. Buscamos la venta
    venta = conn.execute('SELECT * FROM ventas WHERE id = ?', (id,)).fetchone()
    
    if venta is None:
        conn.close()
        return "Ticket no encontrado o enlace inválido", 404

    # 2. Detalles
    detalles = conn.execute('SELECT * FROM venta_detalles WHERE venta_id = ?', (id,)).fetchall()

    # 3. Configuración (CORREGIDO: user_id en lugar de usuario_id)
    # Usamos venta['user_id'] para cargar el logo de quien hizo la venta
    config = conn.execute('SELECT * FROM configuracion WHERE user_id = ?', (venta['user_id'],)).fetchone()

    if config is None:
        config = {'nombre_empresa': 'Mi Negocio', 'slogan': 'Gracias por su compra', 'website': ''}

    conn.close()
    
    return render_template('ticket.html', venta=venta, detalles=detalles, config=config)


@main_bp.route('/configuracion', methods=('GET', 'POST'))
@login_required
def configuracion():
    conn = get_db()
    uid = session['user_id']
    if request.method == 'POST':
        if 'new_username' in request.form:
            try:
                # Corregido: Tabla usuarios (no users)
                conn.execute('UPDATE usuarios SET username=?, password=? WHERE id=?', 
                             (request.form['new_username'], generate_password_hash(request.form['new_password']), uid))
                session['username'] = request.form['new_username']
                flash('Credenciales actualizadas.', 'success')
            except: 
                flash('Usuario ocupado.', 'danger')
        else:
            conn.execute('UPDATE configuracion SET margen_ganancia=?, nombre_empresa=?, slogan=?, website=? WHERE user_id=?', 
                         (request.form['margen'], request.form['nombre_empresa'], request.form['slogan'], request.form['website'], uid))
            flash('Datos guardados.', 'success')
        conn.commit(); conn.close(); return redirect(url_for('main.configuracion'))
    
    config = conn.execute('SELECT * FROM configuracion WHERE user_id=?', (uid,)).fetchone()
    user = conn.execute('SELECT * FROM usuarios WHERE id=?', (uid,)).fetchone()
    conn.close()
    return render_template('configuracion.html', config=config, usuario=user)

@main_bp.route('/terminos')
def terminos():
    return render_template('terminos.html')

@main_bp.route('/descargar_excel')
@login_required
def descargar_excel():
    conn = get_db()
    query = '''SELECT v.id as Folio, v.fecha, v.cliente, v.estado, d.concepto as Producto, d.cantidad, d.precio_unitario as Precio_Venta, d.costo_unitario as Costo_Prod, (d.precio_unitario - d.costo_unitario) as Ganancia_Unit, d.subtotal, v.total as Ticket_Total, v.monto_pagado, v.saldo_pendiente, v.costo_total as Ticket_Costo, (v.total - v.costo_total) as Ticket_Ganancia FROM ventas v JOIN venta_detalles d ON v.id = d.venta_id WHERE v.user_id = ? ORDER BY v.fecha DESC'''
    df = pd.read_sql_query(query, conn, params=(session['user_id'],))
    conn.close()
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False, sheet_name='Detalle Financiero')
    output.seek(0)
    return send_file(output, download_name="reporte_financiero_sian.xlsx", as_attachment=True)