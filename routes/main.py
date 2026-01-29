from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file, jsonify
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta
import pandas as pd
import io
from db import get_db_connection as get_db
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
    try:
        data = {
            'config': conn.execute('SELECT * FROM configuracion WHERE user_id=?', (uid,)).fetchone(),
            'materiales': conn.execute('SELECT * FROM materiales WHERE user_id=?', (uid,)).fetchall(),
            'productos': conn.execute('SELECT * FROM productos WHERE user_id=?', (uid,)).fetchall(),
            'equipos': conn.execute('SELECT * FROM maquinaria WHERE user_id=?', (uid,)).fetchall()
        }
    finally:
        conn.close()
    return render_template('cotizador.html', **data)

# --- GUARDAR VENTA (Lógica de Estados y Saldos) ---
@main_bp.route('/guardar_venta', methods=['POST'])
@login_required
def guardar_venta():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No hay datos'}), 400

    conn = get_db()
    cursor = conn.cursor()
    
    try:
        uid = session['user_id']
        cliente = data.get('cliente', 'Cliente General')
        fecha = datetime.now()
        
        # Datos económicos
        subtotal = float(data.get('subtotal', 0))
        descuento_porcentaje = int(data.get('descuento_porcentaje', 0))
        descuento_monto = float(data.get('descuento_monto', 0))
        total = float(data.get('total', 0))
        costo_total = float(data.get('costo_total', 0)) # Costo de producción
        
        # Lógica de Estados
        estado_solicitado = data.get('estado', 'pagado')
        pago_inicial = float(data.get('pago_inicial', 0))

        estado_db = 'pagado'
        monto_pagado = 0.0
        saldo_pendiente = 0.0
        fecha_vencimiento = None

        if estado_solicitado == 'cotizacion':
            estado_db = 'cotizacion'
            monto_pagado = 0.0
            saldo_pendiente = total
            # Vence en 48 horas
            fecha_vencimiento = (fecha + timedelta(hours=48)).strftime('%Y-%m-%d %H:%M:%S')

        elif estado_solicitado == 'anticipo':
            estado_db = 'anticipo'
            monto_pagado = pago_inicial
            saldo_pendiente = total - monto_pagado
            fecha_vencimiento = None # Ya entró dinero, no vence

        elif estado_solicitado == 'venta_completa':
            estado_db = 'pagado'
            monto_pagado = total
            saldo_pendiente = 0.0
            fecha_vencimiento = None

        # Generar resumen simple de items (Ej: "Termo, Taza...")
        items = data.get('items', [])
        lista_nombres = [i['concepto'] for i in items]
        resumen_items = ", ".join(lista_nombres)[:200]

        # 1. Insertar Venta
        cursor.execute('''
            INSERT INTO ventas (
                user_id, cliente, fecha, 
                subtotal, descuento_porcentaje, descuento_monto, total,
                estado, monto_pagado, saldo_pendiente, 
                fecha_vencimiento, resumen_items, costo_total
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            uid, cliente, fecha,
            subtotal, descuento_porcentaje, descuento_monto, total,
            estado_db, monto_pagado, saldo_pendiente,
            fecha_vencimiento, resumen_items, costo_total
        ))
        
        venta_id = cursor.lastrowid

        # 2. Insertar Detalles
        for item in items:
            cursor.execute('''
                INSERT INTO venta_detalles (venta_id, concepto, cantidad, precio_unitario, costo_unitario, subtotal)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                venta_id,
                item['concepto'],
                float(item['cantidad']),
                float(item['precio']),
                float(item.get('costo', 0)),
                float(item['subtotal'])
            ))

        conn.commit()
        return jsonify({'success': True, 'ticket_id': venta_id})
        
    except Exception as e:
        conn.rollback()
        print(f"Error guardando venta: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# --- NUEVA RUTA: ABONAR A CUENTA (Desde Historial) ---
@main_bp.route('/api/actualizar_venta', methods=['POST'])
@login_required
def actualizar_venta():
    data = request.get_json()
    venta_id = data.get('id')
    abono = float(data.get('abono', 0))
    
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        # Obtener venta
        venta = cursor.execute("SELECT total, monto_pagado, saldo_pendiente FROM ventas WHERE id = ? AND user_id = ?", (venta_id, session['user_id'])).fetchone()
        
        if not venta:
            return jsonify({'success': False, 'message': 'Venta no encontrada'}), 404
            
        total = venta['total']
        pagado_anterior = venta['monto_pagado']
        
        # Calcular nuevos valores
        nuevo_pagado = pagado_anterior + abono
        nuevo_saldo = total - nuevo_pagado
        
        # Determinar estado
        nuevo_estado = 'anticipo'
        if nuevo_saldo <= 0.5: # Margen de error por decimales
            nuevo_saldo = 0
            nuevo_pagado = total
            nuevo_estado = 'pagado'
        
        # Actualizar DB (Quitamos fecha vencimiento al recibir dinero)
        cursor.execute('''
            UPDATE ventas 
            SET monto_pagado = ?, saldo_pendiente = ?, estado = ?, fecha_vencimiento = NULL 
            WHERE id = ?
        ''', (nuevo_pagado, nuevo_saldo, nuevo_estado, venta_id))
        
        conn.commit()
        return jsonify({'success': True, 'nuevo_estado': nuevo_estado})
        
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        conn.close()

# --- RUTAS DE VISUALIZACIÓN ---

@main_bp.route('/historial')
@login_required
def historial():
    conn = get_db()
    uid = session['user_id']
    q = request.args.get('q')
    
    # Query ajustada para traer los campos nuevos
    sql = '''
        SELECT id, cliente, fecha, total, estado, saldo_pendiente, fecha_vencimiento 
        FROM ventas 
        WHERE user_id=? 
    '''
    params = [uid]
    
    if q:
        sql += " AND (id=? OR cliente LIKE ?)"
        params.extend([q, f'%{q}%'])
        
    sql += " ORDER BY id DESC"
    
    ventas = conn.execute(sql, params).fetchall()
    conn.close()
    return render_template('historial.html', ventas=ventas)

@main_bp.route('/api/obtener_detalles/<int:id>')
@login_required
def api_detalles(id):
    conn = get_db()
    # Verificar que la venta sea del usuario actual por seguridad
    venta = conn.execute('SELECT * FROM ventas WHERE id = ? AND user_id = ?', (id, session['user_id'])).fetchone()
    
    if not venta:
        conn.close()
        return jsonify({'success': False, 'message': 'No encontrado'}), 404
        
    detalles = conn.execute('SELECT * FROM venta_detalles WHERE venta_id = ?', (id,)).fetchall()
    conn.close()
    
    # Convertir a dict para JSON
    items = [{'concepto': d['concepto'], 'cantidad': d['cantidad'], 'precio_unitario': d['precio_unitario'], 'costo_unitario': d['costo_unitario'], 'subtotal': d['subtotal']} for d in detalles]
    
    return jsonify({
        'success': True,
        'folio': venta['id'],
        'cliente': venta['cliente'],
        'estado': venta['estado'],
        'total': venta['total'],
        'costo_total': venta['costo_total'],
        'items': items
    })

@main_bp.route('/ticket/<int:id>')
def ver_ticket(id):
    conn = get_db()
    # Permitimos ver ticket sin login (para compartir link), pero buscamos la config del dueño del ticket
    venta = conn.execute('SELECT * FROM ventas WHERE id = ?', (id,)).fetchone()
    
    if venta is None:
        conn.close()
        return "Ticket no encontrado", 404

    detalles = conn.execute('SELECT * FROM venta_detalles WHERE venta_id = ?', (id,)).fetchall()
    config = conn.execute('SELECT * FROM configuracion WHERE user_id = ?', (venta['user_id'],)).fetchone()

    if config is None:
        config = {'nombre_empresa': 'Mi Negocio', 'slogan': 'Gracias por su compra', 'website': ''}

    conn.close()
    return render_template('ticket.html', venta=venta, detalles=detalles, config=config)

# --- CONFIGURACIÓN Y EXPORTACIÓN ---

@main_bp.route('/configuracion', methods=('GET', 'POST'))
@login_required
def configuracion():
    conn = get_db()
    uid = session['user_id']
    if request.method == 'POST':
        if 'new_username' in request.form:
            try:
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
    uid = session['user_id']
    # Query actualizada para incluir los campos financieros nuevos
    query = '''
        SELECT 
            v.id as Folio, v.fecha, v.cliente, v.estado, 
            v.monto_pagado, v.saldo_pendiente,
            d.concepto as Producto, d.cantidad, 
            d.precio_unitario as Precio_Venta, d.costo_unitario as Costo_Prod, 
            (d.precio_unitario - d.costo_unitario) as Ganancia_Unit, 
            d.subtotal, v.total as Ticket_Total
        FROM ventas v 
        JOIN venta_detalles d ON v.id = d.venta_id 
        WHERE v.user_id = ? 
        ORDER BY v.fecha DESC
    '''
    df = pd.read_sql_query(query, conn, params=(uid,))
    conn.close()
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer: 
        df.to_excel(writer, index=False, sheet_name='Detalle Financiero')
    output.seek(0)
    
    return send_file(output, download_name="reporte_financiero_sian.xlsx", as_attachment=True)