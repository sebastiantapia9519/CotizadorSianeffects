import requests
import re
from flask import Blueprint, request, jsonify, session, current_app
from services.shipping_service import ShippingService, obtener_coordenadas_universales

shipping_bp = Blueprint('shipping', __name__)

@shipping_bp.route('/api/cotizar-envio', methods=['POST'])
def cotizar():
    # 1. Validación estricta de sesión PRIMERO
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401
        
    try:
        # 2. Obtener datos de la petición de forma segura
        data = request.get_json()
        if not data:
             return jsonify({"error": "No se enviaron datos"}), 400

        # 3. Inicializar el servicio con el usuario verificado
        servicio = ShippingService(session['user_id'])
        
        # 4. Determinar el tipo de cotización
        if data.get('tipo') == 'local':
            # Validación rápida de que las coordenadas vengan en el payload
            if 'lat' not in data or 'lng' not in data:
                 return jsonify({"error": "Faltan coordenadas para envío local"}), 400
                 
            resultado = servicio.cotizar_local(
                data['lat'], 
                data['lng']
            )
        else:
            # Nacional
            # Validación de campos mínimos para nacional
            if not all(k in data for k in ('peso', 'largo', 'ancho', 'alto', 'estado')):
                 return jsonify({"error": "Faltan dimensiones o estado para envío nacional"}), 400
                 
            resultado = servicio.cotizar_nacional(
                data['peso'],
                data['largo'],
                data['ancho'],
                data['alto'],
                data['estado'] # Ej: 'NL', 'CDMX'
            )
            
        return jsonify(resultado)
        
    except Exception as e:
        current_app.logger.error(f"SHIPPING_QUOTE_ERROR: Usuario {session.get('user_id')} falló al cotizar - {e}")
        # Retornamos el error real temporalmente para ayudarte a depurar si falla la DB
        return jsonify({"error": f"Fallo en el servicio: {str(e)}"}), 500


@shipping_bp.route('/api/resolver-mapa', methods=['POST'])
def resolver_mapa():
    """
    Ruta API para que el frontend envíe un link y reciba las coordenadas.
    """
    data = request.json
    url = data.get('url')
    
    if not url:
        return jsonify({"error": "No se envió ningún link"}), 400

    try:
        # Usamos nuestra nueva función "Universal" desde el servicio
        lat, lng = obtener_coordenadas_universales(url)

        if lat and lng:
            return jsonify({
                "success": True, 
                "lat": lat, 
                "lng": lng
            })
        else:
             return jsonify({
                "error": "No pude encontrar coordenadas válidas en ese link. Por favor revisa que sea un link válido de Google Maps."
            }), 400

    except Exception as e:
        # Devolvemos el error real (str(e)) para verlo en la alerta
        current_app.logger.error(f"MAPS_CONNECTION_ERROR: Fallo al intentar resolver URL '{url}' - {e}")
        return jsonify({"error": f"Fallo Técnico: {str(e)}"}), 500