import json
from utils.datetime_utils import ahora_sql
from flask import Blueprint, jsonify, session, request
from db import get_db_connection as get_db
from helpers import login_required

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
@login_required # Usamos tu decorador en lugar de revisar la sesión a mano
def actualizar_venta():
    data = request.get_json()
    venta_id = data.get('id')
    
    # 1. BLINDAJE: Evitar que truenen el float()
    try:
        abono = float(data.get('abono', 0))
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'El abono debe ser un número válido'}), 400

    # 2. BLINDAJE ANTI-HACK: No pueden abonar ceros ni números negativos
    if abono <= 0:
        return jsonify({'success': False, 'error': 'El abono debe ser mayor a cero'}), 400

    conn = get_db()
    cursor = conn.cursor()

    try:
        venta = cursor.execute(
            'SELECT total, monto_pagado FROM ventas WHERE id = ? AND user_id = ?',
            (venta_id, session['user_id'])
        ).fetchone()

        if not venta:
            return jsonify({'success': False, 'error': 'Venta no encontrada'}), 404

        saldo_actual = venta['total'] - venta['monto_pagado']
        
        # 3. REGLA DE NEGOCIO: No pueden abonar más de lo que deben
        if abono > saldo_actual:
            abono = saldo_actual

        nuevo_pagado = venta['monto_pagado'] + abono
        nuevo_saldo = venta['total'] - nuevo_pagado

        # Matamos decimales residuales para evitar estados inconsistentes
        if nuevo_saldo < 0.05:
            nuevo_saldo = 0.0
            estado = 'pagado'
        else:
            estado = 'anticipo'

        cursor.execute('''
            UPDATE ventas
            SET monto_pagado = ?, saldo_pendiente = ?, estado = ?
            WHERE id = ?
        ''', (nuevo_pagado, nuevo_saldo, estado, venta_id))

        conn.commit()
        return jsonify({'success': True, 'nuevo_estado': estado, 'nuevo_saldo': nuevo_saldo})

    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@api_bp.route('/cancelar_venta', methods=['POST'])
@login_required
def cancelar_venta():
    data = request.get_json()
    venta_id = data.get('id')

    conn = get_db()
    cursor = conn.cursor()

    try:
        # 1. BLINDAJE: Verificar si la venta existe y su estado actual
        venta = cursor.execute(
            'SELECT estado FROM ventas WHERE id = ? AND user_id = ?',
            (venta_id, session['user_id'])
        ).fetchone()

        if not venta:
            return jsonify({'success': False, 'error': 'Venta no encontrada'}), 404
        
        # Si ya está cancelada, abortamos para no duplicar la devolución de inventario
        if venta['estado'] == 'cancelada':
            return jsonify({'success': False, 'error': 'La venta ya estaba cancelada'}), 400

        # 2. REGLA DE NEGOCIO: ¿Tiene el inventario activo?
        config = cursor.execute('SELECT inventario_activo FROM configuracion WHERE user_id=?', (session['user_id'],)).fetchone()
        usar_inventario = config['inventario_activo'] if config else 0

        # 3. DEVOLUCIÓN DE INVENTARIO (Solo si lo tiene activo)
        if usar_inventario:
            detalles = cursor.execute('SELECT cantidad, composicion FROM venta_detalles WHERE venta_id = ?', (venta_id,)).fetchall()
            
            materiales_a_devolver = {}
            
            # Agrupamos los materiales igual que cuando vendimos
            for detalle in detalles:
                try:
                    composicion = json.loads(detalle['composicion'] or '[]')
                    cantidad_producto = float(detalle['cantidad'])
                    
                    for comp in composicion:
                        if comp.get('tipo') == 'material':
                            mat_id = comp.get('id')
                            # Multiplicamos la cantidad del material por la cantidad de productos cancelados
                            cantidad_requerida = float(comp.get('cantidad', 0)) * cantidad_producto
                            
                            if cantidad_requerida > 0:
                                if mat_id in materiales_a_devolver:
                                    materiales_a_devolver[mat_id] += cantidad_requerida
                                else:
                                    materiales_a_devolver[mat_id] = cantidad_requerida
                except Exception as e:
                    current_app.logger.error(f"Error procesando devolución de receta: {e}")

            # Impactamos la base de datos: Devolvemos el stock de golpe
            for mat_id, total_devolucion in materiales_a_devolver.items():
                cursor.execute('''
                    UPDATE materiales 
                    SET stock_actual = stock_actual + ? 
                    WHERE id = ?
                ''', (total_devolucion, mat_id))
                
                # Dejamos la huella de auditoría
                cursor.execute('''
                    INSERT INTO movimientos_inventario 
                    (user_id, material_id, tipo, cantidad, motivo, stock_resultante, fecha)
                    VALUES (?, ?, 'entrada', ?, ?, 
                        (SELECT stock_actual FROM materiales WHERE id=?),
                        ?
                    )
                ''', (
                    session['user_id'],
                    mat_id,
                    total_devolucion,
                    f"Cancelación Venta #{venta_id} - Devolución de stock",
                    mat_id,
                    ahora_sql()
                ))

        # 4. Finalmente, cambiamos el estatus de la venta
        cursor.execute(
            'UPDATE ventas SET estado = "cancelada" WHERE id = ? AND user_id = ?',
            (venta_id, session['user_id'])
        )
        
        conn.commit()
        return jsonify({'success': True})

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"Error al cancelar venta {venta_id}: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()