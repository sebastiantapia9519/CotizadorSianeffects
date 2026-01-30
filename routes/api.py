from flask import Blueprint, jsonify, session, request
from db import get_db_connection as get_db

api_bp = Blueprint('api', __name__)

# ==========================================
# 1. GESTIÓN DE INVENTARIO (COTIZADOR)
# ==========================================

# A. OBTENER UN SOLO MATERIAL (Para el dropdown del cotizador)
@api_bp.route('/material/<int:id>')
def obtener_material(id):
    if 'user_id' not in session:
        return jsonify({'error': 'No autorizado'}), 401

    conn = get_db()
    material = conn.execute('SELECT * FROM materiales WHERE id = ? AND user_id = ?', 
                            (id, session['user_id'])).fetchone()
    conn.close()
    
    if material:
        return jsonify({
            'id': material['id'],
            'nombre': material['nombre'],
            'precio': material['precio_unitario'],
            'tipo': material['tipo_entrada']
        })
    return jsonify({'error': 'Material no encontrado'}), 404

# B. OBTENER RECETA COMPLETA (Para cargar recetas guardadas)
@api_bp.route('/receta/<int:id>')
def obtener_receta(id):
    if 'user_id' not in session:
        return jsonify({'error': 'No autorizado'}), 401

    conn = get_db()
    
    # 1. Datos básicos del producto
    producto = conn.execute('SELECT * FROM productos WHERE id = ? AND user_id = ?', 
                           (id, session['user_id'])).fetchone()
    
    if not producto:
        conn.close()
        return jsonify({'error': 'Receta no encontrada'}), 404

    # 2. Sus materiales
    detalles = conn.execute('''
        SELECT m.id, m.nombre, m.precio_unitario, pd.cantidad 
        FROM producto_detalles pd
        JOIN materiales m ON pd.material_id = m.id
        WHERE pd.producto_id = ?
    ''', (id,)).fetchall()

    # 3. Su maquinaria
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
# 2. GESTIÓN DE VENTAS (HISTORIAL Y COBROS)
# ==========================================

# C. OBTENER DETALLES DE UNA VENTA (Para el Modal del Ojo)
@api_bp.route('/obtener_detalles/<int:id>')
def obtener_detalles_venta(id):
    if 'user_id' not in session:
        return jsonify({'error': 'No autorizado'}), 401

    conn = get_db()
    # Verificar que la venta pertenezca al usuario
    venta = conn.execute('SELECT * FROM ventas WHERE id = ? AND user_id = ?', 
                         (id, session['user_id'])).fetchone()
    
    if not venta:
        conn.close()
        return jsonify({'success': False, 'message': 'No encontrado'}), 404
        
    detalles = conn.execute('SELECT * FROM venta_detalles WHERE venta_id = ?', (id,)).fetchall()
    conn.close()
    
    # Formatear items para JSON
    items = [{
        'concepto': d['concepto'], 
        'cantidad': d['cantidad'], 
        'precio_unitario': d['precio_unitario'], 
        'costo_unitario': d['costo_unitario'], 
        'subtotal': d['subtotal']
    } for d in detalles]
    
    return jsonify({
        'success': 'PRUEBA_SEBASTIAN_123',  # <--- Cambia esto
        'folio': venta['id'],
        'cliente': venta['cliente'],
        'estado': venta['estado'],
        'total': venta['total'],
        'costo_total': venta['costo_total'],
        'monto_pagado': venta['monto_pagado'],
        'saldo_pendiente': venta['saldo_pendiente'],
        'items': items
    })

# D. ACTUALIZAR VENTA (Registrar Abono)
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
        # Obtener datos actuales
        venta = cursor.execute('SELECT total, monto_pagado FROM ventas WHERE id = ? AND user_id = ?', 
                               (venta_id, session['user_id'])).fetchone()
        
        if not venta:
            return jsonify({'success': False, 'message': 'Venta no encontrada'}), 404
            
        total = venta['total']
        pagado_anterior = venta['monto_pagado']
        
        # Calcular nuevos montos
        nuevo_pagado = pagado_anterior + abono
        nuevo_saldo = total - nuevo_pagado
        
        # Lógica de nuevo estado
        nuevo_estado = 'anticipo'
        if nuevo_saldo <= 0.5: # Usamos 0.5 para evitar errores de redondeo
            nuevo_saldo = 0
            nuevo_pagado = total
            nuevo_estado = 'pagado'
        
        # Actualizar DB: Importante limpiar fecha_vencimiento
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

# E. CANCELAR VENTA (Botón de Basura)
@api_bp.route('/cancelar_venta', methods=['POST'])
def cancelar_venta():
    if 'user_id' not in session:
        return jsonify({'error': 'No autorizado'}), 401

    data = request.get_json()
    venta_id = data.get('id')

    conn = get_db()
    try:
        # Solo marcamos como cancelada, no borramos el registro para auditoría
        conn.execute('''
            UPDATE ventas 
            SET estado = 'cancelada', saldo_pendiente = 0, fecha_vencimiento = NULL 
            WHERE id = ? AND user_id = ?
        ''', (venta_id, session['user_id']))
        
        conn.commit()
        return jsonify({'success': True, 'message': 'Venta cancelada'})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        conn.close()