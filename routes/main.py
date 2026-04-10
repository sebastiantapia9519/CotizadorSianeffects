from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file, jsonify, send_from_directory, abort, current_app
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta, timezone
import pandas as pd
import io
import json
import math
from utils.datetime_utils import now_utc, utc_to_local, ahora_sql
from db import get_db_connection as get_db
from dateutil import parser
from helpers import login_required, subscription_required, obtener_alertas

main_bp = Blueprint('main', __name__)

# --- INYECTOR DE NOTIFICACIONES (PARA TODAS LAS PÁGINAS) ---
@main_bp.app_context_processor
def inject_notifications():
    if 'user_id' in session:
        return {'notificaciones': obtener_alertas(session['user_id'])}
    return {'notificaciones': []}


# --- HELPER INTERNO PARA FORMATEAR FECHAS A LOCAL ---
def procesar_fila_fechas(fila_db):
    if not fila_db: return None
    item = dict(fila_db)
    campos_fecha = ['fecha', 'fecha_vencimiento', 'created_at']
    for campo in campos_fecha:
        valor_original = item.get(campo)
        if valor_original:
            try:
                str_fecha = str(valor_original).replace('T', ' ')[:19]
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

@main_bp.route('/')
def index():
    return redirect(url_for('main.cotizador'))



# --- RUTA DE MIGRACIÓN PARA INVENTARIO ---
@main_bp.route('/migrar-inventario')
@login_required
def migrar_inventario_db():
    conn = get_db()
    try:
        try: conn.execute("ALTER TABLE configuracion ADD COLUMN inventario_activo BOOLEAN DEFAULT 0")
        except: pass
        try: conn.execute("ALTER TABLE materiales ADD COLUMN stock_actual REAL DEFAULT 0")
        except: pass
        try: conn.execute("ALTER TABLE materiales ADD COLUMN stock_minimo REAL DEFAULT 5")
        except: pass
        conn.execute("""
        CREATE TABLE IF NOT EXISTS movimientos_inventario (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            material_id INTEGER,
            tipo TEXT, cantidad REAL, motivo TEXT, stock_resultante REAL,
            fecha DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(material_id) REFERENCES materiales(id)
        )""")
        conn.commit()
        return "Base de datos actualizada con éxito. <a href='/configuracion'>Ir a Configuración</a>"
    except Exception as e:
        return f"Error en migración: {e}"
    finally:
        conn.close()

@main_bp.route('/cotizador')
@subscription_required
def cotizador():
    conn = get_db()
    uid = session['user_id']
    try:
        # --- VERIFICACIÓN DE TUTORIAL ---
        user = conn.execute('SELECT tutorial_visto FROM usuarios WHERE id=?', (uid,)).fetchone()
        
        # Si no ha visto el tutorial (es 0 o Null)
        if not user or not user['tutorial_visto']:
            # 1. Lo marcamos como visto para que no lo moleste la próxima vez
            conn.execute('UPDATE usuarios SET tutorial_visto=1 WHERE id=?', (uid,))
            conn.commit()
            
            # 2. Le avisamos y lo redirigimos
            flash('👋 ¡Bienvenido! Te hemos traído al Manual para que conozcas tu nuevo sistema.', 'info')
            conn.close() # Importante cerrar antes del return
            return redirect(url_for('main.ayuda'))
        # ---------------------------------------

        # Si ya lo vio, carga el cotizador normal

        data = {
            'config': conn.execute('SELECT * FROM configuracion WHERE user_id=?', (uid,)).fetchone(),
            'materiales': conn.execute('SELECT * FROM materiales WHERE user_id=?', (uid,)).fetchall(),
            'productos': conn.execute('SELECT * FROM productos WHERE user_id=?', (uid,)).fetchall(),
            'equipos': conn.execute('SELECT * FROM maquinaria WHERE user_id=?', (uid,)).fetchall()
        }
    finally:
        conn.close()
    return render_template('cotizador.html', **data)

# --- API PARA CARGAR RECETA EN EL COTIZADOR ---
@main_bp.route('/api/receta/<int:id>')
@login_required
def obtener_receta_api(id):
    conn = get_db()
    try:
        # 1. Datos básicos
        prod = conn.execute("SELECT id, nombre FROM productos WHERE id=? AND user_id=?", (id, session['user_id'])).fetchone()
        if not prod:
            return jsonify({'error': 'Receta no encontrada'}), 404

        # 2. Materiales
        materiales_rows = conn.execute("""
            SELECT material_id as id, cantidad
            FROM producto_detalles
            WHERE producto_id = ?
        """, (id,)).fetchall()
        materiales_lista = [dict(row) for row in materiales_rows]

        # 3. Maquinaria
        maquinaria_rows = conn.execute("""
            SELECT maquinaria_id as id
            FROM producto_maquinaria
            WHERE producto_id = ?
        """, (id,)).fetchall()
        maquinaria_lista = [dict(row) for row in maquinaria_rows]

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
        conn.close()

# --- GUARDAR VENTA ---
@main_bp.route('/guardar_venta', methods=['POST'])
@login_required
def guardar_venta():
    data = request.get_json()
    conn = get_db() 
    cursor = conn.cursor()
    
    try:
        config = cursor.execute('SELECT inventario_activo FROM configuracion WHERE user_id=?', (session['user_id'],)).fetchone()
        usar_inventario = config['inventario_activo'] if config else 0

        venta_id = data.get('id')
        if venta_id in ("", None):
            venta_id = None
        else:
            venta_id = int(venta_id)


        # --- 1. RECIBIMOS DATOS (Confiamos en la intención, no en los totales) ---
        cliente = data.get('cliente', 'Cliente General')
        items = data.get('items', [])
        costo_envio = float(data.get('envio', 0))
        descuento_pct = float(data.get('descuento_porcentaje', 0))
        descuento_monto = float(data.get('descuento_monto', 0))
        tax_percent = float(data.get('tax_percent', 0))
        estado = data.get('estado', 'pagado')
        monto_pagado_request = float(data.get('pago_inicial', 0)) # Lo que el usuario dice que pagó

        # --- 2. INICIO DEL BLINDAJE MATEMÁTICO ---
        subtotal_calculado = 0.0
        costo_total_calculado = 0.0

        # Recalculamos línea por línea basándonos estrictamente en cantidad * precio
        for item in items:
            # Forzamos a que sean números para evitar inyecciones raras o errores de tipo
            cantidad = float(item.get('cantidad', 0))
            precio_u = float(item.get('precio_unitario', 0))
            costo_u = float(item.get('costo_unitario', 0))
            
            # Validación de seguridad: no pueden vender cantidades negativas ni precios raros
            if cantidad <= 0 or precio_u < 0:
                return jsonify({'success': False, 'error': 'Cantidades o precios inválidos'}), 400
                
            # Ignoramos el subtotal del item que manda el frontend y lo calculamos nosotros
            item_subtotal_real = cantidad * precio_u 
            item['subtotal'] = item_subtotal_real # Actualizamos el diccionario con la verdad absoluta
            
            subtotal_calculado += item_subtotal_real
            costo_total_calculado += (cantidad * costo_u)

        # --- 3. APLICAMOS REGLAS DE NEGOCIO ---
        # Tope de seguridad: no puedes descontar más de lo que cuesta
        if descuento_monto > subtotal_calculado:
            descuento_monto = subtotal_calculado 

        subtotal_con_descuento = subtotal_calculado - descuento_monto

        # Sumamos el envío ANTES de calcular impuestos
        base_imponible = subtotal_con_descuento + costo_envio

        # Calculamos impuestos reales nosotros mismos
        tax_amount_calculado = 0.0
        tax_engine = "none"

        if tax_percent > 0:
            tax_amount_calculado = base_imponible * (tax_percent / 100)
            tax_engine = f"IVA {int(tax_percent)}%" if tax_percent.is_integer() else f"IVA {tax_percent}%"

        # --- 4. EL TOTAL SAGRADO ---
        total_calculado = base_imponible + tax_amount_calculado

        # --- 5. CUADRAMOS LOS PAGOS ---
        # Si dice que pagó más del total, lo topamos al total real
        monto_pagado_real = min(monto_pagado_request, total_calculado) 
        saldo_pendiente_real = total_calculado - monto_pagado_real

        # Si el saldo pendiente es muy cercano a 0 (por decimales), lo matamos
        if saldo_pendiente_real < 0.05: 
            saldo_pendiente_real = 0.0
            estado = 'pagado'

        fecha_actual = ahora_sql()
        fecha_vencimiento = ahora_sql(dias=2) #Si la venta no se concreta en 2 dias se elimina

        if venta_id:
            cursor.execute('''
                UPDATE ventas 
                SET cliente=?, subtotal=?, envio=?, descuento_porcentaje=?, descuento_monto=?,
                    impuestos=?, tax_engine=?,
                    total=?, costo_total=?, estado=?, monto_pagado=?, saldo_pendiente=?
                WHERE id=? AND user_id=?
            ''', (
                cliente, subtotal_calculado, costo_envio, descuento_pct, descuento_monto,
                tax_amount_calculado, tax_engine,
                total_calculado, costo_total_calculado, estado, monto_pagado_real, saldo_pendiente_real,
                venta_id, session['user_id']
            ))
            cursor.execute('DELETE FROM venta_detalles WHERE venta_id=?', (venta_id,))
        else:
            cursor.execute('''
                INSERT INTO ventas (
                    user_id, fecha, cliente, subtotal, envio, 
                    descuento_porcentaje, descuento_monto, 
                    impuestos, tax_engine,
                    total, costo_total, estado, 
                    monto_pagado, saldo_pendiente, fecha_vencimiento
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                session['user_id'], fecha_actual, cliente, subtotal_calculado, costo_envio, 
                descuento_pct, descuento_monto, 
                tax_amount_calculado, tax_engine,
                total_calculado, costo_total_calculado, estado, 
                monto_pagado_real, saldo_pendiente_real, fecha_vencimiento
            ))
            
            # ---  NOTA DE MIGRACIÓN A POSTGRESQL  ---
            # SQLite usa .lastrowid para obtener el ID recién creado. Postgres NO lo soporta así.
            # Cuando migres, tu query de INSERT deberá terminar con: "... VALUES (...) RETURNING id"
            # Y esta línea de abajo cambiará a: venta_id = cursor.fetchone()[0]
            venta_id = cursor.lastrowid
        
        # 1. Creamos variable en memoria para acumular las cantidades a descontar.
        # Las llaves serán los ID de los materiales, y los valores la cantidad total a restar.
        materiales_a_descontar = {}

        for item in items:
            # Insertamos el detalle de la venta (una fila por producto)
            cursor.execute('''
                INSERT INTO venta_detalles (
                    venta_id, concepto, cantidad, precio_unitario, 
                    costo_unitario, subtotal, composicion
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                venta_id,
                item['concepto'],
                float(item['cantidad']),
                float(item.get('precio_unitario', 0)),
                float(item.get('costo_unitario', 0)),
                float(item['subtotal']),
                item.get('composicion', '[]')
            ))

            # 2. Si usamos inventario y es una venta nueva (no edición)
            if usar_inventario and not data.get('id'): 
                try:
                    composicion = json.loads(item.get('composicion', '[]'))
                    cantidad_producto = float(item['cantidad'])

                    for comp in composicion:
                        if comp.get('tipo') == 'material':
                            material_id = comp.get('id')
                            # Calculamos cuánto material requiere este producto en específico
                            cantidad_requerida = float(comp.get('cantidad', 0)) * cantidad_producto
                            
                            if cantidad_requerida > 0:
                                # En lugar de ir a la BD, sumamos en nuestro diccionario global
                                if material_id in materiales_a_descontar:
                                    materiales_a_descontar[material_id] += cantidad_requerida
                                else:
                                    materiales_a_descontar[material_id] = cantidad_requerida
                except Exception as e:
                    current_app.logger.warning(f"INVENTORY_CALC_WARNING: Error calculando receta en memoria para venta - {e}")

        # 3. IMPACTO A LA BASE DE DATOS (FUERA DEL CICLO FOR DE PRODUCTOS)
        # Ahora que ya sabemos el total exacto de cada material, hacemos los Updates.
        if usar_inventario and not data.get('id') and materiales_a_descontar:
            try:
                for mat_id, total_descuento in materiales_a_descontar.items():
                    # Restamos el stock actual de una sola vez por material
                    cursor.execute('''
                        UPDATE materiales 
                        SET stock_actual = stock_actual - ? 
                        WHERE id = ?
                    ''', (total_descuento, mat_id))
                    
                    # Registramos el movimiento en el historial
                    cursor.execute('''
                        INSERT INTO movimientos_inventario 
                        (user_id, material_id, tipo, cantidad, motivo, stock_resultante, fecha)
                        VALUES (?, ?, 'salida', ?, ?, 
                            (SELECT stock_actual FROM materiales WHERE id=?),
                            ?
                        )
                    ''', (
                        session['user_id'],
                        mat_id,
                        total_descuento,
                        f"Venta #{venta_id} - Descuento agrupado",
                        mat_id,
                        ahora_sql()
                    ))
            except Exception as e:
                current_app.logger.error(f"INVENTORY_DB_ERROR: Error descontando stock para venta {venta_id} - {e}")
                

        conn.commit()
        current_app.logger.info(f"SALE_SAVED: Usuario {session['user_id']} guardó la venta/cotización #{venta_id} con estado '{estado}'.")
        return jsonify({'success': True, 'ticket_id': venta_id})

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"SALE_ERROR: Error guardando venta para usuario {session['user_id']} - {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

# --- ACTUALIZAR VENTA (ABONOS) ---
@main_bp.route('/api/actualizar_venta', methods=['POST'])
@login_required
def actualizar_venta():
    data = request.get_json()
    venta_id = data.get('id')

    abono = float(data.get('abono', 0))
    
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        venta = cursor.execute("SELECT total, monto_pagado, saldo_pendiente FROM ventas WHERE id = ? AND user_id = ?", (venta_id, session['user_id'])).fetchone()
        
        if not venta:
            return jsonify({'success': False, 'message': 'Venta no encontrada'}), 404
            
        total = venta['total']
        pagado_anterior = venta['monto_pagado']
        
        nuevo_pagado = pagado_anterior + abono
        nuevo_saldo = total - nuevo_pagado
        
        nuevo_estado = 'anticipo'
        if nuevo_saldo <= 0.5: 
            nuevo_saldo = 0
            nuevo_pagado = total
            nuevo_estado = 'pagado'
        
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
    
    # 1. Variables de Paginación y Búsqueda
    q = request.args.get('q', '').strip()
    page = request.args.get('page', 1, type=int) # Página actual (por defecto la 1)
    per_page = 20  # Cantidad de registros por página
    offset = (page - 1) * per_page # Desde dónde empezar a cortar

    # 2. Primero contamos el TOTAL de resultados (Query COUNT)
    # Esto es necesario para calcular el número total de páginas
    # Para Postgres cambiarás LIKE por ILIKE
    # sql_count += " AND (CAST(id AS TEXT) ILIKE ? OR cliente ILIKE ?)"
    sql_count = "SELECT COUNT(*) FROM ventas WHERE user_id=?"
    params_count = [uid]

    if q:
        # Usamos CAST(id AS TEXT) para que la búsqueda por folio sea flexible (ej: buscas "1" y encuentra "1", "10", "12")
        sql_count += " AND (CAST(id AS TEXT) LIKE ? OR cliente LIKE ?)"
        params_count.extend([f'%{q}%', f'%{q}%'])

    total_registros = conn.execute(sql_count, params_count).fetchone()[0]
    
    # Calculamos total de páginas (importamos math aquí por si acaso no está arriba)
    total_pages = math.ceil(total_registros / per_page)

    # 3. Ahora traemos los datos de la página actual (Query DATA)
    sql = '''
        SELECT id, cliente, fecha, total, estado, saldo_pendiente, fecha_vencimiento, impuestos, tax_engine
        FROM ventas 
        WHERE user_id=? 
    '''
    params = [uid]
    
    if q:
        sql += " AND (CAST(id AS TEXT) LIKE ? OR cliente LIKE ?)"
        params.extend([f'%{q}%', f'%{q}%'])
        
    # Aquí está la magia: LIMIT y OFFSET recortan los resultados
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([per_page, offset])
    
    ventas_db = conn.execute(sql, params).fetchall()
    conn.close()
    
    ventas_display = [procesar_fila_fechas(v) for v in ventas_db]
    
    # 4. Pasamos las variables de paginación al HTML
    return render_template('historial.html', 
                           ventas=ventas_display, 
                           page=page, 
                           total_pages=total_pages, 
                           q=q)


@main_bp.route('/ticket/<int:id>')
def ver_ticket(id):
    conn = get_db()
    venta_db = conn.execute('SELECT * FROM ventas WHERE id = ?', (id,)).fetchone()
    
    if venta_db is None:
        conn.close()
        return "Ticket no encontrado", 404

    venta = procesar_fila_fechas(venta_db)

    detalles = conn.execute('SELECT * FROM venta_detalles WHERE venta_id = ?', (id,)).fetchall()
    config = conn.execute('SELECT * FROM configuracion WHERE user_id = ?', (venta_db['user_id'],)).fetchone()

    if config is None:
        config = {'nombre_empresa': 'Mi Negocio', 'slogan': 'Gracias por su compra', 'website': ''}

    conn.close()
    return render_template('ticket.html', venta=venta, detalles=detalles, config=config)


@main_bp.route('/terminos')
def terminos():
    return render_template('terminos.html')

@main_bp.route('/privacidad')
def privacidad():
    return render_template('privacidad.html')

@main_bp.route('/plan_vencido')
def plan_vencido():
    return render_template('plan_vencido.html')

# --- API PARA CARGAR UNA VENTA/COTIZACIÓN EXISTENTE EN EL EDITOR ---
@main_bp.route('/api/get_cotizacion/<int:id>')
@login_required
def get_cotizacion(id):
    conn = get_db()
    try:
        # 1. Traer datos generales de la venta
        venta = conn.execute("SELECT * FROM ventas WHERE id=? AND user_id=?", (id, session['user_id'])).fetchone()
        if not venta:
            return jsonify({'error': 'Cotización no encontrada'}), 404

        # 2. Traer los productos detallados del carrito
        items_db = conn.execute("SELECT * FROM venta_detalles WHERE venta_id=?", (id,)).fetchall()
        
        items = []
        for it in items_db:
            items.append({
                'concepto': it['concepto'],
                'cantidad': it['cantidad'],
                'precio_unitario': it['precio_unitario'],
                'costo_unitario': it['costo_unitario'],
                'subtotal': it['subtotal'],
                'composicion': it['composicion'] # Esto ya viene como string JSON de la BD
            })

        return jsonify({
            'success': True,
            'id': venta['id'],
            'cliente': venta['cliente'],
            'descuento': venta['descuento_porcentaje'],
            'tax_percent': venta['tax_engine'].replace('IVA ', '').replace('%', '') if venta['tax_engine'] != 'none' else 0,
            'items': items
        })
    except Exception as e:
        current_app.logger.error(f"QUOTE_LOAD_ERROR: Error al cargar cotización {id} para editor - {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# --- RUTA DE AYUDA Y DOCUMENTACIÓN ---
@main_bp.route('/ayuda')
@login_required
def ayuda():
    return render_template('ayuda.html')

@main_bp.route('/descargar_excel')
@login_required
def descargar_excel():
    conn = get_db()
    uid = session['user_id']
    
    query = '''
        SELECT 
            v.id as Folio, 
            v.fecha as Fecha_Registro,
            v.fecha_vencimiento as Fecha_Vencimiento,
            v.cliente as Cliente, 
            v.estado as Estado_Actual,
            v.document_type as Tipo_Doc,
            d.concepto as Producto, 
            d.cantidad as Cantidad, 
            d.precio_unitario as Precio_Unit_Venta, 
            d.costo_unitario as Costo_Unit_Prod, 
            (d.precio_unitario - d.costo_unitario) as Ganancia_Unitaria,
            d.subtotal as Subtotal_Linea,
            d.composicion as Receta_Materiales,
            v.subtotal as Subtotal_Venta,
            v.descuento_monto as Descuento_Aplicado,
            v.impuestos as Impuestos_Monto,
            v.tax_engine as Impuestos_Info,
            v.total as Total_Ticket,
            v.monto_pagado as Pagado, 
            v.saldo_pendiente as Resta_Por_Pagar
        FROM ventas v 
        JOIN venta_detalles d ON v.id = d.venta_id 
        WHERE v.user_id = ? 
        ORDER BY v.fecha DESC
    '''
    
    try:
        df = pd.read_sql_query(query, conn, params=(uid,))
        conn.close()
        
        if not df.empty:

            # 1. Normalizamos la columna sin importar si viene como String (SQLite) o Datetime (Postgres)
            df['Fecha_Registro'] = pd.to_datetime(df['Fecha_Registro'], errors='coerce')
            
            # 2. Iteramos con .apply() para usar tu función centralizada
            # x.to_pydatetime() transforma el objeto de Pandas a un datetime estándar de Python
            df['Fecha_Registro'] = df['Fecha_Registro'].apply(
                lambda x: utc_to_local(x.to_pydatetime()).strftime('%d/%m/%Y %I:%M %p') if pd.notnull(x) else 'Pendiente'
            )
            
            if 'Fecha_Vencimiento' in df.columns:
                df['Fecha_Vencimiento'] = pd.to_datetime(df['Fecha_Vencimiento'], errors='coerce')
                df['Fecha_Vencimiento'] = df['Fecha_Vencimiento'].apply(
                    # Para el vencimiento usamos el mismo flujo, pero formateando solo el día
                    lambda x: utc_to_local(x.to_pydatetime()).strftime('%d/%m/%Y') if pd.notnull(x) else ''
                )

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer: 
            df.to_excel(writer, index=False, sheet_name='Detalle de Ventas')
            worksheet = writer.sheets['Detalle de Ventas']
            for column_cells in worksheet.columns:
                try:
                    max_len = max(len(str(cell.value)) for cell in column_cells)
                    adjusted_width = min(max_len + 2, 50) 
                    worksheet.column_dimensions[column_cells[0].column_letter].width = adjusted_width
                except: pass

        output.seek(0)
        filename = f"Reporte_SianEffects_{datetime.now().strftime('%Y%m%d')}.xlsx"
        return send_file(output, download_name=filename, as_attachment=True)

    except Exception as e:
        current_app.logger.error(f"EXPORT_ERROR: Usuario {uid} falló al exportar Excel - {e}")
        return f"Error al generar el Excel: {str(e)}", 500