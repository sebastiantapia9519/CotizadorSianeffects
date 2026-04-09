from flask import Blueprint, request, session, jsonify, render_template, redirect, url_for, current_app
import json
from helpers import login_required
from db import get_db_connection as get_db
from utils.datetime_utils import now_utc, ahora_sql

inventory_bp = Blueprint('inventory', __name__)

# ==========================================
# GESTIÓN DE MATERIALES (INVENTARIO)
# ==========================================
@inventory_bp.route('/materiales', methods=['GET', 'POST'])
@login_required
def materiales():
    conn = get_db()
    user_id = session['user_id']

    # --- LÓGICA PARA GUARDAR O EDITAR (MÉTODO POST) ---
    if request.method == 'POST':
        try:
            # Recibimos los datos del formulario del modal
            id_actualizar = request.form.get('id_actualizar')
            nombre = request.form.get('nombre', '').strip()
            tipo = request.form.get('tipo_entrada', 'paquete')
            unidad_medida = request.form.get('unidad_medida', 'pieza') 
            
            # Validación de duplicados: revisamos que el nombre no exista ya para este usuario
            if id_actualizar:
                duplicado = conn.execute(
                    "SELECT id FROM materiales WHERE LOWER(nombre) = LOWER(?) AND user_id = ? AND id != ?", 
                    (nombre, user_id, id_actualizar)
                ).fetchone()
            else:
                duplicado = conn.execute(
                    "SELECT id FROM materiales WHERE LOWER(nombre) = LOWER(?) AND user_id = ?", 
                    (nombre, user_id)
                ).fetchone()

            if duplicado:
                conn.close()
                return f"""
                <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
                <script>
                    window.onload = function() {{
                        Swal.fire({{
                            icon: 'error',
                            title: 'Material duplicado',
                            text: 'Error: Ya tienes un material llamado "{nombre}".',
                            confirmButtonColor: '#ff4757'
                        }}).then((result) => {{
                            window.history.back();
                        }});
                    }};
                </script>
                """

            # Conversión de valores numéricos con manejo de errores
            try:
                precio_compra = float(request.form.get('precio_compra') or 0)
                cantidad_paquete = float(request.form.get('cantidad_paquete') or 1)
            except ValueError:
                precio_compra = 0
                cantidad_paquete = 1
            
            # Calculamos el precio unitario (Costo total / Cantidad del paquete)
            es_paquete = 1 if tipo == 'paquete' else 0
            if cantidad_paquete > 0:
                precio_unitario = precio_compra / cantidad_paquete
            else:
                precio_unitario = 0

            # Ejecutamos la actualización si existe ID, o la inserción si es nuevo
            if id_actualizar:
                conn.execute("""
                    UPDATE materiales 
                    SET nombre=?, es_paquete=?, precio_compra=?, cantidad_paquete=?, precio_unitario=?, unidad_medida=?
                    WHERE id=? AND user_id=?
                """, (nombre, es_paquete, precio_compra, cantidad_paquete, precio_unitario, unidad_medida, id_actualizar, user_id))
            else:
                conn.execute("""
                    INSERT INTO materiales (user_id, nombre, es_paquete, precio_compra, cantidad_paquete, precio_unitario, unidad_medida)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (user_id, nombre, es_paquete, precio_compra, cantidad_paquete, precio_unitario, unidad_medida))
            
            conn.commit()
            
            # Redirigimos a la misma página para ver los cambios
            return redirect(url_for('inventory.materiales'))
            
        except Exception as e:
            return f"Error al guardar: {e}"
        finally:
            conn.close()

    # --- LÓGICA PARA CARGAR LA VISTA (MÉTODO GET) ---
    
    # 1. Obtenemos todos los materiales del usuario
    rows = conn.execute("SELECT * FROM materiales WHERE user_id = ?", (user_id,)).fetchall()
    materiales_lista = [dict(row) for row in rows]
    
    # 2. CARGA DE CONFIGURACIÓN (Solución al error del nombre en el Navbar)
    # Buscamos la configuración para pasar el nombre de la empresa y el estado del inventario
    config_row = conn.execute("SELECT * FROM configuracion WHERE user_id = ?", (user_id,)).fetchone()
    
    # Si no existe configuración, creamos un diccionario por defecto para evitar errores en el layout
    if config_row:
        config_dict = dict(config_row)
    else:
        # Si el SELECT falla, usamos el company_name de la tabla usuarios o uno genérico
        user_info = conn.execute("SELECT company_name FROM usuarios WHERE id = ?", (user_id,)).fetchone()
        config_dict = {
            'nombre_empresa': user_info['company_name'] if user_info['company_name'] else 'Mi Negocio',
            'inventario_activo': 0
        }
    
    conn.close()

    # Renderizamos la plantilla enviando los materiales y el objeto config (Navbar)
    return render_template('materiales.html', 
                           materiales=materiales_lista, 
                           config=config_dict)

@inventory_bp.route('/materiales/eliminar/<int:id>')
@login_required
def eliminar_material(id):
    conn = get_db()
    conn.execute("DELETE FROM materiales WHERE id=? AND user_id=?", (id, session['user_id']))
    conn.commit()
    conn.close()
    return redirect(url_for('inventory.materiales'))

# =========================
# 3. EQUIPOS 
# =========================
@inventory_bp.route('/equipos', methods=['GET', 'POST'])
@login_required
def equipos():
    conn = get_db()
    if request.method == 'POST':
        try:
            nombre = request.form.get('nombre', '').strip()
            duplicado = conn.execute("SELECT id FROM maquinaria WHERE LOWER(nombre) = LOWER(?) AND user_id = ?", (nombre, session['user_id'])).fetchone()
            if duplicado:
                conn.close()
                return f"""
                <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
                <script>
                    window.onload = function() {{
                        Swal.fire({{
                            icon: 'error',
                            title: 'Maquinaria duplicada',
                            text: 'Error: Ya existe maquinaria con el nombre "{nombre}".',
                            confirmButtonColor: '#ff4757'
                        }}).then((result) => {{
                            window.history.back();
                        }});
                    }};
                </script>
                """

            try:
                costo_desgaste = float(request.form.get('costo_desgaste') or 0)
            except ValueError:
                costo_desgaste = 0

            conn.execute("INSERT INTO maquinaria (user_id, nombre, costo_desgaste) VALUES (?, ?, ?)", (session['user_id'], nombre, costo_desgaste))
            conn.commit()
            conn.close()
            return redirect(url_for('inventory.equipos'))
        except Exception as e:
            conn.close()
            return f"Error al guardar equipo: {e}"

    rows = conn.execute("SELECT * FROM maquinaria WHERE user_id = ?", (session['user_id'],)).fetchall()
    conn.close()
    equipos_lista = [dict(row) for row in rows]
    return render_template('equipos.html', equipos=equipos_lista)

@inventory_bp.route('/equipos/eliminar/<int:id>')
@login_required
def eliminar_equipo(id):
    conn = get_db()
    conn.execute('DELETE FROM maquinaria WHERE id=? AND user_id=?', (id, session['user_id']))
    conn.commit()
    conn.close()
    return redirect(url_for('inventory.equipos'))

# =========================
# 4. RECETAS
# =========================
@inventory_bp.route('/recetas', methods=['GET', 'POST'])
@login_required
def recetas():
    conn = get_db()
    if request.method == 'POST':
        try:
            id_actualizar = request.form.get('id_actualizar')
            nombre = request.form.get('nombre', '').strip()
            
            if id_actualizar and nombre:
                duplicado = conn.execute("SELECT id FROM productos WHERE LOWER(nombre) = LOWER(?) AND user_id = ? AND id != ?", (nombre, session['user_id'], id_actualizar)).fetchone()
                if duplicado:
                    conn.close()
                    return f"Error: Ya existe una receta llamada '{nombre}'"

                conn.execute("UPDATE productos SET nombre=? WHERE id=? AND user_id=?", (nombre, id_actualizar, session['user_id']))
                conn.commit()
            conn.close()
            return redirect(url_for('inventory.recetas'))
        except Exception as e:
            conn.close()
            return f"Error al actualizar: {e}"

    try:
        # 1. Consulta principal (ya no necesitamos traer p.items)
        query = """SELECT p.id, p.nombre, COUNT(pd.id) as num_materiales 
                   FROM productos p 
                   LEFT JOIN producto_detalles pd ON p.id=pd.producto_id 
                   WHERE p.user_id=? 
                   GROUP BY p.id"""
        recetas_db = conn.execute(query, (session['user_id'],)).fetchall()
        
        recetas_lista = []
        for r in recetas_db:
            receta_dict = dict(r)
            
            # 2. Buscamos los nombres reales cruzando detalles con la tabla materiales
            detalles = conn.execute("""
                SELECT m.nombre, pd.cantidad 
                FROM producto_detalles pd
                JOIN materiales m ON pd.material_id = m.id
                WHERE pd.producto_id = ?
            """, (receta_dict['id'],)).fetchall()
            
            # 3. Empaquetamos el resultado en un nuevo JSON llamado 'ingredientes_reales'
            receta_dict['ingredientes_reales'] = json.dumps([dict(d) for d in detalles])
            recetas_lista.append(receta_dict)
            
    except Exception as e:
        current_app.logger.error(f"RECIPE_LOAD_ERROR: Fallo al cargar recetas para usuario {session.get('user_id')} - {e}")
        recetas_lista = []
        
    conn.close()
    return render_template('recetas.html', recetas=recetas_lista)


@inventory_bp.route('/guardar_receta', methods=['POST'])
@login_required
def guardar_receta():
    data = request.get_json(silent=True)
    nombre_receta = data.get('nombre', '').strip() if data else ''
    if not data or not nombre_receta:
        return jsonify({'error': 'Datos incompletos'}), 400

    conn = get_db()
    try:
        duplicado = conn.execute("SELECT id FROM productos WHERE LOWER(nombre) = LOWER(?) AND user_id = ?", (nombre_receta, session['user_id'])).fetchone()
        if duplicado:
            return jsonify({'error': f'Ya existe una receta llamada "{nombre_receta}".'}), 400

        # POSTGRES READY: Quitamos el conn.execute('BEGIN') explícito porque el conector ya maneja la transacción.
        
        items_json = json.dumps(data.get('materiales', []))
        cur = conn.execute("INSERT INTO productos (user_id, nombre, items) VALUES (?, ?, ?)", (session['user_id'], nombre_receta, items_json))
        
        # --- 🚨 NOTA DE MIGRACIÓN A POSTGRESQL 🚨 ---
        # Cambiar el INSERT a: "... VALUES (?, ?, ?) RETURNING id"
        # Y esta línea a: pid = cur.fetchone()[0]
        pid = cur.lastrowid

        for m in data.get('materiales', []):
            conn.execute("INSERT INTO producto_detalles (producto_id, material_id, cantidad) VALUES (?, ?, ?)", (pid, m['id'], float(m.get('cantidad', 0))))
        for e in data.get('maquinaria', []):
            conn.execute("INSERT INTO producto_maquinaria (producto_id, maquinaria_id) VALUES (?, ?)", (pid, e['id']))

        conn.commit()
        current_app.logger.info(f"RECIPE_CREATED: Usuario {session.get('user_id')} creó la receta '{nombre_receta}' (ID: {pid})")
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"RECIPE_SAVE_ERROR: Usuario {session.get('user_id')} falló al guardar receta - {e}") 
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@inventory_bp.route('/recetas/eliminar/<int:id>')
@login_required
def eliminar_receta(id):
    conn = get_db()
    try:
        # POSTGRES READY: Quitamos el 'BEGIN'
        conn.execute("DELETE FROM producto_detalles WHERE producto_id=?", (id,))
        conn.execute("DELETE FROM producto_maquinaria WHERE producto_id=?", (id,))
        conn.execute("DELETE FROM productos WHERE id=? AND user_id=?", (id, session['user_id']))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()
    return redirect(url_for('inventory.recetas'))

# =========================
# 5. REGISTRAR COMPRA (STOCK)
# =========================
@inventory_bp.route('/api/registrar_compra', methods=['POST'])
@login_required
def registrar_compra():
    data = request.get_json()
    material_id = data.get('id')
    
    # 1. BLINDAJE NUMÉRICO: Evitar que metan basura
    try:
        cantidad_compra = float(data.get('cantidad', 0))
        nuevo_precio = float(data.get('nuevo_precio', 0))
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'Cantidad o precio inválidos'}), 400
    
    # 2. ANTI-HACK: No pueden comprar cantidades negativas ni precios negativos
    if not material_id or cantidad_compra <= 0 or nuevo_precio < 0:
        return jsonify({'success': False, 'error': 'Datos inválidos o negativos'}), 400

    conn = get_db()
    try:
        mat = conn.execute("SELECT * FROM materiales WHERE id=? AND user_id=?", (material_id, session['user_id'])).fetchone()
        if not mat:
            return jsonify({'success': False, 'error': 'Material no encontrado'}), 404

        # 3. LÓGICA DE PAQUETES: Multiplicador seguro
        cantidad_a_sumar = cantidad_compra
        if mat['es_paquete'] and mat['cantidad_paquete'] > 0: # <-- BLINDAJE DIVISIÓN POR CERO
            cantidad_a_sumar = cantidad_compra * mat['cantidad_paquete']

        sql_update = "UPDATE materiales SET stock_actual = stock_actual + ?"
        params = [cantidad_a_sumar]

        # 4. ACTUALIZACIÓN DE PRECIOS SINCRONIZADA
        if nuevo_precio > 0:
            sql_update += ", precio_compra = ?"
            params.append(nuevo_precio)
            
            # Si es paquete y tiene cantidad válida, calculamos el unitario
            if mat['es_paquete'] and mat['cantidad_paquete'] > 0:
                nuevo_unitario = nuevo_precio / mat['cantidad_paquete']
            else:
                # Si no es paquete (se vende por pieza), el precio unitario es el mismo de compra
                nuevo_unitario = nuevo_precio
                
            sql_update += ", precio_unitario = ?"
            params.append(nuevo_unitario)

        sql_update += " WHERE id = ?"
        params.append(material_id)

        conn.execute(sql_update, params)

        # 5. REGISTRO DE HISTORIAL SEGURO
        try:
            # Usamos ahora_sql() en lugar de now_utc() directo para compatibilidad con el formato
            conn.execute("""
                INSERT INTO movimientos_inventario 
                (user_id, material_id, tipo, cantidad, motivo, stock_resultante, fecha)
                VALUES (?, ?, 'entrada', ?, 'Compra / Ajuste',
                    (SELECT stock_actual FROM materiales WHERE id=?),
                    ?
                )
            """, (
                session['user_id'],
                material_id,
                cantidad_a_sumar,
                material_id,
                ahora_sql()
            ))
        except Exception as aud_error:
            # Imprimimos el error de auditoría para depuración sin tumbar la compra
            current_app.logger.warning(f"INVENTORY_AUDIT_WARNING: No se pudo registrar historial para material {material_id} - {aud_error}")
            pass 

        conn.commit()
        current_app.logger.info(f"STOCK_ADDED: Usuario {session.get('user_id')} ingresó {cantidad_a_sumar} unidades al material ID {material_id}")
        return jsonify({'success': True})

    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()