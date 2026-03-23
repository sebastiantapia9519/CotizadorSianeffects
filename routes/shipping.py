import requests
import re
from flask import Blueprint, request, jsonify, session
from services.shipping_service import ShippingService

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
        print(f"Error crítico en cotización: {e}")
        # Retornamos el error real temporalmente para ayudarte a depurar si falla la DB
        return jsonify({"error": f"Fallo en el servicio: {str(e)}"}), 500

@shipping_bp.route('/api/resolver-mapa', methods=['POST'])
def resolver_mapa():
    data = request.json
    url = data.get('url')
    
    if not url:
        return jsonify({"error": "No se envió ningún link"}), 400

    try:
        # Headers para parecer un navegador real
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        
        # Intentamos seguir el link
        # timeout=10 evita que se quede colgado eternamente
        response = requests.get(url, headers=headers, allow_redirects=True, timeout=10)
        url_final = response.url
        
        # --- DIAGNÓSTICO: Imprimir en consola por si acaso ---
        print(f"DEBUG MAPS: Link original: {url}")
        print(f"DEBUG MAPS: Link final: {url_final}")
        # ---------------------------------------------------

        # 1. Búsqueda Estándar (@lat,lng)
        match = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', url_final)
        if match:
            return jsonify({
                "success": True, 
                "lat": match.group(1), 
                "lng": match.group(2),
                "debug_url": url_final
            })
        
        # 2. Búsqueda Secundaria (?q=lat,lng)
        match_q = re.search(r'q=(-?\d+\.\d+),(-?\d+\.\d+)', url_final)
        if match_q:
             return jsonify({
                "success": True, 
                "lat": match_q.group(1), 
                "lng": match_q.group(2),
                "debug_url": url_final
            })
        
        # 3. Búsqueda Terciaria (!3dlat!4dlng) - A veces Google usa este formato raro
        match_3d = re.search(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', url_final)
        if match_3d:
             return jsonify({
                "success": True, 
                "lat": match_3d.group(1), 
                "lng": match_3d.group(2),
                "debug_url": url_final
            })

        # Si llegamos aquí, conectamos bien pero el Regex falló
        return jsonify({
            "error": f"Conexión OK, pero no hallé coordenadas. URL final: {url_final[:60]}..."
        }), 400

    except Exception as e:
        # AQUÍ ESTÁ EL CAMBIO IMPORTANTE:
        # Devolvemos el error real (str(e)) para verlo en la alerta
        print(f"ERROR CRÍTICO MAPS: {e}")
        return jsonify({"error": f"Fallo Técnico: {str(e)}"}), 500