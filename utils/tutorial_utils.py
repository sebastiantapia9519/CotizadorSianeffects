from db import get_db_connection

# ==========================================
# CONFIGURACIÓN DE TUTORIALES (DRIVER.JS)
# ==========================================
# Aquí controlamos qué versión del tutorial está activa.
# Si mañana cambias el cotizador, le pones un 2 aquí y a todos les volverá a salir.
VERSIONES_APP = {
    'cotizador': 2,
    'materiales': 1,
    'equipos': 1,
    'recetas': 1,
    'historial': 2,       
    'configuracion': 1    
}

def debe_mostrar_tutorial(user_id, modulo):
    """
    Verifica en PostgreSQL si el usuario debe ver el tour guiado.
    Retorna True si es nuevo o si hay una versión más reciente disponible.
    """
    conn = get_db_connection()
    cursor = conn.cursor() # 1. EN POSTGRES SIEMPRE USAMOS CURSOR
    
    try:
        # 2. CAMBIO DE ? POR %s (Sintaxis psycopg2)
        cursor.execute(
            "SELECT version_vista FROM tutoriales_estado WHERE user_id = %s AND modulo = %s",
            (user_id, modulo)
        )
        estado = cursor.fetchone()
        
        version_actual_app = VERSIONES_APP.get(modulo, 1)
        
        # Escenario A: Nunca ha visto el tutorial (No existe registro en la tabla)
        if not estado:
            return True
        
        # Escenario B: Ya vio una versión. Comparamos la guardada con la de VERSIONES_APP.
        # En Postgres, fetchone() devuelve un dict (si configuramos el cursor así) o None.
        if estado['version_vista'] < version_actual_app:
            return True
            
        return False

    except Exception as e:
        # Log de error profesional para monitoreo en Railway
        import logging
        logging.error(f"TUTORIAL_CHECK_ERROR: Fallo al verificar módulo '{modulo}' para user {user_id} - {e}")
        current_app.logger.error(f"TUTORIAL_CHECK_ERROR: Fallo al verificar módulo '{modulo}' para user {user_id} - {e}")
        return False
    finally:
        # 3. SIEMPRE CERRAR CURSOR Y CONEXIÓN
        cursor.close()
        conn.close()

def obtener_version_tutorial(modulo):
    """Devuelve la versión actual de un tutorial específico del diccionario VERSIONES_APP"""
    return VERSIONES_APP.get(modulo, 1)
