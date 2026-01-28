import sys # <--- NECESARIO PARA LOGS
import traceback # <--- NECESARIO PARA VER DETALLES DEL ERROR
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from db import get_db_connection as get_db
from helpers import login_required

inventory_bp = Blueprint('inventory', __name__)

# --- RUTA DE DIAGNÓSTICO PARA GUARDAR ---
@inventory_bp.route('/guardar_receta', methods=['POST'])
@login_required
def guardar_receta():
    # 1. IMPRIMIR QUE LLEGÓ LA SOLICITUD
    print("--- [DEBUG] INICIO INTENTO GUARDAR RECETA ---", file=sys.stderr)
    
    try:
        data = request.get_json()
        print(f"--- [DEBUG] DATOS RECIBIDOS: {data}", file=sys.stderr)
        print(f"--- [DEBUG] USUARIO ID: {session.get('user_id')}", file=sys.stderr)

        if not data or 'nombre' not in data:
            print("--- [ERROR] Faltan datos obligatorios (nombre)", file=sys.stderr)
            return jsonify({'error': 'Datos incompletos'}), 400
            
        conn = get_db()
        
        # 2. INSERTAR PRODUCTO
        print(f"--- [DEBUG] Insertando producto: {data['nombre']}", file=sys.stderr)
        cursor = conn.execute('INSERT INTO productos (user_id, nombre) VALUES (?, ?)', 
                              (session['user_id'], data['nombre']))
        producto_id = cursor.lastrowid
        print(f"--- [DEBUG] Producto creado con ID: {producto_id}", file=sys.stderr)
        
        # 3. INSERTAR MATERIALES
        for mat in data.get('materiales', []):
            print(f"--- [DEBUG] Insertando material: {mat}", file=sys.stderr)
            conn.execute('INSERT INTO producto_detalles (producto_id, material_id, cantidad) VALUES (?, ?, ?)', 
                (producto_id, mat['id'], mat['cantidad']))
                
        # 4. INSERTAR MAQUINARIA
        for maq in data.get('maquinaria', []):
            print(f"--- [DEBUG] Insertando maquinaria ID: {maq}", file=sys.stderr)
            conn.execute('INSERT INTO producto_maquinaria (producto_id, maquinaria_id) VALUES (?, ?)', 
                (producto_id, maq['id'])) # OJO: Aquí el JS manda {id: 1}, verifica si llega así
                
        conn.commit()
        conn.close()
        print("--- [EXITO] Receta guardada correctamente ---", file=sys.stderr)
        return jsonify({'success': True})
        
    except Exception as e:
        # ESTO ES LO QUE QUEREMOS VER EN EL LOG
        print("!!! [ERROR FATAL] EXCEPCIÓN AL GUARDAR !!!", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr) # Imprime todo el error detallado
        if 'conn' in locals(): conn.close()
        return jsonify({'error': str(e)}), 500

# --- EL RESTO DE RUTAS SIGUE IGUAL ---
@inventory_bp.route('/materiales', methods=['GET', 'POST'])
@login_required
def materiales():
    conn = get_db()
    if request.method == 'POST':
        nombre = request.form['nombre']
        tipo = request.form['tipo_entrada']
        precio_compra = float(request.form['precio_compra'])
        cantidad = float(request.form['cantidad_paquete']) if tipo == 'paquete' else 1.0
        precio_unitario = precio_compra / cantidad if cantidad > 0 else 0
        id_act = request.form.get('id_actualizar')
        
        if id_act:
            conn.execute('UPDATE materiales SET nombre=?, tipo_entrada=?, precio_compra=?, cantidad_paquete=?, precio_unitario=? WHERE id=? AND user_id=?', 
                         (nombre, tipo, precio_compra, cantidad, precio_unitario, id_act, session['user_id']))
            flash('Material actualizado', 'success')
        else:
            conn.execute('INSERT INTO materiales (user_id, nombre, tipo_entrada, precio_compra, cantidad_paquete, precio_unitario) VALUES (?, ?, ?, ?, ?, ?)', 
                         (session['user_id'], nombre, tipo, precio_compra, cantidad, precio_unitario))
            flash('Material agregado', 'success')
        conn.commit(); conn.close()
        return redirect(url_for('inventory.materiales'))

    rows = conn.execute('SELECT * FROM materiales WHERE user_id = ? ORDER BY nombre', (session['user_id'],)).fetchall()
    mats = [dict(row) for row in rows]
    conn.close()
    return render_template('materiales.html', materiales=mats)

@inventory_bp.route('/eliminar_material/<int:id>')
@login_required
def eliminar_material(id):
    conn = get_db()
    conn.execute('DELETE FROM materiales WHERE id=? AND user_id=?', (id, session['user_id']))
    conn.commit(); conn.close()
    return redirect(url_for('inventory.materiales'))

@inventory_bp.route('/equipos', methods=('GET', 'POST'))
@login_required
def equipos():
    conn = get_db()
    uid = session['user_id']
    if request.method == 'POST':
        conn.execute('INSERT INTO maquinaria (user_id, nombre, costo_desgaste) VALUES (?, ?, ?)', (uid, request.form['nombre'], request.form['costo_desgaste']))
        conn.commit(); conn.close()
        return redirect(url_for('inventory.equipos'))
    eqs = conn.execute('SELECT * FROM maquinaria WHERE user_id=?', (uid,)).fetchall()
    conn.close()
    return render_template('equipos.html', equipos=eqs)

@inventory_bp.route('/eliminar_equipo/<int:id>')
@login_required
def eliminar_equipo(id):
    conn = get_db()
    conn.execute('DELETE FROM maquinaria WHERE id=? AND user_id=?', (id, session['user_id']))
    conn.commit(); conn.close()
    return redirect(url_for('inventory.equipos'))

@inventory_bp.route('/recetas')
@login_required
def recetas():
    conn = get_db()
    materiales = conn.execute('SELECT * FROM materiales WHERE user_id=?', (session['user_id'],)).fetchall()
    equipos = conn.execute('SELECT * FROM maquinaria WHERE user_id=?', (session['user_id'],)).fetchall()
    query = 'SELECT p.id, p.nombre, COUNT(pd.id) as num_materiales FROM productos p LEFT JOIN producto_detalles pd ON p.id=pd.producto_id WHERE p.user_id=? GROUP BY p.id'
    recetas = conn.execute(query, (session['user_id'],)).fetchall()
    conn.close()
    return render_template('recetas.html', recetas=recetas, materiales=materiales, equipos=equipos)

@inventory_bp.route('/eliminar_receta/<int:id>')
@login_required
def eliminar_receta(id):
    conn = get_db()
    conn.execute('DELETE FROM productos WHERE id=? AND user_id=?', (id, session['user_id']))
    conn.execute('DELETE FROM producto_detalles WHERE producto_id=?', (id,))
    conn.execute('DELETE FROM producto_maquinaria WHERE producto_id=?', (id,))
    conn.commit(); conn.close()
    return redirect(url_for('inventory.recetas'))