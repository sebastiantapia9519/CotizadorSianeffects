from google.genai._interactions.types import interaction_create_params
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file, jsonify, send_from_directory, abort, current_app
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta, timezone
import pandas as pd
import io
import json
import math
import os
from utils.datetime_utils import now_utc, utc_to_local, ahora_sql
from db import get_db_connection as get_db
from dateutil import parser
from helpers import login_required, subscription_required, obtener_alertas
from utils.tutorial_utils import debe_mostrar_tutorial, obtener_version_tutorial

main_bp = Blueprint('main', __name__)

# Definición de estados válidos para la logística de pedidos
ESTADOS_PERMITIDOS = {'pendiente', 'en_proceso', 'listo', 'entregado', 'cancelado'}

# --- RUTAS DE ACCESO PRINCIPAL (EL PORTERO) ---

@main_bp.route('/')
def index():
    if 'user_id' in session:        
        return redirect(url_for('main.cotizador'))
    return render_template('landing_promos.html')

@main_bp.route('/promos/cotizador')
def landing_cotizador():
    return render_template('landing_promos.html')

# ========================================================
# SISTEMA DE ALERTAS (PARA EL MENÚ Y NOTIFICACIONES)
# ========================================================
@main_bp.app_context_processor
def inject_notifications():
    if 'user_id' in session:
        return {'notificaciones': obtener_alertas(session['user_id'])}
    return {'notificaciones': []}

@main_bp.route('/marcar-leida/<int:notif_id>')
@login_required
def marcar_leida(notif_id):
    conn = get_db()  
    cursor = conn.cursor()
    cursor.execute("UPDATE notificaciones_manuales SET leida = TRUE WHERE id = %s", (notif_id,))
    conn.commit()
    cursor.close()
    conn.close()
    return redirect(request.referrer)

@main_bp.route('/marcar-visto-global/<int:anuncio_id>')
@login_required
def marcar_visto_global(anuncio_id):
    conn = get_db()
    cursor = conn.cursor()
    uid = session['user_id']
    try:
        # Usamos ON CONFLICT por si el usuario le pica dos veces rápido
        cursor.execute("""
            INSERT INTO anuncios_vistos (user_id, anuncio_id) 
            VALUES (%s, %s) 
            ON CONFLICT DO NOTHING
        """, (uid, anuncio_id))
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    return redirect(request.referrer)


def procesar_fila_fechas(fila_db):
    if not fila_db: return None
    item = dict(fila_db)
    campos_fecha = ['fecha', 'fecha_vencimiento', 'created_at']
    for campo in campos_fecha:
        valor_original = item.get(campo)
        if valor_original:
            try:
                dt_utc = parser.parse(str(valor_original))
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                dt_local = utc_to_local(dt_utc)               
                if campo == 'fecha_vencimiento':
                    item[campo] = dt_local.strftime('%d/%m/%Y') 
                else:
                    item[campo] = dt_local.strftime('%d/%m/%Y %H:%M')                     
            except ValueError:
                pass 
    return item

@main_bp.route('/cotizador')
@login_required
def cotizador():
    conn = get_db()
    cursor = conn.cursor()
    uid = session['user_id']
    try:
        # actualizar fecha de ultimo ingreso
        cursor.execute("""
            UPDATE usuarios SET last_login = %s WHERE id = %s
        """, (now_utc(), uid))
        conn.commit()

        mostrar_tour = debe_mostrar_tutorial(uid, 'cotizador')
        version_tour = obtener_version_tutorial('cotizador')

        # -------------------------------------------------------------
        # CONSULTA DE CONFIGURACIÓN CON BLINDAJE (ANTI-CRASH)
        # -------------------------------------------------------------
        cursor.execute('SELECT * FROM configuracion WHERE user_id=%s', (uid,))
        config = cursor.fetchone()
        
        # MAGIA DEFENSIVA: Si el usuario (viejo o con error) no tiene configuración, 
        # le pasamos un diccionario vacío en lugar de "None".
        # Así, los "config.get()" del HTML funcionarán perfecto y usarán sus valores por defecto.
        if not config:
            config = {}
        # -------------------------------------------------------------
        
        # Ordenamos usando LOWER() para que 'a' y 'A' se traten igual
        cursor.execute('SELECT * FROM materiales WHERE user_id=%s ORDER BY LOWER(nombre) ASC', (uid,))
        materiales = cursor.fetchall()
        
        cursor.execute('SELECT * FROM productos WHERE user_id=%s ORDER BY LOWER(nombre) ASC', (uid,))
        productos = cursor.fetchall()
        
        cursor.execute('SELECT * FROM maquinaria WHERE user_id=%s ORDER BY LOWER(nombre) ASC', (uid,))
        equipos = cursor.fetchall()

        data = {
            'config': config,
            'materiales': materiales,
            'productos': productos,
            'equipos': equipos,
            'mostrar_tour': mostrar_tour,
            'version_tour': version_tour
        }
    finally:
        cursor.close()
        conn.close()
    return render_template('cotizador.html', **data)

# --- TUTORIALES ---
@main_bp.route('/api/tutorial/completado', methods=['POST'])
@login_required
def tutorial_completado():
    data = request.json
    uid = session['user_id']
    u_name = session.get('username', 'Anonimo')
    modulo = data.get('modulo')
    version = data.get('version')

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO tutoriales_estado (user_id, modulo, version_vista)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, modulo) 
            DO UPDATE SET version_vista = EXCLUDED.version_vista
        """, (uid, modulo, version))
        conn.commit()
        
        current_app.logger.info(f"TUTORIAL_DONE: Usuario '{u_name}' (ID: {uid}) completo el tutorial de {modulo} v{version}")
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# --- API PARA CARGAR RECETA EN EL COTIZADOR ---
@main_bp.route('/api/receta/<int:id>')
@login_required
def obtener_receta_api(id):
    conn = get_db()
    cursor = conn.cursor()
    u_name = session.get('username', 'Anonimo')
    try:
        cursor.execute("SELECT id, nombre FROM productos WHERE id=%s AND user_id=%s", (id, session['user_id']))
        prod = cursor.fetchone()
        if not prod:
            return jsonify({'error': 'Receta no encontrada'}), 404

        cursor.execute("SELECT material_id as id, cantidad FROM producto_detalles WHERE producto_id = %s", (id,))
        materiales_lista = [dict(row) for row in cursor.fetchall()]

        cursor.execute("SELECT maquinaria_id as id FROM producto_maquinaria WHERE producto_id = %s", (id,))
        maquinaria_lista = [dict(row) for row in cursor.fetchall()]

        current_app.logger.info(f"DATA_ACCESS: Usuario '{u_name}' cargo receta para cotizar: {prod['nombre']} (ID: {id})")

        return jsonify({
            'id': prod['id'],
            'nombre': prod['nombre'],
            'materiales': materiales_lista,
            'maquinaria': maquinaria_lista
        })
    except Exception as e:
        current_app.logger.error(f"API_ERROR: Cargando receta ID {id} - {e}")
        return jsonify({'error': 'Error al cargar los detalles'}), 500
    finally:
        cursor.close()
        conn.close()


# --- GUARDAR VENTA ---
@main_bp.route('/guardar_venta', methods=['POST'])
@subscription_required
def guardar_venta():
    data = request.get_json()
    conn = get_db() 
    cursor = conn.cursor()
    u_name = session.get('username', 'Anonimo')
    u_id = session.get('user_id', 'N/A')
    
    try:
        # --- BLOQUE DE FECHAS SEGURO ---
        fecha_usuario = data.get('fecha_venta') 
        # Obtenemos la fecha de hoy local para comparar y no adelantarnos al futuro
        hoy_local = utc_to_local(now_utc()).strftime('%Y-%m-%d')

        # 1. Definimos fecha_actual
        if fecha_usuario and fecha_usuario != hoy_local:
            # Si eligió una fecha manual (pasado), la usamos
            hora_actual = datetime.now().strftime("%H:%M:%S")
            fecha_actual = f"{fecha_usuario} {hora_actual}"
        else:
            # Si no hay fecha o es hoy, usamos tu función estándar
            fecha_actual = ahora_sql()

        # 2. Definimos fecha_vencimiento (Siempre basada en fecha_actual)
        try:
            # Intentamos calcular 2 días después de la fecha de la venta
            if isinstance(fecha_actual, str):
                base_fecha = parser.parse(fecha_actual)
            else:
                base_fecha = fecha_actual
            fecha_vencimiento = base_fecha + timedelta(days=2)
        except Exception as e:
            # Si algo falla, el "Plan B" es 2 días desde hoy
            fecha_vencimiento = ahora_sql(dias=2)
        # --- FIN DEL BLOQUE ---
            
        cursor.execute('SELECT inventario_activo FROM configuracion WHERE user_id=%s', (u_id,))
        config = cursor.fetchone()
        usar_inventario = config['inventario_activo'] if config else False

        venta_id = data.get('id')
        if venta_id in ("", None):
            venta_id = None
        else:
            venta_id = int(venta_id)

        cliente = data.get('cliente', 'Cliente General')
        items = data.get('items', [])
        costo_envio = float(data.get('envio', 0))
        descuento_pct = float(data.get('descuento_porcentaje', 0))
        descuento_monto = float(data.get('descuento_monto', 0))
        tax_percent = float(data.get('tax_percent', 0)) 
        
        estado = data.get('estado', 'pagado')
        monto_pagado_request = float(data.get('pago_inicial', 0)) 

        pagado_historico = 0.0
        if venta_id:
            cursor.execute("""
                SELECT monto_pagado
                FROM ventas
                WHERE id=%s AND user_id=%s
            """, (venta_id, u_id))
            row = cursor.fetchone()
            if row:
                pagado_historico = float(row['monto_pagado'])
        
        monto_pagado_total = pagado_historico + monto_pagado_request

        subtotal_calculado = 0.0
        costo_total_calculado = 0.0

        # --- COSTOS OPERATIVOS (PORCENTAJE) ---
        cursor.execute("""
            SELECT porcentaje_gastos_operativos 
            FROM configuracion 
            WHERE user_id=%s
        """, (u_id,))
        config_row = cursor.fetchone()
        porcentaje_operativo = float(config_row['porcentaje_gastos_operativos']) if config_row and config_row['porcentaje_gastos_operativos'] else 0.0

        for item in items:
            cantidad = float(item.get('cantidad', 0))
            precio_u = float(item.get('precio_unitario', 0))
            costo_u = float(item.get('costo_unitario', 0))
            
            if cantidad <= 0 or precio_u < 0:
                return jsonify({'success': False, 'error': 'Cantidades o precios invalidos'}), 400
                
            item_subtotal_real = cantidad * precio_u 
            item['subtotal'] = item_subtotal_real 
            
            subtotal_calculado += item_subtotal_real
            costo_total_calculado += (cantidad * costo_u)

        subtotal_calculado = round(subtotal_calculado, 2)
        
        if descuento_pct > 0:
            descuento_monto = round(subtotal_calculado * (descuento_pct / 100), 2)
        
        if descuento_monto > subtotal_calculado:
            descuento_monto = subtotal_calculado 

        subtotal_con_descuento = round(subtotal_calculado - descuento_monto, 2)
        base_imponible = round(subtotal_con_descuento + costo_envio, 2)

        tax_amount_calculado = 0.0
        tax_engine = "none"

        if tax_percent > 0:
            tax_amount_calculado = base_imponible * (tax_percent / 100)
            tax_engine = f"IVA {int(tax_percent)}%" if tax_percent.is_integer() else f"IVA {tax_percent}%"

        # Calculamos el costo operativo basado en el costo base de los productos
        costo_operativo_de_esta_venta = costo_total_calculado * (porcentaje_operativo / 100.0)
        
        # Se lo sumamos al costo total para saber tu costo real final
        costo_total_calculado += costo_operativo_de_esta_venta

        total_calculado_exacto = base_imponible + tax_amount_calculado
        total_calculado = math.ceil(total_calculado_exacto) 

        # Si el redondeo hacia arriba cabe dentro del descuento, lo absorbemos ahí
        # para mantener intactos los precios unitarios de los productos.
        diferencia_redondeo = round(total_calculado - total_calculado_exacto, 2)
        factor_tax = 1 + (tax_percent / 100)
        ajuste_descuento = round(diferencia_redondeo / factor_tax, 2)
        if diferencia_redondeo > 0 and descuento_monto >= ajuste_descuento:
            descuento_monto = round(descuento_monto - ajuste_descuento, 2)
            subtotal_con_descuento = round(subtotal_calculado - descuento_monto, 2)
            base_imponible = round(subtotal_con_descuento + costo_envio, 2)
            if tax_percent > 0:
                tax_amount_calculado = base_imponible * (tax_percent / 100)
        
        monto_pagado_real = min(monto_pagado_total, total_calculado)
        saldo_pendiente_real = total_calculado - monto_pagado_real

        if saldo_pendiente_real < 0.05: 
            saldo_pendiente_real = 0.0
            estado = 'pagado'
        elif monto_pagado_real > 0:
            estado = 'anticipo'

        #fecha_actual = ahora_sql()
        #fecha_vencimiento = ahora_sql(dias=2) 

        if venta_id:
            cursor.execute('''
                UPDATE ventas 
                SET cliente=%s, subtotal=%s, envio=%s, descuento_porcentaje=%s, descuento_monto=%s,
                impuestos=%s, tax_engine=%s,
                total=%s, costo_total=%s, costo_fijo_prorrateado=%s,
                estado=%s, monto_pagado=%s, saldo_pendiente=%s, fecha=%s
                WHERE id=%s AND user_id=%s
            ''', (
                    cliente, subtotal_calculado, costo_envio, descuento_pct, descuento_monto,
                    tax_amount_calculado, tax_engine,
                    total_calculado,
                    costo_total_calculado,
                    round(costo_operativo_de_esta_venta, 2),
                    estado,
                    monto_pagado_real,
                    saldo_pendiente_real,
                    fecha_actual,
                    venta_id,
                    u_id
            ))
            cursor.execute('DELETE FROM venta_detalles WHERE venta_id=%s', (venta_id,))
        else:
            cursor.execute('''
                INSERT INTO ventas (
                    user_id, fecha, cliente, subtotal, envio, 
                    descuento_porcentaje, descuento_monto, 
                    impuestos, tax_engine,
                    total, costo_total, costo_fijo_prorrateado,
                    estado, 
                    monto_pagado, saldo_pendiente, fecha_vencimiento
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (
                    u_id,
                    fecha_actual,
                    cliente,
                    subtotal_calculado,
                    costo_envio,
                    descuento_pct,
                    descuento_monto,
                    tax_amount_calculado,
                    tax_engine,
                    total_calculado,
                    costo_total_calculado,
                    round(costo_operativo_de_esta_venta, 2),
                    estado,
                    monto_pagado_real,
                    saldo_pendiente_real,
                    fecha_vencimiento
            ))
            venta_id = cursor.fetchone()['id']
        
        materiales_a_descontar = {}

        for item in items:
            cursor.execute('''
                INSERT INTO venta_detalles (
                    venta_id, concepto, cantidad, precio_unitario, 
                    costo_unitario, subtotal, composicion
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            ''', (
                venta_id,
                item['concepto'],
                float(item['cantidad']),
                float(item.get('precio_unitario', 0)),
                float(item.get('costo_unitario', 0)),
                float(item['subtotal']),
                item.get('composicion', '[]')
            ))

            if usar_inventario and not data.get('id'): 
                try:
                    composicion = json.loads(item.get('composicion', '[]'))
                    cantidad_producto = float(item['cantidad'])

                    for comp in composicion:
                        if comp.get('tipo') == 'material':
                            material_id = comp.get('id')
                            cantidad_requerida = float(comp.get('cantidad', 0)) * cantidad_producto
                            
                            if cantidad_requerida > 0:
                                materiales_a_descontar[material_id] = materiales_a_descontar.get(material_id, 0) + cantidad_requerida
                except Exception as e:
                    current_app.logger.warning(f"INVENTORY_CALC_WARNING: Error calculando receta en memoria para venta - {e}")

        if usar_inventario and not data.get('id') and materiales_a_descontar:
            try:
                for mat_id, total_descuento in materiales_a_descontar.items():
                    cursor.execute('UPDATE materiales SET stock_actual = stock_actual - %s WHERE id = %s', (total_descuento, mat_id))
                    cursor.execute('''
                        INSERT INTO movimientos_inventario 
                        (user_id, material_id, tipo, cantidad, motivo, stock_resultante, fecha)
                        VALUES (%s, %s, 'salida', %s, %s, 
                            (SELECT stock_actual FROM materiales WHERE id=%s), %s)
                    ''', (u_id, mat_id, total_descuento, f"Venta #{venta_id} - Descuento agrupado", mat_id, ahora_sql()))
            except Exception as e:
                current_app.logger.error(f"INVENTORY_DB_ERROR: Error descontando stock para venta {venta_id} - {e}")
                

        try:
            # Determinamos si es nueva o si está editando una existente
            es_nueva = not bool(data.get('id'))
            mensaje_log = f"Creó la Cotización/Venta #{venta_id}" if es_nueva else f"Actualizó la Cotización/Venta #{venta_id}"
            
            cursor.execute("""
                INSERT INTO logs_actividad (user_id, accion, modulo) 
                VALUES (%s, %s, %s)
            """, (u_id, mensaje_log, "Cotizador"))
        except Exception as e:
            current_app.logger.warning(f"Error al guardar log de actividad en Cotizador: {e}")

        conn.commit()
                
        return jsonify({'success': True, 'ticket_id': venta_id})

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"SALE_ERROR: Error guardando venta para usuario {u_id} - {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# ========================================================
# APIS PARA CONTROL DE ESTADOS DE PEDIDO (LOGÍSTICA)
# ========================================================

# 1. CAMBIAR ESTADO DE PEDIDO INDIVIDUAL
@main_bp.route('/api/ventas/<int:venta_id>/estado-pedido', methods=['PATCH'])
@login_required
def update_estado_pedido(venta_id):
    data = request.get_json() or {}
    nuevo_estado = data.get('estado_pedido')
    uid = session['user_id']
    u_name = session.get('username', 'Anonimo')

    if nuevo_estado not in ESTADOS_PERMITIDOS:
        return jsonify({'success': False, 'error': 'Estado de pedido no válido'}), 400

    conn = get_db()
    cursor = conn.cursor()
    try:
        # Asegurarnos de que la venta le pertenece al usuario actual
        cursor.execute("SELECT id, estado FROM ventas WHERE id = %s AND user_id = %s", (venta_id, uid))
        venta = cursor.fetchone()
        if not venta:
            return jsonify({'success': False, 'error': 'Venta no encontrada'}), 404
        if venta['estado'] == 'cancelada':
            return jsonify({'success': False, 'error': 'No se puede modificar un pedido anulado'}), 400

        # Realizar la actualización
        cursor.execute("""
            UPDATE ventas 
            SET estado_pedido = %s 
            WHERE id = %s AND user_id = %s
        """, (nuevo_estado, venta_id, uid))

        # Registro en la bitácora del sistema
        try:
            cursor.execute("""
                INSERT INTO logs_actividad (user_id, accion, modulo) 
                VALUES (%s, %s, %s)
            """, (uid, f"Cambió el estado del pedido a '{nuevo_estado}' en Venta #{venta_id}", "Ventas"))
        except Exception as log_e:
            current_app.logger.warning(f"Error al guardar log de estado_pedido: {log_e}")

        conn.commit()
        current_app.logger.info(f"ORDER_STATUS_UPDATED: Usuario '{u_name}' (ID: {uid}) cambió estado de pedido de Venta #{venta_id} a '{nuevo_estado}'")
        return jsonify({'success': True, 'message': 'Estado del pedido actualizado correctamente'})

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"ORDER_STATUS_ERROR: Error al actualizar estado individual - {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


# 2. CAMBIAR ESTADO DE PEDIDO EN MASA (BULK ACTIONS)
@main_bp.route('/api/ventas/bulk-estado-pedido', methods=['POST'])
@login_required
def bulk_update_estado_pedido():
    data = request.get_json() or {}
    venta_ids = data.get('ids')  # Espera una lista de enteros, ej: [101, 102, 103]
    nuevo_estado = data.get('estado_pedido')
    uid = session['user_id']
    u_name = session.get('username', 'Anonimo')

    if nuevo_estado not in ESTADOS_PERMITIDOS:
        return jsonify({'success': False, 'error': 'Estado no válido'}), 400
    if not venta_ids or not isinstance(venta_ids, list):
        return jsonify({'success': False, 'error': 'IDs no válidos o vacíos'}), 400

    conn = get_db()
    cursor = conn.cursor()
    try:
        # Ejecutar update masivo blindado por el user_id de la sesión
        cursor.execute("""
            UPDATE ventas 
            SET estado_pedido = %s 
            WHERE id IN %s AND user_id = %s AND estado != 'cancelada'
        """, (nuevo_estado, tuple(venta_ids), uid))
        actualizadas = cursor.rowcount

        # Registro en bitácora
        try:
            cursor.execute("""
                INSERT INTO logs_actividad (user_id, accion, modulo) 
                VALUES (%s, %s, %s)
            """, (uid, f"Cambió en masa el estado del pedido a '{nuevo_estado}' para {actualizadas} ventas", "Ventas"))
        except Exception as log_e:
            current_app.logger.warning(f"Error al guardar log de bulk estado_pedido: {log_e}")

        conn.commit()
        current_app.logger.info(f"ORDER_BULK_STATUS_UPDATED: Usuario '{u_name}' (ID: {uid}) actualizó {actualizadas} ventas al estado '{nuevo_estado}'")
        return jsonify({'success': True, 'message': f'{actualizadas} pedidos actualizados con éxito'})

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"ORDER_BULK_STATUS_ERROR: Error en cambio masivo - {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# --- RUTAS DE VISUALIZACIÓN ---
@main_bp.route('/historial')
@login_required
def historial():
    conn = get_db()
    cursor = conn.cursor()
    uid = session['user_id']

    mostrar_tour = debe_mostrar_tutorial(uid, 'historial')
    version_tour = obtener_version_tutorial('historial')
    
    # --- CAPTURA DE PARÁMETROS ---
    q = request.args.get('q', '').strip()
    status = request.args.get('status', 'all')
    page = request.args.get('page', 1, type=int) 
    per_page = 20 
    offset = (page - 1) * per_page 

    # --- 1. QUERY PARA CONTEO ---
    sql_count = "SELECT COUNT(*) FROM ventas WHERE user_id=%s"
    params_count = [uid]

    if q:
        # CAMBIO 1: Ignorar acentos y mayúsculas en el conteo
        sql_count += " AND (CAST(id AS TEXT) ILIKE %s OR TRANSLATE(LOWER(cliente), 'áéíóú', 'aeiou') ILIKE TRANSLATE(LOWER(%s), 'áéíóú', 'aeiou'))"
        params_count.extend([f'%{q}%', f'%{q}%'])
    
    if status != 'all':
        sql_count += " AND estado = %s"
        params_count.append(status)

    cursor.execute(sql_count, params_count)
    total_registros = cursor.fetchone()[0]
    total_pages = math.ceil(total_registros / per_page)

    # --- 2. QUERY PARA DATOS ---
    sql = 'SELECT id, cliente, fecha, total, estado, estado_pedido, saldo_pendiente, fecha_vencimiento, impuestos, tax_engine FROM ventas WHERE user_id=%s'
    params = [uid]
    
    if q:
        # CAMBIO 2: Ignorar acentos y mayúsculas en la búsqueda real
        sql += " AND (CAST(id AS TEXT) ILIKE %s OR TRANSLATE(LOWER(cliente), 'áéíóú', 'aeiou') ILIKE TRANSLATE(LOWER(%s), 'áéíóú', 'aeiou'))"
        params.extend([f'%{q}%', f'%{q}%'])
        
    if status != 'all':
        sql += " AND estado = %s"
        params.append(status)
        
    sql += " ORDER BY id DESC LIMIT %s OFFSET %s"
    params.extend([per_page, offset])
    
    cursor.execute(sql, params)
    ventas_db = cursor.fetchall()
    
    cursor.close()
    conn.close()
    
    # --- 3. PROCESAMIENTO EN PYTHON ---
    ventas_display = [procesar_fila_fechas(v) for v in ventas_db]

    return render_template('historial.html', 
                           ventas=ventas_display, 
                           page=page, 
                           total_pages=total_pages, 
                           q=q,
                           status=status,
                           mostrar_tour=mostrar_tour, 
                           version_tour=version_tour)


@main_bp.route('/ticket/<int:id>')
def ver_ticket(id):
    conn = get_db()
    cursor = conn.cursor()
    u_name = session.get('username', 'Visitante')
    
    cursor.execute('SELECT * FROM ventas WHERE id = %s', (id,))
    venta_db = cursor.fetchone()
    
    if venta_db is None:
        cursor.close(); conn.close(); return "Ticket no encontrado", 404

    venta = procesar_fila_fechas(venta_db)
    cursor.execute('SELECT * FROM venta_detalles WHERE venta_id = %s', (id,))
    detalles = cursor.fetchall()
    
    cursor.execute('SELECT * FROM configuracion WHERE user_id = %s', (venta_db['user_id'],))
    config = cursor.fetchone() or {'nombre_empresa': 'Mi Negocio', 'slogan': 'Gracias por su compra', 'website': ''}

    current_app.logger.info(f"TICKET_VIEW: Usuario '{u_name}' visualizo el ticket #{id}")

    # --- REGISTRAR EN BITÁCORA ---
    try:
        cursor.execute("""
            INSERT INTO logs_actividad (user_id, accion, modulo) 
            VALUES (%s, %s, %s)
        """, (venta_db['user_id'], f"Imprimió o visualizó el Ticket #{id}", "Tickets"))
        conn.commit()
    except Exception as e:
        current_app.logger.error(f"Error al registrar log de ticket: {e}")
    

    cursor.close()
    conn.close()
    return render_template('ticket.html', venta=venta, detalles=detalles, config=config)

@main_bp.route('/terminos')
def terminos(): return render_template('terminos.html')

@main_bp.route('/privacidad')
def privacidad(): return render_template('privacidad.html')

@main_bp.route('/plan_vencido')
def plan_vencido(): 
    return render_template(
        'plan_vencido.html',
        precio_mensual=os.getenv('STRIPE_PRICE_MENSUAL'),
        precio_anual=os.getenv('STRIPE_PRICE_ANUAL')
    )

@main_bp.route('/api/get_cotizacion/<int:id>')
@login_required
def get_cotizacion(id):
    conn = get_db()
    cursor = conn.cursor()
    u_name = session.get('username', 'Anonimo')
    try:
        cursor.execute("SELECT * FROM ventas WHERE id=%s AND user_id=%s", (id, session['user_id']))
        venta = cursor.fetchone()
        if not venta:
            return jsonify({'error': 'Cotizacion no encontrada'}), 404

        cursor.execute("SELECT * FROM venta_detalles WHERE venta_id=%s", (id,))
        items = [{
            'concepto': it['concepto'], 'cantidad': it['cantidad'], 'precio_unitario': it['precio_unitario'],
            'costo_unitario': it['costo_unitario'], 'subtotal': it['subtotal'], 'composicion': it['composicion'] 
        } for it in cursor.fetchall()]

        current_app.logger.info(f"DATA_ACCESS: Usuario '{u_name}' cargo cotizacion #{id} para edicion")
        
        try:
            cursor.execute("""
                INSERT INTO logs_actividad (user_id, accion, modulo) 
                VALUES (%s, %s, %s)
            """, (session['user_id'], f"Cargó la Cotización/Venta #{id} para editar", "Cotizador"))
            conn.commit()
        except Exception as e:
            current_app.logger.warning(f"Error al guardar log de actividad en Cargar Cotizacion: {e}")

        return jsonify({
            'success': True, 
            'id': venta['id'], 
            'cliente': venta['cliente'],
            'descuento_porcentaje': venta['descuento_porcentaje'], 
            'tax_percent': venta['tax_engine'].replace('IVA ', '').replace('%', '') if venta['tax_engine'] != 'none' else 0,
            'items': items
        })
    except Exception as e:
        current_app.logger.error(f"QUOTE_LOAD_ERROR: Error al cargar cotizacion {id} - {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close(); conn.close()

@main_bp.route('/ayuda')
@login_required
def ayuda(): return render_template('ayuda.html')

@main_bp.route('/descargar_excel')
@subscription_required
def descargar_excel():
    conn = get_db()
    uid = session['user_id']
    u_name = session.get('username', 'Anonimo')
    
    query = '''
        SELECT v.id as "Folio", v.fecha as "Fecha_Registro", v.fecha_vencimiento as "Fecha_Vencimiento",
            v.cliente as "Cliente", v.estado as "Estado_Actual", v.total as "Total_Ticket",
            v.monto_pagado as "Pagado", v.saldo_pendiente as "Resta_Por_Pagar",
            d.concepto as "Producto", d.cantidad as "Cantidad", d.precio_unitario as "Precio_Unit_Venta"
        FROM ventas v JOIN venta_detalles d ON v.id = d.venta_id 
        WHERE v.user_id = %s ORDER BY v.fecha DESC
    '''
    try:
        df = pd.read_sql_query(query, conn, params=(uid,))
        conn.close()
        
        if not df.empty:
            df['Fecha_Registro'] = pd.to_datetime(df['Fecha_Registro'], errors='coerce').apply(
                lambda x: utc_to_local(x.to_pydatetime()).strftime('%d/%m/%Y %I:%M %p') if pd.notnull(x) else 'Pendiente'
            )

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer: 
            df.to_excel(writer, index=False, sheet_name='Detalle de Ventas')

        current_app.logger.info(f"EXPORT_DATA: Usuario '{u_name}' (ID: {uid}) descargo el reporte de ventas en Excel")
        

        output.seek(0)
        return send_file(output, download_name=f"Reporte_Sianeffects_{datetime.now().strftime('%Y%m%d')}.xlsx", as_attachment=True)
    except Exception as e:
        current_app.logger.error(f"EXPORT_ERROR: Usuario {uid} fallo al exportar Excel - {e}")
        return f"Error al generar el Excel: {str(e)}", 500
