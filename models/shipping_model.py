import json
from db import get_db_connection

class ShippingModel:
    
    @staticmethod
    def get_config(user_id):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM shipping_configs WHERE user_id = %s", (user_id,)
        )
        config = cursor.fetchone()
        cursor.close()
        conn.close()
        return config

    @staticmethod
    def get_rate_for_zone(zone_id, weight):
        """Busca la tarifa adecuada para el peso y zona"""
        conn = get_db_connection()
        cursor = conn.cursor()
        # Buscamos la tarifa donde el peso máximo sea mayor o igual al peso del paquete
        # Ordenamos por peso para agarrar la más cercana (la más barata que cubra el peso)
        cursor.execute("""
            SELECT * FROM shipping_rates 
            WHERE zone_id = %s AND max_weight_kg >= %s
            ORDER BY max_weight_kg ASC
            LIMIT 1
        """, (zone_id, weight))
        rate = cursor.fetchone()
        cursor.close()
        conn.close()
        return rate

    @staticmethod
    def get_zone_by_state(user_id, state_code):
        """Busca en qué zona está un estado (ej: 'NL')"""
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM shipping_zones WHERE user_id = %s", (user_id,))
        zones = cursor.fetchall()
        cursor.close()
        conn.close()
        
        for zone in zones:
            try:
                # BLINDAJE POSTGRES: Si la columna es JSONB, ya viene como lista. Si es texto, la parseamos.
                datos_estados = zone['states_included']
                states = json.loads(datos_estados) if isinstance(datos_estados, str) else datos_estados
                
                if "ALL" in states or state_code in states:
                    return zone
            except Exception:
                pass # Si hay un error de parseo en una zona, la ignoramos y seguimos buscando
                
        return None