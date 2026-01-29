from flask import Blueprint, request, session, jsonify, render_template
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
                """
                INSERT INTO producto_detalles (producto_id, material_id, cantidad)
                VALUES (?, ?, ?)
                """,
                (producto_id, m['id'], m['cantidad'])
            )

        # Maquinaria
        for e in data.get('maquinaria', []):
            conn.execute(
                """
                INSERT INTO producto_maquinaria (producto_id, maquinaria_id)
                VALUES (?, ?)
                """,
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
# MATERIALES
# =========================
@inventory_bp.route('/materiales')
@login_required
def materiales():
    conn = get_db()
    materiales = conn.execute(
        "SELECT * FROM materiales WHERE user_id = ?",
        (session['user_id'],)
    ).fetchall()
    conn.close()

    return render_template('materiales.html', materiales=materiales)


# =========================
# EQUIPOS / MAQUINARIA
# =========================
@inventory_bp.route('/equipos')
@login_required
def equipos():
    conn = get_db()
    equipos = conn.execute(
        "SELECT * FROM maquinaria WHERE user_id = ?",
        (session['user_id'],)
    ).fetchall()
    conn.close()

    return render_template('equipos.html', equipos=equipos)


@app.route('/recetas')
@login_required
def recetas():
    conn = get_db_connection(); r = conn.execute('SELECT p.id, p.nombre, COUNT(pd.id) as num_materiales FROM productos p LEFT JOIN producto_detalles pd ON p.id=pd.producto_id WHERE p.user_id=? GROUP BY p.id', (session['user_id'],)).fetchall(); conn.close(); return render_template('recetas.html', recetas=r)

@inventory_bp.route('/recetas/eliminar/<int:id>')
@login_required
def eliminar_receta(id):
    conn = get_db()
    try:
        conn.execute("BEGIN")

        conn.execute(
            "DELETE FROM producto_detalles WHERE producto_id=?",
            (id,)
        )
        conn.execute(
            "DELETE FROM producto_maquinaria WHERE producto_id=?",
            (id,)
        )
        conn.execute(
            "DELETE FROM productos WHERE id=? AND user_id=?",
            (id, session['user_id'])
        )

        conn.commit()
    finally:
        conn.close()

    return redirect(url_for('inventory.recetas'))
