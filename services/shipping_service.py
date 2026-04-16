from geopy.distance import geodesic
from models.shipping_model import ShippingModel
import requests
import re
import logging

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
    """
    SÚPER RESOLUTOR DE LINKS
    Toma CUALQUIER link de Google Maps (corto, largo, o de app), 
    sigue las redirecciones HTTP y extrae latitud y longitud.
    """
    if not url_input:
        return None, None

    # --- NUEVO BLINDAJE: DETECTAR LINKS FANTASMA DE MÓVILES ---
    # Si el link es del tipo "googleusercontent.com/maps.google.com/X"
    # Sabemos que es un link corrupto del sistema de compartir de iOS/Android
    if re.search(r'googleusercontent\.com/maps\.google\.com/\d+', url_input):
        logging.warning(f"Link fantasma detectado y bloqueado: {url_input}")
        # En lugar de fallar, devolvemos None, None. 
        # La ruta que llama a esta función deberá manejar este caso.
        return None, None
    # -----------------------------------------------------------

    url_final = url_input
    
    # 1. Si el link es un acortador o un enlace móvil, lo resolvemos primero
    if "goo.gl" in url_input or "maps.app" in url_input:
        try:
            # Ponemos un User-Agent para no parecer un bot malicioso y que Google no nos bloquee
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
            
            # allow_redirects=True es lo que hace la magia de "seguir" el link hasta el destino
            res = requests.get(url_input, headers=headers, allow_redirects=True, timeout=8)
            url_final = res.url
        except Exception as e:
            logging.error(f"Error expandiendo URL '{url_input}': {e}")
            # Si falla la redirección, abortamos devolviendo None
            return None, None

    # 2. MOTOR DE EXTRACCIÓN (Expresiones Regulares)
    # Lista de los formatos conocidos en los que Google Maps esconde las coordenadas en la URL
    patrones = [
        r'@(-?\d+\.\d+),(-?\d+\.\d+)',      # Formato estándar web: @25.6866,-100.3161
        r'q=(-?\d+\.\d+),(-?\d+\.\d+)',     # Formato de búsqueda: ?q=25.6866,-100.3161
        r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)'   # Formato interno de capas 3D de Google
    ]
    
    # Probamos cada patrón uno por uno contra el link final
    for patron in patrones:
        match = re.search(patron, url_final)
        if match:
            # Si hace match, el grupo 1 es Latitud y el grupo 2 es Longitud
            return float(match.group(1)), float(match.group(2))
            
    # Si la URL final no hizo match con ninguno, no pudimos extraer las coordenadas
    logging.warning(f"No se pudieron extraer coordenadas de la URL final: {url_final}")
    return None, None