from flask import Blueprint, request, jsonify, session
from datetime import datetime, timedelta
from db import get_db_connection
from helpers import login_required

api_bp = Blueprint('api', __name__)

@api_bp.route('/api/checkout', methods=['POST'])
@login_required
def checkout():
    data = request.get_json(); items = data.get('carrito')
    subtotal = float(data.get('subtotal', 0)); desc_pct = int(data.get('descuento_porcentaje', 0))
    total = subtotal - (subtotal * (desc_pct / 100))
    
    costo_total = 0; resumen = []
    for i in items:
        c_unit = float(i.get('costo_unitario', 0)); cant = float(i.get('cantidad', 0))
        costo_total += (c_unit * cant)
        cant_str = int(cant) if cant.is_integer() else cant
        resumen.append(f"{cant_str}x {i['concepto']} (${float(i['precio_unitario']):.2f} c/u)")
    
    estado = data.get('estado', 'pagado')
    pago = float(data.get('pago_inicial', 0)) if estado != 'pagado' else total
    saldo = total - pago if total > pago else 0
    vence = (datetime.now() + timedelta(hours=48)) if estado == 'cotizacion' else None
    doc = 'quote' if estado == 'cotizacion' else 'receipt'
    
    conn = get_db_connection()
    try:
        cur = conn.execute('''INSERT INTO ventas (user_id, cliente, fecha, subtotal, descuento_porcentaje, descuento_monto, total, estado, monto_pagado, saldo_pendiente, document_type, tax_engine, fecha_vencimiento, resumen_items, costo_total) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', 
                           (session['user_id'], data.get('cliente','Gral'), datetime.now(), subtotal, desc_pct, subtotal*(desc_pct/100), total, estado, pago, saldo, doc, 'none', vence, ", ".join(resumen), costo_total))
        vid = cur.lastrowid
        for i in items: conn.execute('INSERT INTO venta_detalles (venta_id, concepto, cantidad, precio_unitario, costo_unitario, subtotal) VALUES (?,?,?,?,?,?)', (vid, i['concepto'], i['cantidad'], i['precio_unitario'], i.get('costo_unitario',0), i['subtotal']))
        conn.commit()
        return jsonify({'success': True, 'ticket_id': vid})
    except Exception as e: return jsonify({'success': False, 'message': str(e)})
    finally: conn.close()

@api_bp.route('/api/actualizar_venta', methods=['POST'])
@login_required
def actualizar_venta():
    data = request.get_json(); conn = get_db_connection()
    venta = conn.execute('SELECT * FROM ventas WHERE id=? AND user_id=?', (data['id'], session['user_id'])).fetchone()
    if not venta: return jsonify({'success': False})
    
    abono = float(data['abono']); pagado = venta['monto_pagado'] + abono; saldo = venta['total'] - pagado
    estado = venta['estado']
    if saldo <= 0.5: saldo = 0; estado = 'pagado'
    elif estado == 'cotizacion' and abono > 0: estado = 'anticipo'
    
    conn.execute('UPDATE ventas SET monto_pagado=?, saldo_pendiente=?, estado=?, fecha_vencimiento=NULL WHERE id=?', (pagado, saldo, estado, data['id']))
    conn.commit(); conn.close(); return jsonify({'success': True})

@api_bp.route('/api/cancelar_venta', methods=['POST'])
@login_required
def cancelar_venta():
    conn = get_db_connection(); data = request.get_json()
    conn.execute("UPDATE ventas SET estado='cancelada', saldo_pendiente=0 WHERE id=? AND user_id=?", (data['id'], session['user_id']))
    conn.commit(); conn.close(); return jsonify({'success': True})

@api_bp.route('/api/obtener_detalles/<int:venta_id>')
@login_required
def obtener_detalles(venta_id):
    conn = get_db_connection()
    v = conn.execute('SELECT * FROM ventas WHERE id=? AND user_id=?', (venta_id, session['user_id'])).fetchone()
    if not v: conn.close(); return jsonify({'success': False})
    d = conn.execute('SELECT * FROM venta_detalles WHERE venta_id=?', (venta_id,)).fetchall()
    conn.close()
    items = [{'concepto': i['concepto'], 'cantidad': i['cantidad'], 'precio_unitario': i['precio_unitario'], 'costo_unitario': i['costo_unitario'], 'subtotal': i['subtotal']} for i in d]
    return jsonify({'success': True, 'folio': v['id'], 'cliente': v['cliente'], 'items': items, 'total': v['total'], 'costo_total': v['costo_total'], 'estado': v['estado']})

@api_bp.route('/api/guardar_producto', methods=['POST'])
@login_required
def guardar_producto():
    d = request.get_json(); conn = get_db_connection()
    try:
        pid = conn.execute('INSERT INTO productos (user_id, nombre) VALUES (?,?)', (session['user_id'], d['nombre'])).lastrowid
        for i in d['items']: conn.execute('INSERT INTO producto_detalles (producto_id, material_id, cantidad) VALUES (?,?,?)', (pid, i['id'], i['cantidad']))
        for m in d['maquinaria']: conn.execute('INSERT INTO producto_maquinaria (producto_id, maquinaria_id) VALUES (?,?)', (pid, m))
        conn.commit(); return jsonify({'success': True})
    except Exception as e: return jsonify({'success': False, 'message': str(e)})
    finally: conn.close()

@api_bp.route('/api/cargar_producto/<int:id>')
@login_required
def cargar_producto(id):
    conn = get_db_connection()
    if not conn.execute('SELECT id FROM productos WHERE id=? AND user_id=?', (id, session['user_id'])).fetchone(): return jsonify({'error': 'No auth'}), 403
    mats = conn.execute('SELECT m.id, m.nombre, m.precio_unitario, pd.cantidad FROM producto_detalles pd JOIN materiales m ON pd.material_id = m.id WHERE pd.producto_id=?', (id,)).fetchall()
    maqs = conn.execute('SELECT maquinaria_id FROM producto_maquinaria WHERE producto_id=?', (id,)).fetchall()
    conn.close(); return jsonify({'materiales': [{'id': r['id'], 'nombre': r['nombre'], 'precio': r['precio_unitario'], 'cantidad': r['cantidad']} for r in mats], 'maquinaria': [r['maquinaria_id'] for r in maqs]})