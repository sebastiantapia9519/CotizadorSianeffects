from geopy.distance import geodesic
from models.shipping_model import ShippingModel

class ShippingService:
    
    def __init__(self, user_id):
        self.user_id = user_id
        # Obtenemos la config real de la base de datos
        self.config = ShippingModel.get_config(user_id)

    def calcular_peso_volumetrico(self, largo, ancho, alto):
        return (largo * ancho * alto) / 5000

    def cotizar_local(self, destino_lat, destino_lng):
        if not self.config:
            return {"error": "Configuración de envío no encontrada"}
            
        if not self.config['origin_lat'] or not self.config['origin_lng']:
            return {"error": "Falta configurar la ubicación de origen"}

        origen = (self.config['origin_lat'], self.config['origin_lng'])
        destino = (destino_lat, destino_lng)
        
        distancia_km = geodesic(origen, destino).km
        
        costo_puro = self.config['local_base_rate'] + (distancia_km * self.config['local_km_rate'])
        margen = 1 + (self.config['safety_margin_percent'] / 100)
        
        total = costo_puro * margen
        
        return {
            "tipo": "local",
            "distancia_km": round(distancia_km, 2),
            "costo_sugerido": round(total, 2)
        }

    def cotizar_nacional(self, peso_kg, largo, ancho, alto, estado_destino):
        if not self.config:
            return {"error": "Configuración no encontrada"}

        # 1. Calcular peso cobrable
        peso_vol = self.calcular_peso_volumetrico(largo, ancho, alto)
        peso_cobrable = max(float(peso_kg), peso_vol)
        
        # 2. Encontrar la zona
        zona = ShippingModel.get_zone_by_state(self.user_id, estado_destino)
        if not zona:
            return {"error": f"No hay cobertura configurada para {estado_destino}"}
            
        # 3. Encontrar la tarifa
        tarifa = ShippingModel.get_rate_for_zone(zona['id'], peso_cobrable)
        
        if not tarifa:
            return {"error": "El paquete excede el peso máximo configurado para esta zona"}
            
        return {
            "tipo": "nacional",
            "zona": zona['zone_name'],
            "peso_cobrable": round(peso_cobrable, 2),
            "costo_sugerido": tarifa['price']
        }