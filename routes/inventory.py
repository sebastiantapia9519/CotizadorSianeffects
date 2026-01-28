import sys
import traceback
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from db import get_db_connection as get_db
from helpers import login_required

app.register_blueprint(inventory_bp)

@inventory_bp.route('/test')
def test_inventory():
    return 'INVENTORY OK'

@inventory_bp.route('/guardar_receta', methods=['POST'])
@login_required
def guardar_receta():
    print("--- [DEBUG] INICIO INTENTO GUARDAR RECETA ---", file=sys.stderr)

    try:
        data = request.get_json()
        print(f"--- [DEBUG] DATOS RECIBIDOS: {data}", file=sys.stderr)

        if not data or 'nombre' not in data:
            return jsonify({'error': 'Datos incompletos'}), 400

        conn = get_db()
        cursor = conn.execute(
            'INSERT INTO productos (user_id, nombre) VALUES (?, ?)',
            (session['user_id'], data['nombre'])
        )
        producto_id = cursor.lastrowid

        for mat in data.get('materiales', []):
            conn.execute(
                'INSERT INTO producto_detalles (producto_id, material_id, cantidad) VALUES (?, ?, ?)',
                (producto_id, mat['id'], mat['cantidad'])
            )

        for maq in data.get('maquinaria', []):
            conn.execute(
                'INSERT INTO producto_maquinaria (producto_id, maquinaria_id) VALUES (?, ?)',
                (producto_id, maq['id'])
            )

        conn.commit()
        conn.close()
        return jsonify({'success': True})

    except Exception as e:
        print(traceback.format_exc(), file=sys.stderr)
        if 'conn' in locals():
            conn.close()
        return jsonify({'error': str(e)}), 500
