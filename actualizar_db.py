import sqlite3
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from dotenv import load_dotenv

load_dotenv()

SQLITE_DB = 'papeleria.db'
POSTGRES_URL = os.getenv('DATABASE_URL')

COLUMNAS_BOOL = [
    'terms_accepted', 'tutorial_visto', 'es_paquete', 
    'inventario_activo', 'mostrar_ayuda', 'modo_oscuro', 
    'ticket_bw', 'activo', 'es_demo', 'camara_premium', 
    'bloquear_edicion_invitados'
]

def corregir_booleanos(data_dict):
    for col in data_dict:
        if col in COLUMNAS_BOOL:
            data_dict[col] = bool(data_dict[col]) if data_dict[col] is not None else False
    return data_dict

def obtener_columnas_reales(cursor, tabla):
    cursor.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name = '{tabla}'")
    return [row[0] for row in cursor.fetchall()]

def ejecutar_migracion_total(target_id):
    try:
        sqlite_conn = sqlite3.connect(SQLITE_DB)
        sqlite_conn.row_factory = sqlite3.Row
        pg_conn = psycopg2.connect(POSTGRES_URL)
        pg_cursor = pg_conn.cursor()

        # --- PASO 0: USUARIO ---
        print(f"👤 Verificando usuario ID {target_id}...")
        pg_cursor.execute("SELECT id FROM usuarios WHERE id = %s", (target_id,))
        if not pg_cursor.fetchone():
            u = sqlite_conn.execute("SELECT * FROM usuarios WHERE id = ?", (target_id,)).fetchone()
            if u:
                d_u = corregir_booleanos(dict(u))
                cols_v = obtener_columnas_reales(pg_cursor, 'usuarios')
                d_u_limpio = {k: v for k, v in d_u.items() if k in cols_v}
                pg_cursor.execute(f"INSERT INTO usuarios ({', '.join(d_u_limpio.keys())}) VALUES ({', '.join(['%s']*len(d_u_limpio))})", list(d_u_limpio.values()))

        # --- PASO 1: TABLAS MAESTRAS ---
        for tabla in ['maquinaria', 'materiales', 'configuracion', 'shipping_configs']:
            pg_cursor.execute(f"DELETE FROM {tabla} WHERE user_id = %s", (target_id,))
            items = sqlite_conn.execute(f"SELECT * FROM {tabla} WHERE user_id = ?", (target_id,)).fetchall()
            cols_v = obtener_columnas_reales(pg_cursor, tabla)
            for item in items:
                d = corregir_booleanos(dict(item))
                if 'id' in d: d.pop('id')
                d_limpio = {k: v for k, v in d.items() if k in cols_v}
                pg_cursor.execute(f"INSERT INTO {tabla} ({', '.join(d_limpio.keys())}) VALUES ({', '.join(['%s']*len(d_limpio))})", list(d_limpio.values()))
            print(f"✅ Tabla {tabla}: {len(items)} registros.")

        # --- PASO 2: VENTAS Y PRODUCTOS ---
        for flujo in [{'padre': 'ventas', 'hijo': 'venta_detalles', 'fk': 'venta_id'},
                      {'padre': 'productos', 'hijo': 'producto_detalles', 'fk': 'producto_id'},
                      {'padre': 'shipping_zones', 'hijo': 'shipping_rates', 'fk': 'zone_id'}]:
            pg_cursor.execute(f"DELETE FROM {flujo['padre']} WHERE user_id = %s", (target_id,))
            registros = sqlite_conn.execute(f"SELECT * FROM {flujo['padre']} WHERE user_id = ?", (target_id,)).fetchall()
            cols_p = obtener_columnas_reales(pg_cursor, flujo['padre'])
            cols_h = obtener_columnas_reales(pg_cursor, flujo['hijo'])
            for p in registros:
                d_p = corregir_booleanos(dict(p))
                old_id = d_p.pop('id')
                p_l = {k: v for k, v in d_p.items() if k in cols_p}
                pg_cursor.execute(f"INSERT INTO {flujo['padre']} ({', '.join(p_l.keys())}) VALUES ({', '.join(['%s']*len(p_l))}) RETURNING id", list(p_l.values()))
                new_id = pg_cursor.fetchone()[0]
                detalles = sqlite_conn.execute(f"SELECT * FROM {flujo['hijo']} WHERE {flujo['fk']} = ?", (old_id,)).fetchall()
                for det in detalles:
                    d_d = corregir_booleanos(dict(det))
                    if 'id' in d_d: d_d.pop('id')
                    d_d[flujo['fk']] = new_id
                    d_l = {k: v for k, v in d_d.items() if k in cols_h}
                    pg_cursor.execute(f"INSERT INTO {flujo['hijo']} ({', '.join(d_l.keys())}) VALUES ({', '.join(['%s']*len(d_l))})", list(d_l.values()))
            print(f"✅ {flujo['padre']} finalizado.")

        # --- PASO 3: CATÁLOGO (LIMPIEZA Y RECARGA) ---
        print("🛍️ Migrando Catálogo...")
        # Limpiamos para evitar duplicados en este paso
        pg_cursor.execute("DELETE FROM catalogo_productos")
        pg_cursor.execute("DELETE FROM categorias")
        
        cats = sqlite_conn.execute("SELECT * FROM categorias").fetchall()
        cols_cat = obtener_columnas_reales(pg_cursor, 'categorias')
        cols_prod = obtener_columnas_reales(pg_cursor, 'catalogo_productos')

        for c in cats:
            d = corregir_booleanos(dict(c))
            old_c_id = d.pop('id')
            d_l = {k: v for k, v in d.items() if k in cols_cat}
            pg_cursor.execute(f"INSERT INTO categorias ({', '.join(d_l.keys())}) VALUES ({', '.join(['%s']*len(d_l))}) RETURNING id", list(d_l.values()))
            new_c_id = pg_cursor.fetchone()[0]
            
            prods = sqlite_conn.execute("SELECT * FROM catalogo_productos WHERE categoria_id = ?", (old_c_id,)).fetchall()
            for p in prods:
                dp = corregir_booleanos(dict(p))
                dp.pop('id')
                dp['categoria_id'] = new_c_id
                dp_l = {k: v for k, v in dp.items() if k in cols_prod}
                pg_cursor.execute(f"INSERT INTO catalogo_productos ({', '.join(dp_l.keys())}) VALUES ({', '.join(['%s']*len(dp_l))})", list(dp_l.values()))

        pg_conn.commit()
        print("\n🚀 ¡MIGRACIÓN EXITOSA!")

    except Exception as e:
        print(f"❌ ERROR: {e}")
        pg_conn.rollback()
    finally:
        sqlite_conn.close()
        pg_conn.close()

if __name__ == "__main__":
    ejecutar_migracion_total(target_id=4)