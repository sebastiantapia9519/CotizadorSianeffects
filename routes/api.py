from flask import Blueprint, jsonify, session, request
from db import get_db_connection as get_db

api_bp = Blueprint('api', __name__)

# ==========================================
# 1. GESTIÓN DE INVENTARIO
# ==========================================

@api_bp.route('/material/<int:id>')
def obtener_material(id):
    if 'user_id' not in session:
        return jsonify({'error': 'No autorizado'}), 401

    conn = get_db()
    material = conn.execute(
        'SELECT * FROM materiales WHERE id = ? AND user_id = ?',
        (id, session['user_id'])
    ).fetchone()
    conn.close()

    if not material:
        return jsonify({'error': 'Material no encontrado'}), 404

    return jsonify({
        'id': material['id'],
        'nombre': material['nombre'],
        'precio': material['precio_unitario'],
        'tipo': material['tipo_entrada']
    })


@api_bp.route('/receta/<int:id>')
def obtener_receta(id):
    if 'user_id' not in session:
        return jsonify({'error': 'No autorizado'}), 401

    conn = get_db()
    producto = conn.execute(
        'SELECT * FROM productos WHERE id = ? AND user_id = ?',
        (id, session['user_id'])
    ).fetchone()

    if not producto:
        conn.close()
        return jsonify({'error': 'Receta no encontrada'}), 404

    detalles = conn.execute('''
        SELECT m.id, m.nombre, m.precio_unitario, pd.cantidad
        FROM producto_detalles pd
        JOIN materiales m ON pd.material_id = m.id
        WHERE pd.producto_id = ?
    ''', (id,)).fetchall()

    maquinaria = conn.execute('''
        SELECT mq.id, mq.nombre, mq.costo_desgaste
        FROM producto_maquinaria pm
        JOIN maquinaria mq ON pm.maquinaria_id = mq.id
        WHERE pm.producto_id = ?
    ''', (id,)).fetchall()

    conn.close()

    return jsonify({
        'id': producto['id'],
        'nombre': producto['nombre'],
        'materiales': [dict(d) for d in detalles],
        'maquinaria': [dict(m) for m in maquinaria]
    })


# ==========================================
# 2. GESTIÓN DE VENTAS
# ==========================================

@api_bp.route('/obtener_detalles/<int:id>')
def obtener_detalles_venta(id):
    if 'user_id' not in session:
        return jsonify({'error': 'No autorizado'}), 401

    conn = get_db()
    venta = conn.execute(
        'SELECT * FROM ventas WHERE id = ? AND user_id = ?',
        (id, session['user_id'])
    ).fetchone()

    if not venta:
        conn.close()
        return jsonify({'success': False, 'message': 'No encontrado'}), 404

    detalles = conn.execute(
        'SELECT * FROM venta_detalles WHERE venta_id = ?',
        (id,)
    ).fetchall()

    conn.close()

    items = [{
        'concepto': d['concepto'],
        'cantidad': d['cantidad'],
        'precio_unitario': d['precio_unitario'],
        'costo_unitario': d['costo_unitario'],
        'subtotal': d['subtotal'],
        'composicion': d['composicion']
    } for d in detalles]

    return jsonify({
        'success': True,
        'folio': venta['id'],
        'cliente': venta['cliente'],
        'estado': venta['estado'],
        'total': venta['total'],
        'costo_total': venta['costo_total'],
        'monto_pagado': venta['monto_pagado'],
        'saldo_pendiente': venta['saldo_pendiente'],
        'items': items
    })


@api_bp.route('/actualizar_venta', methods=['POST'])
def actualizar_venta():
    if 'user_id' not in session:
        return jsonify({'error': 'No autorizado'}), 401

    data = request.get_json()
    venta_id = data.get('id')
    abono = float(data.get('abono', 0))

    conn = get_db()
    cursor = conn.cursor()

    try:
        venta = cursor.execute(
            'SELECT total, monto_pagado FROM ventas WHERE id = ? AND user_id = ?',
            (venta_id, session['user_id'])
        ).fetchone()

        if not venta:
            return jsonify({'success': False}), 404

        nuevo_pagado = venta['monto_pagado'] + abono
        nuevo_saldo = venta['total'] - nuevo_pagado

        estado = 'pagado' if nuevo_saldo <= 0 else 'anticipo'

        cursor.execute('''
            UPDATE ventas
            SET monto_pagado = ?, saldo_pendiente = ?, estado = ?
            WHERE id = ?
        ''', (nuevo_pagado, max(nuevo_saldo, 0), estado, venta_id))

        conn.commit()
        return jsonify({'success': True})

    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@api_bp.route('/cancelar_venta', methods=['POST'])
def cancelar_venta():
    if 'user_id' not in session:
        return jsonify({'error': 'No autorizado'}), 401

    data = request.get_json()
    venta_id = data.get('id')

    conn = get_db()
    try:
        conn.execute(
            'UPDATE ventas SET estado = "cancelada" WHERE id = ? AND user_id = ?',
            (venta_id, session['user_id'])
        )
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()
