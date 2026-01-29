from flask import Blueprint, request, session, jsonify, render_template
from helpers import login_required
from db import get_db_connection as get_db

inventory_bp = Blueprint('inventory', __name__)

@inventory_bp.route('/test')
def test_inventory():
    return 'INVENTORY OK'

@inventory_bp.route('/guardar_receta', methods=['POST'])
@login_required
def guardar_receta():
    return jsonify({'ok': True})

@inventory_bp.route('/materiales')
@login_required
def materiales():
    return render_template('materiales.html')

@inventory_bp.route('/equipos')
@login_required
def equipos():
    return render_template('equipos.html')

@inventory_bp.route('/recetas')
@login_required
def recetas():
    return render_template('recetas.html')
