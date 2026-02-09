import requests
import re
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

@shipping_bp.route('/api/resolver-mapa', methods=['POST'])
def resolver_mapa():
    data = request.json
    url = data.get('url')
    
    if not url:
        return jsonify({"error": "No se envió ningún link"}), 400

    try:
        # 1. Fingimos ser un navegador real para que Google no nos bloquee
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        # 2. Seguimos el link corto hasta su destino final
        response = requests.get(url, headers=headers, allow_redirects=True, timeout=10)
        url_final = response.url
        
        # 3. Buscamos las coordenadas en la URL final (formato @lat,lng)
        # Regex mejorado para capturar coordenadas
        match = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', url_final)
        
        if match:
            return jsonify({
                "success": True,
                "lat": match.group(1),
                "lng": match.group(2),
                "url_expandida": url_final
            })
        
        # Intento secundario: a veces están en parámetros ?q=lat,lng
        match_q = re.search(r'q=(-?\d+\.\d+),(-?\d+\.\d+)', url_final)
        if match_q:
             return jsonify({
                "success": True,
                "lat": match_q.group(1),
                "lng": match_q.group(2)
            })
            
        return jsonify({"error": "No pude encontrar coordenadas en ese link. Intenta copiar el link de la barra de arriba."}), 400

    except Exception as e:
        print(f"Error resolviendo mapa: {e}")
        return jsonify({"error": "Error al conectar con Google Maps"}), 500