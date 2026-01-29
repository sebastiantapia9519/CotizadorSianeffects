from flask import Blueprint, request, session, jsonify, render_template, redirect, url_for
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
# MATERIALES (VER, CREAR, EDITAR)
# =========================
@inventory_bp.route('/materiales', methods=['GET', 'POST'])
@login_required
def materiales():
    conn = get_db()

    # --- GUARDAR O EDITAR ---
    if request.method == 'POST':
        try:
            id_actualizar = request.form.get('id_actualizar')
            nombre = request.form.get('nombre')
            tipo = request.form.get('tipo_entrada') # 'unidad' o 'paquete'
            
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
    # Aquí es donde leía la base de datos
    rows = conn.execute("SELECT * FROM materiales WHERE user_id = ?", (session['user_id'],)).fetchall()
    conn.close()

    # TRUCO DE MAGIA: Convertimos las 'Rows' a 'Diccionarios' reales de Python
    # Esto arregla el error "Object of type Row is not JSON serializable"
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
# 3. EQUIPOS Y RECETAS (El resto de tu código)
# =========================
@inventory_bp.route('/equipos')
@login_required
def equipos():
    conn = get_db()
    equipos = conn.execute("SELECT * FROM maquinaria WHERE user_id = ?", (session['user_id'],)).fetchall()
    conn.close()
    return render_template('equipos.html', equipos=equipos)

@inventory_bp.route('/equipos/eliminar/<int:id>')
@login_required
def eliminar_equipo(id):
    conn = get_db()
    conn.execute('DELETE FROM maquinaria WHERE id=? AND user_id=?', (id, session['user_id']))
    conn.commit()
    conn.close()
    return redirect(url_for('inventory.equipos'))

@inventory_bp.route('/recetas')
@login_required
def recetas():
    conn = get_db()
    # Usamos try/except para compatibilidad con nombre de tabla
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
        # Fallback si la tabla se llama recetas
        recetas = conn.execute("SELECT *, 0 as num_materiales FROM recetas WHERE user_id=?", (session['user_id'],)).fetchall()
        
    conn.close()
    
    # IMPORTANTE: Enviamos materiales y equipos para que el modal de recetas funcione
    conn = get_db()
    try:
        mats = conn.execute("SELECT * FROM materiales WHERE user_id=?", (session['user_id'],)).fetchall()
        eqs = conn.execute("SELECT * FROM maquinaria WHERE user_id=?", (session['user_id'],)).fetchall()
    except:
        mats = []
        eqs = []
    conn.close()

    return render_template('recetas.html', recetas=recetas, materiales=mats, equipos=eqs)

@inventory_bp.route('/guardar_receta', methods=['POST'])
@login_required
def guardar_receta():
    data = request.get_json(silent=True)
    if not data or not data.get('nombre'):
        return jsonify({'error': 'Datos incompletos'}), 400

    conn = get_db()
    try:
        conn.execute('BEGIN')
        cur = conn.execute("INSERT INTO productos (user_id, nombre) VALUES (?, ?)", (session['user_id'], data['nombre']))
        pid = cur.lastrowid

        for m in data.get('materiales', []):
            conn.execute("INSERT INTO producto_detalles (producto_id, material_id, cantidad) VALUES (?, ?, ?)", (pid, m['id'], m['cantidad']))
            
        for e in data.get('maquinaria', []):
            conn.execute("INSERT INTO producto_maquinaria (producto_id, maquinaria_id) VALUES (?, ?)", (pid, e['id']))

        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
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