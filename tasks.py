import sqlite3
from datetime import datetime, timedelta

def ejecutar_limpieza_sianeffects():
    # Conectar a tu base de datos (ajusta la ruta a tu archivo .db)
    conn = sqlite3.connect('sianeffects.db')
    cursor = conn.cursor()
    
    hoy = datetime.now()
    
    # 1. Definir los plazos
    # 3 meses (90 días) para bloqueo de datos
    fecha_limite_datos = (hoy - timedelta(days=90)).strftime('%Y-%m-%d %H:%M:%S')
    
    # 12 meses (365 días) para borrado de cuenta
    fecha_limite_cuenta = (hoy - timedelta(days=365)).strftime('%Y-%m-%d %H:%M:%S')

    print(f"--- Iniciando depuración de SianEffects ({hoy.strftime('%Y-%m-%d')}) ---")

    # 2. BORRADO DEFINITIVO (12 meses de inactividad)
    # Solo usuarios con role 0 (clientes) y cuya suscripción terminó hace más de un año
    cursor.execute("""
        DELETE FROM usuarios 
        WHERE role = 0 
        AND subscription_end < ?
    """, (fecha_limite_cuenta,))
    
    cuentas_borradas = cursor.rowcount
    if cuentas_borradas > 0:
        print(f"[*] Se eliminaron {cuentas_borradas} cuentas por inactividad de 12 meses.")

    # 3. IDENTIFICAR USUARIOS PARA RESTRICCIÓN (Opcional)
    # Como no tienes un campo 'status', puedes usar 'last_login' para auditoría 
    # o simplemente confiar en la lógica de tu App al momento del login.
    
    conn.commit()
    conn.close()
    print("--- Depuración finalizada con éxito ---")

if __name__ == "__main__":
    ejecutar_limpieza_sianeffects()