import os
import sqlite3
from werkzeug.security import generate_password_hash
from datetime import datetime, timezone
from dotenv import load_dotenv

# Cargamos dotenv aquí también por si ejecutamos db.py por separado (scripts)
load_dotenv()

DB_NAME = os.getenv('DB_NAME', 'papeleria.db')

# ======================================================
# CONEXIÓN
# ======================================================
def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

# ======================================================
# INIT DB (HOMOLOGADO CON PRD + UTC)
# ======================================================
def init_db():
    conn = get_db_connection()

    # =========================
    # 1. USUARIOS
    # =========================
    conn.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        email TEXT UNIQUE,
        password TEXT,
        telefono TEXT,
        company_name TEXT,
        role INTEGER DEFAULT 0,
        subscription_end DATETIME,
        created_at DATETIME,
        terms_accepted BOOLEAN DEFAULT 0,
        country_code TEXT DEFAULT 'MX',
        last_login DATETIME,
        tutorial_visto BOOLEAN DEFAULT 0
    )
    """)

    # =========================
    # 2. CONFIGURACIÓN (CON CAMPO INVENTARIO)
    # =========================
    conn.execute("""
    CREATE TABLE IF NOT EXISTS configuracion (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        margen_ganancia INTEGER DEFAULT 200,
        nombre_empresa TEXT DEFAULT 'Mi Negocio',
        slogan TEXT DEFAULT 'Servicios Creativos',
        website TEXT DEFAULT '',
        inventario_activo BOOLEAN DEFAULT 0  -- <--- NUEVO CAMPO (Feature Flag)
    )
    """)

    # =========================
    # 3. MAQUINARIA
    # =========================
    conn.execute("""
    CREATE TABLE IF NOT EXISTS maquinaria (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        nombre TEXT,
        costo_desgaste REAL
    )
    """)

    # =========================
    # 4. MATERIALES (CON STOCK)
    # =========================
    conn.execute("""
    CREATE TABLE IF NOT EXISTS materiales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        nombre TEXT,
        es_paquete BOOLEAN,
        precio_compra REAL,
        cantidad_paquete REAL,
        precio_unitario REAL,
        stock_actual REAL DEFAULT 0,   -- <--- NUEVO
        stock_minimo REAL DEFAULT 5    -- <--- NUEVO (Alerta)
    )
    """)

    # =========================
    # 4.5 MOVIMIENTOS DE INVENTARIO (HISTORIAL)
    # =========================
    conn.execute("""
    CREATE TABLE IF NOT EXISTS movimientos_inventario (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        material_id INTEGER,
        tipo TEXT,          -- 'entrada' (compra) o 'salida' (venta/uso)
        cantidad REAL,
        motivo TEXT,        -- Ej: 'Venta #123', 'Compra Factura A', 'Ajuste Manual'
        stock_resultante REAL, -- Cuánto quedó después del movimiento
        fecha DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(material_id) REFERENCES materiales(id)
    )
    """)

    # =========================
    # 5. PRODUCTOS
    # =========================
    conn.execute("""
    CREATE TABLE IF NOT EXISTS productos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        nombre TEXT,
        items TEXT
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS producto_detalles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        producto_id INTEGER,
        material_id INTEGER,
        cantidad REAL,
        FOREIGN KEY(producto_id) REFERENCES productos(id),
        FOREIGN KEY(material_id) REFERENCES materiales(id)
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS producto_maquinaria (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        producto_id INTEGER,
        maquinaria_id INTEGER,
        FOREIGN KEY(producto_id) REFERENCES productos(id),
        FOREIGN KEY(maquinaria_id) REFERENCES maquinaria(id)
    )
    """)

    # =========================
    # 6. VENTAS
    # =========================
    conn.execute("""
    CREATE TABLE IF NOT EXISTS ventas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        cliente TEXT,
        fecha DATETIME,
        subtotal REAL,
        descuento_porcentaje INTEGER,
        descuento_monto REAL,
        total REAL,
        estado TEXT DEFAULT 'pagado',
        monto_pagado REAL DEFAULT 0,
        saldo_pendiente REAL DEFAULT 0,
        fecha_vencimiento DATETIME,
        resumen_items TEXT,
        costo_total REAL DEFAULT 0,
        document_type TEXT DEFAULT 'receipt',
        tax_engine TEXT DEFAULT 'none',
        impuestos REAL DEFAULT 0
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS venta_detalles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    # 7. SUPER ADMIN
    # =========================
    admin = conn.execute(
        "SELECT id FROM usuarios WHERE username = 'admin'"
    ).fetchone()

    if not admin:
        now_utc_str = datetime.now(timezone.utc).isoformat()
        hashed_pw = generate_password_hash('admin123')

        conn.execute("""
        INSERT INTO usuarios (
            username, email, password, company_name, role,
            subscription_end, created_at, terms_accepted,
            country_code, last_login
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            'admin', 'contacto@sianeffects.com', hashed_pw, 'SianEffects HQ', 2,
            '2099-12-31T23:59:59Z', now_utc_str, 1, 'MX', now_utc_str
        ))

        admin_id = conn.execute(
            "SELECT id FROM usuarios WHERE username = 'admin'"
        ).fetchone()['id']

        conn.execute("""
        INSERT INTO configuracion (user_id, margen_ganancia, nombre_empresa)
        VALUES (?, ?, ?)
        """, (admin_id, 200, 'SianEffects Admin'))

        for nombre, costo in [('Corte Plotter', 5.0), ('Impresión', 1.5), ('Plancha Calor', 12.0)]:
            conn.execute("""
            INSERT INTO maquinaria (user_id, nombre, costo_desgaste)
            VALUES (?, ?, ?)
            """, (admin_id, nombre, costo))

    # =========================
    # 8. CATALOGO
    # =========================
    conn.execute("""
    CREATE TABLE IF NOT EXISTS categorias (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        descripcion TEXT,
        orden INTEGER DEFAULT 0,      
        activo BOOLEAN DEFAULT 1,    
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS productos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        categoria_id INTEGER,     
        sku TEXT NOT NULL,
        titulo TEXT NOT NULL,
        descripcion TEXT,
        media_url TEXT NOT NULL,
        media_type TEXT DEFAULT 'image',
        precio REAL DEFAULT 0,
        stock INTEGER DEFAULT 1,
        orden INTEGER DEFAULT 0,
        activo BOOLEAN DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (categoria_id) REFERENCES categorias (id)
    )
    """)


 # =========================
    # 9. ENVÍOS (LOGÍSTICA)
    # =========================
    conn.execute("""
    CREATE TABLE IF NOT EXISTS shipping_configs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        origin_address TEXT, 
        origin_lat REAL,
        origin_lng REAL,
        local_base_rate REAL DEFAULT 0,
        local_km_rate REAL DEFAULT 0,
        free_shipping_threshold REAL DEFAULT 0,
        safety_margin_percent INTEGER DEFAULT 10,
        FOREIGN KEY(user_id) REFERENCES usuarios(id)
    )
    """)

    # Tabla de Zonas (Nacional)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS shipping_zones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        zone_name TEXT,
        states_included TEXT, -- Guardamos el JSON como texto en SQLite
        FOREIGN KEY(user_id) REFERENCES usuarios(id)
    )
    """)

    # Tabla de Tarifas
    conn.execute("""
    CREATE TABLE IF NOT EXISTS shipping_rates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        zone_id INTEGER,
        max_weight_kg REAL,
        price REAL,
        FOREIGN KEY(zone_id) REFERENCES shipping_zones(id)
    )
    """)


    # SECCION DE INVITACIONES

    #-- Tabla para tus 5 canciones gestionables
    conn.execute("""
    CREATE TABLE IF NOT EXISTS lista_musica (
        id INTEGER PRIMARY KEY,
        nombre_cancion TEXT,
        url_cloudflare TEXT,
        activa BOOLEAN DEFAULT 1
    );
    """)

#-- Tabla principal de la invitación
    conn.execute("""
    CREATE TABLE IF NOT EXISTS invitaciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE,           -- Ejemplo: 'boda-sebastian-y-atlas'
        config_json TEXT,           -- Aquí guardamos el ORDEN de los items [1, 5, 2...]
        musica_id INTEGER,          -- Cuál de tus 5 canciones suena
        fecha_evento DATETIME,
        vigencia DATETIME,          -- Hasta cuándo funciona el link
        datos_cliente_json TEXT,     -- Nombres, Maps, Cuenta bancaria, etc.
        fotos_json TEXT,            -- URLs de las 5 fotos en Cloudflare
        FOREIGN KEY(musica_id) REFERENCES lista_musica(id)
    );
    """)

    conn.commit()
    conn.close()