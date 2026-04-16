from geopy.distance import geodesic
from models.shipping_model import ShippingModel
import requests
import re
import logging
from urllib.parse import urlparse, parse_qs, unquote
from geopy.geocoders import Nominatim
import time

class ShippingService:
    
    def __init__(self, user_id):
        """
        Inicializa el servicio.
        Carga la configuración de envíos del usuario desde la base de datos
        en el momento en que se instancia la clase.
        """
        self.user_id = user_id
        row = ShippingModel.get_config(user_id)
        # Convertimos la fila a un diccionario nativo de Python para facilitar su uso
        self.config = dict(row) if row else None

    def calcular_peso_volumetrico(self, largo, ancho, alto):
        """
        Calcula el peso volumetrico estandar de paqueteria.
        Fórmula estándar de la industria: (L * A * A) / 5000.
        """
        return (largo * ancho * alto) / 5000.0

    def cotizar_local(self, destino_lat, destino_lng):
        """
        Calcula el costo de envio local basado en la distancia en linea recta (geodesica)
        usando las coordenadas de origen configuradas por el usuario.
        """
        # Verificamos que el usuario sí haya guardado su configuración en el panel
        if not self.config:
            return {"error": "Configuracion de envio no encontrada"}
            
        # Extraemos las coordenadas del negocio (el "Punto A")
        origin_lat = self.config.get('origin_lat')
        origin_lng = self.config.get('origin_lng')
        
        if not origin_lat or not origin_lng:
            return {"error": "Falta configurar la ubicacion de origen"}

        # Blindaje: Convertimos todo a float. Si llega basura (ej. letras), atrapamos el error.
        try:
            lat_origen = float(origin_lat)
            lng_origen = float(origin_lng)
            lat_destino = float(destino_lat)
            lng_destino = float(destino_lng)
        except (ValueError, TypeError):
            return {"error": "Las coordenadas proporcionadas no son validas."}

        # Armamos las tuplas que requiere la librería geopy
        origen = (lat_origen, lng_origen)
        destino = (lat_destino, lng_destino)
        
        # Calculamos la distancia en línea recta
        try:
            distancia_km = geodesic(origen, destino).km
        except Exception as e:
            return {"error": f"Error al calcular la distancia: {str(e)}"}
        
        # Extraemos las tarifas. Usamos 'or 0.0' para que no truene la matemática si hay nulos en la BD.
        base_rate = float(self.config.get('local_base_rate') or 0.0)
        km_rate = float(self.config.get('local_km_rate') or 0.0)
        safety_margin = float(self.config.get('safety_margin_percent') or 0.0)
        
        # Matemáticas de la cotización
        costo_puro = base_rate + (distancia_km * km_rate)
        margen = 1.0 + (safety_margin / 100.0) # Si el margen es 10%, multiplica por 1.10
        
        total = costo_puro * margen
        
        return {
            "tipo": "local",
            "distancia_km": round(distancia_km, 2),
            "costo_sugerido": round(total, 2)
        }

    def cotizar_nacional(self, peso_kg, largo, ancho, alto, estado_destino):
        """
        Calcula el costo de envio nacional buscando la zona correspondiente al estado
        y determinando el peso cobrable (el mayor entre peso real y volumetrico).
        """
        if not self.config:
            return {"error": "Configuracion no encontrada. Ve a Configuracion > Envios."}

        # 1. Blindaje: Convertimos medidas a float para evitar errores
        try:
            peso_kg = float(peso_kg)
            largo = float(largo)
            ancho = float(ancho)
            alto = float(alto)
        except (ValueError, TypeError):
            return {"error": "Las dimensiones y peso deben ser numeros validos."}

        # Validación lógica: No cobramos por paquetes fantasma
        if peso_kg <= 0 or largo <= 0 or ancho <= 0 or alto <= 0:
            return {"error": "Las dimensiones y el peso deben ser mayores a cero."}

        # 2. Calcular peso cobrable (La regla de oro de las paqueterías)
        peso_vol = self.calcular_peso_volumetrico(largo, ancho, alto)
        peso_cobrable = max(peso_kg, peso_vol)
        
        # 3. Encontrar la zona donde cae el estado destino
        zona_row = ShippingModel.get_zone_by_state(self.user_id, estado_destino)
        
        # Si no mapeó el estado específico, buscamos si tiene una tarifa comodín ("ALL")
        if not zona_row:
             zona_row = ShippingModel.get_zone_by_state(self.user_id, "ALL")

        # Si de plano no cubre ese estado ni tiene tarifa general, abortamos
        if not zona_row:
            return {"error": f"No hay cobertura configurada para {estado_destino} ni tarifa Nacional General."}
            
        zona = dict(zona_row)
            
        # 4. Encontrar la tarifa correcta dentro de esa zona basándonos en el peso
        zona_id = zona.get('id')
        tarifa_row = ShippingModel.get_rate_for_zone(zona_id, peso_cobrable)
        
        # Si el paquete pesa más que la tarifa máxima que dio de alta el usuario
        if not tarifa_row:
            return {"error": f"Tu paquete ({peso_cobrable:.2f}kg cobrables) excede el peso maximo configurado en la tarifa."}
            
        tarifa = dict(tarifa_row)
            
        # Regresamos el desglose completo para que el cliente lo vea en el ticket
        return {
            "tipo": "nacional",
            "zona": zona.get('zone_name', 'General'),
            "peso_real": peso_kg,
            "peso_volumetrico": round(peso_vol, 2),
            "peso_cobrable": round(peso_cobrable, 2),
            "costo_sugerido": float(tarifa.get('price', 0.0))
        }


def obtener_coordenadas_universales(url_input):
    if not url_input:
        return None, None

    url_final = url_input
    html_content = ""

    # 1. Expandir links cortos o raros
    if any(dominio in url_input.lower() for dominio in ["goo.gl", "maps.app", "googleusercontent.com"]):
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X)',
                'Accept-Language': 'es-MX,es;q=0.9'
            }
            res = requests.get(url_input, headers=headers, allow_redirects=True, timeout=8)
            url_final = res.url
            html_content = res.text
        except Exception as e:
            logging.error(f"Error expandiendo URL '{url_input}': {e}")
            return None, None

    # 2. PLAN A: regex directo
    patrones_url = [
        r'@(-?\d+\.\d+),(-?\d+\.\d+)',
        r'q=(-?\d+\.\d+),(-?\d+\.\d+)',
        r'll=(-?\d+\.\d+),(-?\d+\.\d+)',
        r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)'
    ]

    for patron in patrones_url:
        match = re.search(patron, url_final)
        if match:
            return float(match.group(1)), float(match.group(2))

    # 3. extraer dirección (daddr)
    try:
        parsed = urlparse(url_final)
        params = parse_qs(parsed.query)

        if 'daddr' in params:
            direccion = unquote(params['daddr'][0])

            # Geocoding con geopy (OpenStreetMap)
            geolocator = Nominatim(user_agent="shipping_app")

            time.sleep(1)  # ⚠️ IMPORTANTE: evitar rate limit

            location = geolocator.geocode(direccion)

            if location:
                return location.latitude, location.longitude
        
        # extraer dirección desde q=
        if 'q' in params:
            direccion = unquote(params['q'][0])

            # Evitar falsos positivos (coords ya vienen en q)
            if not re.match(r'^-?\d+\.\d+,-?\d+\.\d+$', direccion):
                geolocator = Nominatim(user_agent="shipping_app")
                time.sleep(1)

                location = geolocator.geocode(direccion)

                if location:
                    return location.latitude, location.longitude

    except Exception as e:
        logging.error(f"Error geocodificando dirección: {e}")

    # 4. PLAN B (último intento HTML)
    if html_content:
        match_meta = re.search(r'center=(-?\d+\.\d+)(?:%2C|,)(-?\d+\.\d+)', html_content)
        if match_meta:
            return float(match_meta.group(1)), float(match_meta.group(2))

    logging.warning(f"No se pudieron extraer coordenadas: {url_final}")
    return None, None