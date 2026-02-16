from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file, jsonify, send_from_directory, abort
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta, timezone
import pandas as pd
import io
import json
import math
from utils.datetime_utils import now_utc, utc_to_local
from db import get_db_connection as get_db
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
                dt_utc = datetime.strptime(str_fecha, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                dt_local = utc_to_local(dt_utc)
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
        print(f"Error API Receta ID {id}: {e}")
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
    print("DATA RECIBIDA:", data)
    
    try:
        config = cursor.execute('SELECT inventario_activo FROM configuracion WHERE user_id=?', (session['user_id'],)).fetchone()
        usar_inventario = config['inventario_activo'] if config else 0

        venta_id = data.get('id')
        if venta_id in ("", None):
            venta_id = None
        else:
            venta_id = int(venta_id)


        cliente = data.get('cliente', 'Cliente General')
        items = data.get('items', [])
        subtotal = data.get('subtotal', 0)
        descuento_pct = data.get('descuento_porcentaje', 0)
        descuento_monto = data.get('descuento_monto', 0)
        
        tax_amount = float(data.get('tax_amount', 0))
        tax_percent = float(data.get('tax_percent', 0))
        
        if tax_amount > 0:
            tax_engine = f"IVA {int(tax_percent)}%" if tax_percent.is_integer() else f"IVA {tax_percent}%"
        else:
            tax_engine = "none"

        total = data.get('total', 0)
        costo_total = data.get('costo_total', 0)
        estado = data.get('estado', 'pagado')
        monto_pagado = data.get('pago_inicial', total)
        
        saldo_pendiente = total - monto_pagado
        if saldo_pendiente < 0: saldo_pendiente = 0

        fecha_actual = now_utc()
        fecha_vencimiento = (now_utc() + timedelta(days=7)).isoformat()

        if venta_id:
            cursor.execute('''
                UPDATE ventas 
                SET cliente=?, subtotal=?, descuento_porcentaje=?, descuento_monto=?,
                    impuestos=?, tax_engine=?,
                    total=?, costo_total=?, estado=?, monto_pagado=?, saldo_pendiente=?
                WHERE id=? AND user_id=?
            ''', (
                cliente, subtotal, descuento_pct, descuento_monto,
                tax_amount, tax_engine,
                total, costo_total, estado, monto_pagado, saldo_pendiente,
                venta_id, session['user_id']
            ))
            cursor.execute('DELETE FROM venta_detalles WHERE venta_id=?', (venta_id,))
        else:
            cursor.execute('''
                INSERT INTO ventas (
                    user_id, fecha, cliente, subtotal, 
                    descuento_porcentaje, descuento_monto, 
                    impuestos, tax_engine,
                    total, costo_total, estado, 
                    monto_pagado, saldo_pendiente, fecha_vencimiento
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                session['user_id'], fecha_actual, cliente, subtotal, 
                descuento_pct, descuento_monto, 
                tax_amount, tax_engine,
                total, costo_total, estado, 
                monto_pagado, saldo_pendiente, fecha_vencimiento
            ))
            venta_id = cursor.lastrowid

        for item in items:
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

            print("VENTA_ID ANTES IF:", venta_id, type(venta_id))

            if usar_inventario and not data.get('id'): 
                try:
                    composicion = json.loads(item.get('composicion', '[]'))
                    cantidad_producto = float(item['cantidad'])

                    for comp in composicion:
                        if comp.get('tipo') == 'material':
                            material_id = comp.get('id')
                            cantidad_a_descontar = float(comp.get('cantidad', 0)) * cantidad_producto
                            
                            if cantidad_a_descontar > 0:
                                cursor.execute('''
                                    UPDATE materiales 
                                    SET stock_actual = stock_actual - ? 
                                    WHERE id = ?
                                ''', (cantidad_a_descontar, material_id))

                                cursor.execute('''
                                    INSERT INTO movimientos_inventario 
                                    (user_id, material_id, tipo, cantidad, motivo, stock_resultante)
                                    VALUES (?, ?, 'salida', ?, ?, (SELECT stock_actual FROM materiales WHERE id=?))
                                ''', (
                                    session['user_id'], 
                                    material_id, 
                                    cantidad_a_descontar, 
                                    f"Venta #{venta_id} - {item['concepto']}", 
                                    material_id
                                ))
                except Exception as e:
                    print(f"Error descontando inventario: {e}")
                    print("DATA RECIBIDA:", data)


        conn.commit()
        return jsonify({'success': True, 'ticket_id': venta_id})

    except Exception as e:
        conn.rollback()
        print(f"Error guardando venta: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

# --- ACTUALIZAR VENTA (ABONOS) ---
@main_bp.route('/api/actualizar_venta', methods=['POST'])
@login_required
def actualizar_venta():
    data = request.get_json()
    venta_id = data.get('id')

    print("DEBUG guardar_venta → data recibida:", data)
    print("DEBUG guardar_venta → venta_id:", venta_id)

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
    sql_count = "SELECT COUNT(*) FROM ventas WHERE user_id=?"
    params_count = [uid]

    if q:
        # Usamos CAST(id AS TEXT) para que la búsqueda por folio sea flexible (ej: buscas "1" y encuentra "1", "10", "12")
        sql_count += " AND (CAST(id AS TEXT) LIKE ? OR cliente LIKE ?)"
        params_count.extend([f'%{q}%', f'%{q}%'])

    total_registros = conn.execute(sql_count, params_count).fetchone()[0]
    
    # Calculamos total de páginas (importamos math aquí por si acaso no está arriba)
    import math 
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

# --- CONFIGURACIÓN Y EXPORTACIÓN ---

@main_bp.route('/configuracion', methods=('GET', 'POST'))
@login_required
def configuracion():
    conn = get_db()
    uid = session['user_id']
    shipping_config = None

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'update_profile':
            new_username = request.form['username']
            new_email = request.form['email']
            new_phone = request.form['telefono']
            new_country = request.form.get('country_code', 'MX')
            new_password = request.form.get('password')

            try:
                if new_password and new_password.strip() != "":
                    hashed_pw = generate_password_hash(new_password)
                    conn.execute('''
                        UPDATE usuarios 
                        SET username=?, email=?, telefono=?, country_code=?, password=? 
                        WHERE id=?
                    ''', (new_username, new_email, new_phone, new_country, hashed_pw, uid))
                    flash('Perfil y contraseña actualizados.', 'success')
                else:
                    conn.execute('''
                        UPDATE usuarios 
                        SET username=?, email=?, telefono=?, country_code=? 
                        WHERE id=?
                    ''', (new_username, new_email, new_phone, new_country, uid))
                    flash('Perfil actualizado correctamente.', 'success')

                session['username'] = new_username

            except Exception as e:
                print(f"Error update profile: {e}")
                flash('Error: El nombre de usuario o correo ya está en uso.', 'danger')

        elif action == 'update_password':
            new_password = request.form['password']
            if new_password and len(new_password) >= 6:
                hashed_pw = generate_password_hash(new_password)
                conn.execute('UPDATE usuarios SET password=? WHERE id=?', (hashed_pw, uid))
                flash('Contraseña actualizada. Por favor inicia sesión de nuevo.', 'success')
            else:
                flash('La contraseña es muy corta.', 'danger')

        elif action == 'update_business':
            margen = request.form['margen']
            empresa = request.form['nombre_empresa'] # Este es el dato clave
            slogan = request.form['slogan']
            website = request.form['website']
            
            inventario_activo = 1 if request.form.get('inventario_activo') else 0

            # PASO 1: Actualizar la tabla USUARIOS (Fuente de la verdad del nombre)
            conn.execute('UPDATE usuarios SET company_name=? WHERE id=?', (empresa, uid))

            # PASO 2: Actualizar o Crear la tabla CONFIGURACION
            config_existente = conn.execute('SELECT id FROM configuracion WHERE user_id=?', (uid,)).fetchone()

            if config_existente:
                conn.execute('''
                    UPDATE configuracion
                    SET margen_ganancia=?, nombre_empresa=?, slogan=?, website=?, inventario_activo=?
                    WHERE user_id=?
                ''', (margen, empresa, slogan, website, inventario_activo, uid))
            else:
                conn.execute('''
                    INSERT INTO configuracion (user_id, margen_ganancia, nombre_empresa, slogan, website, inventario_activo)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (uid, margen, empresa, slogan, website, inventario_activo))

            
            flash('Datos del negocio guardados correctamente.', 'success')

        # --- GESTIÓN DE ENVÍOS (Config Base) ---
        elif action == 'update_shipping':
            try:
                origin_lat = request.form.get('origin_lat')
                origin_lng = request.form.get('origin_lng')
                local_base = float(request.form.get('local_base_rate') or 0)
                local_km = float(request.form.get('local_km_rate') or 0)
                safety_margin = int(request.form.get('safety_margin') or 10)

                existing = conn.execute("SELECT id FROM shipping_configs WHERE user_id=?", (uid,)).fetchone()

                if existing:
                    conn.execute("""
                        UPDATE shipping_configs 
                        SET origin_lat=?, origin_lng=?, local_base_rate=?, local_km_rate=?, safety_margin_percent=?
                        WHERE user_id=?
                    """, (origin_lat, origin_lng, local_base, local_km, safety_margin, uid))
                else:
                    conn.execute("""
                        INSERT INTO shipping_configs (user_id, origin_lat, origin_lng, local_base_rate, local_km_rate, safety_margin_percent)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (uid, origin_lat, origin_lng, local_base, local_km, safety_margin))

                flash('Configuración de envíos actualizada.', 'success')
            except Exception as e:
                print(f"Error shipping config: {e}")
                flash(f'Error al guardar envíos: {e}', 'danger')

        # --- GESTIÓN DE ZONAS (Crear) ---
        elif action == 'create_zone':
            try:
                nombre = request.form.get('zone_name')
                estados_str = request.form.get('zone_states', '').upper()
                
                # Convertimos "NL, COAH" a lista ["NL", "COAH"]
                if 'TODOS' in estados_str or 'ALL' in estados_str:
                    estados_json = json.dumps(['ALL'])
                else:
                    estados_lista = [x.strip() for x in estados_str.split(',') if x.strip()]
                    estados_json = json.dumps(estados_lista)

                conn.execute("INSERT INTO shipping_zones (user_id, zone_name, states_included) VALUES (?, ?, ?)",
                             (uid, nombre, estados_json))
                flash('Zona de envío creada con éxito.', 'success')
            except Exception as e:
                flash(f'Error al crear zona: {e}', 'danger')

        # --- GESTIÓN DE ZONAS (Borrar) ---
        elif action == 'delete_zone':
            try:
                zone_id = int(request.form.get('zone_id')) # FORZAMOS ENTERO
                # Primero borramos las tarifas asociadas
                conn.execute("DELETE FROM shipping_rates WHERE zone_id=?", (zone_id,))
                conn.execute("DELETE FROM shipping_zones WHERE id=? AND user_id=?", (zone_id, uid))
                flash('Zona y sus tarifas eliminadas.', 'warning')
            except Exception as e:
                flash(f'Error al eliminar zona: {e}', 'danger')

        # --- GESTIÓN DE TARIFAS (Agregar) - AQUÍ ESTABA EL ERROR ---
        elif action == 'add_rate':
            try:
                raw_zone_id = request.form.get('zone_id')
                
                # VALIDACIÓN DE SEGURIDAD
                if not raw_zone_id or raw_zone_id == 'None':
                    raise ValueError("El ID de la zona no se cargó correctamente. Recarga la página.")

                zone_id = int(raw_zone_id) 
                peso = float(request.form.get('max_weight'))
                precio = float(request.form.get('price'))
                
                conn.execute("INSERT INTO shipping_rates (zone_id, max_weight_kg, price) VALUES (?, ?, ?)",
                             (zone_id, peso, precio))
                flash('Tarifa agregada correctamente.', 'success')
            except Exception as e:
                print(f"Error rate: {e}")
                flash(f'No se pudo agregar: {e}', 'danger')

        # --- GESTIÓN DE TARIFAS (Borrar) ---
        elif action == 'delete_rate':
            try:
                rate_id = int(request.form.get('rate_id')) # FORZAMOS ENTERO
                conn.execute("DELETE FROM shipping_rates WHERE id=?", (rate_id,))
                flash('Tarifa eliminada.', 'warning')
            except Exception as e:
                flash(f'Error al eliminar tarifa: {e}', 'danger')

        conn.commit()
        conn.close()
        return redirect(url_for('main.configuracion'))

    # --- GET: Cargar datos para mostrar ---
    config = conn.execute('SELECT * FROM configuracion WHERE user_id=?', (uid,)).fetchone()
   # 1. Traemos al usuario (que TIENE el company_name "AAA")
    user_raw = conn.execute('SELECT * FROM usuarios WHERE id=?', (uid,)).fetchone()
    user_display = procesar_fila_fechas(user_raw) # Tu función existente
    
    # 2. Traemos la configuración (que puede estar vacía o incompleta)
    config_row = conn.execute('SELECT * FROM configuracion WHERE user_id=?', (uid,)).fetchone()
    
    # 3. CREAMOS UN DICCIONARIO HÍBRIDO (LA SOLUCIÓN FINAL)
    # Convertimos la fila de BD a un diccionario editable de Python
    if config_row:
        # Convertimos sqlite3.Row a diccionario normal para poder editarlo
        config = dict(config_row) 
    else:
        # Valores por defecto si no hay configuración previa
        config = {
            'margen_ganancia': 100, 
            'slogan': '', 
            'website': '', 
            'inventario_activo': 0
        }
    
    # AQUI ESTÁ EL TRUCO: 
    # Sobrescribimos 'nombre_empresa' con lo que diga la tabla de usuarios ('company_name').
    # Así, aunque en config esté vacío, se verá "AAA" o lo que tenga el usuario.
    config['nombre_empresa'] = user_raw['company_name'] if user_raw['company_name'] else ''

     # -------------------------------------------------
    # Zonas y tarifas (sin tocar lógica)
    # -------------------------------------------------
    zones_db = conn.execute(
        "SELECT * FROM shipping_zones WHERE user_id=?",
        (uid,)
    ).fetchall()

    zones = []
    for z in zones_db:
        z_dict = dict(z)

        rates_db = conn.execute(
            "SELECT * FROM shipping_rates WHERE zone_id=? ORDER BY max_weight_kg ASC",
            (z['id'],)
        ).fetchall()
        z_dict['rates'] = [dict(r) for r in rates_db]

        try:
            states_list = json.loads(z['states_included'])
            z_dict['states_str'] = ", ".join(states_list)
        except:
            z_dict['states_str'] = z['states_included']

        zones.append(z_dict)



    conn.close()
    return render_template('configuracion.html', 
                           config=config, 
                           usuario=user_display, 
                           shipping_config=shipping_config,
                           zones=zones)


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
        print(f"Error cargando cotización {id}: {e}")
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
            df['Fecha_Registro'] = pd.to_datetime(df['Fecha_Registro'], utc=True, errors='coerce')
            df['Fecha_Registro'] = df['Fecha_Registro'].dt.tz_convert('America/Mexico_City')
            df['Fecha_Registro'] = df['Fecha_Registro'].dt.strftime('%d/%m/%Y %I:%M %p').fillna('Pendiente')
            
            if 'Fecha_Vencimiento' in df.columns:
                df['Fecha_Vencimiento'] = pd.to_datetime(df['Fecha_Vencimiento'], utc=True, errors='coerce')
                df['Fecha_Vencimiento'] = df['Fecha_Vencimiento'].dt.strftime('%d/%m/%Y').fillna('')

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
        print(f"Error exportando Excel: {e}")
        return f"Error al generar el Excel: {str(e)}", 500