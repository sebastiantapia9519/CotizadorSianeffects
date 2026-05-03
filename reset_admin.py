import os
from werkzeug.security import generate_password_hash
# Importamos tu conexión centralizada a PostgreSQL
from db import get_db_connection

# 1. Configuración
NUEVO_USER = "admin"       # Tu usuario de siempre
NUEVA_PASS = "admin123"    # La contraseña temporal que quieres poner

def resetear_admin():
    try:
        # Conectar a PostgreSQL en Railway usando tu helper
        conn = get_db_connection()
        cursor = conn.cursor()

        # Encriptar la nueva contraseña (¡Nunca la guardes en texto plano!)
        hashed_pw = generate_password_hash(NUEVA_PASS)

        # Buscar al usuario con rol 2 (Dueño) y actualizarlo
        # POSTGRES: Usamos %s en lugar de ?
        cursor.execute('''
            UPDATE usuarios 
            SET password = %s, username = %s 
            WHERE role = 2
        ''', (hashed_pw, NUEVO_USER))

        if cursor.rowcount > 0:
            print(f"¡ÉXITO! Se actualizó el Dueño Supremo en la nube.")
            print(f"Usuario: {NUEVO_USER}")
            print(f"Password: {NUEVA_PASS}")
        else:
            print("ERROR: No se encontró ningún usuario con Rol 2 (Dueño) en la base de datos.")

        conn.commit()

    except Exception as e:
        print(f"Ocurrió un error: {e}")
        current_app.logger.error(f"Error en reset_admin: {e}")
    finally:
        # Siempre cerramos el cursor y la conexión para no dejar procesos colgados
        if 'cursor' in locals() and cursor: cursor.close()
        if 'conn' in locals() and conn: conn.close()

if __name__ == "__main__":
    print("--- INICIANDO PROTOCOLO DE RECUPERACIÓN (POSTGRESQL) ---")
    resetear_admin()