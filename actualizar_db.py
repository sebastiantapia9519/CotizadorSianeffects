import sqlite3

def agregar_columna_suscripcion():
    try:
        # Asegúrate de que el nombre de tu archivo DB sea el correcto (ej. database.db)
        conn = sqlite3.connect('database.db') 
        cursor = conn.cursor()
        
        print("🛠️ Agregando columna 'subscription_end' a la tabla 'usuarios'...")
        
        # Agregamos la columna. Si ya existe, dará error y no pasa nada.
        cursor.execute("ALTER TABLE usuarios ADD COLUMN subscription_end TEXT")
        
        conn.commit()
        print("✅ ¡Listo! Columna agregada. Ahora ya puedes registrar usuarios.")
        
    except sqlite3.OperationalError as e:
        print(f"⚠️ Aviso: {e} (Probablemente la columna ya existía o la tabla se llama diferente).")
    except Exception as e:
        print(f"❌ Error inesperado: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    agregar_columna_suscripcion()