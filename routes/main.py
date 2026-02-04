from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file, jsonify
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta, timezone
import pandas as pd
import io

from db import get_db_connection as get_db
from helpers import login_required
from helpers import subscription_required

# --- HELPER INTERNO PARA FORMATEAR FECHAS A LOCAL ---
def procesar_fila_fechas(fila_db):
    """
    Convierte una fila de SQLite a dict y transforma las fechas UTC 
    a hora local con formato bonito (DD/MM/YYYY HH:MM).
    """
    if not fila_db:
        return None
    
    # Convertimos a diccionario
    item = dict(fila_db)
    
    # Campos a procesar
    campos_fecha = ['fecha', 'fecha_vencimiento', 'created_at']
    
    for campo in campos_fecha:
        valor_original = item.get(campo)
        if valor_original:
            try:
                # 1. LIMPIEZA DE FORMATO aqui
                # Convertimos a string, quitamos la 'T' si es ISO, y cortamos milisegundos
                # Ej: "2026-02-11T01:38:11.068..." -> "2026-02-11 01:38:11"
                str_fecha = str(valor_original).replace('T', ' ')[:19]
                
                # 2. Parsear (Leer la fecha como UTC)
                dt_utc = datetime.strptime(str_fecha, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                
                # 3. Convertir a hora local (Tu zona horaria)
                dt_local = utc_to_local(dt_utc)
                
                # 4. FORMATEAR: Día/Mes/Año Hora:Minuto (24h)
                # Ej: 11/02/2026 13:30
                item[campo] = dt_local.strftime('%d/%m/%Y %H:%M')
                
            except ValueError:
                # Si falla algo raro, dejamos el dato original para no romper nada
                pass 
                
    return item

@main_bp.route('/')
def index():
    return redirect(url_for('main.cotizador'))

@main_bp.route('/cotizador')
@subscription_required
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
    conn = get_db() 
    cursor = conn.cursor()
    
    try:
        # Recuperar datos básicos
        venta_id = data.get('id') # Puede venir vacío (null) o con un número
        cliente = data.get('cliente', 'Cliente General')
        items = data.get('items', [])
        
        # Totales calculados en JS (confiamos en ellos, o podrías recalcular aquí)
        subtotal = data.get('subtotal', 0)
        descuento_pct = data.get('descuento_porcentaje', 0)
        descuento_monto = data.get('descuento_monto', 0)
        total = data.get('total', 0)
        costo_total = data.get('costo_total', 0)
        estado = data.get('estado', 'pagado')
        monto_pagado = data.get('pago_inicial', total)
        
        # Calculamos saldo pendiente
        saldo_pendiente = total - monto_pagado
        if saldo_pendiente < 0: saldo_pendiente = 0

        # Lógica de Vencimiento (7 días)
        fecha_vencimiento = (now_utc() + timedelta(days=7)).isoformat()

        if venta_id:
            # =================================================
            # MODO ACTUALIZACIÓN (UPDATE)
            # =================================================
            # 1. Actualizamos la cabecera de la venta
            cursor.execute('''
                UPDATE ventas 
                SET cliente=?, subtotal=?, descuento_porcentaje=?, descuento_monto=?,
                    total=?, costo_total=?, estado=?, monto_pagado=?, saldo_pendiente=?
                WHERE id=? AND user_id=?
            ''', (
                cliente, subtotal, descuento_pct, descuento_monto,
                total, costo_total, estado, monto_pagado, saldo_pendiente,
                venta_id, session['user_id']
            ))
            
            # 2. Borramos los detalles viejos (para reescribirlos limpios)
            cursor.execute('DELETE FROM venta_detalles WHERE venta_id=?', (venta_id,))
            
        else:
            # =================================================
            # MODO CREACIÓN (INSERT)
            # =================================================
            cursor.execute('''
                INSERT INTO ventas (
                    user_id, cliente, subtotal, descuento_porcentaje, 
                    descuento_monto, total, costo_total, estado, 
                    monto_pagado, saldo_pendiente, fecha_vencimiento
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                session['user_id'], cliente, subtotal, descuento_pct, 
                descuento_monto, total, costo_total, estado, 
                monto_pagado, saldo_pendiente, fecha_vencimiento
            ))
            venta_id = cursor.lastrowid

        # =================================================
        # INSERTAR DETALLES (COMÚN PARA AMBOS)
        # =================================================
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
                float(item['precio']),
                float(item.get('costo', 0)),
                float(item['subtotal']),
                item.get('composicion', '[]')
            ))

        conn.commit()
        return jsonify({'success': True, 'ticket_id': venta_id})

    except Exception as e:
        conn.rollback()
        print(f"Error guardando venta: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

# --- ACTUALIZAR VENTA ---
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
    q = request.args.get('q')
    
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
    
    ventas_db = conn.execute(sql, params).fetchall()
    conn.close()
    
    # PROCESAR FECHAS: De UTC a Local
    ventas_display = [procesar_fila_fechas(v) for v in ventas_db]
    
    return render_template('historial.html', ventas=ventas_display)


@main_bp.route('/ticket/<int:id>')
def ver_ticket(id):
    conn = get_db()
    venta_db = conn.execute('SELECT * FROM ventas WHERE id = ?', (id,)).fetchone()
    
    if venta_db is None:
        conn.close()
        return "Ticket no encontrado", 404

    # Procesar fecha del ticket individual
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

    if request.method == 'POST':
        # Obtenemos la "acción" para saber qué formulario envió el usuario
        # En tu HTML debes tener <input type="hidden" name="action" value="update_profile"> 
        # o value="update_business" según el form.
        action = request.form.get('action')

        # =========================================================
        # SECCIÓN 1: ACTUALIZAR PERFIL DE USUARIO
        # (Username, Email, Teléfono, País y Password Opcional)
        # =========================================================
        if action == 'update_profile':
            new_username = request.form['username']
            new_email = request.form['email']
            new_phone = request.form['telefono']
            new_country = request.form.get('country_code', 'MX') # Default MX si no viene
            new_password = request.form.get('password') # Puede venir vacío

            try:
                # CASO A: El usuario escribió una nueva contraseña
                if new_password and new_password.strip() != "":
                    hashed_pw = generate_password_hash(new_password)
                    conn.execute('''
                        UPDATE usuarios 
                        SET username=?, email=?, telefono=?, country_code=?, password=? 
                        WHERE id=?
                    ''', (new_username, new_email, new_phone, new_country, hashed_pw, uid))
                    
                    flash('Perfil y contraseña actualizados.', 'success')

                # CASO B: Solo actualizamos datos, mantenemos la contraseña vieja
                else:
                    conn.execute('''
                        UPDATE usuarios 
                        SET username=?, email=?, telefono=?, country_code=? 
                        WHERE id=?
                    ''', (new_username, new_email, new_phone, new_country, uid))
                    
                    flash('Perfil actualizado correctamente.', 'success')

                # Actualizamos la sesión por si cambió el username
                session['username'] = new_username

            except Exception as e:
                # Capturamos error si el username o email ya existen en otro usuario
                print(f"Error update profile: {e}")
                flash('Error: El nombre de usuario o correo ya está en uso.', 'danger')

        # =========================================================
        # SECCIÓN SEGURIDAD (SOLO PASSWORD)
        # =========================================================
        elif action == 'update_password':
            new_password = request.form['password']
            
            if new_password and len(new_password) >= 6:
                hashed_pw = generate_password_hash(new_password)
                conn.execute('UPDATE usuarios SET password=? WHERE id=?', (hashed_pw, uid))
                flash('Contraseña actualizada. Por favor inicia sesión de nuevo.', 'success')
                # Opcional: Cerrar sesión para obligarlo a entrar con la nueva
                # session.clear()
                # return redirect(url_for('auth.login'))
            else:
                flash('La contraseña es muy corta.', 'danger')
        # =========================================================
        # SECCIÓN 2: ACTUALIZAR CONFIGURACIÓN DEL NEGOCIO
        # (Nombre empresa, Slogan, Website, Margen)
        # =========================================================
        elif action == 'update_business':
            margen = request.form['margen']
            empresa = request.form['nombre_empresa']
            slogan = request.form['slogan']
            website = request.form['website']

            # Verificamos si ya existe una configuración para hacer UPDATE o INSERT
            config_existente = conn.execute('SELECT id FROM configuracion WHERE user_id=?', (uid,)).fetchone()

            if config_existente:
                conn.execute('''
                    UPDATE configuracion
                    SET margen_ganancia=?, nombre_empresa=?, slogan=?, website=?
                    WHERE user_id=?
                ''', (margen, empresa, slogan, website, uid))
            else:
                conn.execute('''
                    INSERT INTO configuracion (user_id, margen_ganancia, nombre_empresa, slogan, website)
                    VALUES (?, ?, ?, ?, ?)
                ''', (uid, margen, empresa, slogan, website))

            flash('Datos del negocio guardados correctamente.', 'success')

        # Guardamos cambios y cerramos conexión de escritura
        conn.commit()
        conn.close()
        return redirect(url_for('main.configuracion'))

    # =========================================================
    # SECCIÓN 3: CARGA DE DATOS (GET)
    # (Para mostrar los valores actuales en los inputs)
    # =========================================================
    
    # 1. Traemos la config del negocio
    config = conn.execute('SELECT * FROM configuracion WHERE user_id=?', (uid,)).fetchone()
    
    # 2. Traemos los datos del usuario
    user_raw = conn.execute('SELECT * FROM usuarios WHERE id=?', (uid,)).fetchone()
    
    # 3. Procesamos fechas (UTC -> Local) usando tu helper 'procesar_fila_fechas'
    # Esto asegura que si muestras "Miembro desde: X", la fecha salga correcta en hora local
    user_display = procesar_fila_fechas(user_raw)

    conn.close()
    
    # Enviamos todo al template
    return render_template('configuracion.html', config=config, usuario=user_display)


@main_bp.route('/terminos')
def terminos():
    return render_template('terminos.html')

@main_bp.route('/plan_vencido')
def plan_vencido():
    return render_template('plan_vencido.html')


@main_bp.route('/descargar_excel')
@login_required
def descargar_excel():
    conn = get_db()
    uid = session['user_id']
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
    
    # Pandas lee las fechas como strings o objetos
    df = pd.read_sql_query(query, conn, params=(uid,))
    conn.close()
    
    if not df.empty:
        # Convertir columna fecha a datetime
        df['fecha'] = pd.to_datetime(df['fecha'])
        
        # Asumimos que lo que viene de BD es UTC (aunque pandas a veces no lo sabe)
        # 1. Localizamos en UTC
        # 2. Convertimos a Mexico_City
        df['fecha'] = df['fecha'].dt.tz_localize('UTC').dt.tz_convert('America/Mexico_City')
        
        # Quitamos la info de zona horaria para que Excel no se queje
        df['fecha'] = df['fecha'].dt.strftime('%Y-%m-%d %H:%M:%S')

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer: 
        df.to_excel(writer, index=False, sheet_name='Detalle Financiero')
    output.seek(0)
    
    return send_file(output, download_name="reporte_financiero_sian.xlsx", as_attachment=True)