from flask import Blueprint, request, jsonify, session
from services.shipping_service import ShippingService

shipping_bp = Blueprint('shipping', __name__)

@shipping_bp.route('/api/cotizar-envio', methods=['POST'])
def cotizar():
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401
        
    data = request.json
    servicio = ShippingService(session['user_id'])
    
    try:
        if data.get('tipo') == 'local':
            resultado = servicio.cotizar_local(
                data['lat'], 
                data['lng']
            )
        else:
            # Nacional
            resultado = servicio.cotizar_nacional(
                data['peso'],
                data['largo'],
                data['ancho'],
                data['alto'],
                data['estado'] # Ej: 'NL', 'CDMX'
            )
            
        return jsonify(resultado)
        
    except Exception as e:
        print(f"Error en cotización: {e}")
        return jsonify({"error": "Error interno al calcular envío"}), 500