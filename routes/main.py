from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file, jsonify
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta, timezone
import pandas as pd
import io
from utils.datetime_utils import now_utc, utc_to_local
from db import get_db_connection as get_db
from helpers import login_required
from helpers import subscription_required

main_bp = Blueprint('main', __name__)

# --- HELPER INTERNO PARA FORMATEAR FECHAS A LOCAL ---
def procesar_fila_fechas(fila_db):
    """
    Convierte una fila de SQLite a dict y transforma las fechas UTC 
    a hora local con formato bonito (DD/MM/YYYY HH:MM).
    """
    if not fila_db:
        return None
    
    item = dict(fila_db)
    
    campos_fecha = ['fecha', 'fecha_vencimiento', 'created_at']
    
    for campo in campos_fecha:
        valor_original = item.get(campo)
        if valor_original:
            try:
                # 1. Limpieza de formato ISO
                str_fecha = str(valor_original).replace('T', ' ')[:19]
                
                # 2. Parsear (Leer como UTC)
                dt_utc = datetime.strptime(str_fecha, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                
                # 3. Convertir a hora local
                dt_local = utc_to_local(dt_utc)
                
                # 4. Formatear
                item[campo] = dt_local.strftime('%d/%m/%Y %H:%M')
                
            except ValueError:
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

# --- GUARDAR VENTA (Lógica de Estados, Saldos e Impuestos) ---
@main_bp.route('/guardar_venta', methods=['POST'])
@login_required
def guardar_venta():
    data = request.get_json()
    conn = get_db() 
    cursor = conn.cursor()
    
    try:
        # Recuperar datos básicos
        venta_id = data.get('id')
        cliente = data.get('cliente', 'Cliente General')
        items = data.get('items', [])
        
        # Totales calculados en JS
        subtotal = data.get('subtotal', 0)
        descuento_pct = data.get('descuento_porcentaje', 0)
        descuento_monto = data.get('descuento_monto', 0)
        
        # --- NUEVOS CAMPOS DE IMPUESTOS ---
        # Recibimos el monto exacto y el porcentaje para construir el texto
        tax_amount = float(data.get('tax_amount', 0))
        tax_percent = float(data.get('tax_percent', 0))
        
        # Construimos el string descriptivo (ej: "IVA 16%" o "none")
        if tax_amount > 0:
            tax_engine = f"IVA {int(tax_percent)}%" if tax_percent.is_integer() else f"IVA {tax_percent}%"
        else:
            tax_engine = "none"
        # ----------------------------------

        total = data.get('total', 0)
        costo_total = data.get('costo_total', 0)
        estado = data.get('estado', 'pagado')
        monto_pagado = data.get('pago_inicial', total)
        
        saldo_pendiente = total - monto_pagado
        if saldo_pendiente < 0: saldo_pendiente = 0

        fecha_actual = now_utc()
        fecha_vencimiento = (now_utc() + timedelta(days=7)).isoformat()

        if venta_id:
            # =================================================
            # MODO ACTUALIZACIÓN (UPDATE)
            # =================================================
            cursor.execute('''
                UPDATE ventas 
                SET cliente=?, subtotal=?, descuento_porcentaje=?, descuento_monto=?,
                    impuestos=?, tax_engine=?,  -- <--- ACTUALIZAMOS IMPUESTOS
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
            # =================================================
            # MODO CREACIÓN (INSERT)
            # =================================================
            cursor.execute('''
                INSERT INTO ventas (
                    user_id, fecha, cliente, subtotal, 
                    descuento_porcentaje, descuento_monto, 
                    impuestos, tax_engine,  -- <--- INSERTAMOS IMPUESTOS
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

        # =================================================
        # INSERTAR DETALLES
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
    q = request.args.get('q')
    
    # Agregamos impuestos y tax_engine a la consulta para mostrarlos si se quiere
    sql = '''
        SELECT id, cliente, fecha, total, estado, saldo_pendiente, fecha_vencimiento, impuestos, tax_engine
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
    
    ventas_display = [procesar_fila_fechas(v) for v in ventas_db]
    
    return render_template('historial.html', ventas=ventas_display)


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
            empresa = request.form['nombre_empresa']
            slogan = request.form['slogan']
            website = request.form['website']

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

        conn.commit()
        conn.close()
        return redirect(url_for('main.configuracion'))

    config = conn.execute('SELECT * FROM configuracion WHERE user_id=?', (uid,)).fetchone()
    user_raw = conn.execute('SELECT * FROM usuarios WHERE id=?', (uid,)).fetchone()
    user_display = procesar_fila_fechas(user_raw)

    conn.close()
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
            v.id as Folio, 
            v.fecha as Fecha_Registro,
            v.fecha_vencimiento as Fecha_Vencimiento,
            v.cliente as Cliente, 
            v.estado as Estado_Actual,
            v.document_type as Tipo_Doc,
            
            -- Detalles
            d.concepto as Producto, 
            d.cantidad as Cantidad, 
            d.precio_unitario as Precio_Unit_Venta, 
            d.costo_unitario as Costo_Unit_Prod, 
            (d.precio_unitario - d.costo_unitario) as Ganancia_Unitaria,
            d.subtotal as Subtotal_Linea,
            d.composicion as Receta_Materiales,
            
            -- Totales
            v.subtotal as Subtotal_Venta,
            v.descuento_monto as Descuento_Aplicado,
            v.impuestos as Impuestos_Monto,   -- <--- NUEVO EN EXCEL
            v.tax_engine as Impuestos_Info,   -- <--- NUEVO EN EXCEL
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
                except:
                    pass

        output.seek(0)
        filename = f"Reporte_SianEffects_{datetime.now().strftime('%Y%m%d')}.xlsx"
        return send_file(output, download_name=filename, as_attachment=True)

    except Exception as e:
        print(f"Error exportando Excel: {e}")
        return f"Error al generar el Excel: {str(e)}", 500