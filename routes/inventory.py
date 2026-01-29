from flask import Blueprint, request, session, jsonify, render_template, redirect, url_for
from helpers import login_required
from db import get_db_connection as get_db

inventory_bp = Blueprint('inventory', __name__)

# =========================
# TEST
# =========================
@inventory_bp.route('/test')
def test_inventory():
    return 'INVENTORY OK'

# =========================
# GUARDAR RECETA (PRODUCTO)
# =========================
@inventory_bp.route('/guardar_receta', methods=['POST'])
@login_required
def guardar_receta():
    data = request.get_json(silent=True)
    if not data or not data.get('nombre'):
        return jsonify({'error': 'Datos incompletos'}), 400

    conn = get_db()
    try:
        conn.execute('BEGIN')
        # Producto
        cur = conn.execute(
            "INSERT INTO productos (user_id, nombre) VALUES (?, ?)",
            (session['user_id'], data['nombre'])
        )
        producto_id = cur.lastrowid

        # Materiales
        for m in data.get('materiales', []):
            conn.execute(
                "INSERT INTO producto_detalles (producto_id, material_id, cantidad) VALUES (?, ?, ?)",
                (producto_id, m['id'], m['cantidad'])
            )

        # Maquinaria
        for e in data.get('maquinaria', []):
            conn.execute(
                "INSERT INTO producto_maquinaria (producto_id, maquinaria_id) VALUES (?, ?)",
                (producto_id, e['id'])
            )

        conn.commit()
        return jsonify({'success': True})

    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# =========================
# VISTAS (PÁGINAS)
# =========================

# =========================
# MATERIALES (VER, CREAR, EDITAR)
# =========================
@inventory_bp.route('/materiales', methods=['GET', 'POST'])
@login_required
def materiales():
    conn = get_db()

    # Si el usuario mandó el formulario (GUARDAR o EDITAR)
    if request.method == 'POST':
        id_actualizar = request.form.get('id_actualizar')
        nombre = request.form.get('nombre')
        tipo = request.form.get('tipo_entrada') # 'unidad' o 'paquete'
        precio_compra = float(request.form.get('precio_compra') or 0)
        
        # Lógica para paquetes
        es_paquete = 1 if tipo == 'paquete' else 0
        cantidad_paquete = float(request.form.get('cantidad_paquete') or 1)
        
        # Calculamos el precio unitario automáticamente para el cotizador
        # Si es paquete de 100 hojas a $100, la unidad cuesta $1
        precio_unitario = precio_compra / cantidad_paquete if cantidad_paquete > 0 else 0

        if id_actualizar:
            # ACTUALIZAR EXISTENTE
            conn.execute("""
                UPDATE materiales 
                SET nombre=?, es_paquete=?, precio_compra=?, cantidad_paquete=?, precio_unitario=?
                WHERE id=? AND user_id=?
            """, (nombre, es_paquete, precio_compra, cantidad_paquete, precio_unitario, id_actualizar, session['user_id']))
        else:
            # INSERTAR NUEVO
            conn.execute("""
                INSERT INTO materiales (user_id, nombre, es_paquete, precio_compra, cantidad_paquete, precio_unitario)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (session['user_id'], nombre, es_paquete, precio_compra, cantidad_paquete, precio_unitario))
        
        conn.commit()
        conn.close()
        # Recargamos la página para ver los cambios
        return redirect(url_for('inventory.materiales'))

    # Si solo está viendo la página (GET)
    materiales = conn.execute("SELECT * FROM materiales WHERE user_id = ?", (session['user_id'],)).fetchall()
    conn.close()
    return render_template('materiales.html', materiales=materiales)


# Esta es la función que te faltaba y hubiera causado error
@inventory_bp.route('/materiales/eliminar/<int:id>')
@login_required
def eliminar_material(id):
    conn = get_db()
    conn.execute("DELETE FROM materiales WHERE id=? AND user_id=?", (id, session['user_id']))
    conn.commit()
    conn.close()
    return redirect(url_for('inventory.materiales'))

@inventory_bp.route('/equipos')
@login_required
def equipos():
    conn = get_db()
    equipos = conn.execute("SELECT * FROM maquinaria WHERE user_id = ?", (session['user_id'],)).fetchall()
    conn.close()
    return render_template('equipos.html', equipos=equipos)

@inventory_bp.route('/recetas')
@login_required
def recetas():
    conn = get_db()
    # Esta consulta cuenta cuántos materiales tiene cada receta para mostrarlo en la tabla
    query = """
        SELECT p.id, p.nombre, COUNT(pd.id) as num_materiales 
        FROM productos p 
        LEFT JOIN producto_detalles pd ON p.id=pd.producto_id 
        WHERE p.user_id=? 
        GROUP BY p.id
    """
    recetas = conn.execute(query, (session['user_id'],)).fetchall()
    conn.close()
    return render_template('recetas.html', recetas=recetas)

# =========================
# ELIMINAR
# =========================

@inventory_bp.route('/equipos/eliminar/<int:id>')
@login_required
def eliminar_equipo(id):
    conn = get_db()
    conn.execute('DELETE FROM maquinaria WHERE id=? AND user_id=?', (id, session['user_id']))
    conn.commit()
    conn.close()
    return redirect(url_for('inventory.equipos'))

@inventory_bp.route('/recetas/eliminar/<int:id>')
@login_required
def eliminar_receta(id):
    conn = get_db()
    try:
        conn.execute("BEGIN")
        # Borramos en orden para mantener integridad
        conn.execute("DELETE FROM producto_detalles WHERE producto_id=?", (id,))
        conn.execute("DELETE FROM producto_maquinaria WHERE producto_id=?", (id,))
        conn.execute("DELETE FROM productos WHERE id=? AND user_id=?", (id, session['user_id']))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()
    return redirect(url_for('inventory.recetas'))