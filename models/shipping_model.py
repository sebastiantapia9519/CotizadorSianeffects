import json
from db import get_db_connection

class ShippingModel:
    
    @staticmethod
    def get_config(user_id):
        conn = get_db_connection()
        config = conn.execute(
            "SELECT * FROM shipping_configs WHERE user_id = ?", (user_id,)
        ).fetchone()
        conn.close()
        return config

    @staticmethod
    def get_rate_for_zone(zone_id, weight):
        """Busca la tarifa adecuada para el peso y zona"""
        conn = get_db_connection()
        # Buscamos la tarifa donde el peso máximo sea mayor o igual al peso del paquete
        # Ordenamos por peso para agarrar la más cercana (la más barata que cubra el peso)
        rate = conn.execute("""
            SELECT * FROM shipping_rates 
            WHERE zone_id = ? AND max_weight_kg >= ?
            ORDER BY max_weight_kg ASC
            LIMIT 1
        """, (zone_id, weight)).fetchone()
        conn.close()
        return rate

    @staticmethod
    def get_zone_by_state(user_id, state_code):
        """Busca en qué zona está un estado (ej: 'NL')"""
        conn = get_db_connection()
        zones = conn.execute("SELECT * FROM shipping_zones WHERE user_id = ?", (user_id,)).fetchall()
        conn.close()
        
        for zone in zones:
            # Convertimos el texto guardado en DB de vuelta a lista Python
            states = json.loads(zone['states_included']) 
            if "ALL" in states or state_code in states:
                return zone
        return None