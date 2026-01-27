from flask import Blueprint, jsonify, session
from db import get_db_connection as get_db

api_bp = Blueprint('api', __name__)

# 1. OBTENER UN SOLO MATERIAL (Para cuando seleccionas del dropdown)
@api_bp.route('/material/<int:id>')
def obtener_material(id):
    if 'user_id' not in session:
        return jsonify({'error': 'No autorizado'}), 401

    conn = get_db()
    # Usamos user_id (inglés) que es el correcto en tu base de datos
    material = conn.execute('SELECT * FROM materiales WHERE id = ? AND user_id = ?', 
                            (id, session['user_id'])).fetchone()
    conn.close()
    
    if material:
        return jsonify({
            'id': material['id'],
            'nombre': material['nombre'],
            'precio': material['precio_unitario'],
            'tipo': material['tipo_entrada']
        })
    return jsonify({'error': 'Material no encontrado'}), 404

# 2. OBTENER RECETA COMPLETA (¡Esto faltaba para cargar las de ayer!)
@api_bp.route('/receta/<int:id>')
def obtener_receta(id):
    if 'user_id' not in session:
        return jsonify({'error': 'No autorizado'}), 401

    conn = get_db()
    
    # A. Datos básicos del producto (receta)
    producto = conn.execute('SELECT * FROM productos WHERE id = ? AND user_id = ?', 
                           (id, session['user_id'])).fetchone()
    
    if not producto:
        conn.close()
        return jsonify({'error': 'Receta no encontrada'}), 404

    # B. Sus materiales
    detalles = conn.execute('''
        SELECT m.id, m.nombre, m.precio_unitario, pd.cantidad 
        FROM producto_detalles pd
        JOIN materiales m ON pd.material_id = m.id
        WHERE pd.producto_id = ?
    ''', (id,)).fetchall()

    # C. Su maquinaria
    maquinaria = conn.execute('''
        SELECT mq.id, mq.nombre, mq.costo_desgaste
        FROM producto_maquinaria pm
        JOIN maquinaria mq ON pm.maquinaria_id = mq.id
        WHERE pm.producto_id = ?
    ''', (id,)).fetchall()

    conn.close()

    # Empaquetamos todo para enviarlo al JS
    return jsonify({
        'id': producto['id'],
        'nombre': producto['nombre'],
        'materiales': [dict(d) for d in detalles],
        'maquinaria': [dict(m) for m in maquinaria]
    })