from flask import flash
from flask import Blueprint, request, session, jsonify, render_template, redirect, url_for, current_app
import json
from helpers import login_required, subscription_required
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
    u_name = session.get('username', 'Anonimo')

    # --- LÓGICA PARA GUARDAR O EDITAR (MÉTODO POST) ---
    if request.method == 'POST':
        if session.get('role', 0) < 1 and not session.get('is_pro_active'):
            return jsonify({"status": "error", "message": "Acceso de Solo Lectura. Renueva tu plan PRO para modificar materiales.", "code": "PRO_REQUIRED"}), 403

        # REGLA DEL DÍA DE GRACIA
        if session.get('grace_period'):
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT role FROM usuarios WHERE id = %s", (user_id,))
            user = cursor.fetchone()
            
            if user and user['role'] < 1:
                flash(f'Disfruta tus últimas horas gratis. Tu suscripción finalizó ayer.', "warning")
                session.pop('grace_period', None)
            cursor.close()
            conn.close()

        conn = get_db()
        cursor = conn.cursor()
        try:
            # Recibimos los datos del formulario del modal
            id_actualizar = request.form.get('id_actualizar')
            nombre = request.form.get('nombre', '').strip()
            tipo = request.form.get('tipo_entrada', 'paquete')
            unidad_medida = request.form.get('unidad_medida', 'pieza') 
            
            # Validación de duplicados: revisamos que el nombre no exista ya para este usuario
            if id_actualizar:
                cursor.execute(
                    "SELECT id FROM materiales WHERE LOWER(nombre) = LOWER(%s) AND user_id = %s AND id != %s", 
                    (nombre, user_id, id_actualizar)
                )
            else:
                cursor.execute(
                    "SELECT id FROM materiales WHERE LOWER(nombre) = LOWER(%s) AND user_id = %s", 
                    (nombre, user_id)
                )
            
            duplicado = cursor.fetchone()

            if duplicado:
                return jsonify({
                    "status": "error", 
                    "message": f'Ya tienes un material llamado "{nombre}".'
                }), 400

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
            
            # Calculamos el precio unitario y ajustamos booleano para Postgres
            es_paquete = True if tipo == 'paquete' else False
            precio_unitario = (precio_compra / cantidad_paquete) if cantidad_paquete > 0 else 0

            if id_actualizar:
                cursor.execute("""
                    UPDATE materiales 
                    SET nombre=%s, es_paquete=%s, precio_compra=%s, cantidad_paquete=%s, precio_unitario=%s, unidad_medida=%s, stock_minimo=%s
                    WHERE id=%s AND user_id=%s
                """, (nombre, es_paquete, precio_compra, cantidad_paquete, precio_unitario, unidad_medida, stock_minimo, id_actualizar, user_id))
                
                # --- NUEVO: REGISTRO ADMIN ---
                cursor.execute("INSERT INTO logs_actividad (user_id, accion, modulo) VALUES (%s, %s, %s)", 
                               (user_id, f"Actualizó material '{nombre}'", "Inventario"))
                
            else:
                cursor.execute("""
                    INSERT INTO materiales (user_id, nombre, es_paquete, precio_compra, cantidad_paquete, precio_unitario, unidad_medida, stock_minimo)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (user_id, nombre, es_paquete, precio_compra, cantidad_paquete, precio_unitario, unidad_medida, stock_minimo))
                
                # --- NUEVO: REGISTRO ADMIN ---
                cursor.execute("INSERT INTO logs_actividad (user_id, accion, modulo) VALUES (%s, %s, %s)", 
                               (user_id, f"Creó el material '{nombre}'", "Inventario"))
            
            conn.commit()
            return jsonify({"status": "success", "message": "Material guardado."})
            
        except Exception as e:
            conn.rollback()
            current_app.logger.error(f"MATERIAL_SAVE_ERROR: Usuario '{u_name}' (ID: {user_id}) fallo al guardar material - {e}")
            return jsonify({"status": "error", "message": str(e)}), 500
        finally:
            cursor.close()
            conn.close()

    # --- LÓGICA PARA CARGAR LA VISTA (MÉTODO GET) ---
    conn = get_db()
    cursor = conn.cursor()
    # Ordenamos alfabéticamente ignorando mayúsculas/minúsculas
    cursor.execute("SELECT * FROM materiales WHERE user_id = %s ORDER BY LOWER(nombre) ASC", (user_id,))
    rows = cursor.fetchall()
    materiales_lista = [dict(row) for row in rows]
    
    cursor.execute("SELECT * FROM configuracion WHERE user_id = %s", (user_id,))
    config_row = cursor.fetchone()
    
    if config_row:
        config_dict = dict(config_row)
    else:
        cursor.execute("SELECT company_name FROM usuarios WHERE id = %s", (user_id,))
        user_info = cursor.fetchone()
        config_dict = {
            'nombre_empresa': user_info['company_name'] if user_info['company_name'] else 'Mi Negocio',
            'inventario_activo': False
        }
    cursor.close()
    conn.close()

    # LOG DE ACCESO
    current_app.logger.info(f"DATA_ACCESS: Usuario '{u_name}' (ID: {user_id}) consulto el catalogo de materiales")

    # LÓGICA DEL TUTORIAL
    mostrar_tour = debe_mostrar_tutorial(user_id, 'materiales')
    version_tour = obtener_version_tutorial('materiales')

    return render_template('materiales.html', 
                           materiales=materiales_lista, 
                           config=config_dict,
                           mostrar_tour=mostrar_tour,
                           version_tour=version_tour)

@inventory_bp.route('/materiales/eliminar/<int:id>')
@subscription_required
def eliminar_material(id):
    user_id = session['user_id']
    conn = get_db()
    cursor = conn.cursor()
    try:
        # 1. Buscar dependencias
        cursor.execute("""
            SELECT p.nombre FROM productos p
            JOIN producto_detalles pd ON p.id = pd.producto_id
            WHERE pd.material_id = %s AND p.user_id = %s
        """, (id, user_id))
        recetas = cursor.fetchall()

        if recetas:
            # En lugar de devolver un script, devolvemos JSON
            return jsonify({
                "status": "blocked",
                "recetas": [r['nombre'] for r in recetas]
            })

        # 2. Borrar si no hay dependencias
        cursor.execute("DELETE FROM materiales WHERE id=%s AND user_id=%s", (id, user_id))
        conn.commit()
        return jsonify({"status": "success"})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# =========================
# 3. EQUIPOS 
# =========================
@inventory_bp.route('/equipos', methods=['GET', 'POST'])
@login_required
def equipos():
    user_id = session['user_id']
    u_name = session.get('username', 'Anonimo')
    
    if request.method == 'POST':
        if session.get('role', 0) < 1 and not session.get('is_pro_active'):
            return jsonify({"status": "error", "message": "Acceso de Solo Lectura. Renueva tu plan PRO para modificar equipos.", "code": "PRO_REQUIRED"}), 403
            
        conn = get_db()
        cursor = conn.cursor()
        try:
            id_actualizar = request.form.get('id_actualizar')
            nombre = request.form.get('nombre', '').strip()
            
            if id_actualizar:
                cursor.execute("SELECT id FROM maquinaria WHERE LOWER(nombre) = LOWER(%s) AND user_id = %s AND id != %s", (nombre, user_id, id_actualizar))
            else:
                cursor.execute("SELECT id FROM maquinaria WHERE LOWER(nombre) = LOWER(%s) AND user_id = %s", (nombre, user_id))
            
            duplicado = cursor.fetchone()
            
            if duplicado:
                return jsonify({"status": "error", "message": f'Ya existe maquinaria con el nombre "{nombre}".'}), 400

            try:
                costo_desgaste = float(request.form.get('costo_desgaste') or 0)
            except ValueError:
                costo_desgaste = 0

            if id_actualizar:
                cursor.execute("UPDATE maquinaria SET nombre=%s, costo_desgaste=%s WHERE id=%s AND user_id=%s", 
                             (nombre, costo_desgaste, id_actualizar, user_id))
                
                # --- NUEVO: REGISTRO ADMIN ---
                cursor.execute("INSERT INTO logs_actividad (user_id, accion, modulo) VALUES (%s, %s, %s)", 
                               (user_id, f"Actualizó equipo '{nombre}'", "Equipos"))
            else:
                cursor.execute("INSERT INTO maquinaria (user_id, nombre, costo_desgaste) VALUES (%s, %s, %s)", 
                             (user_id, nombre, costo_desgaste))
                             
                # --- NUEVO: REGISTRO ADMIN ---
                cursor.execute("INSERT INTO logs_actividad (user_id, accion, modulo) VALUES (%s, %s, %s)", 
                               (user_id, f"Registró el equipo '{nombre}'", "Equipos"))
            
            conn.commit()
            return jsonify({"status": "success", "message": "Equipo guardado correctamente."})
            
        except Exception as e:
            conn.rollback()
            current_app.logger.error(f"EQUIPMENT_SAVE_ERROR: Usuario '{u_name}' (ID: {user_id}) fallo al guardar equipo - {e}")
            return jsonify({"status": "error", "message": f"Error: {str(e)}"}), 500
        finally:
            cursor.close()
            conn.close()

    # --- MÉTODO GET ---
    conn = get_db()
    cursor = conn.cursor()
    # Ordenamos la maquinaria alfabéticamente
    cursor.execute("SELECT * FROM maquinaria WHERE user_id = %s ORDER BY LOWER(nombre) ASC", (user_id,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    equipos_lista = [dict(row) for row in rows]

    current_app.logger.info(f"DATA_ACCESS: Usuario '{u_name}' (ID: {user_id}) consulto catalogo de equipos")

    # LÓGICA DEL TUTORIAL
    mostrar_tour = debe_mostrar_tutorial(user_id, 'equipos')
    version_tour = obtener_version_tutorial('equipos')

    return render_template('equipos.html', 
                           equipos=equipos_lista,
                           mostrar_tour=mostrar_tour,
                           version_tour=version_tour)

@inventory_bp.route('/equipos/eliminar/<int:id>')
@subscription_required
def eliminar_equipo(id):
    user_id = session['user_id']
    conn = get_db()
    cursor = conn.cursor()
    try:
        # 1. BUSCAR DEPENDENCIAS (En producto_maquinaria)
        cursor.execute("""
            SELECT p.nombre 
            FROM productos p
            JOIN producto_maquinaria pm ON p.id = pm.producto_id
            WHERE pm.maquinaria_id = %s AND p.user_id = %s
        """, (id, user_id))
        
        recetas_usando_equipo = cursor.fetchall()

        # 2. SI HAY DEPENDENCIAS: Bloqueamos y mandamos JSON
        if recetas_usando_equipo:
            return jsonify({
                "status": "blocked",
                "recetas": [r['nombre'] for r in recetas_usando_equipo]
            })

        # 3. SI NO HAY DEPENDENCIAS: Borrado normal
        cursor.execute('DELETE FROM maquinaria WHERE id=%s AND user_id=%s', (id, user_id))
        conn.commit()
        return jsonify({"status": "success"})
        
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"EQUIPMENT_DELETE_ERROR: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        cursor.close()
        conn.close()
# =========================
# 4. RECETAS
# =========================
@inventory_bp.route('/recetas', methods=['GET', 'POST'])
@login_required
def recetas():
    user_id = session['user_id']
    u_name = session.get('username', 'Anonimo')
    
    if request.method == 'POST':
        if session.get('role', 0) < 1 and not session.get('is_pro_active'):
            from flask import flash
            flash("Acceso de Solo Lectura. Renueva tu plan PRO para modificar recetas.", "warning")
            return redirect(url_for('inventory.recetas'))
            
        conn = get_db()
        cursor = conn.cursor()
        try:
            id_actualizar = request.form.get('id_actualizar')
            nombre = request.form.get('nombre', '').strip()
            
            if id_actualizar and nombre:
                cursor.execute("SELECT id FROM productos WHERE LOWER(nombre) = LOWER(%s) AND user_id = %s AND id != %s", (nombre, user_id, id_actualizar))
                duplicado = cursor.fetchone()
                if duplicado:
                    cursor.close()
                    conn.close()
                    return f"Error: Ya existe una receta llamada '{nombre}'"

                cursor.execute("UPDATE productos SET nombre=%s WHERE id=%s AND user_id=%s", (nombre, id_actualizar, user_id))
                conn.commit()
                current_app.logger.info(f"RECIPE_RENAMED: Usuario '{u_name}' (ID: {user_id}) renombro receta #{id_actualizar} a '{nombre}'")
            
            return redirect(url_for('inventory.recetas', renombrada='true'))
        except Exception as e:
            conn.rollback()
            current_app.logger.error(f"RECIPE_SAVE_ERROR: Usuario '{u_name}' (ID: {user_id}) fallo al renombrar receta - {e}")
            return f"Error al actualizar: {e}"
        finally:
            cursor.close()
            conn.close()

    # --- MÉTODO GET ---
    conn = get_db()
    cursor = conn.cursor()
    try:
        query = """SELECT p.id, p.nombre, COUNT(pd.id) as num_materiales 
                   FROM productos p 
                   LEFT JOIN producto_detalles pd ON p.id=pd.producto_id 
                   WHERE p.user_id=%s 
                   GROUP BY p.id
                   ORDER BY LOWER(p.nombre) ASC"""
        cursor.execute(query, (user_id,))
        recetas_db = cursor.fetchall()
        
        recetas_lista = []
        for r in recetas_db:
            receta_dict = dict(r)
            cursor.execute("""
                SELECT m.nombre, pd.cantidad 
                FROM producto_detalles pd
                JOIN materiales m ON pd.material_id = m.id
                WHERE pd.producto_id = %s
            """, (receta_dict['id'],))
            detalles = cursor.fetchall()
            
            receta_dict['ingredientes_reales'] = json.dumps([dict(d) for d in detalles])
            recetas_lista.append(receta_dict)
            
        # Ordenamos los materiales disponibles para que sea fácil encontrarlos al armar recetas
        cursor.execute("SELECT id, nombre, precio_unitario FROM materiales WHERE user_id = %s ORDER BY LOWER(nombre) ASC", (user_id,))
        materiales_disponibles = [dict(row) for row in cursor.fetchall()]

        # Ordenamos la maquinaria disponible
        cursor.execute("SELECT id, nombre, costo_desgaste FROM maquinaria WHERE user_id = %s ORDER BY LOWER(nombre) ASC", (user_id,))
        maquinaria_disponible = [dict(row) for row in cursor.fetchall()]
            
    except Exception as e:
        current_app.logger.error(f"RECIPE_LOAD_ERROR: Usuario '{u_name}' (ID: {user_id}) fallo al cargar recetas - {e}")
        recetas_lista = []
        materiales_disponibles = [] # Evitamos que crashee el template si hay error
        maquinaria_disponible = []
    finally:
        cursor.close()  # <-- Aquí se cerraba antes de tiempo
        conn.close()

    current_app.logger.info(f"DATA_ACCESS: Usuario '{u_name}' (ID: {user_id}) consulto catalogo de recetas")

    # LÓGICA DEL TUTORIAL
    mostrar_tour = debe_mostrar_tutorial(user_id, 'recetas')
    version_tour = obtener_version_tutorial('recetas')

    return render_template('recetas.html', 
                           recetas=recetas_lista,
                           materiales_disponibles=materiales_disponibles,
                           maquinaria_disponible=maquinaria_disponible,
                           mostrar_tour=mostrar_tour,
                           version_tour=version_tour)

@inventory_bp.route('/guardar_receta', methods=['POST'])
@subscription_required
def guardar_receta():
    user_id = session['user_id']
    u_name = session.get('username', 'Anonimo')
    
    data = request.get_json(silent=True)
    nombre_receta = data.get('nombre', '').strip() if data else ''
    id_receta = data.get('id', None) # <--- NUEVO: Recibimos el ID si estamos editando
    
    if not data or not nombre_receta:
        return jsonify({'error': 'Datos incompletos'}), 400

    conn = get_db()
    cursor = conn.cursor()
    try:
        # Validar duplicados (excluyendo la receta actual si la estamos editando)
        if id_receta:
            cursor.execute("SELECT id FROM productos WHERE LOWER(nombre) = LOWER(%s) AND user_id = %s AND id != %s", (nombre_receta, user_id, id_receta))
        else:
            cursor.execute("SELECT id FROM productos WHERE LOWER(nombre) = LOWER(%s) AND user_id = %s", (nombre_receta, user_id))
            
        duplicado = cursor.fetchone()
        if duplicado:
            return jsonify({'error': f'Ya existe una receta llamada "{nombre_receta}".'}), 400

        items_json = json.dumps(data.get('materiales', []))
        
        if id_receta:
            # MODO EDICIÓN: Actualizamos la tabla principal y borramos detalles viejos
            cursor.execute("UPDATE productos SET nombre=%s, items=%s WHERE id=%s AND user_id=%s", 
                           (nombre_receta, items_json, id_receta, user_id))
            pid = id_receta
            cursor.execute("DELETE FROM producto_detalles WHERE producto_id=%s", (pid,))
            cursor.execute("DELETE FROM producto_maquinaria WHERE producto_id=%s", (pid,))
            accion_log = f"Editó la receta '{nombre_receta}'"
        else:
            # MODO CREACIÓN: Insertamos la receta nueva
            cursor.execute("INSERT INTO productos (user_id, nombre, items) VALUES (%s, %s, %s) RETURNING id", 
                           (user_id, nombre_receta, items_json))
            pid = cursor.fetchone()['id']
            accion_log = f"Creó la receta '{nombre_receta}'"

        # Insertar los materiales y maquinarias (aplica tanto para edición como creación)
        for m in data.get('materiales', []):
            cursor.execute("INSERT INTO producto_detalles (producto_id, material_id, cantidad) VALUES (%s, %s, %s)", 
                           (pid, m['id'], float(m.get('cantidad', 0))))
        for e in data.get('maquinaria', []):
            cursor.execute("INSERT INTO producto_maquinaria (producto_id, maquinaria_id) VALUES (%s, %s)", 
                           (pid, e['id']))

        cursor.execute("INSERT INTO logs_actividad (user_id, accion, modulo) VALUES (%s, %s, %s)", 
                       (user_id, accion_log, "Recetas"))

        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"RECIPE_SAVE_ERROR: Usuario '{u_name}' fallo al guardar/editar receta - {e}") 
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@inventory_bp.route('/recetas/eliminar/<int:id>')
@subscription_required
def eliminar_receta(id):
    user_id = session['user_id']
    u_name = session.get('username', 'Anonimo')
    conn = get_db()
    cursor = conn.cursor()
    try:
        # Limpieza de dependencias
        cursor.execute("DELETE FROM producto_detalles WHERE producto_id=%s", (id,))
        cursor.execute("DELETE FROM producto_maquinaria WHERE producto_id=%s", (id,))
        cursor.execute("DELETE FROM productos WHERE id=%s AND user_id=%s", (id, user_id))
        conn.commit()
        # Retornamos éxito en JSON para que el JS lo maneje
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"RECIPE_DELETE_ERROR: Usuario '{u_name}' (ID: {user_id}) fallo al eliminar receta #{id} - {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# =========================
# 5. REGISTRAR COMPRA (STOCK)
# =========================
@inventory_bp.route('/api/registrar_compra', methods=['POST'])
@subscription_required
def registrar_compra():
    user_id = session['user_id']
    u_name = session.get('username', 'Anonimo')
    
    data = request.get_json()
    material_id = data.get('id')
    
    try:
        cantidad_compra = float(data.get('cantidad', 0))
        nuevo_precio = float(data.get('nuevo_precio', 0))
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'Cantidad o precio invalidos'}), 400
    
    if not material_id or cantidad_compra <= 0 or nuevo_precio < 0:
        return jsonify({'success': False, 'error': 'Datos invalidos o negativos'}), 400

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM materiales WHERE id=%s AND user_id=%s", (material_id, user_id))
        mat = cursor.fetchone()
        if not mat:
            return jsonify({'success': False, 'error': 'Material no encontrado'}), 404

        tipo_ingreso = data.get('tipo_ingreso', 'paquete')

        if tipo_ingreso == 'paquete' and mat['es_paquete'] and mat['cantidad_paquete'] > 0:
            cantidad_a_sumar = cantidad_compra * mat['cantidad_paquete']
        else:
            cantidad_a_sumar = cantidad_compra 

        # Construcción dinámica con %s
        sql_update = "UPDATE materiales SET stock_actual = stock_actual + %s"
        params = [cantidad_a_sumar]

        # --- CÓDIGO CORREGIDO ---
        if nuevo_precio > 0:
            if tipo_ingreso == 'paquete':
                # Si el ingreso es por paquete, dividimos el precio entre lo que trae UN paquete (no el total de la compra)
                cantidad_por_paquete = mat['cantidad_paquete'] if mat['cantidad_paquete'] > 0 else 1
                nuevo_unitario = nuevo_precio / cantidad_por_paquete
                
                sql_update += ", precio_compra = %s, precio_unitario = %s"
                params.extend([nuevo_precio, nuevo_unitario])
            else:
                # Si el ingreso es por pieza suelta, el precio que pones ES el costo unitario
                nuevo_unitario = nuevo_precio
                sql_update += ", precio_unitario = %s"
                params.append(nuevo_unitario)

        sql_update += " WHERE id = %s"
        params.append(material_id)

        cursor.execute(sql_update, params)

        try:
            cursor.execute("""
                INSERT INTO movimientos_inventario 
                (user_id, material_id, tipo, cantidad, motivo, stock_resultante, fecha)
                VALUES (%s, %s, 'entrada', %s, 'Compra / Ajuste',
                    (SELECT stock_actual FROM materiales WHERE id=%s),
                    %s
                )
            """, (
                user_id,
                material_id,
                cantidad_a_sumar,
                material_id,
                ahora_sql()
            ))
        except Exception as aud_error:
            current_app.logger.warning(f"INVENTORY_AUDIT_WARNING: Usuario '{u_name}' (ID: {user_id}) sin historial para mat #{material_id} - {aud_error}")
            pass 

        conn.commit()
        current_app.logger.info(f"STOCK_ADDED: Usuario '{u_name}' (ID: {user_id}) ingreso {cantidad_a_sumar} unidades al material #{material_id}")
        return jsonify({'success': True})

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"STOCK_ADD_ERROR: Usuario '{u_name}' (ID: {user_id}) fallo al agregar stock - {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# =========================
# 6. REGISTRAR MERMA (AJUSTE NEGATIVO)
# =========================
@inventory_bp.route('/api/registrar_merma', methods=['POST'])
@subscription_required
def registrar_merma():
    user_id = session['user_id']
    u_name = session.get('username', 'Anonimo')
    
    data = request.get_json()
    material_id = data.get('id')
    
    try:
        cantidad_merma = float(data.get('cantidad', 0))
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'Cantidad invalida'}), 400
    
    if not material_id or cantidad_merma <= 0:
        return jsonify({'success': False, 'error': 'Datos invalidos o cantidad negativa'}), 400

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT stock_actual FROM materiales WHERE id=%s AND user_id=%s", 
                       (material_id, user_id))
        mat = cursor.fetchone()
        
        if not mat:
            return jsonify({'success': False, 'error': 'Material no encontrado'}), 404
        
        if mat['stock_actual'] < cantidad_merma:
            return jsonify({'success': False, 'error': f"Stock insuficiente. Tienes {mat['stock_actual']}"}), 400

        cursor.execute("UPDATE materiales SET stock_actual = stock_actual - %s WHERE id = %s", 
                       (cantidad_merma, material_id))

        try:
            cursor.execute("""
                INSERT INTO movimientos_inventario 
                (user_id, material_id, tipo, cantidad, motivo, stock_resultante, fecha)
                VALUES (%s, %s, 'salida', %s, 'Merma / Ajuste Manual',
                    (SELECT stock_actual FROM materiales WHERE id=%s),
                    %s
                )
            """, (
                user_id,
                material_id,
                cantidad_merma,
                material_id,
                ahora_sql()
            ))
        except Exception as aud_error:
            current_app.logger.warning(f"MERMA_AUDIT_WARNING: Usuario '{u_name}' (ID: {user_id}) sin historial para mat #{material_id} - {aud_error}")
            pass 

        conn.commit()
        
        current_app.logger.info(f"STOCK_REMOVED: Usuario '{u_name}' (ID: {user_id}) registro merma de {cantidad_merma} unidades en material #{material_id}")
        return jsonify({'success': True, 'nuevo_stock': mat['stock_actual'] - cantidad_merma})

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"MERMA_SAVE_ERROR: Usuario '{u_name}' (ID: {user_id}) fallo al guardar merma - {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# =========================
# 7. OBTENER HISTORIAL OPTIMIZADO (Paginación)
# =========================
@inventory_bp.route('/api/inventario/historial', methods=['GET'])
@login_required
def obtener_historial():
    user_id = session['user_id']
    u_name = session.get('username', 'Anonimo')
    
    offset = request.args.get('offset', 0, type=int)
    limit = request.args.get('limit', 20, type=int)
    
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT mi.tipo, mi.cantidad, mi.motivo, mi.stock_resultante, mi.fecha, 
                   m.nombre as material_nombre, m.unidad_medida
            FROM movimientos_inventario mi
            JOIN materiales m ON mi.material_id = m.id
            WHERE mi.user_id = %s
            ORDER BY mi.fecha DESC
            LIMIT %s OFFSET %s
        """, (user_id, limit, offset))
        rows = cursor.fetchall()
        
        current_app.logger.info(f"DATA_ACCESS: Usuario '{u_name}' (ID: {user_id}) consulto el historial de inventario (offset: {offset})")
        return jsonify([dict(row) for row in rows])
        
    except Exception as e:
        current_app.logger.error(f"HISTORIAL_LOAD_ERROR: Usuario '{u_name}' (ID: {user_id}) fallo al cargar historial de inventario - {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()