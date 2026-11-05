import random
import string
import os
import uuid
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app, session
from db import get_db_connection as get_db
from helpers import login_required, admin_required
import boto3
from botocore.config import Config
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# Carga las variables del archivo .env para mantener las credenciales seguras
load_dotenv()  

catalogo_bp = Blueprint('catalogo', __name__)

# Cantidad máxima de productos destacados (aleatorios) en la portada
MAX_DESTACADOS = 8

# Extensiones por tipo de archivo (usadas para detectar el tipo de media a partir de la URL)
EXT_AUDIO = ('.mp3', '.wav', '.ogg', '.m4a')
EXT_IMAGEN = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg')
EXT_VIDEO = ('.mp4', '.mov', '.avi', '.webm', '.mkv')
EXT_DOCUMENTO = ('.pdf',)


def admin_api_required(f):
    """
    Igual que admin_required pero para endpoints JSON: responde 403 en JSON
    en lugar de redirigir (una redirección rompería el fetch del frontend).
    Debe ir DEBAJO de @login_required, que es quien sincroniza session['role'] con la BD.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get('role', 0) < 1:
            return jsonify({'success': False, 'error': 'Acceso denegado'}), 403
        return f(*args, **kwargs)
    return wrapper


def tipo_por_url(url):
    """
    Devuelve 'image', 'video', 'audio' o 'documento' según la extensión de la URL.
    Ignora querystrings (?v=1). Devuelve '' si no se reconoce o no hay URL.
    """
    if not url:
        return ''
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext in EXT_AUDIO:
        return 'audio'
    if ext in EXT_VIDEO:
        return 'video'
    if ext in EXT_IMAGEN:
        return 'image'
    if ext in EXT_DOCUMENTO:
        return 'documento'
    return ''

# =========================================================
# CONFIGURACION DE CLOUDFLARE R2
# =========================================================
# Obtenemos las credenciales desde las variables de entorno
ACCESS_KEY = os.getenv('R2_ACCESS_KEY')
SECRET_KEY = os.getenv('R2_SECRET_KEY')
ENDPOINT_URL = os.getenv('R2_ENDPOINT_URL')
BUCKET_NAME = os.getenv('R2_BUCKET_NAME')
PUBLIC_URL = os.getenv('R2_PUBLIC_URL')

# Inicializamos el cliente de boto3 configurado para Cloudflare R2
s3_client = boto3.client(
    service_name='s3',
    endpoint_url=ENDPOINT_URL,
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    region_name='auto',
    config=Config(signature_version='s3v4')
)

# =========================================================
# SUBIDA DE ARCHIVOS A CLOUDFLARE R2 (ORGANIZADO EN CARPETAS)
# =========================================================
@catalogo_bp.route('/upload-r2', methods=['POST'])
@login_required 
@admin_api_required
def upload_r2():
    """
    Recibe un archivo, arma una ruta de carpetas dinámica basada en 
    la categoría y el tipo de archivo, y lo sube a R2.
    """
    u_name = session.get('username', 'Anonimo')
    u_id = session.get('user_id', 'N/A')
    
    file = request.files.get('file')
    if not file:
        return jsonify({"success": False, "error": "No se recibio ningun archivo"}), 400

    # 1. Obtenemos el ID de la categoría (enviado desde el FormData de JS)
    categoria_id = request.form.get('categoria_id', 'general')

    # 2. Determinamos la carpeta según el tipo de archivo
    mime_type = file.content_type or ''
    if 'video' in mime_type:
        tipo_carpeta = 'videos'
    elif 'audio' in mime_type:
        tipo_carpeta = 'audios'
    else:
        tipo_carpeta = 'imagenes'

    # Sanitizamos el nombre base
    # file.filename puede ser None o vacío; en ese caso usamos un nombre genérico
    base_filename = secure_filename(file.filename or '') or 'archivo'
    unique_filename = f"{uuid.uuid4().hex}_{base_filename}"
    
    # 3. ARMAMOS LA RUTA COMPLETA (Cloudflare crea las carpetas mágicamente)
    # Ejemplo: categoria_4/imagenes/1234abcd_foto.jpg
    ruta_r2 = f"categoria_{categoria_id}/{tipo_carpeta}/{unique_filename}"
    
    try:
        # Subimos usando la ruta completa (Key)
        s3_client.upload_fileobj(
            file,
            BUCKET_NAME,
            ruta_r2,
            ExtraArgs={'ContentType': mime_type}
        )
        
        # Construimos la URL publica final
        url_final = f"{PUBLIC_URL}/{ruta_r2}"
        
        current_app.logger.info(f"R2_UPLOAD_SUCCESS: Usuario '{u_name}' (ID: {u_id}) subio '{ruta_r2}'")
        return jsonify({"success": True, "url": url_final})
    except Exception as e:
        current_app.logger.error(f"R2_UPLOAD_ERROR: Usuario '{u_name}' (ID: {u_id}) fallo al subir archivo - {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# =========================================================
# 1. GESTION DE CATEGORIAS (PANEL PRINCIPAL)
# =========================================================
@catalogo_bp.route('/admin/catalogo', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_categorias():
    u_name = session.get('username', 'Anonimo')
    u_id = session.get('user_id', 'N/A')

    conn = get_db()
    cursor = conn.cursor()
    try:
        if request.method == 'POST':
            nombre = request.form['nombre']
            orden = request.form.get('orden', 0)

            try:
                cursor.execute('INSERT INTO categorias (nombre, orden) VALUES (%s, %s)', (nombre, orden))
                conn.commit()
                current_app.logger.info(f"CATALOG_CATEGORY_CREATED: Usuario '{u_name}' (ID: {u_id}) creo la categoria '{nombre}'")
                flash('Categoria creada con exito.', 'success')
            except Exception as e:
                conn.rollback()
                current_app.logger.error(f"CATALOG_CATEGORY_ERROR: Usuario '{u_name}' (ID: {u_id}) - {e}")
                flash(f'Error al crear categoria: {str(e)}', 'danger')

            return redirect(url_for('catalogo.admin_categorias'))

        cursor.execute('SELECT * FROM categorias ORDER BY orden ASC, id DESC')
        categorias = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    return render_template('catalogo/admin_categorias.html', categorias=categorias)

# =========================================================
# 2. GESTION DE PRODUCTOS (DENTRO DE UNA CATEGORIA)
# =========================================================
@catalogo_bp.route('/admin/catalogo/<int:cat_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_productos(cat_id):
    u_name = session.get('username', 'Anonimo')
    u_id = session.get('user_id', 'N/A')

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT * FROM categorias WHERE id = %s', (cat_id,))
        categoria = cursor.fetchone()

        if request.method == 'POST':
            producto_id = request.form.get('producto_id')
            titulo = request.form['titulo']
            descripcion = request.form['descripcion']
            precio = request.form.get('precio', 0)
            stock_status = True if request.form.get('en_stock') else False
            media_url = request.form.get('media_url', '').strip()
            media_type = request.form.get('media_type')

            if media_url:
                ext = media_url.split('.')[-1].lower() 
                if ext in ['mp3', 'wav', 'ogg', 'm4a']:
                    media_type = 'audio'
                elif ext in ['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg']:
                    media_type = 'imagen' 
                elif ext in ['mp4', 'mov', 'avi', 'webm', 'mkv']:
                    media_type = 'video'
                elif ext == 'pdf':
                    # Los PDF se muestran con vista previa en el modal del catálogo
                    media_type = 'documento'

            try:
                if producto_id:
                    cursor.execute('''
                        UPDATE catalogo_productos
                        SET titulo = %s, descripcion = %s, precio = %s, media_url = %s, media_type = %s, stock = %s
                        WHERE id = %s
                    ''', (titulo, descripcion, precio, media_url, media_type, stock_status, producto_id))
                    conn.commit()
                    current_app.logger.info(f"CATALOG_PRODUCT_UPDATED: Usuario '{u_name}' (ID: {u_id}) actualizo el producto '{titulo}' (ID: {producto_id})")
                    flash('Producto actualizado correctamente.', 'success')
                else:
                    nombre_limpio = ''.join(filter(str.isalpha, categoria['nombre']))
                    prefix = nombre_limpio[:3].upper() if len(nombre_limpio) >= 2 else "PROD"

                    while True:
                        random_digits = ''.join(random.choices(string.digits, k=5))
                        sku_generado = f"{prefix}-{random_digits}"

                        cursor.execute('SELECT id FROM catalogo_productos WHERE sku = %s', (sku_generado,))
                        existe = cursor.fetchone()
                        if not existe:
                            break

                    cursor.execute('''
                        INSERT INTO catalogo_productos
                        (categoria_id, sku, titulo, descripcion, media_url, media_type, precio, stock)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ''', (cat_id, sku_generado, titulo, descripcion, media_url, media_type, precio, stock_status))
                    conn.commit()
                    current_app.logger.info(f"CATALOG_PRODUCT_CREATED: Usuario '{u_name}' (ID: {u_id}) creo el producto '{titulo}' (SKU: {sku_generado})")
                    flash(f'Producto agregado. SKU asignado: {sku_generado}', 'success')
            except Exception as e:
                conn.rollback()
                current_app.logger.error(f"CATALOG_PRODUCT_ERROR: Usuario '{u_name}' (ID: {u_id}) - {e}")
                flash(f'Error al guardar producto: {str(e)}', 'danger')

            return redirect(url_for('catalogo.admin_productos', cat_id=cat_id))

        cursor.execute(
            'SELECT * FROM catalogo_productos WHERE categoria_id = %s ORDER BY id DESC',
            (cat_id,)
        )
        productos_db = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    productos = [dict(row) for row in productos_db] 

    return render_template('catalogo/admin_productos.html', categoria=categoria, productos=productos)

# =========================================================
# 3. INTERRUPTOR RAPIDO (ON/OFF DE VISIBILIDAD)
# =========================================================
@catalogo_bp.route('/api/catalogo/toggle', methods=['POST'])
@login_required
@admin_api_required
def toggle_status():
    u_name = session.get('username', 'Anonimo')
    u_id = session.get('user_id', 'N/A')
    data = request.get_json()
    tipo = data.get('tipo')
    id_obj = data.get('id')
    nuevo_estado = True if data.get('activo') else False
    
    conn = get_db()
    cursor = conn.cursor()
    tabla = 'categorias' if tipo == 'categoria' else 'catalogo_productos'
    
    try:
        query = f'UPDATE {tabla} SET activo = %s WHERE id = %s'
        cursor.execute(query, (nuevo_estado, id_obj))
        conn.commit()
        estado_str = "Activo" if nuevo_estado else "Inactivo"
        current_app.logger.info(f"CATALOG_TOGGLE: Usuario '{u_name}' (ID: {u_id}) cambio visibilidad de {tipo} ID {id_obj} a {estado_str}")
        return jsonify({'success': True})
    except Exception as e:
        current_app.logger.error(f"CATALOG_TOGGLE_ERROR: Usuario '{u_name}' (ID: {u_id}) fallo al cambiar estado de {tipo} ID {id_obj} - {e}")
        return jsonify({'success': False, 'error': str(e)})
    finally:
        cursor.close()
        conn.close()

# =========================================================
# 4. EDITAR CATEGORIA (NOMBRE Y ORDEN)
# =========================================================
@catalogo_bp.route('/admin/catalogo/editar_categoria', methods=['POST'])
@login_required
@admin_required
def editar_categoria():
    u_name = session.get('username', 'Anonimo')
    u_id = session.get('user_id', 'N/A')
    conn = get_db()
    cursor = conn.cursor()
    cat_id = request.form['cat_id']
    nombre = request.form['nombre']
    orden = request.form.get('orden', 0)
    
    try:
        cursor.execute(
            'UPDATE categorias SET nombre = %s, orden = %s WHERE id = %s',
            (nombre, orden, cat_id)
        )
        conn.commit()
        current_app.logger.info(f"CATALOG_CATEGORY_EDITED: Usuario '{u_name}' (ID: {u_id}) edito categoria ID {cat_id}")
        flash('Categoria actualizada correctamente.', 'success')
    except Exception as e:
        current_app.logger.error(f"CATALOG_CATEGORY_EDIT_ERROR: Usuario '{u_name}' (ID: {u_id}) - {e}")
        flash(f'Error al editar: {e}', 'danger')
    finally:
        cursor.close()
        conn.close()
        
    return redirect(url_for('catalogo.admin_categorias'))

# =========================================================
# 5. ELIMINAR ITEMS (CON LIMPIEZA TOTAL DE R2 Y CARPETAS)
# =========================================================
@catalogo_bp.route('/admin/catalogo/delete/<tipo>/<int:id_obj>', methods=['POST'])
@login_required
@admin_required
def delete_item(tipo, id_obj):
    u_name = session.get('username', 'Anonimo')
    u_id = session.get('user_id', 'N/A')
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        if tipo == 'categoria':
            cursor.execute("""
                SELECT media_url FROM catalogo_productos 
                WHERE categoria_id = %s AND media_url IS NOT NULL AND media_url != ''
            """, (id_obj,))
            productos = cursor.fetchall()
            
            for prod in productos:
                url_archivo = prod['media_url']
                try:
                    # NUEVA LÓGICA: Extraemos la ruta completa del archivo restando el dominio base
                    key_archivo = url_archivo.replace(PUBLIC_URL, "").lstrip("/")
                    s3_client.delete_object(Bucket=BUCKET_NAME, Key=key_archivo)
                except Exception as e:
                    current_app.logger.warning(f"R2_DELETE_WARNING: Fallo al borrar archivo huerfano de R2 - {e}")

            cursor.execute('DELETE FROM catalogo_productos WHERE categoria_id = %s', (id_obj,))
            cursor.execute('DELETE FROM categorias WHERE id = %s', (id_obj,))
            conn.commit()
            
            current_app.logger.info(f"CATALOG_CATEGORY_DELETED: Usuario '{u_name}' (ID: {u_id}) elimino categoria ID {id_obj} y sus productos")
            flash('Categoria y todos sus productos eliminados.', 'warning')
            return redirect(url_for('catalogo.admin_categorias'))
            
        else: 
            cursor.execute(
                'SELECT categoria_id, media_url FROM catalogo_productos WHERE id = %s', 
                (id_obj,)
            )
            prod = cursor.fetchone()

            if prod:
                cat_id = prod['categoria_id']
                url_archivo = prod['media_url']
                
                if url_archivo:
                    try:
                        # NUEVA LÓGICA: Extraemos la ruta completa (carpeta + archivo)
                        key_archivo = url_archivo.replace(PUBLIC_URL, "").lstrip("/")
                        s3_client.delete_object(Bucket=BUCKET_NAME, Key=key_archivo)
                        current_app.logger.info(f"R2_DELETE_SUCCESS: Archivo eliminado correctamente.")
                    except Exception as e:
                        current_app.logger.warning(f"R2_DELETE_WARNING: Fallo al borrar archivo de R2 - {e}")

                cursor.execute('DELETE FROM catalogo_productos WHERE id = %s', (id_obj,))
                conn.commit()
                current_app.logger.info(f"CATALOG_PRODUCT_DELETED: Usuario '{u_name}' (ID: {u_id}) elimino producto ID {id_obj}")
                flash('Producto y archivo eliminados correctamente.', 'success')
                return redirect(url_for('catalogo.admin_productos', cat_id=cat_id))
            else:
                flash('Producto no encontrado', 'danger')
                return redirect(url_for('catalogo.admin_categorias'))
                
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"CATALOG_DELETE_ERROR: Usuario '{u_name}' (ID: {u_id}) fallo al eliminar - {e}")
        flash(f'Error al eliminar: {e}', 'danger')
        return redirect(url_for('catalogo.admin_categorias'))
    finally:
        cursor.close()
        conn.close()

# =========================================================
# 6. VISTA PUBLICA (CLIENTES FINAL)
# =========================================================
@catalogo_bp.route('/catalogo')
def ver_catalogo():
    """
    Arma los datos de la vista pública:
      - catalogo:   categorías activas con sus productos activos (+ portada calculada)
      - destacados: productos en stock elegidos AL AZAR en cada carga (hasta MAX_DESTACADOS)
      - hero_imgs / promo_img: imágenes aleatorias para el collage del hero (lista) y el banner (o None)
    """
    conn = get_db()
    cursor = conn.cursor()
    catalogo_data = []

    try:
        cursor.execute('SELECT * FROM categorias WHERE activo = True ORDER BY orden ASC')
        categorias = cursor.fetchall()

        # Una sola consulta para todos los productos (evita una consulta por categoría)
        cursor.execute('''
            SELECT * FROM catalogo_productos
            WHERE activo = True
            ORDER BY orden ASC, id DESC
        ''')
        productos_por_cat = {}
        for prod in cursor.fetchall():
            productos_por_cat.setdefault(prod['categoria_id'], []).append(dict(prod))
    finally:
        cursor.close()
        conn.close()

    todos_los_productos = []

    for cat in categorias:
        productos = productos_por_cat.get(cat['id'], [])
        if not productos:
            continue

        # El nombre de la categoría viaja con cada producto (lo usa el modal)
        for p in productos:
            p['categoria_nombre'] = cat['nombre']

        # Portada de la categoría: preferimos la primera imagen; si no hay, el primer media disponible
        portada, portada_tipo = '', ''
        for p in productos:
            tipo = tipo_por_url(p.get('media_url'))
            if tipo == 'image':
                portada, portada_tipo = p['media_url'], tipo
                break
            if tipo and not portada:
                portada, portada_tipo = p['media_url'], tipo

        info = dict(cat)
        info['portada'] = portada
        info['portada_tipo'] = portada_tipo

        catalogo_data.append({'info': info, 'productos': productos})
        todos_los_productos.extend(productos)

    # Destacados aleatorios (solo con stock). Cuando haya productos reales para destacar,
    # basta con cambiar este bloque (por ejemplo, filtrar por una columna "destacado").
    con_stock = [p for p in todos_los_productos if p.get('stock')]
    destacados = random.sample(con_stock, min(MAX_DESTACADOS, len(con_stock)))

    # Imágenes aleatorias: 3 para el collage del hero y 1 para el banner (distintas entre sí cuando hay suficientes)
    imagenes = [p['media_url'] for p in todos_los_productos if tipo_por_url(p.get('media_url')) == 'image']
    muestra = random.sample(imagenes, min(4, len(imagenes)))
    hero_imgs = muestra[:3]
    promo_img = muestra[3] if len(muestra) > 3 else (muestra[0] if muestra else None)

    # LOG Opcional para vista pública.
    u_name = session.get('username', 'Visitante')
    current_app.logger.info(f"CATALOG_VIEW: Usuario '{u_name}' visualizo el catalogo publico")
    
    return render_template(
        'catalogo/galeria_sianeffects.html',
        catalogo=catalogo_data,
        destacados=destacados,
        hero_imgs=hero_imgs,
        promo_img=promo_img,
        year=datetime.now().year
    )
        
# =========================================================
# 7. ACTUALIZAR STOCK (API)
# =========================================================
@catalogo_bp.route('/api/catalogo/update-stock', methods=['POST'])
@login_required
@admin_api_required
def update_stock():
    u_name = session.get('username', 'Anonimo')
    u_id = session.get('user_id', 'N/A')
    data = request.get_json()
    prod_id = data.get('id')
    nuevo_stock = True if data.get('stock') else False 
    
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('UPDATE catalogo_productos SET stock = %s WHERE id = %s', (nuevo_stock, prod_id))
        conn.commit()
        stock_str = "En Stock" if nuevo_stock else "Agotado"
        current_app.logger.info(f"CATALOG_STOCK_UPDATE: Usuario '{u_name}' (ID: {u_id}) marco producto ID {prod_id} como '{stock_str}'")
        return jsonify({'success': True})
    except Exception as e:
        current_app.logger.error(f"CATALOG_STOCK_ERROR: Usuario '{u_name}' (ID: {u_id}) fallo al actualizar stock de producto ID {prod_id} - {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()