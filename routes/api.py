from flask import Blueprint, jsonify, session, request
from db import get_db_connection as get_db

# ESTO ES LO QUE BUSCA EL SERVIDOR: 'api_bp'
api_bp = Blueprint('api', __name__)

@api_bp.route('/material/<int:id>')
def obtener_material(id):
    if 'user_id' not in session:
        return jsonify({'error': 'No autorizado'}), 401

    conn = get_db()
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