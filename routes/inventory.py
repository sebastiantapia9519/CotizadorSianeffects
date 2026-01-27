from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from db import get_db  # Usaremos get_db para todo, es más estándar
from helpers import login_required

inventory_bp = Blueprint('inventory', __name__)

# ---------------------------------------------------
# 1. GESTIÓN DE MATERIALES
# ---------------------------------------------------
@inventory_bp.route('/materiales', methods=['GET', 'POST'])
@login_required
def materiales():
    conn = get_db()
    
    if request.method == 'POST':
        nombre = request.form['nombre']
        tipo = request.form['tipo_entrada'] # 'unidad' o 'paquete'
        precio_compra = float(request.form['precio_compra'])
        
        cantidad = 1.0
        if tipo == 'paquete':
            cantidad = float(request.form['cantidad_paquete'])
            
        precio_unitario = precio_compra / cantidad if cantidad > 0 else 0
        
        # Revisar si es actualización o nuevo
        id_act = request.form.get('id_actualizar')
        
        if id_act: # ACTUALIZAR
            conn.execute('''
                UPDATE materiales 
                SET nombre=?, tipo_entrada=?, precio_compra=?, cantidad_paquete=?, precio_unitario=?
                WHERE id=? AND usuario_id=?
            ''', (nombre, tipo, precio_compra, cantidad, precio_unitario, id_act, session['user_id']))
            flash('Material actualizado correctamente', 'success')
        else: # NUEVO
            conn.execute('''
                INSERT INTO materiales (usuario_id, nombre, tipo_entrada, precio_compra, cantidad_paquete, precio_unitario)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (session['user_id'], nombre, tipo, precio_compra, cantidad, precio_unitario))
            flash('Material agregado correctamente', 'success')
            
        conn.commit()
        conn.close()
        return redirect(url_for('inventory.materiales'))

    # --- GET: Obtener lista ---
    rows = conn.execute('SELECT * FROM materiales WHERE usuario_id = ? ORDER BY nombre', (session['user_id'],)).fetchall()
    
    # CONVERTIR ROWS A DICCIONARIOS PARA QUE NO FALLE EL JSON
    mats = [dict(row) for row in rows]
    
    conn.close()
    return render_template('materiales.html', materiales=mats)

@inventory_bp.route('/eliminar_material/<int:id>')
@login_required
def eliminar_material(id):
    conn = get_db()
    # Corregido: Usamos 'usuario_id' para ser consistentes con la tabla materiales
    conn.execute('DELETE FROM materiales WHERE id=? AND usuario_id=?', (id, session['user_id']))
    conn.commit()
    conn.close()
    return redirect(url_for('inventory.materiales'))

# ---------------------------------------------------
# 2. GESTIÓN DE EQUIPOS (MAQUINARIA)
# ---------------------------------------------------
@inventory_bp.route('/equipos', methods=('GET', 'POST'))
@login_required
def equipos():
    conn = get_db()
    uid = session['user_id']
    
    if request.method == 'POST':
        conn.execute('INSERT INTO maquinaria (user_id, nombre, costo_desgaste) VALUES (?, ?, ?)', 
                     (uid, request.form['nombre'], request.form['costo_desgaste']))
        conn.commit()
        conn.close() # Importante cerrar si rediriges
        return redirect(url_for('inventory.equipos'))
    
    eqs = conn.execute('SELECT * FROM maquinaria WHERE user_id=?', (uid,)).fetchall()
    conn.close()
    return render_template('equipos.html', equipos=eqs)

@inventory_bp.route('/eliminar_equipo/<int:id>')
@login_required
def eliminar_equipo(id):
    conn = get_db()
    conn.execute('DELETE FROM maquinaria WHERE id=? AND user_id=?', (id, session['user_id']))
    conn.commit()
    conn.close()
    return redirect(url_for('inventory.equipos'))

# ---------------------------------------------------
# 3. GESTIÓN DE RECETAS
# ---------------------------------------------------
@inventory_bp.route('/recetas')
@login_required
def recetas():
    conn = get_db()
    # Consulta un poco compleja, mejor dejarla clara
    query = '''
        SELECT p.id, p.nombre, COUNT(pd.id) as num_materiales 
        FROM productos p 
        LEFT JOIN producto_detalles pd ON p.id=pd.producto_id 
        WHERE p.user_id=? 
        GROUP BY p.id
    '''
    r = conn.execute(query, (session['user_id'],)).fetchall()
    conn.close()
    return render_template('recetas.html', recetas=r)

@inventory_bp.route('/eliminar_receta/<int:id>')
@login_required
def eliminar_receta(id):
    conn = get_db()
    # Borrado en cascada manual (por seguridad)
    conn.execute('DELETE FROM productos WHERE id=? AND user_id=?', (id, session['user_id']))
    conn.execute('DELETE FROM producto_detalles WHERE producto_id=?', (id,))
    conn.execute('DELETE FROM producto_maquinaria WHERE producto_id=?', (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('inventory.recetas'))