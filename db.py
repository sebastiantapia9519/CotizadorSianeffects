"""
================================================================================
DATABASE MODULE - SIANEFFECTS
================================================================================
Gestor de conexiones PostgreSQL con pool optimizado y tablas con índices.

Características:
  - ThreadedConnectionPool para manejo eficiente de conexiones
  - Wrapper transparente que simula conexiones normales
  - Inicialización automática de tablas con índices optimizados
  - Validación de entorno (DEV vs PROD) para evitar errores críticos
  - Timestamps en UTC para consistencia global
  
Autor: Sebastian (Sianeffects)
Última actualización: Mayo 2026
================================================================================
"""

import os
import psycopg2
import psycopg2.extras
from psycopg2 import pool
from werkzeug.security import generate_password_hash
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()


# ================================================================================
# SECCIÓN 1: CONFIGURACIÓN DEL POOL DE CONEXIONES
# ================================================================================
"""
Un "pool" es un conjunto de conexiones reutilizables a la base de datos.
En lugar de crear una conexión nueva cada vez (lento), mantenemos 1-20 
conexiones "vivas" y las prestamos/devolvemos según sea necesario.

ThreadedConnectionPool = seguro para usar en threads múltiples (Flask)
1 = conexión mínima (desarrollo)
20 = conexión máxima (permite 20 requests simultáneos)
"""

_db_pool = None  # Variable global para almacenar el pool (una sola vez)


def get_pool():
    """
    Obtiene o crea el pool de conexiones.
    
    Esta función usa "lazy initialization": el pool se crea la PRIMERA vez
    que se llama, y luego se reutiliza siempre. Esto es eficiente porque:
    - No crea conexiones si no las necesitas
    - Las conexiones persisten durante toda la ejecución de la app
    
    Args:
        None
        
    Returns:
        psycopg2.pool.ThreadedConnectionPool: Pool de conexiones a PostgreSQL
        
    Raises:
        Exception: Si la URL de BD apunta a la DB equivocada (DEV en PROD, etc)
        ValueError: Si falta la variable de entorno de BD
        
    Validación de Seguridad:
        - En PROD: rechaza URLs con "shinkansen" (BD de DEV)
        - En DEV: rechaza URLs con "nozomi" (BD de PROD)
        Esto evita sobrescribir datos de producción por accidente
    """
    global _db_pool
    
    if _db_pool is None:
        # =============================
        # 1.1 Determinar entorno
        # =============================
        env = os.getenv("FLASK_ENV", "development")
        
        # =============================
        # 1.2 Obtener URL de BD según entorno
        # =============================
        if env == "production":
            database_url = os.getenv("DATABASE_URL_PROD")
            # Validación: rechazar si contiene "shinkansen" (apunta a DEV)
            if database_url and "shinkansen" in database_url:
                raise Exception(
                    "❌ ERROR CRÍTICO: FLASK_ENV=production pero DATABASE_URL_PROD "
                    "apunta a DEV (contiene 'shinkansen'). "
                    "Esto podría sobrescribir datos de desarrollo. Abortar."
                )
        else:
            database_url = os.getenv("DATABASE_URL_DEV")
            # Validación: rechazar si contiene "nozomi" (apunta a PROD)
            if database_url and "nozomi" in database_url:
                raise Exception(
                    "❌ ERROR CRÍTICO: FLASK_ENV=development pero DATABASE_URL_DEV "
                    "apunta a PROD (contiene 'nozomi'). "
                    "Esto podría leer datos de producción. Abortar."
                )

        # =============================
        # 1.3 Validar que existe la URL
        # =============================
        if not database_url:
            raise ValueError(
                f"❌ Falta variable de entorno: "
                f"DATABASE_URL_{env.upper()} no está configurada. "
                f"Define en .env o en Heroku/Railway"
            )

        # =============================
        # 1.4 Crear el pool
        # =============================
        """
        ThreadedConnectionPool(minconn, maxconn, dsn, ...)
          minconn: 1  = crea 1 conexión al inicio (ligero)
          maxconn: 20 = máximo 20 conexiones simultáneas (suficiente para SaaS pequeño)
          
          cursor_factory=DictCursor: hace que los resultados sean diccionarios
          en lugar de tuplas. Así puedes hacer: row['user_id'] en lugar de row[0]
        """
        _db_pool = psycopg2.pool.ThreadedConnectionPool(
            1, 20,
            database_url,
            cursor_factory=psycopg2.extras.DictCursor
        )
        
        print(f"✅ Pool de conexiones creado ({env.upper()})")
    
    return _db_pool


# ================================================================================
# SECCIÓN 2: WRAPPER DE CONEXIÓN
# ================================================================================
"""
Este wrapper es un "patrón de diseño" que permite que nuestro código Flask
trate la conexión del pool como si fuera una conexión normal de psycopg2.

La magia: cuando llamamos a conn.close(), en lugar de destruir la conexión,
la devuelve al pool para que otro request la reutilice.

Esto es crítico para rendimiento: crear/destruir conexiones es lento.
Reutilizarlas es rápido.
"""


class PooledConnectionWrapper:
    """
    Simula una conexión PostgreSQL normal, pero internamente usa el pool.
    
    Ejemplo de uso:
        conn = get_db_connection()  # Obtiene conexión del pool
        cursor = conn.cursor()       # Crea cursor
        cursor.execute("SELECT ...")
        conn.commit()                # Commit
        conn.close()                 # ← Devuelve al pool (no destruye)
    
    Atributos privados (prefijo _):
        _pool: referencia al pool global
        _conn: conexión actual obtenida del pool
    """
    
    def __init__(self, pool):
        """
        Inicializa el wrapper obteniendo una conexión del pool.
        
        Args:
            pool: psycopg2.pool.ThreadedConnectionPool
        """
        self._pool = pool
        # getconn() OBTIENE una conexión viva (o espera si todas están en uso)
        self._conn = pool.getconn()
    
    def cursor(self, *args, **kwargs):
        """
        Crea un cursor desde la conexión del pool.
        
        Los argumentos se pasan tal cual a psycopg2.connection.cursor()
        Ejemplo: cursor(cursor_factory=RealDictCursor)
        
        Returns:
            psycopg2.extensions.cursor: Cursor listo para ejecutar queries
        """
        return self._conn.cursor(*args, **kwargs)
    
    def commit(self):
        """
        Confirma los cambios en la base de datos.
        
        Sin esto, los INSERTs/UPDATEs/DELETEs se revierten automáticamente.
        
        Ejemplo:
            cursor.execute("UPDATE usuarios SET nombre = %s", (name,))
            conn.commit()  # ← Guardar cambios de verdad
        """
        self._conn.commit()
    
    def rollback(self):
        """
        Deshace los cambios desde el último commit.
        
        Útil si algo falla a mitad de una transacción.
        
        Ejemplo:
            try:
                cursor.execute("INSERT ...")
                cursor.execute("UPDATE ...")
                conn.commit()
            except Exception as e:
                conn.rollback()  # Undo de ambas queries
                raise
        """
        self._conn.rollback()
    
    def close(self):
        """
        Devuelve la conexión al pool (NO la destruye).
        
        Esto es la magia del wrapper. En lugar de:
            self._conn.close()  # ← Destruir (lento)
        
        Hacemos:
            self._pool.putconn(self._conn)  # ← Devolver al pool (rápido)
        
        La siguiente request reutilizará esta misma conexión en milisegundos.
        """
        self._pool.putconn(self._conn)


# ================================================================================
# SECCIÓN 3: FUNCIÓN PRINCIPAL DE CONEXIÓN
# ================================================================================
"""
Esta es la función que usarás en TODAS tus rutas Flask:

    @app.route('/api/ventas')
    def get_ventas():
        conn = get_db_connection()  # ← UNA LÍNEA
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM ventas WHERE user_id = %s", (user_id,))
        resultado = cursor.fetchall()
        conn.close()  # Devuelve al pool automáticamente
        return jsonify(resultado)
"""


def get_db_connection():
    """
    Obtiene una conexión reutilizable del pool.
    
    Esta es la función que importarás en tu app Flask:
        from db import get_db_connection
        
    Uso recomendado:
        1. conn = get_db_connection()
        2. cursor = conn.cursor()
        3. cursor.execute(...)
        4. conn.commit()
        5. conn.close()  # Devuelve al pool
    
    Returns:
        PooledConnectionWrapper: Conexión que simula psycopg2.connection
        
    Ventajas:
        - Rápido: conexión reutilizada del pool (~1-5ms)
        - Seguro: thread-safe, funciona en Flask con múltiples threads
        - Simple: misma API que psycopg2 normal
    
    Nota: NUNCA hagas conn.close() sin llamar a conn.commit() antes
    si hay cambios. El close() devuelve la conexión al pool.
    """
    p = get_pool()
    return PooledConnectionWrapper(p)


# ================================================================================
# SECCIÓN 4: INICIALIZACIÓN DE BASE DE DATOS
# ================================================================================
"""
init_db() crea todas las tablas E índices de una sola vez.

Se ejecuta típicamente:
  1. Al iniciar la app por primera vez
  2. En scripts de setup
  3. Nunca en producción (datos ya existen)

Estructura:
  - Crea tablas base (usuarios, configuracion)
  - Crea tablas de dominio (ventas, materiales, productos)
  - Crea tablas de negocio (invitaciones, planners)
  - Crea TODOS los índices (lo más importante para performance)
  - Crea usuario admin de seed
"""


def init_db():
    """
    Inicializa la base de datos con tablas, índices y datos de seed.
    
    Esta función debe ejecutarse UNA SOLA VEZ:
        python db.py  # Ejecutar como script principal
    
    O en tu app Flask (si es la primera vez):
        from db import init_db
        if os.getenv('INIT_DB') == 'true':
            init_db()
    
    Comportamiento:
        - Si una tabla ya existe → CREATE TABLE IF NOT EXISTS (no error)
        - Si un índice ya existe → CREATE INDEX IF NOT EXISTS (no error)
        - Idempotente: puedes ejecutarla 10 veces sin problema
    
    Validaciones de Seguridad:
        - Rechaza si estás en DEV pero apuntas a PROD
        - Rechaza si estás en PROD pero apuntas a DEV
        
    Estructura de tablas (en orden de dependencias):
        1. Usuarios → tabla base, todos dependemos de aquí
        2. Configuracion → datos por usuario
        3. Materiales → inventario
        4. Productos → recipes (usa materiales)
        5. Clientes y Ventas → transacciones
        6. Invitaciones → planners de eventos
        7. Admin seed → usuario por defecto
    """
    
    # =============================
    # 4.1 Obtener conexión y validar
    # =============================
    conn = get_db_connection()
    cursor = conn.cursor()
    env = os.getenv("FLASK_ENV", "development")

    # Validación de entorno (segunda capa de seguridad)
    if env == "development":
        db_url = os.getenv("DATABASE_URL_DEV", "")
        if "nozomi" in db_url:
            raise Exception(
                "❌ SECURITY ERROR: DEV environment pero DATABASE_URL_DEV "
                "contiene 'nozomi' (PROD). No inicializar."
            )

    if env == "production":
        db_url = os.getenv("DATABASE_URL_PROD", "")
        if "shinkansen" in db_url:
            raise Exception(
                "❌ SECURITY ERROR: PROD environment pero DATABASE_URL_PROD "
                "contiene 'shinkansen' (DEV). No inicializar."
            )

    print(f"🔄 Inicializando BD ({env.upper()})...")

    # =============================
    # 4.2 TABLA: USUARIOS
    # =============================
    """
    Tabla central de la app. Todos los datos se relacionan con usuarios.
    
    Campos importantes:
      - email: UNIQUE → no puede haber 2 emails iguales
      - stripe_customer_id: para integración de pagos
      - subscription_end: fecha de expiración del plan
      - estado_suscripcion: 'Trial', 'Active', 'Canceled'
      
    Índices añadidos:
      - idx_usuarios_email: búsqueda rápida en login
      - idx_usuarios_username: búsqueda en buscar usuarios
      - idx_usuarios_stripe_customer_id: vinculación con Stripe
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE,
        email TEXT UNIQUE,
        password TEXT,
        telefono TEXT,
        company_name TEXT,
        role INTEGER DEFAULT 0,  -- 0=user, 1=admin, 2=superadmin
        subscription_end TIMESTAMP,
        created_at TIMESTAMP,
        terms_accepted BOOLEAN DEFAULT FALSE,
        country_code TEXT DEFAULT 'MX',
        last_login TIMESTAMP,
        origen_registro TEXT DEFAULT 'desconocido',
        utm_campaign TEXT,
        estado_suscripcion TEXT DEFAULT 'Trial',  -- Trial, Active, Canceled
        fecha_cancelacion TIMESTAMP,
        plan_type TEXT DEFAULT 'Free',  -- Free, Pro, Enterprise
        trial_start TIMESTAMP,
        dias_regalados INTEGER DEFAULT 0,
        recordatorio_enviado BOOLEAN DEFAULT FALSE,
        stripe_customer_id TEXT,
        verificado BOOLEAN DEFAULT FALSE,
        tutorial_visto BOOLEAN DEFAULT FALSE,
        active_module TEXT DEFAULT 'cotizador'
    )
    """)
    print("  ✓ Tabla: usuarios")

    # Índices de usuarios
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_usuarios_email ON usuarios(email)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_usuarios_username ON usuarios(username)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_usuarios_stripe_customer_id ON usuarios(stripe_customer_id)
    """)
    print("    ✓ Índices: email, username, stripe_customer_id")

    # =============================
    # 4.2B TABLA: USER_MODULES
    # =============================
    """
    Modulos activados por usuario.

    No reemplaza usuarios.email ni usuarios.active_module:
      - email sigue siendo unico.
      - active_module indica el modulo actual.
      - user_modules indica que modulos puede usar la cuenta.
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_modules (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
        module_key TEXT NOT NULL,
        status TEXT DEFAULT 'trial',
        plan_type TEXT,
        trial_start TIMESTAMP,
        trial_ends_at TIMESTAMP,
        subscription_end TIMESTAMP,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, module_key)
    )
    """)
    cursor.execute("ALTER TABLE user_modules ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'trial'")
    cursor.execute("ALTER TABLE user_modules ALTER COLUMN status SET DEFAULT 'trial'")
    cursor.execute("ALTER TABLE user_modules ADD COLUMN IF NOT EXISTS plan_type TEXT")
    cursor.execute("ALTER TABLE user_modules ADD COLUMN IF NOT EXISTS trial_start TIMESTAMP")
    cursor.execute("ALTER TABLE user_modules ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMP")
    cursor.execute("ALTER TABLE user_modules ADD COLUMN IF NOT EXISTS subscription_end TIMESTAMP")
    cursor.execute("ALTER TABLE user_modules ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP")
    cursor.execute("ALTER TABLE user_modules ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP")
    cursor.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_user_modules_user_module
    ON user_modules(user_id, module_key)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_user_modules_module_status
    ON user_modules(module_key, status)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_user_modules_user_id
    ON user_modules(user_id)
    """)
    cursor.execute("""
    INSERT INTO user_modules (
        user_id, module_key, status, plan_type, trial_start, trial_ends_at,
        subscription_end, created_at, updated_at
    )
    SELECT id,
           module_key,
           module_status,
           plan_type,
           CASE WHEN module_status = 'trial' THEN COALESCE(created_at, CURRENT_TIMESTAMP) ELSE NULL END,
           CASE WHEN module_status = 'trial' THEN COALESCE(created_at, CURRENT_TIMESTAMP) + INTERVAL '7 days' ELSE NULL END,
           CASE WHEN module_status IN ('trial', 'active') THEN subscription_end ELSE NULL END,
           COALESCE(created_at, CURRENT_TIMESTAMP),
           CURRENT_TIMESTAMP
    FROM (
        SELECT id,
               COALESCE(active_module, 'cotizador') AS module_key,
               CASE
                   WHEN LOWER(COALESCE(estado_suscripcion, '')) IN ('activo', 'active') THEN 'active'
                   WHEN LOWER(COALESCE(estado_suscripcion, '')) IN ('inactivo', 'inactive', 'pago fallido') THEN 'inactive'
                   WHEN LOWER(COALESCE(estado_suscripcion, '')) IN ('cancelado', 'cancelled') THEN 'cancelled'
                   ELSE 'trial'
               END AS module_status,
               plan_type,
               subscription_end,
               created_at
        FROM usuarios
    ) usuarios_modulos
    ON CONFLICT (user_id, module_key) DO NOTHING
    """)
    print("  ✓ Tabla: user_modules")

    # =============================
    # 4.3 TABLA: TUTORIALES_ESTADO
    # =============================
    """
    Rastrea qué tutoriales ha visto cada usuario y en qué versión.
    
    Campo UNIQUE (user_id, modulo):
      - Cada usuario puede ver cada tutorial solo 1 vez
      - Si hay versión 2 del tutorial, version_vista se actualiza
      
    Índice:
      - idx_tutoriales_estado_user_id: rápido obtener tutoriales de usuario
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tutoriales_estado (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        modulo TEXT NOT NULL,
        version_vista INTEGER DEFAULT 0,
        UNIQUE(user_id, modulo),
        FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE CASCADE
    )
    """)
    print("  ✓ Tabla: tutoriales_estado")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_tutoriales_estado_user_id ON tutoriales_estado(user_id)
    """)

    # =============================
    # 4.4 TABLA: PASSWORD_RESETS
    # =============================
    """
    Tokens temporales para resetear contraseña (ej: "recuperar contraseña").
    
    Flujo:
      1. Usuario hace click en "Olvidé contraseña"
      2. Generamos token unique y lo insertamos aquí
      3. Enviamos email con link: /reset?token=abc123
      4. Usuario hace click, validator verifica que el token NO ha expirado
      5. Usuario resetea contraseña, marcamos used=TRUE
    
    Índices:
      - idx_password_resets_user_id: si el usuario quiere resetear otra vez
      - idx_password_resets_token: búsqueda rápida del token
    
    Importante: expires_at es TIMESTAMP WITH TIME ZONE (UTC siempre)
    """
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
    print("  ✓ Tabla: password_resets")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_password_resets_user_id ON password_resets(user_id)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets(token)
    """)

    # =============================
    # 4.5 TABLA: CONFIGURACION
    # =============================
    """
    Preferencias y settings de cada usuario (1 por usuario).
    
    Campos importantes:
      - margen_ganancia: porcentaje de ganancia en productos (default 200%)
      - nombre_empresa: nombre del negocio del usuario
      - inventario_activo: boolean para activar/desactivar inventario
      - icono_empresa: emoji para representar el negocio (ej: 💅 para salón)
      - modo_oscuro: preferencia de UI
      - salario_deseado: ingreso mensual deseado (para cálculos)
    
    user_id UNIQUE: cada usuario tiene exactamente 1 fila de configuración
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS configuracion (
        id SERIAL PRIMARY KEY,
        user_id INTEGER UNIQUE,
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
    print("  ✓ Tabla: configuracion")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_configuracion_user_id ON configuracion(user_id)
    """)

    # =============================
    # 4.6 TABLA: GASTOS_FIJOS
    # =============================
    """
    Gastos mensuales del usuario (renta, servicios, etc).
    
    Ejemplo:
      - Nombre: "Renta local"
      - Monto_mensual: 8000
      - Activo: TRUE
    
    Se usa para calcular el costo de operación y márgenes.
    
    Índices:
      - idx_gastos_fijos_user_id: obtener gastos del usuario
      - idx_gastos_fijos_activo PARTIAL: solo gastos activos (más rápido)
    
    Índice PARTIAL (WHERE activo = TRUE):
      - Ocupa 50% menos espacio
      - Es más rápido si la mayoría de registros están inactivos
      - PostgreSQL: feature muy poderoso
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS gastos_fijos (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        nombre TEXT NOT NULL,
        monto_mensual REAL DEFAULT 0,
        activo BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES usuarios(id) ON DELETE CASCADE
    )
    """)
    print("  ✓ Tabla: gastos_fijos")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_gastos_fijos_user_id ON gastos_fijos(user_id)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_gastos_fijos_activo ON gastos_fijos(activo) WHERE activo = TRUE
    """)

    # =============================
    # 4.7 TABLA: MAQUINARIA
    # =============================
    """
    Máquinas/equipos del usuario y su costo de desgaste.
    
    Ejemplo:
      - Nombre: "Cortadora Plotter"
      - Costo_desgaste: 5.00 (pesos por cada uso)
    
    Se usa en cotizador para agregar costo de máquina a cada producto.
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS maquinaria (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        nombre TEXT,
        costo_desgaste REAL
    )
    """)
    print("  ✓ Tabla: maquinaria")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_maquinaria_user_id ON maquinaria(user_id)
    """)

    # =============================
    # 4.8 TABLA: MATERIALES
    # =============================
    """
    Inventario de materiales/insumos del usuario.
    
    Campos importantes:
      - es_paquete: si es un paquete (ej: "Pack de 100 etiquetas")
      - precio_unitario: costo por unidad
      - stock_actual: cuántos hay ahora
      - stock_minimo: alertar cuando baje de esto
    
    Ejemplo:
      - Nombre: "Vinilo adhesivo blanco"
      - Es_paquete: FALSE (es unitario)
      - Precio_compra: 50 (por rollo)
      - Cantidad_paquete: 10 (metros por rollo)
      - Precio_unitario: 5 (por metro)
      - Stock_actual: 23.5 (metros disponibles)
    
    Índices:
      - idx_materiales_user_id: obtener materiales del usuario (FRECUENTE)
      - idx_materiales_nombre: búsqueda por nombre en autocomplete
    """
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
    print("  ✓ Tabla: materiales")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_materiales_user_id ON materiales(user_id)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_materiales_nombre ON materiales(nombre)
    """)

    # =============================
    # 4.9 TABLA: MOVIMIENTOS_INVENTARIO
    # =============================
    """
    Historial de cambios en el inventario (auditoría).
    
    Cada vez que el usuario:
      - Ingresa materiales (compra)
      - Usa materiales (en venta)
      - Ajusta inventario (error, merma)
    
    Se crea un registro aquí.
    
    Campos:
      - tipo: 'compra', 'venta', 'ajuste'
      - cantidad: cuánto cambió
      - motivo: descripción del cambio
      - stock_resultante: stock después del cambio
    
    Importante: es un registro INMUTABLE. Nunca se edita.
    Esto garantiza una auditoría completa.
    
    Índices:
      - idx_movimientos_inventario_user_id: historial del usuario
      - idx_movimientos_inventario_fecha: ordenar por fecha (reportes)
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS movimientos_inventario (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        material_id INTEGER,
        tipo TEXT,          -- 'compra', 'venta', 'ajuste'
        cantidad REAL,
        motivo TEXT,
        stock_resultante REAL,
        fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(material_id) REFERENCES materiales(id) ON DELETE CASCADE
    )
    """)
    print("  ✓ Tabla: movimientos_inventario")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_movimientos_inventario_user_id ON movimientos_inventario(user_id)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_movimientos_inventario_material_id ON movimientos_inventario(material_id)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_movimientos_inventario_fecha ON movimientos_inventario(fecha DESC)
    """)

    # =============================
    # 4.10 TABLA: PRODUCTOS (Cotizador)
    # =============================
    """
    Productos/recipes guardadas por el usuario.
    
    Ejemplo:
      - Nombre: "Sticker personalizado 5x5"
      - Items: referencias a materiales que usa
    
    La tabla es simple; los detalles están en producto_detalles.
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS productos (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        nombre TEXT,
        items TEXT  -- JSON o texto serializado
    )
    """)
    print("  ✓ Tabla: productos")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_productos_user_id ON productos(user_id)
    """)

    # =============================
    # 4.11 TABLA: PRODUCTO_DETALLES
    # =============================
    """
    Detalles de cada producto: qué materiales usa y cuánto.
    
    Ejemplo:
      Producto: "Sticker 5x5"
        - Material: "Vinilo blanco" | Cantidad: 0.025 metros
        - Material: "Tinta color" | Cantidad: 1 gramo
    
    Índices:
      - idx_producto_detalles_producto_id: obtener materiales de un producto
      - idx_producto_detalles_material_id: obtener dónde se usa un material
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS producto_detalles (
        id SERIAL PRIMARY KEY,
        producto_id INTEGER,
        material_id INTEGER,
        cantidad REAL,
        FOREIGN KEY(producto_id) REFERENCES productos(id) ON DELETE CASCADE,
        FOREIGN KEY(material_id) REFERENCES materiales(id) ON DELETE CASCADE
    )
    """)
    print("  ✓ Tabla: producto_detalles")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_producto_detalles_producto_id ON producto_detalles(producto_id)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_producto_detalles_material_id ON producto_detalles(material_id)
    """)

    # =============================
    # 4.12 TABLA: PRODUCTO_MAQUINARIA
    # =============================
    """
    Máquinas que se usan en cada producto.
    
    Ejemplo:
      Producto: "Sticker 5x5"
        - Maquinaria: "Cortadora Plotter" (se usa para cortar)
        - Maquinaria: "Impresora color" (se usa para imprimir)
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS producto_maquinaria (
        id SERIAL PRIMARY KEY,
        producto_id INTEGER,
        maquinaria_id INTEGER,
        FOREIGN KEY(producto_id) REFERENCES productos(id) ON DELETE CASCADE,
        FOREIGN KEY(maquinaria_id) REFERENCES maquinaria(id) ON DELETE CASCADE
    )
    """)
    print("  ✓ Tabla: producto_maquinaria")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_producto_maquinaria_producto_id ON producto_maquinaria(producto_id)
    """)

    # =============================
    # 4.13 TABLA: LOGS_ACTIVIDAD
    # =============================
    """
    Auditoría: qué hizo cada usuario y cuándo.
    
    Se llena automáticamente en cada acción importante:
      - Usuario login
      - Usuario crea venta
      - Usuario edita material
      - Admin ejecuta acción
    
    Importante: nunca se edita, solo se inserta. Auditoría inmutable.
    
    Índices:
      - idx_logs_actividad_user_id: obtener historial del usuario
      - idx_logs_actividad_created_at DESC: ordenar por fecha (más reciente primero)
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS logs_actividad (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        accion TEXT,
        modulo TEXT,
        fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        detalle TEXT,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE CASCADE
    )
    """)
    print("  ✓ Tabla: logs_actividad")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_logs_actividad_user_id ON logs_actividad(user_id)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_logs_actividad_created_at ON logs_actividad(created_at DESC)
    """)

    # =============================
    # 4.14 TABLA: AUTH_CODES
    # =============================
    """
    Códigos de autenticación de 6 dígitos para verificación de email.
    
    Flujo:
      1. Usuario se registra con email
      2. Enviamos email con código: "123456"
      3. Usuario ingresa el código en la app
      4. Validamos: ¿código existe? ¿no ha expirado? ¿no fue usado?
      5. Marcamos used=TRUE
    
    expires_at: debe expirar rápido (ej: 10 minutos)
    """
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
    print("  ✓ Tabla: auth_codes")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_auth_codes_user_id ON auth_codes(user_id)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_auth_codes_code ON auth_codes(code)
    """)
    cursor.execute("""
    ALTER TABLE auth_codes
    ADD COLUMN IF NOT EXISTS purpose TEXT DEFAULT 'verify_email'
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_auth_codes_activation_lookup
    ON auth_codes(user_id, email, code, purpose, used, expires_at)
    """)

    # =============================
    # 4.15 TABLA: CLIENTES
    # =============================
    """
    Directorio de clientes del usuario.
    
    Campos:
      - nombre: "Juan Pérez"
      - plataforma: "Instagram", "WhatsApp", "Tienda física", etc
      - notas_cliente: información especial ("Pide siempre con diseño azul")
    
    Índices:
      - idx_clientes_user_id: obtener clientes del usuario
      - idx_clientes_nombre: búsqueda por nombre en autocomplete
    """
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
    print("  ✓ Tabla: clientes")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_clientes_user_id ON clientes(user_id)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_clientes_nombre ON clientes(nombre)
    """)

    # =============================
    # 4.16 TABLA: VENTAS (LA MÁS IMPORTANTE)
    # =============================
    """
    Todas las transacciones de venta.
    
    Campos cruciales:
      - user_id: a quién pertenece la venta
      - estado: 'pagado', 'pendiente', 'cancelado'
      - fecha: cuándo se hizo (TIMESTAMPTZ = UTC)
      - subtotal: precio sin impuestos
      - descuento_porcentaje: si hay promoción
      - total: precio final
      - costo_total: cuánto costó hacer el producto
      - impuestos: monto de impuesto
    
    ÍNDICES CRÍTICOS (lo más importante):
      - idx_ventas_user_id: filtrar por usuario (99% de queries)
      - idx_ventas_estado: filtrar por estado (pagado/pendiente)
      - idx_ventas_user_estado: COMPOSITE (user_id + estado)
        → "mostrar ventas pendientes del usuario 123" → ultra rápido
      - idx_ventas_fecha DESC: reportes ordenados por fecha
    """
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
        estado TEXT DEFAULT 'pagado',  -- 'pagado', 'pendiente', 'cancelado'
        monto_pagado REAL DEFAULT 0,
        saldo_pendiente REAL DEFAULT 0,
        fecha_vencimiento TIMESTAMP,
        resumen_items TEXT,
        costo_total REAL DEFAULT 0,
        document_type TEXT DEFAULT 'receipt',
        tax_engine TEXT DEFAULT 'none',
        impuestos REAL DEFAULT 0,
        envio REAL DEFAULT 0,
        cliente_id INTEGER REFERENCES clientes(id) ON DELETE SET NULL,
        fecha_entrega TIMESTAMPTZ,
        metodo_entrega TEXT,
        notas_pedido TEXT,
        costo_fijo_prorrateado NUMERIC DEFAULT 0
    )
    """)
    print("  ✓ Tabla: ventas")
    
    # Índices de ventas - CRÍTICOS
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_ventas_user_id ON ventas(user_id)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_ventas_estado ON ventas(estado)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_ventas_fecha ON ventas(fecha DESC)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_ventas_cliente_id ON ventas(cliente_id)
    """)
    # Índice compuesto: user_id + estado (búsqueda muy común)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_ventas_user_estado ON ventas(user_id, estado)
    """)
    print("    ✓ Índices: user_id, estado, fecha, cliente_id, (user_id, estado)")

    # =============================
    # 4.17 TABLA: VENTA_DETALLES
    # =============================
    """
    Líneas de items en cada venta.
    
    Ejemplo - Venta ID 5:
      - Item 1: "Sticker 5x5" | Cantidad: 50 | Precio unitario: 2
      - Item 2: "Diseño personalizado" | Cantidad: 1 | Precio unitario: 100
    
    Campos:
      - concepto: nombre del item
      - composicion: JSON con qué materiales se usaron
      - costo_unitario: cuánto costó hacer 1 unidad
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS venta_detalles (
        id SERIAL PRIMARY KEY,
        venta_id INTEGER,
        concepto TEXT,
        cantidad REAL,
        precio_unitario REAL,
        costo_unitario REAL,
        subtotal REAL,
        composicion TEXT,  -- JSON de materiales usados
        FOREIGN KEY(venta_id) REFERENCES ventas(id) ON DELETE CASCADE
    )
    """)
    print("  ✓ Tabla: venta_detalles")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_venta_detalles_venta_id ON venta_detalles(venta_id)
    """)

    # =============================
    # 4.18 TABLA: CATEGORIAS
    # =============================
    """
    Categorías del catálogo de productos (para planners).
    
    Ejemplo:
      - Nombre: "Invitaciones de boda"
      - Orden: 1
      - Activo: TRUE
    """
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
    print("  ✓ Tabla: categorias")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_categorias_activo ON categorias(activo) WHERE activo = TRUE
    """)

    # =============================
    # 4.19 TABLA: CATALOGO_PRODUCTOS
    # =============================
    """
    Productos del catálogo de templates (no confundir con productos del usuario).
    
    Se usa en planners para mostrar templates de invitaciones.
    
    Campos:
      - sku: identificador único (ej: "INVITATION_001")
      - media_url: URL de imagen/video de preview
      - media_type: 'image', 'video'
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS catalogo_productos (
        id SERIAL PRIMARY KEY,
        categoria_id INTEGER,
        sku TEXT NOT NULL UNIQUE,
        titulo TEXT NOT NULL,
        descripcion TEXT,
        media_url TEXT NOT NULL,
        media_type TEXT DEFAULT 'image',  -- 'image', 'video'
        precio REAL DEFAULT 0,
        stock BOOLEAN DEFAULT TRUE,
        orden INTEGER DEFAULT 0,
        activo BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (categoria_id) REFERENCES categorias (id) ON DELETE CASCADE
    )
    """)
    print("  ✓ Tabla: catalogo_productos")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_catalogo_productos_categoria_id ON catalogo_productos(categoria_id)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_catalogo_productos_sku ON catalogo_productos(sku)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_catalogo_productos_activo ON catalogo_productos(activo) WHERE activo = TRUE
    """)

    # =============================
    # 4.20 TABLA: SHIPPING_CONFIGS
    # =============================
    """
    Configuración de envíos por usuario.
    
    Campos:
      - origin_address: dirección desde donde se envía
      - local_base_rate: tarifa base local (ej: $35)
      - local_km_rate: tarifa por km (ej: $8/km)
    
    user_id UNIQUE: cada usuario tiene 1 sola configuración de envíos
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS shipping_configs (
        id SERIAL PRIMARY KEY,
        user_id INTEGER UNIQUE,
        origin_address TEXT,
        origin_lat REAL,
        origin_lng REAL,
        local_base_rate REAL DEFAULT 35,
        local_km_rate REAL DEFAULT 8,
        free_shipping_threshold REAL DEFAULT 0,
        safety_margin_percent INTEGER DEFAULT 10,
        FOREIGN KEY(user_id) REFERENCES usuarios(id) ON DELETE CASCADE
    )
    """)
    print("  ✓ Tabla: shipping_configs")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_shipping_configs_user_id ON shipping_configs(user_id)
    """)

    # =============================
    # 4.21 TABLA: SHIPPING_ZONES
    # =============================
    """
    Zonas de envío definidas por el usuario.
    
    Ejemplo:
      - Zone: "Área metropolitana"
      - States_included: "Nuevo León, Coahuila"
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS shipping_zones (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        zone_name TEXT,
        states_included TEXT,  -- JSON o texto separado por comas
        FOREIGN KEY(user_id) REFERENCES usuarios(id) ON DELETE CASCADE
    )
    """)
    print("  ✓ Tabla: shipping_zones")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_shipping_zones_user_id ON shipping_zones(user_id)
    """)

    # =============================
    # 4.22 TABLA: SHIPPING_RATES
    # =============================
    """
    Tarifas de envío por zona y peso.
    
    Ejemplo:
      - Zone: "Área metropolitana"
      - Max_weight_kg: 5
      - Price: 150
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS shipping_rates (
        id SERIAL PRIMARY KEY,
        zone_id INTEGER,
        max_weight_kg REAL,
        price REAL,
        FOREIGN KEY(zone_id) REFERENCES shipping_zones(id) ON DELETE CASCADE
    )
    """)
    print("  ✓ Tabla: shipping_rates")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_shipping_rates_zone_id ON shipping_rates(zone_id)
    """)

    # =============================
    # 4.23 TABLA: LISTA_MUSICA
    # =============================
    """
    Canciones disponibles para invitaciones (planners).
    
    Campos:
      - nombre_cancion: "Canon en D"
      - url_cloudflare: URL a MP3 en Cloudflare R2
      - activa: si está disponible
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS lista_musica (
        id SERIAL PRIMARY KEY,
        nombre_cancion TEXT,
        url_cloudflare TEXT,
        activa BOOLEAN DEFAULT TRUE
    );
    """)
    print("  ✓ Tabla: lista_musica")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_lista_musica_activa ON lista_musica(activa) WHERE activa = TRUE
    """)

    # =============================
    # 4.24 TABLA: INVITACIONES (MÓDULO PLANNERS)
    # =============================
    """
    INVITACIONES: la tabla más compleja. Guarda eventos (bodas, XV años, etc).
    
    Campos clave:
      - slug: URL única (ej: "boda-juan-maria-2024")
      - config_json: configuración de la invitación (colores, fuentes, etc)
      - fecha_evento: cuándo es el evento
      - codigo_acceso_cliente: contraseña para que invitados accedan
      
    ÍNDICES CRÍTICOS:
      - idx_invitaciones_slug: búsqueda por URL (MUST BE FAST)
      - idx_invitaciones_codigo_acceso_cliente: búsqueda por password
      - idx_invitaciones_planner_id: invitaciones de un planner
      - idx_invitaciones_created_at DESC: ordenar recientes primero
    """
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
        tipo_evento TEXT DEFAULT 'boda',  -- 'boda', 'XV', 'graduacion', etc
        bloquear_edicion_invitados BOOLEAN DEFAULT FALSE,
        template_id TEXT DEFAULT 'clasico',
        tiene_modulo_invitados BOOLEAN DEFAULT FALSE,
        estilo_apertura TEXT DEFAULT 'simple',
        codigo_acceso_cliente TEXT UNIQUE,
        mesas_json TEXT DEFAULT '[]',
        planner_id INTEGER,
        FOREIGN KEY(musica_id) REFERENCES lista_musica(id) ON DELETE SET NULL
    );
    """)
    print("  ✓ Tabla: invitaciones")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_invitaciones_slug ON invitaciones(slug)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_invitaciones_codigo_acceso_cliente ON invitaciones(codigo_acceso_cliente)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_invitaciones_planner_id ON invitaciones(planner_id)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_invitaciones_created_at ON invitaciones(created_at DESC)
    """)
    print("    ✓ Índices: slug, codigo_acceso_cliente, planner_id, created_at")

    # =============================
    # 4.25 TABLA: FOTOS_INVITADOS
    # =============================
    """
    Fotos compartidas por invitados en el evento.
    
    Si camara_premium=TRUE, se procesó con IA (mejor calidad).
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fotos_invitados (
        id SERIAL PRIMARY KEY,
        invitacion_id INTEGER NOT NULL,
        url TEXT NOT NULL,
        camara_premium BOOLEAN DEFAULT FALSE,
        fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(invitacion_id) REFERENCES invitaciones(id) ON DELETE CASCADE
    );
    """)
    print("  ✓ Tabla: fotos_invitados")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_fotos_invitados_invitacion_id ON fotos_invitados(invitacion_id)
    """)

    # =============================
    # 4.26 TABLA: PASES_INVITADOS
    # =============================
    """
    Pases de acceso para invitados (para verificar entrada).
    
    Flujo:
      1. Planner crea familia con 2 pases
      2. Enviamos QR a la familia
      3. En la puerta, leen QR
      4. Si pases_usados < pases_totales → ENTRAN
      5. Registramos asistencia
    
    ÍNDICE CRÍTICO:
      - idx_pases_invitados_codigo_qr: búsqueda RÁPIDA de QR (en puerta)
    """
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
        estado_asistencia TEXT DEFAULT 'Pendiente',  -- 'Pendiente', 'Asistió', 'No asistió'
        FOREIGN KEY (invitacion_id) REFERENCES invitaciones(id) ON DELETE CASCADE
    )
    """)
    print("  ✓ Tabla: pases_invitados")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_pases_invitados_invitacion_id ON pases_invitados(invitacion_id)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_pases_invitados_codigo_qr ON pases_invitados(codigo_qr_unique)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_pases_invitados_estado ON pases_invitados(estado_asistencia)
    """)
    print("    ✓ Índices: invitacion_id, codigo_qr_unique (CRÍTICO), estado_asistencia")

    # =============================
    # 4.27 TABLA: BUENOS_DESEOS
    # =============================
    """
    Mensajes de invitados dejados en el evento.
    
    Ejemplo: "¡Felicidades Juan y María! Que sean muy felices 💕"
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS buenos_deseos (
        id SERIAL PRIMARY KEY,
        invitacion_id INTEGER,
        nombre TEXT NOT NULL,
        mensaje TEXT NOT NULL,
        fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (invitacion_id) REFERENCES invitaciones(id) ON DELETE CASCADE
    )
    """)
    print("  ✓ Tabla: buenos_deseos")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_buenos_deseos_invitacion_id ON buenos_deseos(invitacion_id)
    """)

    # =============================
    # 4.28 TABLA: PLANNERS
    # =============================
    """
    Planners de eventos (wedding planners, coordinadores, etc).
    
    Ellos crean invitaciones para sus clientes.
    
    codigo_acceso_planner: "código maestro" para que puedan entrar a su area
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS planners (
        id SERIAL PRIMARY KEY,
        nombre_contacto TEXT,
        nombre_empresa TEXT,
        telefono TEXT,
        codigo_acceso_planner TEXT UNIQUE,
        notas TEXT,
        estado TEXT DEFAULT 'activo',  -- 'activo', 'inactivo', 'suspendido'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    print("  ✓ Tabla: planners")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_planners_codigo_acceso ON planners(codigo_acceso_planner)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_planners_estado ON planners(estado)
    """)

    # =============================
    # 4.29 TABLA: PLANNER_PAQUETES
    # =============================
    """
    Paquetes de invitaciones comprados por planners.
    
    Ejemplo:
      - Planner compró 100 invitaciones
      - Ya usó 45
      - Le quedan 55
      - Expira el 2025-12-31
    """
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
        FOREIGN KEY(planner_id) REFERENCES planners(id) ON DELETE CASCADE
    )
    """)
    print("  ✓ Tabla: planner_paquetes")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_planner_paquetes_planner_id ON planner_paquetes(planner_id)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_planner_paquetes_activo ON planner_paquetes(activo) WHERE activo = TRUE
    """)

    # =============================
    # 4.30 TABLA: ANUNCIOS_GLOBALES
    # =============================
    """
    Anuncios del sistema para todos los usuarios.
    
    Ejemplo:
      - Título: "Mantenimiento programado"
      - Mensaje: "La app estará inactiva el sábado"
      - Tipo: "warning"
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS anuncios_globales (
            id SERIAL PRIMARY KEY,
            titulo VARCHAR(255) NOT NULL,
            mensaje TEXT NOT NULL,
            tipo VARCHAR(50) DEFAULT 'info',  -- 'info', 'warning', 'error'
            url TEXT,
            activo BOOLEAN DEFAULT TRUE,
            fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    print("  ✓ Tabla: anuncios_globales")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_anuncios_globales_activo ON anuncios_globales(activo) WHERE activo = TRUE
    """)

    # =============================
    # 4.31 TABLA: ANUNCIOS_VISTOS
    # =============================
    """
    Rastro de qué usuarios ya vieron qué anuncios (para no molestar).
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS anuncios_vistos (
            user_id INTEGER NOT NULL,
            anuncio_id INTEGER NOT NULL,
            fecha_visto TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, anuncio_id),
            FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE CASCADE,
            FOREIGN KEY (anuncio_id) REFERENCES anuncios_globales(id) ON DELETE CASCADE
        )
    """)
    print("  ✓ Tabla: anuncios_vistos")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_anuncios_vistos_user_id ON anuncios_vistos(user_id)
    """)

    # =============================
    # 4.32 TABLA: NOTIFICACIONES_MANUALES
    # =============================
    """
    Notificaciones específicas para cada usuario.
    
    Ejemplo:
      - "Tu suscripción vence en 3 días"
      - "Nuevo comentario en tu invitación"
      - "Alguien usó un pase de tu evento"
    """
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
            fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE CASCADE
        )
    """)
    print("  ✓ Tabla: notificaciones_manuales")
    
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_notificaciones_manuales_user_id ON notificaciones_manuales(user_id)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_notificaciones_manuales_leida ON notificaciones_manuales(leida) WHERE leida = FALSE
    """)

    # =============================
    # 4.33 TABLAS: WHATSAPP BOT
    # =============================
    """
    Bandeja de entrada para WhatsApp Business.

    conversaciones:
      - Una fila por número de cliente.
      - bot_activo controla el handover humano/IA.

    mensajes_whatsapp:
      - Historial completo por conversación.
      - whatsapp_message_id evita procesar duplicados enviados por Meta.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversaciones (
            id SERIAL PRIMARY KEY,
            numero_cliente VARCHAR(32) NOT NULL UNIQUE,
            nombre_cliente VARCHAR(255),
            bot_activo BOOLEAN DEFAULT TRUE,
            ultima_actividad TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    print("  ✓ Tabla: conversaciones")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mensajes_whatsapp (
            id SERIAL PRIMARY KEY,
            conversacion_id INTEGER NOT NULL,
            remitente VARCHAR(20) NOT NULL CHECK (remitente IN ('cliente', 'bot', 'agente')),
            texto TEXT NOT NULL,
            whatsapp_message_id VARCHAR(255) UNIQUE,
            fecha_envio TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (conversacion_id) REFERENCES conversaciones(id) ON DELETE CASCADE
        )
    """)
    print("  ✓ Tabla: mensajes_whatsapp")

    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_conversaciones_numero_cliente ON conversaciones(numero_cliente)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_conversaciones_ultima_actividad ON conversaciones(ultima_actividad DESC)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_mensajes_whatsapp_conversacion_fecha ON mensajes_whatsapp(conversacion_id, fecha_envio ASC)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_mensajes_whatsapp_message_id ON mensajes_whatsapp(whatsapp_message_id) WHERE whatsapp_message_id IS NOT NULL
    """)

    # =============================
    # 4.34 TABLAS: SIANEFFECTS NAILS
    # =============================
    """
    Módulo Sianeffects Nails separado del cotizador actual.
    Solo comparte la tabla usuarios mediante active_module.
    """
    cursor.execute("""
-- ============================================================
-- SIANEFFECTS NAILS - MIGRACIÓN INICIAL
-- ============================================================
-- Objetivo:
-- Agregar módulo Sianeffects Nails separado del cotizador actual.
-- Solo se comparte la tabla usuarios.
-- ============================================================


-- ============================================================
-- 1. USUARIOS: módulo activo
-- ============================================================

ALTER TABLE usuarios
ADD COLUMN IF NOT EXISTS active_module TEXT DEFAULT 'cotizador';

UPDATE usuarios
SET active_module = 'cotizador'
WHERE active_module IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_usuarios_active_module'
    ) THEN
        ALTER TABLE usuarios
        ADD CONSTRAINT chk_usuarios_active_module
        CHECK (active_module IN ('cotizador', 'nails'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_usuarios_active_module
ON usuarios(active_module);


-- ============================================================
-- 2. NAILS_BUSINESSES
-- Negocio/salón ligado a un usuario.
-- ============================================================

CREATE TABLE IF NOT EXISTS nails_businesses (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,

    name TEXT NOT NULL,
    slug TEXT UNIQUE,

    logo_url TEXT,
    primary_color TEXT DEFAULT '#d946ef',
    secondary_color TEXT DEFAULT '#fce7f3',
    accent_color TEXT DEFAULT '#f0abfc',

    whatsapp TEXT,
    instagram TEXT,
    address TEXT,

    timezone TEXT DEFAULT 'America/Monterrey',
    currency TEXT DEFAULT 'MXN',

    business_hours_json TEXT DEFAULT '{}',
    cancellation_policy TEXT,
    deposit_policy TEXT,
    catalog_tagline TEXT DEFAULT 'Uñas que expresan tu estilo, hechas con amor y detalle.',
    join_code TEXT NOT NULL,

    is_active BOOLEAN DEFAULT TRUE,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_nails_businesses_user_id
ON nails_businesses(user_id);

CREATE INDEX IF NOT EXISTS idx_nails_businesses_slug
ON nails_businesses(slug);

ALTER TABLE nails_businesses
ADD COLUMN IF NOT EXISTS catalog_tagline TEXT DEFAULT 'Uñas que expresan tu estilo, hechas con amor y detalle.';

ALTER TABLE nails_businesses
ADD COLUMN IF NOT EXISTS join_code TEXT;

UPDATE nails_businesses
SET join_code = UPPER(SUBSTRING(MD5(id::text || '-' || COALESCE(slug, '') || '-' || created_at::text), 1, 8))
WHERE join_code IS NULL OR TRIM(join_code) = '';

ALTER TABLE nails_businesses
ALTER COLUMN join_code SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_nails_businesses_join_code
ON nails_businesses(join_code);

CREATE INDEX IF NOT EXISTS idx_nails_businesses_is_active
ON nails_businesses(is_active)
WHERE is_active = TRUE;


-- ============================================================
-- 3. NAILS_STAFF
-- Personal/técnicas. Preparado para multi-staff.
-- ============================================================

CREATE TABLE IF NOT EXISTS nails_staff (
    id SERIAL PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES nails_businesses(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,

    name TEXT NOT NULL,
    email TEXT,
    phone TEXT,

    role TEXT DEFAULT 'owner',
    color TEXT DEFAULT '#d946ef',

    commission_type TEXT DEFAULT 'none',
    commission_value NUMERIC(10,2) DEFAULT 0,

    is_active BOOLEAN DEFAULT TRUE,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_nails_staff_role'
    ) THEN
        ALTER TABLE nails_staff
        ADD CONSTRAINT chk_nails_staff_role
        CHECK (role IN ('owner', 'admin', 'staff', 'reception'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_nails_staff_commission_type'
    ) THEN
        ALTER TABLE nails_staff
        ADD CONSTRAINT chk_nails_staff_commission_type
        CHECK (commission_type IN ('none', 'percent', 'fixed'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_nails_staff_business_id
ON nails_staff(business_id);

CREATE INDEX IF NOT EXISTS idx_nails_staff_user_id
ON nails_staff(user_id);

CREATE INDEX IF NOT EXISTS idx_nails_staff_is_active
ON nails_staff(is_active)
WHERE is_active = TRUE;


-- ============================================================
-- 4. NAILS_CLIENTS
-- Clientas del salón.
-- ============================================================

CREATE TABLE IF NOT EXISTS nails_clients (
    id SERIAL PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES nails_businesses(id) ON DELETE CASCADE,

    name TEXT NOT NULL,
    phone TEXT,
    email TEXT,
    instagram TEXT,

    birthday DATE,

    notes TEXT,
    preferences TEXT,
    allergies_notes TEXT,

    total_visits INTEGER DEFAULT 0,
    total_spent NUMERIC(10,2) DEFAULT 0,

    is_active BOOLEAN DEFAULT TRUE,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_nails_clients_business_id
ON nails_clients(business_id);

CREATE INDEX IF NOT EXISTS idx_nails_clients_name
ON nails_clients(name);

CREATE INDEX IF NOT EXISTS idx_nails_clients_phone
ON nails_clients(phone);

CREATE INDEX IF NOT EXISTS idx_nails_clients_is_active
ON nails_clients(is_active)
WHERE is_active = TRUE;


-- ============================================================
-- 5. NAILS_SERVICE_CATEGORIES
-- Categorías de servicios.
-- Ejemplo: Manos, Pies, Acrílico, Gelish, Extras.
-- ============================================================

CREATE TABLE IF NOT EXISTS nails_service_categories (
    id SERIAL PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES nails_businesses(id) ON DELETE CASCADE,

    name TEXT NOT NULL,
    description TEXT,
    sort_order INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_nails_service_categories_business_id
ON nails_service_categories(business_id);

CREATE INDEX IF NOT EXISTS idx_nails_service_categories_active
ON nails_service_categories(is_active)
WHERE is_active = TRUE;


-- ============================================================
-- 6. NAILS_SERVICES
-- Servicios principales del salón.
-- ============================================================

CREATE TABLE IF NOT EXISTS nails_services (
    id SERIAL PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES nails_businesses(id) ON DELETE CASCADE,
    category_id INTEGER REFERENCES nails_service_categories(id) ON DELETE SET NULL,

    name TEXT NOT NULL,
    description TEXT,

    base_price NUMERIC(10,2) DEFAULT 0,
    duration_minutes INTEGER DEFAULT 60,

    image_url TEXT,
    service_icon TEXT DEFAULT 'hand-sparkles',

    requires_deposit BOOLEAN DEFAULT FALSE,
    deposit_amount NUMERIC(10,2) DEFAULT 0,

    is_public BOOLEAN DEFAULT TRUE,
    is_active BOOLEAN DEFAULT TRUE,

    sort_order INTEGER DEFAULT 0,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE nails_services
ADD COLUMN IF NOT EXISTS service_icon TEXT DEFAULT 'hand-sparkles';

CREATE INDEX IF NOT EXISTS idx_nails_services_business_id
ON nails_services(business_id);

CREATE INDEX IF NOT EXISTS idx_nails_services_category_id
ON nails_services(category_id);

CREATE INDEX IF NOT EXISTS idx_nails_services_is_public
ON nails_services(is_public)
WHERE is_public = TRUE;

CREATE INDEX IF NOT EXISTS idx_nails_services_is_active
ON nails_services(is_active)
WHERE is_active = TRUE;


-- ============================================================
-- 7. NAILS_EXTRAS
-- Extras vendibles.
-- Ejemplo: pedrería, diseño, largo extra, retiro.
-- ============================================================

CREATE TABLE IF NOT EXISTS nails_extras (
    id SERIAL PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES nails_businesses(id) ON DELETE CASCADE,

    name TEXT NOT NULL,
    description TEXT,

    price NUMERIC(10,2) DEFAULT 0,
    duration_minutes INTEGER DEFAULT 0,
    allow_quantity BOOLEAN DEFAULT FALSE,

    is_active BOOLEAN DEFAULT TRUE,
    sort_order INTEGER DEFAULT 0,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_nails_extras_business_id
ON nails_extras(business_id);

CREATE INDEX IF NOT EXISTS idx_nails_extras_is_active
ON nails_extras(is_active)
WHERE is_active = TRUE;


-- ============================================================
-- 8. NAILS_APPOINTMENTS
-- Agenda de citas.
-- ============================================================

CREATE TABLE IF NOT EXISTS nails_appointments (
    id SERIAL PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES nails_businesses(id) ON DELETE CASCADE,

    client_id INTEGER REFERENCES nails_clients(id) ON DELETE SET NULL,
    staff_id INTEGER REFERENCES nails_staff(id) ON DELETE SET NULL,
    service_id INTEGER REFERENCES nails_services(id) ON DELETE SET NULL,

    title TEXT,

    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ NOT NULL,

    status TEXT DEFAULT 'pendiente',

    estimated_total NUMERIC(10,2) DEFAULT 0,
    deposit_amount NUMERIC(10,2) DEFAULT 0,

    notes TEXT,
    internal_notes TEXT,

    reminder_sent BOOLEAN DEFAULT FALSE,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_nails_appointments_status'
    ) THEN
        ALTER TABLE nails_appointments
        ADD CONSTRAINT chk_nails_appointments_status
        CHECK (status IN ('pendiente', 'confirmada', 'atendida', 'cancelada', 'no_asistio'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_nails_appointments_business_id
ON nails_appointments(business_id);

CREATE INDEX IF NOT EXISTS idx_nails_appointments_client_id
ON nails_appointments(client_id);

CREATE INDEX IF NOT EXISTS idx_nails_appointments_staff_id
ON nails_appointments(staff_id);

CREATE INDEX IF NOT EXISTS idx_nails_appointments_service_id
ON nails_appointments(service_id);

CREATE INDEX IF NOT EXISTS idx_nails_appointments_start_time
ON nails_appointments(start_time DESC);

CREATE INDEX IF NOT EXISTS idx_nails_appointments_status
ON nails_appointments(status);

CREATE INDEX IF NOT EXISTS idx_nails_appointments_business_start
ON nails_appointments(business_id, start_time DESC);

CREATE INDEX IF NOT EXISTS idx_nails_appointments_business_status
ON nails_appointments(business_id, status);


-- ============================================================
-- 9. NAILS_APPOINTMENT_EXTRAS
-- Extras ligados a una cita.
-- ============================================================

CREATE TABLE IF NOT EXISTS nails_appointment_extras (
    id SERIAL PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES nails_appointments(id) ON DELETE CASCADE,
    extra_id INTEGER REFERENCES nails_extras(id) ON DELETE SET NULL,

    name TEXT NOT NULL,
    price NUMERIC(10,2) DEFAULT 0,
    duration_minutes INTEGER DEFAULT 0,
    quantity INTEGER DEFAULT 1,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_nails_appointment_extras_appointment_id
ON nails_appointment_extras(appointment_id);

CREATE INDEX IF NOT EXISTS idx_nails_appointment_extras_extra_id
ON nails_appointment_extras(extra_id);


-- ============================================================
-- 9B. NAILS_APPOINTMENT_SERVICES
-- Servicios ligados a una cita.
-- Permite que una misma clienta agende varios servicios en una visita.
-- ============================================================

CREATE TABLE IF NOT EXISTS nails_appointment_services (
    id SERIAL PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES nails_appointments(id) ON DELETE CASCADE,
    service_id INTEGER REFERENCES nails_services(id) ON DELETE SET NULL,

    name TEXT NOT NULL,
    price NUMERIC(10,2) DEFAULT 0,
    duration_minutes INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_nails_appointment_services_appointment_id
ON nails_appointment_services(appointment_id);

CREATE INDEX IF NOT EXISTS idx_nails_appointment_services_service_id
ON nails_appointment_services(service_id);


-- ============================================================
-- 10. NAILS_SALES
-- Ventas/tickets del salón.
-- ============================================================

CREATE TABLE IF NOT EXISTS nails_sales (
    id SERIAL PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES nails_businesses(id) ON DELETE CASCADE,

    client_id INTEGER REFERENCES nails_clients(id) ON DELETE SET NULL,
    appointment_id INTEGER REFERENCES nails_appointments(id) ON DELETE SET NULL,
    staff_id INTEGER REFERENCES nails_staff(id) ON DELETE SET NULL,

    sale_number TEXT,

    subtotal NUMERIC(10,2) DEFAULT 0,
    discount_amount NUMERIC(10,2) DEFAULT 0,
    discount_percentage NUMERIC(10,2) DEFAULT 0,

    tax_amount NUMERIC(10,2) DEFAULT 0,
    total NUMERIC(10,2) DEFAULT 0,

    paid_amount NUMERIC(10,2) DEFAULT 0,
    balance_due NUMERIC(10,2) DEFAULT 0,

    payment_method TEXT,
    status TEXT DEFAULT 'pagada',

    notes TEXT,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_nails_sales_status'
    ) THEN
        ALTER TABLE nails_sales
        ADD CONSTRAINT chk_nails_sales_status
        CHECK (status IN ('pendiente', 'anticipo', 'pagada', 'cancelada'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_nails_sales_payment_method'
    ) THEN
        ALTER TABLE nails_sales
        ADD CONSTRAINT chk_nails_sales_payment_method
        CHECK (
            payment_method IS NULL
            OR payment_method IN ('efectivo', 'transferencia', 'tarjeta', 'mixto', 'otro')
        );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_nails_sales_business_id
ON nails_sales(business_id);

CREATE INDEX IF NOT EXISTS idx_nails_sales_client_id
ON nails_sales(client_id);

CREATE INDEX IF NOT EXISTS idx_nails_sales_appointment_id
ON nails_sales(appointment_id);

CREATE INDEX IF NOT EXISTS idx_nails_sales_staff_id
ON nails_sales(staff_id);

CREATE INDEX IF NOT EXISTS idx_nails_sales_created_at
ON nails_sales(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_nails_sales_status
ON nails_sales(status);

CREATE INDEX IF NOT EXISTS idx_nails_sales_business_created
ON nails_sales(business_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_nails_sales_business_status
ON nails_sales(business_id, status);

CREATE INDEX IF NOT EXISTS idx_nails_sales_sale_number
ON nails_sales(sale_number);


-- ============================================================
-- 11. NAILS_SALE_DETAILS
-- Detalle de cada venta/ticket.
-- ============================================================

CREATE TABLE IF NOT EXISTS nails_sale_details (
    id SERIAL PRIMARY KEY,
    sale_id INTEGER NOT NULL REFERENCES nails_sales(id) ON DELETE CASCADE,

    item_type TEXT NOT NULL,
    item_id INTEGER,

    name TEXT NOT NULL,
    description TEXT,

    quantity NUMERIC(10,2) DEFAULT 1,
    unit_price NUMERIC(10,2) DEFAULT 0,
    total NUMERIC(10,2) DEFAULT 0,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_nails_sale_details_item_type'
    ) THEN
        ALTER TABLE nails_sale_details
        ADD CONSTRAINT chk_nails_sale_details_item_type
        CHECK (item_type IN ('service', 'extra', 'product', 'custom'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_nails_sale_details_sale_id
ON nails_sale_details(sale_id);

CREATE INDEX IF NOT EXISTS idx_nails_sale_details_item_type
ON nails_sale_details(item_type);


-- ============================================================
-- 12. NAILS_PAYMENTS
-- Pagos/anticipos ligados a ventas.
-- Sirve para pagos parciales, anticipos y saldos.
-- ============================================================

CREATE TABLE IF NOT EXISTS nails_payments (
    id SERIAL PRIMARY KEY,
    sale_id INTEGER NOT NULL REFERENCES nails_sales(id) ON DELETE CASCADE,

    amount NUMERIC(10,2) NOT NULL DEFAULT 0,
    payment_method TEXT DEFAULT 'efectivo',

    payment_type TEXT DEFAULT 'pago',
    notes TEXT,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_nails_payments_method'
    ) THEN
        ALTER TABLE nails_payments
        ADD CONSTRAINT chk_nails_payments_method
        CHECK (payment_method IN ('efectivo', 'transferencia', 'tarjeta', 'mixto', 'otro'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_nails_payments_type'
    ) THEN
        ALTER TABLE nails_payments
        ADD CONSTRAINT chk_nails_payments_type
        CHECK (payment_type IN ('anticipo', 'pago', 'ajuste', 'reembolso'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_nails_payments_sale_id
ON nails_payments(sale_id);

CREATE INDEX IF NOT EXISTS idx_nails_payments_created_at
ON nails_payments(created_at DESC);


-- ============================================================
-- 13. NAILS_GALLERY
-- Galería/catálogo público de trabajos.
-- ============================================================

CREATE TABLE IF NOT EXISTS nails_gallery (
    id SERIAL PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES nails_businesses(id) ON DELETE CASCADE,
    service_id INTEGER REFERENCES nails_services(id) ON DELETE SET NULL,

    title TEXT,
    description TEXT,
    image_url TEXT NOT NULL,

    is_public BOOLEAN DEFAULT TRUE,
    is_active BOOLEAN DEFAULT TRUE,
    sort_order INTEGER DEFAULT 0,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE nails_gallery
ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;

UPDATE nails_gallery
SET is_active = TRUE
WHERE is_active IS NULL;

CREATE INDEX IF NOT EXISTS idx_nails_gallery_business_id
ON nails_gallery(business_id);

CREATE INDEX IF NOT EXISTS idx_nails_gallery_service_id
ON nails_gallery(service_id);

CREATE INDEX IF NOT EXISTS idx_nails_gallery_is_public
ON nails_gallery(is_public)
WHERE is_public = TRUE;

CREATE INDEX IF NOT EXISTS idx_nails_gallery_is_active
ON nails_gallery(is_active)
WHERE is_active = TRUE;


-- ============================================================
-- 14. NAILS_ACTIVITY_LOGS
-- Auditoría específica de Nails.
-- ============================================================

CREATE TABLE IF NOT EXISTS nails_activity_logs (
    id SERIAL PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES nails_businesses(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,

    action TEXT NOT NULL,
    module TEXT,
    detail TEXT,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_nails_activity_logs_business_id
ON nails_activity_logs(business_id);

CREATE INDEX IF NOT EXISTS idx_nails_activity_logs_user_id
ON nails_activity_logs(user_id);

CREATE INDEX IF NOT EXISTS idx_nails_activity_logs_created_at
ON nails_activity_logs(created_at DESC);


-- ============================================================
-- 15. NAILS_EXPENSES
-- Gastos del salón.
-- Sirve para registrar materiales, renta, luz, servicios,
-- publicidad, sueldos, comisiones y gastos recurrentes.
-- ============================================================

CREATE TABLE IF NOT EXISTS nails_expenses (
    id SERIAL PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES nails_businesses(id) ON DELETE CASCADE,

    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'otros',

    amount NUMERIC(10,2) NOT NULL DEFAULT 0,

    expense_date DATE NOT NULL DEFAULT CURRENT_DATE,
    payment_method TEXT DEFAULT 'efectivo',

    is_recurring BOOLEAN DEFAULT FALSE,
    recurring_day INTEGER,
    recurring_frequency TEXT,

    notes TEXT,

    status TEXT DEFAULT 'activo',

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);


-- ============================================================
-- Constraints
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_nails_expenses_category'
    ) THEN
        ALTER TABLE nails_expenses
        ADD CONSTRAINT chk_nails_expenses_category
        CHECK (
            category IN (
                'materiales',
                'renta',
                'servicios',
                'sueldos',
                'comisiones',
                'publicidad',
                'mantenimiento',
                'capacitacion',
                'otros'
            )
        );
    END IF;
END $$;


DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_nails_expenses_payment_method'
    ) THEN
        ALTER TABLE nails_expenses
        ADD CONSTRAINT chk_nails_expenses_payment_method
        CHECK (
            payment_method IN (
                'efectivo',
                'transferencia',
                'tarjeta',
                'mixto',
                'otro'
            )
        );
    END IF;
END $$;


DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_nails_expenses_recurring_frequency'
    ) THEN
        ALTER TABLE nails_expenses
        ADD CONSTRAINT chk_nails_expenses_recurring_frequency
        CHECK (
            recurring_frequency IS NULL
            OR recurring_frequency IN (
                'semanal',
                'quincenal',
                'mensual',
                'bimestral',
                'anual'
            )
        );
    END IF;
END $$;


DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_nails_expenses_status'
    ) THEN
        ALTER TABLE nails_expenses
        ADD CONSTRAINT chk_nails_expenses_status
        CHECK (
            status IN ('activo', 'cancelado')
        );
    END IF;
END $$;


DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_nails_expenses_amount_positive'
    ) THEN
        ALTER TABLE nails_expenses
        ADD CONSTRAINT chk_nails_expenses_amount_positive
        CHECK (amount >= 0);
    END IF;
END $$;


DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_nails_expenses_recurring_day'
    ) THEN
        ALTER TABLE nails_expenses
        ADD CONSTRAINT chk_nails_expenses_recurring_day
        CHECK (
            recurring_day IS NULL
            OR recurring_day BETWEEN 1 AND 31
        );
    END IF;
END $$;


-- ============================================================
-- Índices
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_nails_expenses_business_id
ON nails_expenses(business_id);

CREATE INDEX IF NOT EXISTS idx_nails_expenses_category
ON nails_expenses(category);

CREATE INDEX IF NOT EXISTS idx_nails_expenses_expense_date
ON nails_expenses(expense_date DESC);

CREATE INDEX IF NOT EXISTS idx_nails_expenses_business_date
ON nails_expenses(business_id, expense_date DESC);

CREATE INDEX IF NOT EXISTS idx_nails_expenses_business_category
ON nails_expenses(business_id, category);

CREATE INDEX IF NOT EXISTS idx_nails_expenses_recurring
ON nails_expenses(business_id, is_recurring)
WHERE is_recurring = TRUE;

CREATE INDEX IF NOT EXISTS idx_nails_expenses_status
ON nails_expenses(status);
    """)
    print("  ✓ Tablas: Sianeffects Nails")

    # =============================
    # 4.35 CREAR USUARIO ADMIN
    # =============================
    """
    Crea usuario admin por defecto (seed data).
    
    Este usuario siempre existe y se usa para:
      - Acceso administrativo
      - Testing
      - Operaciones del sistema
      
    Si el usuario admin ya existe, no hace nada (seguridad).
    """
    print("\n🔐 Configurando usuario admin...")
    
    cursor.execute("SELECT id FROM usuarios WHERE email = 'contacto@sianeffects.com'")
    admin = cursor.fetchone()

    if not admin:
        # Generar timestamp UTC actual
        now_utc_str = datetime.now(timezone.utc).isoformat()
        # Hash de contraseña (usar "admin123" solo en desarrollo)
        hashed_pw = generate_password_hash('admin123')

        # Insertar usuario admin
        cursor.execute("""
        INSERT INTO usuarios (
            username, email, password, company_name, role,
            subscription_end, created_at, terms_accepted,
            country_code, last_login
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """, (
            'admin',                           # username
            'contacto@sianeffects.com',       # email
            hashed_pw,                        # password (hasheada)
            'Sianeffects',                    # company_name
            2,                                # role: 2 = superadmin
            '2099-12-31 23:59:59',            # subscription_end (infinita)
            now_utc_str,                      # created_at (ahora)
            True,                             # terms_accepted
            'MX',                             # country_code
            now_utc_str                       # last_login
        ))
        
        admin_id = cursor.fetchone()['id']

        # Crear configuración por defecto para el admin
        cursor.execute("""
        INSERT INTO configuracion (user_id, margen_ganancia, nombre_empresa)
        VALUES (%s, %s, %s)
        """, (admin_id, 200, 'Sianeffects Admin'))

        # Insertar máquinas de ejemplo
        for nombre, costo in [
            ('Corte Plotter', 5.0),
            ('Impresión', 1.5),
            ('Plancha Calor', 12.0)
        ]:
            cursor.execute("""
            INSERT INTO maquinaria (user_id, nombre, costo_desgaste)
            VALUES (%s, %s, %s)
            """, (admin_id, nombre, costo))

        print(f"  ✓ Usuario admin creado (ID: {admin_id})")
    else:
        print("  ✓ Usuario admin ya existe")

    # =============================
    # 4.36 GUARDAR CAMBIOS Y CERRAR
    # =============================
    """
    commit(): guardar TODOS los cambios (CREATE TABLE, CREATE INDEX, INSERT)
    close(): devolver conexión al pool
    """
    conn.commit()
    cursor.close()
    conn.close()

    print("\n✅ Base de datos inicializada correctamente")
    print(f"   Entorno: {env.upper()}")
    print("   Todas las tablas e índices están listos")


# ================================================================================
# SECCIÓN 5: PUNTO DE ENTRADA
# ================================================================================
"""
Permite ejecutar este script directamente:
    python db.py
    
Esto inicializa la base de datos.
"""

if __name__ == '__main__':
    init_db()
    print("\n ¡Listo! Tu BD está lista para usar.")
    print("\n   Importa en tu app Flask:")
    print("   from db import get_db_connection")
    print("\n   Y usa en tus rutas:")
    print("   conn = get_db_connection()")
