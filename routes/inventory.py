from flask import Blueprint, render_template, request, redirect, url_for, session
from db import get_db_connection
from helpers import login_required

inventory_bp = Blueprint('inventory', __name__)

@inventory_bp.route('/materiales', methods=('GET', 'POST'))
@login_required
def materiales():
    conn = get_db_connection(); uid = session['user_id']
    if request.method == 'POST':
        n, t, p = request.form['nombre'], request.form['tipo_entrada'], float(request.form['precio_compra'])
        cant = float(request.form['cantidad_paquete']) if t == 'paquete' else 1
        if request.form.get('id_actualizar'): conn.execute('UPDATE materiales SET nombre=?, es_paquete=?, precio_compra=?, cantidad_paquete=?, precio_unitario=? WHERE id=?', (n, t=='paquete', p, cant, p/cant, request.form['id_actualizar']))
        else: conn.execute('INSERT INTO materiales (user_id, nombre, es_paquete, precio_compra, cantidad_paquete, precio_unitario) VALUES (?,?,?,?,?,?)', (uid, n, t=='paquete', p, cant, p/cant))
        conn.commit(); return redirect(url_for('inventory.materiales'))
    mats = conn.execute('SELECT * FROM materiales WHERE user_id=?', (uid,)).fetchall()
    edit = conn.execute('SELECT * FROM materiales WHERE id=?', (request.args.get('editar'),)).fetchone() if request.args.get('editar') else None
    conn.close(); return render_template('materiales.html', materiales=mats, edicion=edit)

@inventory_bp.route('/eliminar_material/<int:id>')
@login_required
def eliminar_material(id):
    conn = get_db_connection(); conn.execute('DELETE FROM materiales WHERE id=? AND user_id=?', (id, session['user_id'])); conn.commit(); conn.close(); return redirect(url_for('inventory.materiales'))

@inventory_bp.route('/equipos', methods=('GET', 'POST'))
@login_required
def equipos():
    conn = get_db_connection(); uid = session['user_id']
    if request.method == 'POST': conn.execute('INSERT INTO maquinaria (user_id, nombre, costo_desgaste) VALUES (?, ?, ?)', (uid, request.form['nombre'], request.form['costo_desgaste'])); conn.commit(); return redirect(url_for('inventory.equipos'))
    eqs = conn.execute('SELECT * FROM maquinaria WHERE user_id=?', (uid,)).fetchall(); conn.close(); return render_template('equipos.html', equipos=eqs)

@inventory_bp.route('/eliminar_equipo/<int:id>')
@login_required
def eliminar_equipo(id):
    conn = get_db_connection(); conn.execute('DELETE FROM maquinaria WHERE id=? AND user_id=?', (id, session['user_id'])); conn.commit(); conn.close(); return redirect(url_for('inventory.equipos'))

@inventory_bp.route('/recetas')
@login_required
def recetas():
    conn = get_db_connection(); r = conn.execute('SELECT p.id, p.nombre, COUNT(pd.id) as num_materiales FROM productos p LEFT JOIN producto_detalles pd ON p.id=pd.producto_id WHERE p.user_id=? GROUP BY p.id', (session['user_id'],)).fetchall(); conn.close(); return render_template('recetas.html', recetas=r)

@inventory_bp.route('/eliminar_receta/<int:id>')
@login_required
def eliminar_receta(id):
    conn = get_db_connection(); conn.execute('DELETE FROM productos WHERE id=? AND user_id=?', (id, session['user_id'])); conn.execute('DELETE FROM producto_detalles WHERE producto_id=?',(id,)); conn.execute('DELETE FROM producto_maquinaria WHERE producto_id=?',(id,)); conn.commit(); conn.close(); return redirect(url_for('inventory.recetas'))