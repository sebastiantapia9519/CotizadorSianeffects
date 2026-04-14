from db import get_db_connection

# ==========================================
# CONFIGURACIÓN DE TUTORIALES (DRIVER.JS)
# ==========================================
# Aquí controlamos qué versión del tutorial está activa.
# Si mañana cambias el cotizador, le pones un 2 aquí y a todos les volverá a salir.
VERSIONES_APP = {
    'cotizador': 1,
    'materiales': 1,
    'equipos': 1,
    'recetas': 1,
    'historial': 1,       
    'configuracion': 1    
}

def debe_mostrar_tutorial(user_id, modulo):
    conn = get_db_connection()
    try:
        # Buscamos qué versión vio el usuario de este módulo
        estado = conn.execute(
            "SELECT version_vista FROM tutoriales_estado WHERE user_id = ? AND modulo = ?",
            (user_id, modulo)
        ).fetchone()
        
        version_actual_app = VERSIONES_APP.get(modulo, 1)
        
        # Escenario A: Nunca ha visto el tutorial (No existe en la tabla)
        if not estado:
            return True
        
        # Escenario B: Ya vio una versión, ¿es menor a la más nueva?
        if estado['version_vista'] < version_actual_app:
            return True
            
        return False
    except Exception as e:
        # Si falla (ej. tabla no creada), retornamos False para no romper la app
        print(f"Error verificando tutorial: {e}")
        return False
    finally:
        conn.close()

def obtener_version_tutorial(modulo):
    """Devuelve la versión actual de un tutorial específico"""
    return VERSIONES_APP.get(modulo, 1)