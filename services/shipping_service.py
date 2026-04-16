from geopy.distance import geodesic
from models.shipping_model import ShippingModel
import requests
import re
import logging
from urllib.parse import urlparse, parse_qs, unquote
from geopy.geocoders import Nominatim
import time

# 🔥 Instancia GLOBAL (no crear cada vez)
geolocator = Nominatim(user_agent="shipping_app")

# 🔥 Cache simple en memoria
cache_geo = {}

class ShippingService:
    
    def __init__(self, user_id):
        self.user_id = user_id
        row = ShippingModel.get_config(user_id)
        self.config = dict(row) if row else None

    def calcular_peso_volumetrico(self, largo, ancho, alto):
        return (largo * ancho * alto) / 5000.0

    def cotizar_local(self, destino_lat, destino_lng):
        if not self.config:
            return {"error": "Configuracion de envio no encontrada"}
            
        origin_lat = self.config.get('origin_lat')
        origin_lng = self.config.get('origin_lng')
        
        if not origin_lat or not origin_lng:
            return {"error": "Falta configurar la ubicacion de origen"}

        try:
            lat_origen = float(origin_lat)
            lng_origen = float(origin_lng)
            lat_destino = float(destino_lat)
            lng_destino = float(destino_lng)
        except (ValueError, TypeError):
            return {"error": "Las coordenadas proporcionadas no son validas."}

        origen = (lat_origen, lng_origen)
        destino = (lat_destino, lng_destino)
        
        try:
            distancia_km = geodesic(origen, destino).km
        except Exception as e:
            return {"error": f"Error al calcular la distancia: {str(e)}"}
        
        base_rate = float(self.config.get('local_base_rate') or 0.0)
        km_rate = float(self.config.get('local_km_rate') or 0.0)
        safety_margin = float(self.config.get('safety_margin_percent') or 0.0)
        
        costo_puro = base_rate + (distancia_km * km_rate)
        margen = 1.0 + (safety_margin / 100.0)
        
        total = costo_puro * margen
        
        return {
            "tipo": "local",
            "distancia_km": round(distancia_km, 2),
            "costo_sugerido": round(total, 2)
        }

    def cotizar_nacional(self, peso_kg, largo, ancho, alto, estado_destino):
        if not self.config:
            return {"error": "Configuracion no encontrada. Ve a Configuracion > Envios."}

        try:
            peso_kg = float(peso_kg)
            largo = float(largo)
            ancho = float(ancho)
            alto = float(alto)
        except (ValueError, TypeError):
            return {"error": "Las dimensiones y peso deben ser numeros validos."}

        if peso_kg <= 0 or largo <= 0 or ancho <= 0 or alto <= 0:
            return {"error": "Las dimensiones y el peso deben ser mayores a cero."}

        peso_vol = self.calcular_peso_volumetrico(largo, ancho, alto)
        peso_cobrable = max(peso_kg, peso_vol)
        
        zona_row = ShippingModel.get_zone_by_state(self.user_id, estado_destino)
        
        if not zona_row:
             zona_row = ShippingModel.get_zone_by_state(self.user_id, "ALL")

        if not zona_row:
            return {"error": f"No hay cobertura configurada para {estado_destino} ni tarifa Nacional General."}
            
        zona = dict(zona_row)
        zona_id = zona.get('id')
        tarifa_row = ShippingModel.get_rate_for_zone(zona_id, peso_cobrable)
        
        if not tarifa_row:
            return {"error": f"Tu paquete ({peso_cobrable:.2f}kg) excede el peso maximo."}
            
        tarifa = dict(tarifa_row)
            
        return {
            "tipo": "nacional",
            "zona": zona.get('zone_name', 'General'),
            "peso_real": peso_kg,
            "peso_volumetrico": round(peso_vol, 2),
            "peso_cobrable": round(peso_cobrable, 2),
            "costo_sugerido": float(tarifa.get('price', 0.0))
        }


# 🔥 Limpia la dirección
def limpiar_direccion(direccion):
    direccion = direccion.replace('+', ' ')
    direccion = direccion.replace('%20', ' ')
    return direccion.strip()


# 🔥 Geocoding inteligente + cache
def geocodificar(direccion):
    direccion = limpiar_direccion(direccion)

    # Cache
    if direccion in cache_geo:
        return cache_geo[direccion]

    try:
        # Intento 1: completo
        time.sleep(1)
        location = geolocator.geocode(direccion)

        if location:
            coords = (location.latitude, location.longitude)
            cache_geo[direccion] = coords
            return coords

        # Intento 2: quitar nombre del negocio
        partes = direccion.split(',')
        if len(partes) > 1:
            direccion_simple = ','.join(partes[1:]).strip()

            time.sleep(1)
            location = geolocator.geocode(direccion_simple)

            if location:
                coords = (location.latitude, location.longitude)
                cache_geo[direccion] = coords
                return coords

    except Exception as e:
        logging.error(f"Error en geocoding: {e}")

    return None, None


def obtener_coordenadas_universales(url_input):
    if not url_input:
        return None, None

    url_final = url_input
    html_content = ""

    # 🔥 1. Expandir links
    if any(d in url_input.lower() for d in ["goo.gl", "maps.app", "googleusercontent.com"]):
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (iPhone)',
                'Accept-Language': 'es-MX,es;q=0.9'
            }
            res = requests.get(url_input, headers=headers, allow_redirects=True, timeout=8)
            url_final = res.url
            html_content = res.text
        except Exception as e:
            logging.error(f"Error expandiendo URL: {e}")
            return None, None

    # 🔥 2. Regex directo
    patrones = [
        r'@(-?\d+\.\d+),(-?\d+\.\d+)',
        r'q=(-?\d+\.\d+),(-?\d+\.\d+)',
        r'll=(-?\d+\.\d+),(-?\d+\.\d+)',
        r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)'
    ]

    for p in patrones:
        match = re.search(p, url_final)
        if match:
            return float(match.group(1)), float(match.group(2))

    # 🔥 3. Extraer parámetros
    try:
        parsed = urlparse(url_final)
        params = parse_qs(parsed.query)

        # daddr
        if 'daddr' in params:
            direccion = unquote(params['daddr'][0])
            lat, lng = geocodificar(direccion)
            if lat and lng:
                return lat, lng

        # q (🔥 ESTE ERA TU BUG)
        if 'q' in params:
            direccion = unquote(params['q'][0])

            if not re.match(r'^-?\d+\.\d+,-?\d+\.\d+$', direccion):
                lat, lng = geocodificar(direccion)
                if lat and lng:
                    return lat, lng

    except Exception as e:
        logging.error(f"Error procesando URL: {e}")

    # 🔥 4. Último intento HTML
    if html_content:
        match = re.search(r'center=(-?\d+\.\d+)(?:%2C|,)(-?\d+\.\d+)', html_content)
        if match:
            return float(match.group(1)), float(match.group(2))

    logging.warning(f"No se pudieron extraer coordenadas: {url_final}")
    return None, None