import random
import string
import os
import uuid
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app, session
from db import get_db_connection as get_db
from helpers import login_required
import boto3
from botocore.config import Config
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# Carga las variables del archivo .env para mantener las credenciales seguras
load_dotenv()  

catalogo_bp = Blueprint('catalogo', __name__)

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
    base_filename = secure_filename(file.filename) 
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
def admin_categorias():
    conn = get_db()
    cursor = conn.cursor()
    u_name = session.get('username', 'Anonimo')
    u_id = session.get('user_id', 'N/A')
    
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
    
    cursor.close()
    conn.close()
    
    return render_template('catalogo/admin_categorias.html', categorias=categorias)

# =========================================================
# 2. GESTION DE PRODUCTOS (DENTRO DE UNA CATEGORIA)
# =========================================================
@catalogo_bp.route('/admin/catalogo/<int:cat_id>', methods=['GET', 'POST'])
@login_required
def admin_productos(cat_id):
    conn = get_db()
    cursor = conn.cursor()
    u_name = session.get('username', 'Anonimo')
    u_id = session.get('user_id', 'N/A')

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
    
    cursor.close()
    conn.close()

    productos = [dict(row) for row in productos_db] 

    return render_template('catalogo/admin_productos.html', categoria=categoria, productos=productos)

# =========================================================
# 3. INTERRUPTOR RAPIDO (ON/OFF DE VISIBILIDAD)
# =========================================================
@catalogo_bp.route('/api/catalogo/toggle', methods=['POST'])
@login_required
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
@catalogo_bp.route('/admin/catalogo/delete/<tipo>/<int:id_obj>')
@login_required
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
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM categorias WHERE activo = True ORDER BY orden ASC')
    categorias = cursor.fetchall()
    
    catalogo_data = []
    
    for cat in categorias:
        cursor.execute('''
            SELECT * FROM catalogo_productos
            WHERE categoria_id = %s AND activo = True
            ORDER BY orden ASC, id DESC
        ''', (cat['id'],))
        productos = cursor.fetchall()
        
        if productos:
            catalogo_data.append({
                'info': dict(cat),
                'productos': [dict(prod) for prod in productos]
            })
            
    cursor.close()
    conn.close()
    
    # LOG Opcional para vista pública.
    u_name = session.get('username', 'Visitante')
    current_app.logger.info(f"CATALOG_VIEW: Usuario '{u_name}' visualizo el catalogo publico")
    
    return render_template(
        'catalogo/galeria_sianeffects.html',
        catalogo=catalogo_data
    )
        
# =========================================================
# 7. ACTUALIZAR STOCK (API)
# =========================================================
@catalogo_bp.route('/api/catalogo/update-stock', methods=['POST'])
@login_required
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