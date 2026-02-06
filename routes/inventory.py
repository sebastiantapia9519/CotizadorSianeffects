from flask import Blueprint, request, session, jsonify, render_template, redirect, url_for
import json
from helpers import login_required
from db import get_db_connection as get_db

inventory_bp = Blueprint('inventory', __name__)

# =========================
# 1. TEST Y REPARACIÓN (CRUCIAL PARA QUE FUNCIONE)
# =========================
@inventory_bp.route('/test')
def test_inventory():
    return 'INVENTORY OK'

@inventory_bp.route('/reparar-materiales')
@login_required
def reparar_materiales():
    # ESTA FUNCIÓN AGREGA LAS COLUMNAS FALTANTES SIN BORRAR NADA
    conn = get_db()
    try:
        # Intentamos agregar las columnas. Si ya existen, ignoramos el error.
        columns = [
            ("es_paquete", "BOOLEAN DEFAULT 0"),
            ("precio_compra", "REAL DEFAULT 0"),
            ("cantidad_paquete", "REAL DEFAULT 1"),
            ("precio_unitario", "REAL DEFAULT 0")
        ]
        
        mensaje = "Resultado: "
        for col, tipo in columns:
            try:
                conn.execute(f"ALTER TABLE materiales ADD COLUMN {col} {tipo}")
                mensaje += f"Columna {col} agregada. "
            except Exception:
                pass # La columna ya existía
        
        conn.commit()
        return f"Base de datos sincronizada correctamente. {mensaje} <a href='/materiales'>Ir a Materiales</a>"
    except Exception as e:
        return f"Error: {e}"
    finally:
        conn.close()

# =========================
# MATERIALES
# =========================
@inventory_bp.route('/materiales', methods=['GET', 'POST'])
@login_required
def materiales():
    conn = get_db()

    if request.method == 'POST':
        try:
            id_actualizar = request.form.get('id_actualizar')
            nombre = request.form.get('nombre', '').strip()
            
            # --- BLINDAJE TOTAL (LOWER) ---
            if id_actualizar:
                # Al editar: Buscamos si existe otro material con el MISMO nombre (ignorando mayúsculas)
                duplicado = conn.execute(
                    "SELECT id FROM materiales WHERE LOWER(nombre) = LOWER(?) AND user_id = ? AND id != ?", 
                    (nombre, session['user_id'], id_actualizar)
                ).fetchone()
            else:
                # Al crear: Buscamos si existe alguno igual (ignorando mayúsculas)
                duplicado = conn.execute(
                    "SELECT id FROM materiales WHERE LOWER(nombre) = LOWER(?) AND user_id = ?", 
                    (nombre, session['user_id'])
                ).fetchone()

            if duplicado:
                conn.close()
                # Mostramos el nombre tal cual lo escribió el usuario en la alerta
                return f"""
                    <script>
                        alert('Error: Ya tienes un material llamado "{nombre}" (o similar).');
                        window.history.back();
                    </script>
                """
            # --------------------------------------

            # Convertimos a números seguros
            try:
                precio_compra = float(request.form.get('precio_compra') or 0)
                cantidad_paquete = float(request.form.get('cantidad_paquete') or 1)
            except ValueError:
                precio_compra = 0
                cantidad_paquete = 1
            
            es_paquete = 1 if tipo == 'paquete' else 0
            
            # Cálculo de precio unitario
            if cantidad_paquete > 0:
                precio_unitario = precio_compra / cantidad_paquete
            else:
                precio_unitario = 0

            if id_actualizar:
                # ACTUALIZAR
                conn.execute("""
                    UPDATE materiales 
                    SET nombre=?, es_paquete=?, precio_compra=?, cantidad_paquete=?, precio_unitario=?
                    WHERE id=? AND user_id=?
                """, (nombre, es_paquete, precio_compra, cantidad_paquete, precio_unitario, id_actualizar, session['user_id']))
            else:
                # CREAR NUEVO
                conn.execute("""
                    INSERT INTO materiales (user_id, nombre, es_paquete, precio_compra, cantidad_paquete, precio_unitario)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (session['user_id'], nombre, es_paquete, precio_compra, cantidad_paquete, precio_unitario))
            
            conn.commit()
            conn.close()
            return redirect(url_for('inventory.materiales'))
            
        except Exception as e:
            conn.close()
            return f"Error al guardar: {e}"

    # --- VER LISTA (GET) ---
    rows = conn.execute("SELECT * FROM materiales WHERE user_id = ?", (session['user_id'],)).fetchall()
    conn.close()
    materiales_lista = [dict(row) for row in rows]
    
    return render_template('materiales.html', materiales=materiales_lista)

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
            
            # --- BLINDAJE TOTAL (LOWER) ---
            duplicado = conn.execute(
                "SELECT id FROM maquinaria WHERE LOWER(nombre) = LOWER(?) AND user_id = ?", 
                (nombre, session['user_id'])
            ).fetchone()

            if duplicado:
                conn.close()
                return f"""
                    <script>
                        alert('Error: Ya tienes una maquinaria llamada "{nombre}".');
                        window.history.back();
                    </script>
                """
            # ----------------------------

            try:
                costo_desgaste = float(request.form.get('costo_desgaste') or 0)
            except ValueError:
                costo_desgaste = 0

            conn.execute("""
                INSERT INTO maquinaria (user_id, nombre, costo_desgaste)
                VALUES (?, ?, ?)
            """, (session['user_id'], nombre, costo_desgaste))
            
            conn.commit()
            conn.close()
            return redirect(url_for('inventory.equipos'))
            
        except Exception as e:
            conn.close()
            return f"Error al guardar equipo: {e}"

    # --- VER LISTA (GET) ---
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

@inventory_bp.route('/recetas', methods=['GET', 'POST'])
@login_required
def recetas():
    conn = get_db()

    if request.method == 'POST':
        try:
            id_actualizar = request.form.get('id_actualizar')
            nombre = request.form.get('nombre', '').strip()
            
            if id_actualizar and nombre:
                # --- BLINDAJE TOTAL (LOWER) ---
                duplicado = conn.execute(
                    "SELECT id FROM productos WHERE LOWER(nombre) = LOWER(?) AND user_id = ? AND id != ?", 
                    (nombre, session['user_id'], id_actualizar)
                ).fetchone()

                if duplicado:
                    conn.close()
                    return f"Error: Ya existe una receta similar a '{nombre}'"
                

                conn.execute("UPDATE productos SET nombre=? WHERE id=? AND user_id=?", 
                             (nombre, id_actualizar, session['user_id']))
                conn.commit()
            
            conn.close()
            return redirect(url_for('inventory.recetas'))
        except Exception as e:
            conn.close()
            return f"Error al actualizar: {e}"

    # --- VER LISTA (GET) ---
    try:
        query = """
            SELECT p.id, p.nombre, COUNT(pd.id) as num_materiales 
            FROM productos p 
            LEFT JOIN producto_detalles pd ON p.id=pd.producto_id 
            WHERE p.user_id=? 
            GROUP BY p.id
        """
        recetas = conn.execute(query, (session['user_id'],)).fetchall()
    except Exception:
        recetas = []
        
    conn.close()
    
    # Truco de diccionario para el JSON del botón editar
    recetas_lista = [dict(row) for row in recetas]

    return render_template('recetas.html', recetas=recetas_lista)

@inventory_bp.route('/guardar_receta', methods=['POST'])
@login_required
def guardar_receta():
    data = request.get_json(silent=True)
    nombre_receta = data.get('nombre', '').strip()

    if not data or not nombre_receta:
        return jsonify({'error': 'Datos incompletos'}), 400

    conn = get_db()
    try:
        # --- BLINDAJE TOTAL (LOWER) ---
        # Comparamos minúsculas con minúsculas
        duplicado = conn.execute(
            "SELECT id FROM productos WHERE LOWER(nombre) = LOWER(?) AND user_id = ?",
            (nombre_receta, session['user_id'])
        ).fetchone()

        if duplicado:
            return jsonify({'error': f'Ya existe una receta llamada "{nombre_receta}".'}), 400
        # ------------------------------

        conn.execute('BEGIN')
        # 1. Convertimos la lista de materiales a Texto JSON
        lista_materiales = data.get('materiales', [])
        items_json = json.dumps(lista_materiales)
        
        # 2. Guardamos en la tabla 'productos'
        cur = conn.execute("""
            INSERT INTO productos (user_id, nombre, items) 
            VALUES (?, ?, ?)
        """, (session['user_id'], nombre_receta, items_json))
        
        pid = cur.lastrowid

        # 3. Guardado en tablas relacionales (detalle y maquinaria)
        for m in data.get('materiales', []):
            conn.execute("INSERT INTO producto_detalles (producto_id, material_id, cantidad) VALUES (?, ?, ?)", (pid, m['id'], m['cantidad']))
            
        for e in data.get('maquinaria', []):
            conn.execute("INSERT INTO producto_maquinaria (producto_id, maquinaria_id) VALUES (?, ?)", (pid, e['id']))

        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        print(f"Error guardando receta: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@inventory_bp.route('/recetas/eliminar/<int:id>')
@login_required
def eliminar_receta(id):
    conn = get_db()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM producto_detalles WHERE producto_id=?", (id,))
        conn.execute("DELETE FROM producto_maquinaria WHERE producto_id=?", (id,))
        conn.execute("DELETE FROM productos WHERE id=? AND user_id=?", (id, session['user_id']))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()
    return redirect(url_for('inventory.recetas'))

    @inventory_bp.route('/api/registrar_compra', methods=['POST'])
@login_required
def registrar_compra():
    data = request.get_json()
    material_id = data.get('id')
    cantidad_compra = float(data.get('cantidad', 0))
    nuevo_precio = float(data.get('nuevo_precio', 0)) # Opcional: Actualizar costo al comprar
    
    if not material_id or cantidad_compra <= 0:
        return jsonify({'success': False, 'error': 'Datos inválidos'}), 400

    conn = get_db()
    try:
        # 1. Obtener datos actuales del material
        mat = conn.execute("SELECT * FROM materiales WHERE id=? AND user_id=?", (material_id, session['user_id'])).fetchone()
        if not mat:
            return jsonify({'success': False, 'error': 'Material no encontrado'}), 404

        # 2. Calcular cuánto sumar al stock (Conversión de Unidades)
        # Si es paquete (ej. 1 paquete de 100 hojas), y compras 2, sumamos 200 hojas.
        cantidad_a_sumar = cantidad_compra
        if mat['es_paquete'] and mat['cantidad_paquete'] > 1:
            cantidad_a_sumar = cantidad_compra * mat['cantidad_paquete']

        # 3. Actualizar Stock y Precio (si cambió)
        sql_update = "UPDATE materiales SET stock_actual = stock_actual + ?"
        params = [cantidad_a_sumar]

        if nuevo_precio > 0:
            sql_update += ", precio_compra = ?"
            params.append(nuevo_precio)
            
            # Recalcular precio unitario si cambia el precio de compra
            if mat['cantidad_paquete'] > 0:
                nuevo_unitario = nuevo_precio / mat['cantidad_paquete']
                sql_update += ", precio_unitario = ?"
                params.append(nuevo_unitario)

        sql_update += " WHERE id = ?"
        params.append(material_id)

        conn.execute(sql_update, params)

        # 4. Registrar en Historial (Movimientos)
        conn.execute("""
            INSERT INTO movimientos_inventario (user_id, material_id, tipo, cantidad, motivo, stock_resultante)
            VALUES (?, ?, 'entrada', ?, 'Compra / Ajuste', (SELECT stock_actual FROM materiales WHERE id=?))
        """, (session['user_id'], material_id, cantidad_a_sumar, material_id))

        conn.commit()
        return jsonify({'success': True})

    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()