import sys
import traceback
from flask import Blueprint, request, session, jsonify
from db import get_db_connection as get_db

inventory_bp = Blueprint('inventory', __name__, url_prefix='/inventory')


@inventory_bp.route('/test')
def test_inventory():
    return 'INVENTORY OK'


@inventory_bp.route('/guardar_receta', methods=['POST'])
def guardar_receta():
    print("--- [DEBUG] INICIO INTENTO GUARDAR RECETA ---", file=sys.stderr)

    # 🔐 VALIDACIÓN DE SESIÓN (API-SAFE, sin redirects)
    if 'user_id' not in session:
        return jsonify({'error': 'No autorizado'}), 401

    try:
        # 📦 Validar JSON
        data = request.get_json(silent=True)
        print(f"--- [DEBUG] DATOS RECIBIDOS: {data}", file=sys.stderr)

        if not data or not data.get('nombre'):
            return jsonify({'error': 'Datos incompletos'}), 400

        conn = get_db()

        # 🧱 Transacción explícita
        conn.execute('BEGIN')

        # 🧾 Insert producto
        cursor = conn.execute(
            'INSERT INTO productos (user_id, nombre) VALUES (?, ?)',
            (session['user_id'], data['nombre'])
        )
        producto_id = cursor.lastrowid

        # 🧱 Materiales
        for mat in data.get('materiales', []):
            if 'id' not in mat or 'cantidad' not in mat:
                raise ValueError('Material inválido')

            conn.execute(
                '''
                INSERT INTO producto_detalles (producto_id, material_id, cantidad)
                VALUES (?, ?, ?)
                ''',
                (producto_id, mat['id'], mat['cantidad'])
            )

        # ⚙️ Maquinaria
        for maq in data.get('maquinaria', []):
            if 'id' not in maq:
                raise ValueError('Maquinaria inválida')

            conn.execute(
                '''
                INSERT INTO producto_maquinaria (producto_id, maquinaria_id)
                VALUES (?, ?)
                ''',
                (producto_id, maq['id'])
            )

        # ✅ Commit final
        conn.commit()
        conn.close()

        return jsonify({'success': True, 'producto_id': producto_id})

    except Exception as e:
        print("--- [ERROR] EXCEPCIÓN AL GUARDAR RECETA ---", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)

        if 'conn' in locals():
            conn.rollback()
            conn.close()

        return jsonify({
            'error': 'Error interno al guardar receta',
            'detail': str(e)
        }), 500
