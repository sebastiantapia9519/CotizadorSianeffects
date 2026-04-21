import requests
import re
from flask import Blueprint, request, jsonify, session, current_app
from services.shipping_service import ShippingService, obtener_coordenadas_universales

shipping_bp = Blueprint('shipping', __name__)

@shipping_bp.route('/api/cotizar-envio', methods=['POST'])
def cotizar():
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401
        
    u_id = session['user_id']
    u_name = session.get('username', 'Anonimo')
        
    try:
        data = request.get_json()
        if not data:
             return jsonify({"error": "No se enviaron datos"}), 400

        servicio = ShippingService(u_id)
        tipo_envio = data.get('tipo')
        
        if tipo_envio == 'local':
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
                        current_app.logger.info(f"COTIZADOR_AUTO_RESOLVE: Usuario '{u_name}' (ID: {u_id}) resolvio link a {lat}, {lng}")
                except Exception as e:
                    current_app.logger.warning(f"COTIZADOR_AUTO_RESOLVE_FAIL: Usuario '{u_name}' (ID: {u_id}) fallo al resolver link - {e}")

            # Ahora sí, validamos si después de intentar resolver tenemos los datos
            if not lat or not lng:
                 return jsonify({"error": "Faltan coordenadas para envio local. Por favor ingresa un link valido."}), 400
                 
            resultado = servicio.cotizar_local(lat, lng)
        else:
            # Nacional (Se mantiene igual)
            if not all(k in data for k in ('peso', 'largo', 'ancho', 'alto', 'estado')):
                 return jsonify({"error": "Faltan dimensiones o estado para envio nacional"}), 400
                 
            resultado = servicio.cotizar_nacional(
                data['peso'], data['largo'], data['ancho'], data['alto'], data['estado']
            )
            
        # LOG DE OPERACIÓN EXITOSA
        current_app.logger.info(f"SHIPPING_QUOTED: Usuario '{u_name}' (ID: {u_id}) cotizo envio {tipo_envio} exitosamente")
        
        return jsonify(resultado)
        
    except Exception as e:
        current_app.logger.error(f"SHIPPING_QUOTE_ERROR: Usuario '{u_name}' (ID: {u_id}) fallo en cotizacion - {e}")
        return jsonify({"error": f"Fallo en el servicio: {str(e)}"}), 500


@shipping_bp.route('/api/resolver-mapa', methods=['POST'])
def resolver_mapa():
    """
    Ruta API para que el frontend envíe un link y reciba las coordenadas.
    """
    u_name = session.get('username', 'Anonimo')
    u_id = session.get('user_id', 'N/A')
    
    data = request.json
    url = data.get('url')
    
    if not url:
        return jsonify({"error": "No se envio ningun link"}), 400

    try:
        # Usamos nuestra nueva función "Universal" desde el servicio
        lat, lng = obtener_coordenadas_universales(url)

        if lat and lng:
            current_app.logger.info(f"MAPS_RESOLVED: Usuario '{u_name}' (ID: {u_id}) resolvio coordenadas externas exitosamente")
            return jsonify({
                "success": True, 
                "lat": lat, 
                "lng": lng
            })
        else:
             return jsonify({
                "error": "No pude encontrar coordenadas validas en ese link. Por favor revisa que sea un link valido de Google Maps."
            }), 400

    except Exception as e:
        # Devolvemos el error real (str(e)) para verlo en la alerta
        current_app.logger.error(f"MAPS_CONNECTION_ERROR: Usuario '{u_name}' (ID: {u_id}) fallo al intentar resolver URL '{url}' - {e}")
        return jsonify({"error": f"Fallo Tecnico: {str(e)}"}), 500