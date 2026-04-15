import requests
import re
from flask import Blueprint, request, jsonify, session, current_app
from services.shipping_service import ShippingService, obtener_coordenadas_universales

shipping_bp = Blueprint('shipping', __name__)

@shipping_bp.route('/api/cotizar-envio', methods=['POST'])
def cotizar():
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401
        
    try:
        data = request.get_json()
        if not data:
             return jsonify({"error": "No se enviaron datos"}), 400

        servicio = ShippingService(session['user_id'])
        
        if data.get('tipo') == 'local':
            lat = data.get('lat')
            lng = data.get('lng')
            direccion = data.get('direccion') # Recibimos el link

            # --- LÓGICA DE AUTO-RESOLUCIÓN ---
            # Si no hay coordenadas pero tenemos un link, intentamos resolverlo aquí mismo
            if (not lat or not lng) and direccion:
                try:
                    lat_res, lng_res = obtener_coordenadas_universales(direccion)
                    if lat_res and lng_res:
                        lat, lng = lat_res, lng_res
                        current_app.logger.info(f"COTIZADOR_AUTO_RESOLVE: Link resuelto a {lat}, {lng}")
                except Exception as e:
                    current_app.logger.warning(f"COTIZADOR_AUTO_RESOLVE_FAIL: {e}")

            # Ahora sí, validamos si después de intentar resolver tenemos los datos
            if not lat or not lng:
                 return jsonify({"error": "Faltan coordenadas para envío local. Por favor ingresa un link válido."}), 400
                 
            resultado = servicio.cotizar_local(lat, lng)
        else:
            # Nacional (Se mantiene igual)
            if not all(k in data for k in ('peso', 'largo', 'ancho', 'alto', 'estado')):
                 return jsonify({"error": "Faltan dimensiones o estado para envío nacional"}), 400
                 
            resultado = servicio.cotizar_nacional(
                data['peso'], data['largo'], data['ancho'], data['alto'], data['estado']
            )
            
        return jsonify(resultado)
        
    except Exception as e:
        current_app.logger.error(f"SHIPPING_QUOTE_ERROR: {e}")
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