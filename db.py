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
        last_login DATETIME
    )
    """)

    # =========================
    # 2. CONFIGURACIÓN
    # =========================
    conn.execute("""
    CREATE TABLE IF NOT EXISTS configuracion (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        margen_ganancia INTEGER DEFAULT 200,
        nombre_empresa TEXT DEFAULT 'Mi Negocio',
        slogan TEXT DEFAULT 'Servicios Creativos',
        website TEXT DEFAULT ''
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
    # 4. MATERIALES
    # =========================
    conn.execute("""
    CREATE TABLE IF NOT EXISTS materiales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        nombre TEXT,
        es_paquete BOOLEAN,
        precio_compra REAL,
        cantidad_paquete REAL,
        precio_unitario REAL
    )
    """)

    # =========================
    # 5. PRODUCTOS
    # =========================
    conn.execute("""
    CREATE TABLE IF NOT EXISTS productos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        nombre TEXT
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
        tax_engine TEXT DEFAULT 'none'
    )
    """)

    # CORRECCIÓN AQUÍ: Faltaba la coma después de composicion TEXT
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
    # 7. SUPER ADMIN (SI NO EXISTE)
    # =========================
    admin = conn.execute(
        "SELECT id FROM usuarios WHERE username = 'admin'"
    ).fetchone()

    if not admin:
        now_utc = datetime.now(timezone.utc).isoformat()

        hashed_pw = generate_password_hash('admin123')

        conn.execute("""
        INSERT INTO usuarios (
            username, email, password, company_name, role,
            subscription_end, created_at, terms_accepted,
            country_code, last_login
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            'admin',
            'contacto@sianeffects.com',
            hashed_pw,
            'SianEffects HQ',
            2,
            '2099-12-31T23:59:59Z',
            now_utc,
            1,
            'MX',
            now_utc
        ))

        admin_id = conn.execute(
            "SELECT id FROM usuarios WHERE username = 'admin'"
        ).fetchone()['id']

        conn.execute("""
        INSERT INTO configuracion (user_id, margen_ganancia, nombre_empresa)
        VALUES (?, ?, ?)
        """, (admin_id, 200, 'SianEffects Admin'))

        for nombre, costo in [
            ('Corte Plotter', 5.0),
            ('Impresión', 1.5),
            ('Plancha Calor', 12.0)
        ]:
            conn.execute("""
            INSERT INTO maquinaria (user_id, nombre, costo_desgaste)
            VALUES (?, ?, ?)
            """, (admin_id, nombre, costo))

    conn.commit()
    conn.close()