import json
from datetime import datetime
from dateutil.relativedelta import relativedelta
from utils.datetime_utils import ahora_sql, now_utc, utc_to_local
from flask import Blueprint, jsonify, session, request, current_app
from db import get_db_connection as get_db
from helpers import login_required, subscription_required

api_bp = Blueprint('api', __name__)

# ==========================================
# 1. GESTIÓN DE INVENTARIO
# ==========================================

@api_bp.route('/material/<int:id>')
def obtener_material(id):
    if 'user_id' not in session:
        return jsonify({'error': 'No autorizado'}), 401

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT * FROM materiales WHERE id = %s AND user_id = %s',
        (id, session['user_id'])
    )
    material = cursor.fetchone()
    cursor.close()
    conn.close()

    if not material:
        return jsonify({'error': 'Material no encontrado'}), 404

    # LOG DE CONSULTA
    u_name = session.get('username', 'Anonimo')
    current_app.logger.info(f"DATA_ACCESS: Usuario '{u_name}' consulto informacion del material #{id} ({material['nombre']})")

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
    cursor = conn.cursor()
    cursor.execute(
        'SELECT * FROM productos WHERE id = %s AND user_id = %s',
        (id, session['user_id'])
    )
    producto = cursor.fetchone()

    if not producto:
        cursor.close()
        conn.close()
        return jsonify({'error': 'Receta no encontrada'}), 404

    cursor.execute('''
        SELECT m.id, m.nombre, m.precio_unitario, pd.cantidad
        FROM producto_detalles pd
        JOIN materiales m ON pd.material_id = m.id
        WHERE pd.producto_id = %s
        ORDER BY LOWER(m.nombre) ASC
    ''', (id,))
    detalles = cursor.fetchall()

    cursor.execute('''
        SELECT mq.id, mq.nombre, mq.costo_desgaste
        FROM producto_maquinaria pm
        JOIN maquinaria mq ON pm.maquinaria_id = mq.id
        WHERE pm.producto_id = %s
        ORDER BY LOWER(mq.nombre) ASC
    ''', (id,))
    maquinaria = cursor.fetchall()

    cursor.close()
    conn.close()

    # LOG DE CONSULTA DE RECETA
    u_name = session.get('username', 'Anonimo')
    current_app.logger.info(f"DATA_ACCESS: Usuario '{u_name}' consulto receta del producto #{id} ({producto['nombre']})")

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
    cursor = conn.cursor()
    cursor.execute(
        'SELECT * FROM ventas WHERE id = %s AND user_id = %s',
        (id, session['user_id'])
    )
    venta = cursor.fetchone()

    if not venta:
        cursor.close()
        conn.close()
        return jsonify({'success': False, 'message': 'No encontrado'}), 404

    cursor.execute(
        'SELECT * FROM venta_detalles WHERE venta_id = %s',
        (id,)
    )
    detalles = cursor.fetchall()

    cursor.close()
    conn.close()

    # LOG DE CONSULTA DE VENTA
    u_name = session.get('username', 'Anonimo')
    current_app.logger.info(f"DATA_ACCESS: Usuario '{u_name}' consulto detalles de Venta #{id} (Cliente: {venta['cliente']})")

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
        'envio': venta['envio'] if 'envio' in venta.keys() else 0,
        'items': items
    })


@api_bp.route('/actualizar_venta', methods=['POST'])
@subscription_required
def actualizar_venta():
    data = request.get_json()
    venta_id = data.get('id')
    
    try:
        abono = float(data.get('abono', 0))
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'El abono debe ser un número válido'}), 400

    if abono <= 0:
        return jsonify({'success': False, 'error': 'El abono debe ser mayor a cero'}), 400

    conn = get_db()
    cursor = conn.cursor()

    try:
        cursor.execute(
            'SELECT total, monto_pagado FROM ventas WHERE id = %s AND user_id = %s',
            (venta_id, session['user_id'])
        )
        venta = cursor.fetchone()

        if not venta:
            return jsonify({'success': False, 'error': 'Venta no encontrada'}), 404

        saldo_actual = venta['total'] - venta['monto_pagado']
        
        if abono > saldo_actual:
            abono = saldo_actual

        nuevo_pagado = venta['monto_pagado'] + abono
        nuevo_saldo = venta['total'] - nuevo_pagado

        if nuevo_saldo < 0.05:
            nuevo_saldo = 0.0
            estado = 'pagado'
        else:
            estado = 'anticipo'

        cursor.execute('''
            UPDATE ventas
            SET monto_pagado = %s, saldo_pendiente = %s, estado = %s
            WHERE id = %s
        ''', (nuevo_pagado, nuevo_saldo, estado, venta_id))

        # --- NUEVO: REGISTRAR EN LA BITÁCORA DEL ADMIN ---
        cursor.execute("""
            INSERT INTO logs_actividad (user_id, accion, modulo) 
            VALUES (%s, %s, %s)
        """, (session['user_id'], f"Registró abono de ${abono:,.2f} a Venta #{venta_id}", "Ventas"))

        conn.commit()
        
        # --- LOG DE DINERO MEJORADO ---
        u_name = session.get('username', 'Anonimo')
        u_id = session.get('user_id', 'N/A')
        current_app.logger.info(
            f"SALE_PAYMENT: Usuario '{u_name}' (ID: {u_id}) registro abono de ${abono} "
            f"a la Venta #{venta_id}. Estado resultante: {estado.upper()}"
        )
        
        return jsonify({'success': True, 'nuevo_estado': estado, 'nuevo_saldo': nuevo_saldo})

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"SYSTEM_ERROR en actualizar_venta: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@api_bp.route('/actualizar_expiracion_cotizacion', methods=['POST'])
@subscription_required
def actualizar_expiracion_cotizacion():
    data = request.get_json()
    cotizacion_id = data.get('id')
    nueva_fecha_str = data.get('nueva_fecha') 
    
    if not cotizacion_id or not nueva_fecha_str:
        return jsonify({'success': False, 'error': 'Faltan datos (ID o nueva fecha)'}), 400

    try:
        import pytz
        from datetime import datetime
        from dateutil.relativedelta import relativedelta
        from utils.datetime_utils import now_utc, utc_to_local
        
        # 1. Parsear fecha solo para la validación lógica inicial
        nueva_fecha_date = datetime.strptime(nueva_fecha_str, '%Y-%m-%d').date()
        ahora_local = utc_to_local(now_utc())
        hoy_local_date = ahora_local.date()
        limite_mes_date = (ahora_local + relativedelta(months=1)).date()

        if nueva_fecha_date <= hoy_local_date:
            return jsonify({'success': False, 'error': 'La fecha de expiración debe ser en el futuro'}), 400
        
        if nueva_fecha_date > limite_mes_date:
            return jsonify({'success': False, 'error': 'No puedes extender la cotización más de 1 mes'}), 400

        # ==========================================
        # 2. FIX DEL DESFASE DE ZONA HORARIA
        # ==========================================
        # Creamos un datetime y le asignamos el final del día (23:59:59)
        nueva_fecha_dt = datetime.strptime(nueva_fecha_str, '%Y-%m-%d')
        nueva_fecha_dt = nueva_fecha_dt.replace(hour=23, minute=59, second=59)
        
        # Le asignamos la zona horaria local para que la matemática sea exacta
        tz_local = pytz.timezone('America/Mexico_City')
        nueva_fecha_local = tz_local.localize(nueva_fecha_dt)
        
        # Convertimos a UTC (Que es como tu sistema lee/escribe nativamente en Postgres)
        nueva_fecha_utc = nueva_fecha_local.astimezone(pytz.utc)
        # ==========================================

        conn = get_db()
        cursor = conn.cursor()

        # 3. Verificar que exista y pertenezca al usuario
        cursor.execute(
            "SELECT id FROM ventas WHERE id = %s AND user_id = %s AND estado = 'cotizacion'",
            (cotizacion_id, session['user_id'])
        )
        cotizacion = cursor.fetchone()

        if not cotizacion:
            return jsonify({'success': False, 'error': 'Cotización no encontrada o sin permisos'}), 404

        # 4. Actualizar enviando el objeto UTC exacto
        cursor.execute('''
            UPDATE ventas
            SET fecha_vencimiento = %s
            WHERE id = %s
        ''', (nueva_fecha_utc, cotizacion_id))

        cursor.execute("""
            INSERT INTO logs_actividad (user_id, accion, modulo) 
            VALUES (%s, %s, %s)
        """, (session['user_id'], f"Extendió vigencia de Cotización #{cotizacion_id} a {nueva_fecha_str}", "Ventas"))

        conn.commit()
        
        u_name = session.get('username', 'Anonimo')
        current_app.logger.info(f"QUOTE_UPDATED: Usuario '{u_name}' extendió la cotización #{cotizacion_id} a {nueva_fecha_str}")

        return jsonify({'success': True, 'nueva_fecha': nueva_fecha_str})

    except ValueError:
        return jsonify({'success': False, 'error': 'El formato de la fecha es inválido. Usa YYYY-MM-DD'}), 400
    except Exception as e:
        if 'conn' in locals() and conn:
            conn.rollback()
        current_app.logger.error(f"SYSTEM_ERROR en actualizar_expiracion_cotizacion: {str(e)}")
        return jsonify({'success': False, 'error': 'Ocurrió un error en el servidor'}), 500
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()

@api_bp.route('/cancelar_venta', methods=['POST'])
@subscription_required
def cancelar_venta():
    data = request.get_json()
    venta_id = data.get('id')

    conn = get_db()
    cursor = conn.cursor()

    try:
        cursor.execute(
            'SELECT estado FROM ventas WHERE id = %s AND user_id = %s',
            (venta_id, session['user_id'])
        )
        venta = cursor.fetchone()

        if not venta:
            return jsonify({'success': False, 'error': 'Venta no encontrada'}), 404
        
        if venta['estado'] == 'cancelada':
            return jsonify({'success': False, 'error': 'La venta ya estaba cancelada'}), 400

        cursor.execute('SELECT inventario_activo FROM configuracion WHERE user_id=%s', (session['user_id'],))
        config = cursor.fetchone()
        usar_inventario = config['inventario_activo'] if config else False

        if usar_inventario:
            cursor.execute('SELECT cantidad, composicion FROM venta_detalles WHERE venta_id = %s', (venta_id,))
            detalles = cursor.fetchall()
            
            materiales_a_devolver = {}
            
            for detalle in detalles:
                try:
                    composicion = json.loads(detalle['composicion'] or '[]')
                    cantidad_producto = float(detalle['cantidad'])
                    
                    for comp in composicion:
                        if comp.get('tipo') == 'material':
                            mat_id = comp.get('id')
                            cantidad_requerida = float(comp.get('cantidad', 0)) * cantidad_producto
                            
                            if cantidad_requerida > 0:
                                if mat_id in materiales_a_devolver:
                                    materiales_a_devolver[mat_id] += cantidad_requerida
                                else:
                                    materiales_a_devolver[mat_id] = cantidad_requerida
                except Exception as e:
                    current_app.logger.error(f"Error procesando devolucion de receta en Venta #{venta_id}: {e}")

            for mat_id, total_devolucion in materiales_a_devolver.items():
                cursor.execute('''
                    UPDATE materiales 
                    SET stock_actual = stock_actual + %s 
                    WHERE id = %s
                ''', (total_devolucion, mat_id))
                
                cursor.execute('''
                    INSERT INTO movimientos_inventario 
                    (user_id, material_id, tipo, cantidad, motivo, stock_resultante, fecha)
                    VALUES (%s, %s, 'entrada', %s, %s, 
                        (SELECT stock_actual FROM materiales WHERE id=%s),
                        %s
                    )
                ''', (
                    session['user_id'], mat_id, total_devolucion,
                    f"Cancelacion Venta #{venta_id} - Devolucion de stock",
                    mat_id, ahora_sql()
                ))

        cursor.execute(
            "UPDATE ventas SET estado = 'cancelada' WHERE id = %s AND user_id = %s",
            (venta_id, session['user_id'])
        )

        # --- REGISTRAR EN LA BITÁCORA DEL ADMIN ---
        cursor.execute("""
            INSERT INTO logs_actividad (user_id, accion, modulo) 
            VALUES (%s, %s, %s)
        """, (session['user_id'], f"Canceló la Venta #{venta_id}", "Ventas"))
        
        conn.commit()
        
        # --- LOG DE CANCELACIÓN MEJORADO ---
        u_name = session.get('username', 'Anonimo')
        u_id = session.get('user_id', 'N/A')
        inv_status = "con devolucion de stock" if usar_inventario else "sin afectar stock"
        current_app.logger.info(
            f"SALE_CANCELLED: Usuario '{u_name}' (ID: {u_id}) cancelo la Venta #{venta_id} ({inv_status})."
        )
        
        return jsonify({'success': True})

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"SYSTEM_ERROR al cancelar venta {venta_id}: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()