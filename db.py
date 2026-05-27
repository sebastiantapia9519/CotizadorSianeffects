import os
import psycopg2
import psycopg2.extras
from psycopg2 import pool 
from werkzeug.security import generate_password_hash
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()



# ======================================================
# VARIABLES GLOBALES PARA EL POOL
# ======================================================
_db_pool = None

def get_pool():
    """Crea la piscina de conexiones una sola vez y la mantiene viva."""
    global _db_pool
    if _db_pool is None:
        env = os.getenv("FLASK_ENV", "development")
        
        if env == "production":
            database_url = os.getenv("DATABASE_URL_PROD")
            if "shinkansen" in database_url:
                raise Exception("ERROR: Estás apuntando a DEV en producción")
        else:
            database_url = os.getenv("DATABASE_URL_DEV")
            if "nozomi" in database_url:
                raise Exception("ERROR: Estás apuntando a PROD en desarrollo")

        if not database_url:
            raise ValueError("Falta la variable de base de datos según el entorno.")

        # Creamos un pool de 1 a 20 conexiones persistentes
        _db_pool = psycopg2.pool.ThreadedConnectionPool(
            1, 20, 
            database_url, 
            cursor_factory=psycopg2.extras.DictCursor
        )
    return _db_pool


# ======================================================
# CLASE WRAPPER
# ======================================================
class PooledConnectionWrapper:
    """
    Simula ser una conexión normal. Así, cuando hagas conn.close() 
    en tus rutas, en lugar de matarla, la devuelve al pool.
    """
    def __init__(self, pool):
        self._pool = pool
        self._conn = pool.getconn()

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        # En lugar de destruir la conexión, la regresamos a la alberca
        self._pool.putconn(self._conn)


# ======================================================
# CONEXIÓN (PostgreSQL)
# ======================================================
def get_db_connection():
    """Ahora extrae una conexión viva en milisegundos."""
    p = get_pool()
    return PooledConnectionWrapper(p)

# ======================================================
# INIT DB
# ======================================================
def init_db():
    conn = get_db_connection()
    env = os.getenv("FLASK_ENV", "development")

    if env == "production":
        database_url = os.getenv("DATABASE_URL_PROD")
    else:
        database_url = os.getenv("DATABASE_URL_DEV")

    if not database_url:
        raise ValueError("Falta la variable de base de datos según el entorno.")

    if env == "development" and "nozomi" in database_url:
        raise Exception("ERROR: Estás apuntando a PROD en desarrollo")

    if env == "production" and "shinkansen" in database_url:
        raise Exception("ERROR: Estás apuntando a DEV en producción")

    conn = psycopg2.connect(
        database_url,
        cursor_factory=psycopg2.extras.DictCursor
    )
    return conn

# ======================================================
# INIT DB (HOMOLOGADO CON PRD + UTC)
# ======================================================
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    # =========================
    # 1. USUARIOS
    # =========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE,
        email TEXT UNIQUE,
        password TEXT,
        telefono TEXT,
        company_name TEXT,
        role INTEGER DEFAULT 0,
        subscription_end TIMESTAMP,
        created_at TIMESTAMP,
        terms_accepted BOOLEAN DEFAULT FALSE,
        country_code TEXT DEFAULT 'MX',
        last_login TIMESTAMP,
        origen_registro TEXT DEFAULT 'desconocido',
        utm_campaign TEXT,
        estado_suscripcion TEXT DEFAULT 'Trial',
        fecha_cancelacion TIMESTAMP,
        plan_type TEXT DEFAULT 'Free',
        trial_start TIMESTAMP,
        dias_regalados INTEGER DEFAULT 0,
        recordatorio_enviado BOOLEAN DEFAULT FALSE,
        stripe_customer_id TEXT,
        verificado BOOLEAN DEFAULT FALSE,
        tutorial_visto BOOLEAN DEFAULT FALSE
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tutoriales_estado (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        modulo TEXT NOT NULL,
        version_vista INTEGER DEFAULT 0,
        UNIQUE(user_id, modulo),
        FOREIGN KEY (user_id) REFERENCES usuarios(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS password_resets (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
        token TEXT UNIQUE NOT NULL,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        used BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # =========================
    # 2. CONFIGURACIÓN
    # =========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS configuracion (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        margen_ganancia INTEGER DEFAULT 200,
        nombre_empresa TEXT DEFAULT 'Mi Negocio',
        slogan TEXT DEFAULT 'Servicios Creativos',
        website TEXT DEFAULT '',
        inventario_activo BOOLEAN DEFAULT FALSE,
        icono_empresa TEXT DEFAULT '🎨',
        logo_empresa TEXT DEFAULT '',
        mostrar_ayuda BOOLEAN DEFAULT TRUE,
        modo_oscuro BOOLEAN DEFAULT FALSE,
        ticket_bw BOOLEAN DEFAULT FALSE,
        servicios_mensuales_estimados INTEGER DEFAULT 100,
        porcentaje_gastos_operativos REAL DEFAULT 0,
        notas_ticket VARCHAR(255) DEFAULT '',
        labor_activa BOOLEAN DEFAULT FALSE,
        salario_deseado NUMERIC DEFAULT 15000,
        horas_semanales NUMERIC DEFAULT 20
    )
    """)

    cursor.execute("""
    ALTER TABLE configuracion
    ALTER COLUMN porcentaje_gastos_operativos SET DEFAULT 0
    """)

    # =========================
    # 2.5 GASTOS FIJOS MENSUALES
    # =========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS gastos_fijos (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        nombre TEXT NOT NULL,
        monto_mensual REAL DEFAULT 0,
        activo BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES usuarios(id)
    )
    """)

    # =========================
    # 3. MAQUINARIA
    # =========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS maquinaria (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        nombre TEXT,
        costo_desgaste REAL
    )
    """)

    # =========================
    # 4. MATERIALES (CON STOCK)
    # =========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS materiales (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        nombre TEXT,
        es_paquete BOOLEAN,
        precio_compra REAL,
        cantidad_paquete REAL,
        precio_unitario REAL,
        unidad_medida TEXT,
        stock_actual REAL DEFAULT 0,
        stock_minimo REAL DEFAULT 5
    )
    """)

    # =========================
    # 4.5 MOVIMIENTOS DE INVENTARIO (HISTORIAL)
    # =========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS movimientos_inventario (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        material_id INTEGER,
        tipo TEXT,          
        cantidad REAL,
        motivo TEXT,        
        stock_resultante REAL, 
        fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(material_id) REFERENCES materiales(id)
    )
    """)

    # =========================
    # 5. PRODUCTOS (Cotizador)
    # =========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS productos (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        nombre TEXT,
        items TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS producto_detalles (
        id SERIAL PRIMARY KEY,
        producto_id INTEGER,
        material_id INTEGER,
        cantidad REAL,
        FOREIGN KEY(producto_id) REFERENCES productos(id),
        FOREIGN KEY(material_id) REFERENCES materiales(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS producto_maquinaria (
        id SERIAL PRIMARY KEY,
        producto_id INTEGER,
        maquinaria_id INTEGER,
        FOREIGN KEY(producto_id) REFERENCES productos(id),
        FOREIGN KEY(maquinaria_id) REFERENCES maquinaria(id)
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS logs_actividad (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        accion TEXT,
        modulo TEXT,
        fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        detalle TEXT,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES usuarios(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS auth_codes (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
        email VARCHAR(255) NOT NULL,
        code VARCHAR(6) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        used BOOLEAN DEFAULT FALSE
    )
    """)

    # =========================
    # 6. VENTAS
    # =========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS clientes (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        nombre TEXT NOT NULL,
        contacto TEXT,
        plataforma TEXT,
        notas_cliente TEXT,
        fecha_registro TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ventas (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        cliente TEXT,
        fecha TIMESTAMPTZ NOT NULL,
        subtotal REAL,
        descuento_porcentaje INTEGER,
        descuento_monto REAL,
        total REAL,
        estado TEXT DEFAULT 'pagado',
        monto_pagado REAL DEFAULT 0,
        saldo_pendiente REAL DEFAULT 0,
        fecha_vencimiento TIMESTAMP,
        resumen_items TEXT,
        costo_total REAL DEFAULT 0,
        document_type TEXT DEFAULT 'receipt',
        tax_engine TEXT DEFAULT 'none',
        impuestos REAL DEFAULT 0,
        envio REAL DEFAULT 0,
        cliente_id INTEGER REFERENCES clientes(id),
        fecha_entrega TIMESTAMPTZ,
        metodo_entrega TEXT,
        notas_pedido TEXT,
        costo_fijo_prorrateado NUMERIC DEFAULT 0
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS venta_detalles (
        id SERIAL PRIMARY KEY,
        venta_id INTEGER,
        concepto TEXT,
        cantidad REAL,
        precio_unitario REAL,
        costo_unitario REAL,
        subtotal REAL,
        composicion TEXT,
        FOREIGN KEY(venta_id) REFERENCES ventas(id)
    )
    """)

    # =========================
    # 8. CATÁLOGO
    # =========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS categorias (
        id SERIAL PRIMARY KEY,
        nombre TEXT NOT NULL,
        descripcion TEXT,
        orden INTEGER DEFAULT 0,      
        activo BOOLEAN DEFAULT TRUE,    
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS catalogo_productos (
        id SERIAL PRIMARY KEY,
        categoria_id INTEGER,     
        sku TEXT NOT NULL,
        titulo TEXT NOT NULL,
        descripcion TEXT,
        media_url TEXT NOT NULL,
        media_type TEXT DEFAULT 'image',
        precio REAL DEFAULT 0,
        stock BOOLEAN DEFAULT TRUE,
        orden INTEGER DEFAULT 0,
        activo BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (categoria_id) REFERENCES categorias (id)
    )
    """)

    # =========================
    # 9. ENVÍOS (LOGÍSTICA)
    # =========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS shipping_configs (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        origin_address TEXT, 
        origin_lat REAL,
        origin_lng REAL,
        local_base_rate REAL DEFAULT 35,
        local_km_rate REAL DEFAULT 8,
        free_shipping_threshold REAL DEFAULT 0,
        safety_margin_percent INTEGER DEFAULT 10,
        FOREIGN KEY(user_id) REFERENCES usuarios(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS shipping_zones (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        zone_name TEXT,
        states_included TEXT, 
        FOREIGN KEY(user_id) REFERENCES usuarios(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS shipping_rates (
        id SERIAL PRIMARY KEY,
        zone_id INTEGER,
        max_weight_kg REAL,
        price REAL,
        FOREIGN KEY(zone_id) REFERENCES shipping_zones(id)
    )
    """)

    # =========================
    # 10. INVITACIONES Y PLANNERS
    # =========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS lista_musica (
        id SERIAL PRIMARY KEY,
        nombre_cancion TEXT,
        url_cloudflare TEXT,
        activa BOOLEAN DEFAULT TRUE
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS invitaciones (
        id SERIAL PRIMARY KEY,
        slug TEXT UNIQUE,           
        config_json TEXT,           
        musica_id INTEGER,          
        fecha_evento TIMESTAMP,
        vigencia TIMESTAMP,         
        datos_cliente_json TEXT,     
        fotos_json TEXT,
        foto_portada_url TEXT,
        estilo_fuente TEXT DEFAULT 'clasico',
        color_fondo TEXT DEFAULT '#fdfbf7',
        url_fondo TEXT,
        mesas_regalos_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        dress_code TEXT,
        hospedaje_json TEXT,
        album_url TEXT,
        color_acentos TEXT DEFAULT '#D4AF37',
        camara_premium BOOLEAN DEFAULT FALSE,
        padres_novia TEXT,
        padres_novio TEXT,
        padrinos TEXT,
        frase_final TEXT,
        historia_json TEXT,
        es_demo BOOLEAN DEFAULT FALSE,
        tipo_evento TEXT DEFAULT 'boda',
        bloquear_edicion_invitados BOOLEAN DEFAULT FALSE,
        template_id TEXT DEFAULT 'clasico',
        tiene_modulo_invitados BOOLEAN DEFAULT FALSE,
        estilo_apertura TEXT DEFAULT 'simple',
        codigo_acceso_cliente TEXT UNIQUE,
        mesas_json TEXT DEFAULT '[]',
        planner_id INTEGER,
        FOREIGN KEY(musica_id) REFERENCES lista_musica(id)
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fotos_invitados (
        id SERIAL PRIMARY KEY,
        invitacion_id INTEGER NOT NULL,
        url TEXT NOT NULL,
        camara_premium BOOLEAN DEFAULT FALSE,
        fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS pases_invitados (
        id SERIAL PRIMARY KEY,
        invitacion_id INTEGER,
        nombre_familia TEXT NOT NULL,
        pases_totales INTEGER DEFAULT 2,
        pases_usados INTEGER DEFAULT 0,
        codigo_qr_unique TEXT UNIQUE,
        mensaje_personalizado TEXT,
        telefono TEXT,
        nombres_acompanantes_json TEXT,
        mesa TEXT DEFAULT '0',
        estado_asistencia TEXT DEFAULT 'Pendiente',
        FOREIGN KEY (invitacion_id) REFERENCES invitaciones(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS buenos_deseos (
        id SERIAL PRIMARY KEY,
        invitacion_id INTEGER,
        nombre TEXT NOT NULL,
        mensaje TEXT NOT NULL,
        fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (invitacion_id) REFERENCES invitaciones(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS planners (
        id SERIAL PRIMARY KEY,
        nombre_contacto TEXT,
        nombre_empresa TEXT,
        telefono TEXT,
        codigo_acceso_planner TEXT UNIQUE, 
        notas TEXT,
        estado TEXT DEFAULT 'activo',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS planner_paquetes (
        id SERIAL PRIMARY KEY,
        planner_id INTEGER,
        cantidad_total INTEGER,   
        cantidad_usada INTEGER DEFAULT 0,
        fecha_compra TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        fecha_vencimiento TIMESTAMP, 
        activo BOOLEAN DEFAULT TRUE,
        notas TEXT,
        FOREIGN KEY(planner_id) REFERENCES planners(id)
    )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS anuncios_globales (
            id SERIAL PRIMARY KEY,
            titulo VARCHAR(255) NOT NULL,
            mensaje TEXT NOT NULL,
            tipo VARCHAR(50) DEFAULT 'info',
            url TEXT,
            activo BOOLEAN DEFAULT TRUE,
            fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS anuncios_vistos (
            user_id INTEGER NOT NULL,
            anuncio_id INTEGER NOT NULL,
            fecha_visto TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notificaciones_manuales (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            titulo VARCHAR(255) NOT NULL,
            mensaje TEXT NOT NULL,
            tipo VARCHAR(50) DEFAULT 'info',
            leida BOOLEAN DEFAULT FALSE,
            url TEXT,
            batch_id VARCHAR(255),
            fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # =========================
    # 7. SUPER ADMIN (SEED BLINDADO)
    # =========================
    cursor.execute("SELECT id FROM usuarios WHERE email = 'contacto@sianeffects.com'")
    admin = cursor.fetchone()

    if not admin:
        now_utc_str = datetime.now(timezone.utc).isoformat()
        hashed_pw = generate_password_hash('admin123')

        # Insertamos el admin y obtenemos el ID generado en Postgres
        cursor.execute("""
        INSERT INTO usuarios (
            username, email, password, company_name, role,
            subscription_end, created_at, terms_accepted,
            country_code, last_login
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """, (
            'admin', 'contacto@sianeffects.com', hashed_pw, 'Sianeffects', 2,
            '2099-12-31 23:59:59', now_utc_str, True, 'MX', now_utc_str
        ))
        
        admin_id = cursor.fetchone()['id']

        cursor.execute("""
        INSERT INTO configuracion (user_id, margen_ganancia, nombre_empresa)
        VALUES (%s, %s, %s)
        """, (admin_id, 200, 'Sianeffects Admin'))

        for nombre, costo in [('Corte Plotter', 5.0), ('Impresión', 1.5), ('Plancha Calor', 12.0)]:
            cursor.execute("""
            INSERT INTO maquinaria (user_id, nombre, costo_desgaste)
            VALUES (%s, %s, %s)
            """, (admin_id, nombre, costo))

    cursor.close()
    conn.close()

if __name__ == '__main__':
    init_db()
    print("Base de datos PostgreSQL inicializada correctamente.")
