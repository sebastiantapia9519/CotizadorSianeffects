from flask import Blueprint, request, session, jsonify, render_template, redirect, url_for, current_app
import json
from helpers import login_required
from db import get_db_connection as get_db
from utils.datetime_utils import now_utc, ahora_sql

# Importamos las herramientas del tutorial
from utils.tutorial_utils import debe_mostrar_tutorial, obtener_version_tutorial

inventory_bp = Blueprint('inventory', __name__)

# ==========================================
# GESTIÓN DE MATERIALES (INVENTARIO)
# ==========================================
@inventory_bp.route('/materiales', methods=['GET', 'POST'])
@login_required
def materiales():
    user_id = session['user_id']

    # --- LÓGICA PARA GUARDAR O EDITAR (MÉTODO POST) ---
    if request.method == 'POST':
        conn = get_db()
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
            
            try:
                stock_min_val = request.form.get('stock_minimo')
                stock_minimo = float(stock_min_val) if stock_min_val else 5.0
            except ValueError:
                stock_minimo = 5.0
            
            # Calculamos el precio unitario
            es_paquete = 1 if tipo == 'paquete' else 0
            precio_unitario = (precio_compra / cantidad_paquete) if cantidad_paquete > 0 else 0

            if id_actualizar:
                conn.execute("""
                    UPDATE materiales 
                    SET nombre=?, es_paquete=?, precio_compra=?, cantidad_paquete=?, precio_unitario=?, unidad_medida=?, stock_minimo=?
                    WHERE id=? AND user_id=?
                """, (nombre, es_paquete, precio_compra, cantidad_paquete, precio_unitario, unidad_medida, stock_minimo, id_actualizar, user_id))
            else:
                conn.execute("""
                    INSERT INTO materiales (user_id, nombre, es_paquete, precio_compra, cantidad_paquete, precio_unitario, unidad_medida, stock_minimo)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (user_id, nombre, es_paquete, precio_compra, cantidad_paquete, precio_unitario, unidad_medida, stock_minimo))
            
            conn.commit()
            return redirect(url_for('inventory.materiales'))
            
        except Exception as e:
            current_app.logger.error(f"MATERIAL_SAVE_ERROR: Fallo al guardar material para usuario {user_id} - {e}")
            return f"Error al guardar: {e}"
        finally:
            conn.close()

    # --- LÓGICA PARA CARGAR LA VISTA (MÉTODO GET) ---
    conn = get_db()
    rows = conn.execute("SELECT * FROM materiales WHERE user_id = ?", (user_id,)).fetchall()
    materiales_lista = [dict(row) for row in rows]
    
    config_row = conn.execute("SELECT * FROM configuracion WHERE user_id = ?", (user_id,)).fetchone()
    if config_row:
        config_dict = dict(config_row)
    else:
        user_info = conn.execute("SELECT company_name FROM usuarios WHERE id = ?", (user_id,)).fetchone()
        config_dict = {
            'nombre_empresa': user_info['company_name'] if user_info['company_name'] else 'Mi Negocio',
            'inventario_activo': 0
        }
    conn.close()

    # 💡 LÓGICA DEL TUTORIAL
    mostrar_tour = debe_mostrar_tutorial(user_id, 'materiales')
    version_tour = obtener_version_tutorial('materiales')

    return render_template('materiales.html', 
                           materiales=materiales_lista, 
                           config=config_dict,
                           mostrar_tour=mostrar_tour,
                           version_tour=version_tour)

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
    user_id = session['user_id']
    if request.method == 'POST':
        conn = get_db()
        try:
            id_actualizar = request.form.get('id_actualizar')
            nombre = request.form.get('nombre', '').strip()
            
            if id_actualizar:
                duplicado = conn.execute("SELECT id FROM maquinaria WHERE LOWER(nombre) = LOWER(?) AND user_id = ? AND id != ?", (nombre, user_id, id_actualizar)).fetchone()
            else:
                duplicado = conn.execute("SELECT id FROM maquinaria WHERE LOWER(nombre) = LOWER(?) AND user_id = ?", (nombre, user_id)).fetchone()
            
            if duplicado:
                conn.close()
                return f"""
                <body style="background-color: #f8f9fa;">
                <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
                <script>
                    window.onload = function() {{
                        Swal.fire({{
                            icon: 'error',
                            title: 'Maquinaria duplicada',
                            text: 'Error: Ya existe maquinaria con el nombre "{nombre}".',
                            confirmButtonColor: '#ff4a5a',
                            borderRadius: '16px'
                        }}).then((result) => {{
                            window.history.back();
                        }});
                    }};
                </script>
                </body>
                """

            try:
                costo_desgaste = float(request.form.get('costo_desgaste') or 0)
            except ValueError:
                costo_desgaste = 0

            if id_actualizar:
                conn.execute("UPDATE maquinaria SET nombre=?, costo_desgaste=? WHERE id=? AND user_id=?", 
                             (nombre, costo_desgaste, id_actualizar, user_id))
            else:
                conn.execute("INSERT INTO maquinaria (user_id, nombre, costo_desgaste) VALUES (?, ?, ?)", 
                             (user_id, nombre, costo_desgaste))
            
            conn.commit()
            return redirect(url_for('inventory.equipos', guardado='true'))
            
        except Exception as e:
            current_app.logger.error(f"EQUIPMENT_SAVE_ERROR: Fallo al guardar equipo para usuario {user_id} - {e}")
            return f"Error al procesar equipo: {e}"
        finally:
            conn.close()

    # --- MÉTODO GET ---
    conn = get_db()
    rows = conn.execute("SELECT * FROM maquinaria WHERE user_id = ?", (user_id,)).fetchall()
    conn.close()
    equipos_lista = [dict(row) for row in rows]

    # 💡 LÓGICA DEL TUTORIAL
    mostrar_tour = debe_mostrar_tutorial(user_id, 'equipos')
    version_tour = obtener_version_tutorial('equipos')

    return render_template('equipos.html', 
                           equipos=equipos_lista,
                           mostrar_tour=mostrar_tour,
                           version_tour=version_tour)

@inventory_bp.route('/equipos/eliminar/<int:id>')
@login_required
def eliminar_equipo(id):
    conn = get_db()
    conn.execute('DELETE FROM maquinaria WHERE id=? AND user_id=?', (id, session['user_id']))
    conn.commit()
    conn.close()
    return redirect(url_for('inventory.equipos', eliminado='true'))


# =========================
# 4. RECETAS
# =========================
@inventory_bp.route('/recetas', methods=['GET', 'POST'])
@login_required
def recetas():
    user_id = session['user_id']
    if request.method == 'POST':
        conn = get_db()
        try:
            id_actualizar = request.form.get('id_actualizar')
            nombre = request.form.get('nombre', '').strip()
            
            if id_actualizar and nombre:
                duplicado = conn.execute("SELECT id FROM productos WHERE LOWER(nombre) = LOWER(?) AND user_id = ? AND id != ?", (nombre, user_id, id_actualizar)).fetchone()
                if duplicado:
                    conn.close()
                    return f"Error: Ya existe una receta llamada '{nombre}'"

                conn.execute("UPDATE productos SET nombre=? WHERE id=? AND user_id=?", (nombre, id_actualizar, user_id))
                conn.commit()
            
            return redirect(url_for('inventory.recetas', renombrada='true'))
        except Exception as e:
            current_app.logger.error(f"RECIPE_SAVE_ERROR: Fallo al guardar receta para usuario {user_id} - {e}")
            return f"Error al actualizar: {e}"
        finally:
            conn.close()

    # --- MÉTODO GET ---
    conn = get_db()
    try:
        query = """SELECT p.id, p.nombre, COUNT(pd.id) as num_materiales 
                   FROM productos p 
                   LEFT JOIN producto_detalles pd ON p.id=pd.producto_id 
                   WHERE p.user_id=? 
                   GROUP BY p.id"""
        recetas_db = conn.execute(query, (user_id,)).fetchall()
        
        recetas_lista = []
        for r in recetas_db:
            receta_dict = dict(r)
            detalles = conn.execute("""
                SELECT m.nombre, pd.cantidad 
                FROM producto_detalles pd
                JOIN materiales m ON pd.material_id = m.id
                WHERE pd.producto_id = ?
            """, (receta_dict['id'],)).fetchall()
            
            receta_dict['ingredientes_reales'] = json.dumps([dict(d) for d in detalles])
            recetas_lista.append(receta_dict)
            
    except Exception as e:
        current_app.logger.error(f"RECIPE_LOAD_ERROR: Fallo al cargar recetas para usuario {user_id} - {e}")
        recetas_lista = []
    finally:
        conn.close()

    # 💡 LÓGICA DEL TUTORIAL
    mostrar_tour = debe_mostrar_tutorial(user_id, 'recetas')
    version_tour = obtener_version_tutorial('recetas')

    return render_template('recetas.html', 
                           recetas=recetas_lista,
                           mostrar_tour=mostrar_tour,
                           version_tour=version_tour)

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

        items_json = json.dumps(data.get('materiales', []))
        cur = conn.execute("INSERT INTO productos (user_id, nombre, items) VALUES (?, ?, ?)", (session['user_id'], nombre_receta, items_json))
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
        conn.execute("DELETE FROM producto_detalles WHERE producto_id=?", (id,))
        conn.execute("DELETE FROM producto_maquinaria WHERE producto_id=?", (id,))
        conn.execute("DELETE FROM productos WHERE id=? AND user_id=?", (id, session['user_id']))
        conn.commit()
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"RECIPE_DELETE_ERROR: Fallo al eliminar receta para usuario {session.get('user_id')} - {e}")
    finally:
        conn.close()
    return redirect(url_for('inventory.recetas', eliminada='true'))

# =========================
# 5. REGISTRAR COMPRA (STOCK)
# =========================
@inventory_bp.route('/api/registrar_compra', methods=['POST'])
@login_required
def registrar_compra():
    data = request.get_json()
    material_id = data.get('id')
    
    try:
        cantidad_compra = float(data.get('cantidad', 0))
        nuevo_precio = float(data.get('nuevo_precio', 0))
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'Cantidad o precio inválidos'}), 400
    
    if not material_id or cantidad_compra <= 0 or nuevo_precio < 0:
        return jsonify({'success': False, 'error': 'Datos inválidos o negativos'}), 400

    conn = get_db()
    try:
        mat = conn.execute("SELECT * FROM materiales WHERE id=? AND user_id=?", (material_id, session['user_id'])).fetchone()
        if not mat:
            return jsonify({'success': False, 'error': 'Material no encontrado'}), 404

        tipo_ingreso = data.get('tipo_ingreso', 'paquete')

        if tipo_ingreso == 'paquete' and mat['es_paquete'] and mat['cantidad_paquete'] > 0:
            cantidad_a_sumar = cantidad_compra * mat['cantidad_paquete']
        else:
            cantidad_a_sumar = cantidad_compra 

        sql_update = "UPDATE materiales SET stock_actual = stock_actual + ?"
        params = [cantidad_a_sumar]

        if nuevo_precio > 0:
            nuevo_unitario = nuevo_precio / cantidad_a_sumar
            if tipo_ingreso == 'paquete':
                sql_update += ", precio_compra = ?"
                params.append(nuevo_precio)
                
            sql_update += ", precio_unitario = ?"
            params.append(nuevo_unitario)

        sql_update += " WHERE id = ?"
        params.append(material_id)

        conn.execute(sql_update, params)

        try:
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
            current_app.logger.warning(f"INVENTORY_AUDIT_WARNING: No se pudo registrar historial para material {material_id} - {aud_error}")
            pass 

        conn.commit()
        current_app.logger.info(f"STOCK_ADDED: Usuario {session.get('user_id')} ingresó {cantidad_a_sumar} unidades al material ID {material_id}")
        return jsonify({'success': True})

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"STOCK_ADD_ERROR: Fallo al agregar stock para usuario {session.get('user_id')} - {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

# =========================
# 6. REGISTRAR MERMA (AJUSTE NEGATIVO)
# =========================
@inventory_bp.route('/api/registrar_merma', methods=['POST'])
@login_required
def registrar_merma():
    data = request.get_json()
    material_id = data.get('id')
    
    try:
        cantidad_merma = float(data.get('cantidad', 0))
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'Cantidad inválida'}), 400
    
    if not material_id or cantidad_merma <= 0:
        return jsonify({'success': False, 'error': 'Datos inválidos o cantidad negativa'}), 400

    conn = get_db()
    try:
        mat = conn.execute("SELECT stock_actual FROM materiales WHERE id=? AND user_id=?", 
                           (material_id, session['user_id'])).fetchone()
        
        if not mat:
            return jsonify({'success': False, 'error': 'Material no encontrado'}), 404
        
        if mat['stock_actual'] < cantidad_merma:
            return jsonify({'success': False, 'error': f"Stock insuficiente. Tienes {mat['stock_actual']}"}), 400

        conn.execute("UPDATE materiales SET stock_actual = stock_actual - ? WHERE id = ?", 
                     (cantidad_merma, material_id))

        try:
            conn.execute("""
                INSERT INTO movimientos_inventario 
                (user_id, material_id, tipo, cantidad, motivo, stock_resultante, fecha)
                VALUES (?, ?, 'salida', ?, 'Merma / Ajuste Manual',
                    (SELECT stock_actual FROM materiales WHERE id=?),
                    ?
                )
            """, (
                session['user_id'],
                material_id,
                cantidad_merma,
                material_id,
                ahora_sql()
            ))
        except Exception as aud_error:
            current_app.logger.warning(f"MERMA_AUDIT_WARNING: {aud_error}")
            pass 

        conn.commit()
        return jsonify({'success': True, 'nuevo_stock': mat['stock_actual'] - cantidad_merma})

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"MERMA_SAVE_ERROR: Fallo al guardar merma para usuario {session.get('user_id')} - {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

# =========================
# 7. OBTENER HISTORIAL OPTIMIZADO (Paginación)
# =========================
@inventory_bp.route('/api/inventario/historial', methods=['GET'])
@login_required
def obtener_historial():
    offset = request.args.get('offset', 0, type=int)
    limit = request.args.get('limit', 20, type=int)
    
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT mi.tipo, mi.cantidad, mi.motivo, mi.stock_resultante, mi.fecha, 
                   m.nombre as material_nombre, m.unidad_medida
            FROM movimientos_inventario mi
            JOIN materiales m ON mi.material_id = m.id
            WHERE mi.user_id = ?
            ORDER BY mi.fecha DESC
            LIMIT ? OFFSET ?
        """, (session['user_id'], limit, offset)).fetchall()
        
        return jsonify([dict(row) for row in rows])
    except Exception as e:
        current_app.logger.error(f"HISTORIAL_LOAD_ERROR: Fallo al cargar historial para usuario {session.get('user_id')} - {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()