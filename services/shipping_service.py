from geopy.distance import geodesic
from models.shipping_model import ShippingModel
import requests
import re
import logging

class ShippingService:
    
    def __init__(self, user_id):
        self.user_id = user_id
        row = ShippingModel.get_config(user_id)
        self.config = dict(row) if row else None

    def calcular_peso_volumetrico(self, largo, ancho, alto):
        """
        Calcula el peso volumetrico estandar de paqueteria.
        Asume que los valores ya fueron validados y convertidos a float.
        """
        return (largo * ancho * alto) / 5000.0

    def cotizar_local(self, destino_lat, destino_lng):
        """
        Calcula el costo de envio local basado en la distancia en linea recta (geodesica)
        usando las coordenadas de origen configuradas por el usuario.
        """
        if not self.config:
            return {"error": "Configuracion de envio no encontrada"}
            
        # Validacion segura de datos de origen en la base de datos
        origin_lat = self.config.get('origin_lat')
        origin_lng = self.config.get('origin_lng')
        
        if not origin_lat or not origin_lng:
            return {"error": "Falta configurar la ubicacion de origen"}

        # Blindaje de tipos de datos para evitar caidas en la libreria geodesic
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
        
        # Obtencion segura de las tarifas con fallback a 0.0 para prevenir errores matematicos
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
        """
        Calcula el costo de envio nacional buscando la zona correspondiente al estado
        y determinando el peso cobrable (el mayor entre peso real y volumetrico).
        """
        if not self.config:
            return {"error": "Configuracion no encontrada. Ve a Configuracion > Envios."}

        # 1. Blindaje de tipos de datos y extraccion de texto residual
        try:
            peso_kg = float(peso_kg)
            largo = float(largo)
            ancho = float(ancho)
            alto = float(alto)
        except (ValueError, TypeError):
            return {"error": "Las dimensiones y peso deben ser numeros validos."}

        # Validacion logica: Prevenir inyeccion de valores negativos o paquetes imposibles
        if peso_kg <= 0 or largo <= 0 or ancho <= 0 or alto <= 0:
            return {"error": "Las dimensiones y el peso deben ser mayores a cero."}

        # 2. Calcular peso cobrable
        peso_vol = self.calcular_peso_volumetrico(largo, ancho, alto)
        peso_cobrable = max(peso_kg, peso_vol)
        
        # 3. Encontrar la zona
        zona_row = ShippingModel.get_zone_by_state(self.user_id, estado_destino)
        
        # Si no hay zona especifica, intentamos buscar la zona general
        if not zona_row:
             zona_row = ShippingModel.get_zone_by_state(self.user_id, "ALL")

        if not zona_row:
            return {"error": f"No hay cobertura configurada para {estado_destino} ni tarifa Nacional General."}
            
        # FIX: Convertimos la fila de SQLite a diccionario de Python
        zona = dict(zona_row)
            
        # 4. Encontrar la tarifa de forma segura
        zona_id = zona.get('id')
        tarifa_row = ShippingModel.get_rate_for_zone(zona_id, peso_cobrable)
        
        if not tarifa_row:
            return {"error": f"Tu paquete ({peso_cobrable:.2f}kg cobrables) excede el peso maximo configurado en la tarifa."}
            
        # Convertimos la tarifa a diccionario
        tarifa = dict(tarifa_row)
            
        return {
            "tipo": "nacional",
            "zona": zona.get('zone_name', 'General'),
            "peso_real": peso_kg,
            "peso_volumetrico": round(peso_vol, 2),
            "peso_cobrable": round(peso_cobrable, 2),
            "costo_sugerido": float(tarifa.get('price', 0.0))
        }


def obtener_coordenadas_de_link_corto(url_corta):
    """
    Toma un link corto de Google Maps, sigue la redirección HTTP,
    y extrae la Latitud y Longitud de la URL final.
    """
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        respuesta = requests.get(url_corta, headers=headers, allow_redirects=True, timeout=5)
        url_final = respuesta.url
        
        # Atrapa tanto el formato @lat,lng como el ?q=lat,lng
        match = re.search(r'(?:@|q=)(-?\d+\.\d+),(-?\d+\.\d+)', url_final)
        
        if match:
            return float(match.group(1)), float(match.group(2))
            
        return None, None
            
    except Exception as e:
        logging.error(f"SHIPPING_MAPS_ERROR: Fallo al resolver el link corto de Google Maps '{url_corta}' - {e}")
        return None, None