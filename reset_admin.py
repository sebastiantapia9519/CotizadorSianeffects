import sqlite3
from werkzeug.security import generate_password_hash

# 1. Configuración
DB_NAME = 'papeleria.db'
NUEVO_USER = "admin"       # Tu usuario de siempre
NUEVA_PASS = "admin123"    # La contraseña temporal que quieres poner

def resetear_admin():
    try:
        # Conectar a la base de datos manualmente
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        # Encriptar la nueva contraseña (¡Nunca la guardes en texto plano!)
        hashed_pw = generate_password_hash(NUEVA_PASS)

        # Buscar al usuario con rol 2 (Dueño) y actualizarlo
        # Si prefieres buscar por nombre, cambia: WHERE role = 2  por  WHERE username = 'admin'
        cursor.execute('''
            UPDATE usuarios 
            SET password = ?, username = ? 
            WHERE role = 2
        ''', (hashed_pw, NUEVO_USER))

        if cursor.rowcount > 0:
            print(f"✅ ¡ÉXITO! Se actualizó el Dueño Supremo.")
            print(f"👤 Usuario: {NUEVO_USER}")
            print(f"🔑 Password: {NUEVA_PASS}")
        else:
            print("❌ ERROR: No se encontró ningún usuario con Rol 2 (Dueño) en la base de datos.")

        conn.commit()
        conn.close()

    except Exception as e:
        print(f"💥 Ocurrió un error: {e}")

if __name__ == "__main__":
    print("--- INICIANDO PROTOCOLO DE RECUPERACIÓN ---")
    resetear_admin()