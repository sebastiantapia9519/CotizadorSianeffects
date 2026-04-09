import random
import string
import os
import uuid
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
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
# SUBIDA DE ARCHIVOS A CLOUDFLARE R2 (UNIFICADO Y BLINDADO)
# =========================================================
@catalogo_bp.route('/upload-r2', methods=['POST'])
@login_required # Proteccion: Solo usuarios autenticados pueden subir archivos
def upload_r2():
    """
    Recibe un archivo desde el cliente, le asigna un nombre unico para 
    evitar sobreescrituras accidentales y lo sube al bucket de R2.
    """
    file = request.files.get('file')
    if not file:
        return jsonify({"success": False, "error": "No se recibio ningun archivo"}), 400

    # Sanitizamos el nombre base para quitar caracteres especiales o espacios
    base_filename = secure_filename(file.filename) 
    
    # Generamos un identificador unico (UUID) y lo concatenamos al nombre
    # Esto asegura que dos archivos con el mismo nombre no se sobreescriban
    unique_filename = f"{uuid.uuid4().hex}_{base_filename}"
    
    try:
        # Subimos el archivo a R2 especificando su ContentType para que los navegadores lo lean bien
        s3_client.upload_fileobj(
            file,
            BUCKET_NAME,
            unique_filename,
            ExtraArgs={'ContentType': file.content_type}
        )
        
        # Construimos la URL publica final que se guardara en la base de datos
        url_final = f"{PUBLIC_URL}/{unique_filename}"
        
        current_app.logger.info(f"R2_UPLOAD_SUCCESS: Usuario {session.get('user_id')} subió el archivo '{unique_filename}'")
        return jsonify({"success": True, "url": url_final})
    except Exception as e:
        current_app.logger.error(f"R2_UPLOAD_ERROR: Usuario {session.get('user_id')} falló al subir archivo - {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# =========================================================
# 1. GESTION DE CATEGORIAS (PANEL PRINCIPAL)
# =========================================================
@catalogo_bp.route('/admin/catalogo', methods=['GET', 'POST'])
@login_required
def admin_categorias():
    """
    Muestra la lista de categorias existentes y permite crear nuevas.
    """
    conn = get_db()
    
    # Procesa la creacion de una nueva categoria
    if request.method == 'POST':
        nombre = request.form['nombre']
        orden = request.form.get('orden', 0)
        
        conn.execute('INSERT INTO categorias (nombre, orden) VALUES (?, ?)', (nombre, orden))
        conn.commit()
        flash('Categoria creada con exito.', 'success')
        return redirect(url_for('catalogo.admin_categorias'))

    # Obtiene todas las categorias ordenadas por el campo 'orden'
    categorias = conn.execute('SELECT * FROM categorias ORDER BY orden ASC, id DESC').fetchall()
    conn.close()
    
    return render_template('catalogo/admin_categorias.html', categorias=categorias)

# =========================================================
# 2. GESTION DE PRODUCTOS (DENTRO DE UNA CATEGORIA)
# =========================================================
@catalogo_bp.route('/admin/catalogo/<int:cat_id>', methods=['GET', 'POST'])
@login_required
def admin_productos(cat_id):
    """
    Muestra los productos de una categoria especifica.
    Permite crear nuevos productos o editar los existentes.
    """
    conn = get_db()

    # Validamos que la categoria exista
    categoria = conn.execute(
        'SELECT * FROM categorias WHERE id = ?', (cat_id,)
    ).fetchone()

    if request.method == 'POST':
        producto_id = request.form.get('producto_id')

        titulo = request.form['titulo']
        descripcion = request.form['descripcion']
        precio = request.form.get('precio', 0)
        stock_status = 1 if request.form.get('en_stock') else 0

        media_url = request.form.get('media_url', '').strip()
        media_type = request.form.get('media_type')

        # Inteligencia de formatos: Forzamos el tipo de archivo basado en su extension
        # Esto previene errores de capa 8 si el usuario selecciona el tipo incorrecto
        if media_url:
            ext = media_url.split('.')[-1].lower() 
            if ext in ['mp3', 'wav', 'ogg', 'm4a']:
                media_type = 'audio'
            elif ext in ['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg']:
                media_type = 'imagen' 
            elif ext in ['mp4', 'mov', 'avi', 'webm', 'mkv']:
                media_type = 'video'

        # Flujo de actualizacion (Editar producto existente)
        if producto_id:
            conn.execute('''
                UPDATE catalogo_productos
                SET titulo = ?, descripcion = ?, precio = ?, media_url = ?, media_type = ?, stock = ?
                WHERE id = ?
            ''', (titulo, descripcion, precio, media_url, media_type, stock_status, producto_id))
            conn.commit()
            flash('Producto actualizado correctamente.', 'success')

        # Flujo de creacion (Nuevo producto)
        else:
            # Generacion de un SKU unico basado en el nombre de la categoria
            nombre_limpio = ''.join(filter(str.isalpha, categoria['nombre']))
            prefix = nombre_limpio[:3].upper() if len(nombre_limpio) >= 2 else "PROD"

            while True:
                random_digits = ''.join(random.choices(string.digits, k=5))
                sku_generado = f"{prefix}-{random_digits}"

                # Verificamos que el SKU no exista ya en la base de datos
                existe = conn.execute(
                    'SELECT id FROM catalogo_productos WHERE sku = ?',
                    (sku_generado,)
                ).fetchone()

                if not existe:
                    break

            conn.execute('''
                INSERT INTO catalogo_productos
                (categoria_id, sku, titulo, descripcion, media_url, media_type, precio, stock)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (cat_id, sku_generado, titulo, descripcion, media_url, media_type, precio, stock_status))
            conn.commit()
            flash(f'Producto agregado. SKU asignado: {sku_generado}', 'success')

        return redirect(url_for('catalogo.admin_productos', cat_id=cat_id))
    
    # Obtencion de los productos de la categoria actual
    productos_db = conn.execute(
        'SELECT * FROM catalogo_productos WHERE categoria_id = ? ORDER BY id DESC',
        (cat_id,)
    ).fetchall()
    
    conn.close()

    # Convertimos las filas a diccionarios para asegurar compatibilidad con JSON en el frontend
    productos = [dict(row) for row in productos_db] 

    return render_template(
        'catalogo/admin_productos.html',
        categoria=categoria, 
        productos=productos  
    )

# =========================================================
# 3. INTERRUPTOR RAPIDO (ON/OFF DE VISIBILIDAD)
# =========================================================
@catalogo_bp.route('/api/catalogo/toggle', methods=['POST'])
@login_required
def toggle_status():
    """
    Activa o desactiva la visibilidad publica de una categoria o producto.
    Usado por llamadas AJAX desde el panel de administracion.
    """
    data = request.get_json()
    tipo = data.get('tipo')
    id_obj = data.get('id')
    nuevo_estado = data.get('activo')
    
    conn = get_db()
    tabla = 'categorias' if tipo == 'categoria' else 'catalogo_productos'
    
    try:
        query = f'UPDATE {tabla} SET activo = ? WHERE id = ?'
        conn.execute(query, (nuevo_estado, id_obj))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        current_app.logger.error(f"CATALOG_TOGGLE_ERROR: Fallo al cambiar estado de {tipo} ID {id_obj} - {e}")
        return jsonify({'success': False, 'error': str(e)})
    finally:
        conn.close()

# =========================================================
# 4. EDITAR CATEGORIA (NOMBRE Y ORDEN)
# =========================================================
@catalogo_bp.route('/admin/catalogo/editar_categoria', methods=['POST'])
@login_required
def editar_categoria():
    """
    Actualiza la informacion basica de una categoria.
    """
    conn = get_db()
    cat_id = request.form['cat_id']
    nombre = request.form['nombre']
    orden = request.form.get('orden', 0)
    
    try:
        conn.execute(
            'UPDATE categorias SET nombre = ?, orden = ? WHERE id = ?',
            (nombre, orden, cat_id)
        )
        conn.commit()
        flash('Categoria actualizada correctamente.', 'success')
    except Exception as e:
        flash(f'Error al editar: {e}', 'error')
    finally:
        conn.close()
        
    return redirect(url_for('catalogo.admin_categorias'))

# =========================================================
# 5. ELIMINAR ITEMS (CON LIMPIEZA TOTAL DE R2)
# =========================================================
@catalogo_bp.route('/admin/catalogo/delete/<tipo>/<int:id_obj>')
@login_required
def delete_item(tipo, id_obj):
    """
    Elimina categorias o productos de la base de datos y, crucialmente,
    elimina los archivos fisicos asociados en Cloudflare R2 para ahorrar espacio.
    """
    conn = get_db()
    
    try:
        if tipo == 'categoria':
            # Buscamos todos los productos de esta categoria que tengan archivo en R2
            productos = conn.execute(
                'SELECT media_url FROM catalogo_productos WHERE categoria_id = ? AND media_url IS NOT NULL AND media_url != ""', 
                (id_obj,)
            ).fetchall()
            
            # Borramos iterativamente los archivos de la nube
            for prod in productos:
                url_archivo = prod['media_url']
                try:
                    nombre_archivo = url_archivo.split('/')[-1]
                    s3_client.delete_object(Bucket=BUCKET_NAME, Key=nombre_archivo)
                except Exception as e:
                    current_app.logger.warning(f"R2_DELETE_WARNING: Fallo al borrar archivo huérfano '{nombre_archivo}' de R2 - {e}")

            # Borramos los registros en cascada
            conn.execute('DELETE FROM catalogo_productos WHERE categoria_id = ?', (id_obj,))
            conn.execute('DELETE FROM categorias WHERE id = ?', (id_obj,))
            conn.commit()
            
            flash('Categoria y todos sus productos eliminados.', 'warning')
            return redirect(url_for('catalogo.admin_categorias'))
            
        else: 
            # Logica para eliminar un solo producto
            prod = conn.execute(
                'SELECT categoria_id, media_url FROM catalogo_productos WHERE id = ?', 
                (id_obj,)
            ).fetchone()

            if prod:
                cat_id = prod['categoria_id']
                url_archivo = prod['media_url']
                
                # Si el producto tiene archivo, lo borramos de R2
                if url_archivo:
                    try:
                        nombre_archivo = url_archivo.split('/')[-1]
                        s3_client.delete_object(Bucket=BUCKET_NAME, Key=nombre_archivo)
                        current_app.logger.info(f"R2_DELETE_SUCCESS: Archivo '{nombre_archivo}' eliminado correctamente de la nube.")
                    except Exception as e:
                        current_app.logger.warning(f"R2_DELETE_WARNING: Fallo al borrar archivo '{nombre_archivo}' de R2 - {e}")

                conn.execute('DELETE FROM catalogo_productos WHERE id = ?', (id_obj,))
                conn.commit()
                flash('Producto y archivo eliminados correctamente.', 'success')
                return redirect(url_for('catalogo.admin_productos', cat_id=cat_id))
            else:
                flash('Producto no encontrado', 'error')
                return redirect(url_for('catalogo.admin_categorias'))
                
    except Exception as e:
        conn.rollback()
        flash(f'Error al eliminar: {e}', 'error')
        return redirect(url_for('catalogo.admin_categorias'))
    finally:
        conn.close()

# =========================================================
# 6. VISTA PUBLICA (CLIENTES FINAL)
# =========================================================
@catalogo_bp.route('/catalogo')
def ver_catalogo():
    """
    Renderiza el catalogo publico. Solo muestra categorias y productos
    cuyo flag 'activo' este en 1.
    """
    conn = get_db()
    
    categorias = conn.execute(
        'SELECT * FROM categorias WHERE activo = 1 ORDER BY orden ASC'
    ).fetchall()
    
    catalogo_data = []
    
    # Anidamos los productos dentro de su respectiva categoria
    for cat in categorias:
        productos = conn.execute('''
            SELECT * FROM catalogo_productos
            WHERE categoria_id = ? AND activo = 1
            ORDER BY orden ASC, id DESC
        ''', (cat['id'],)).fetchall()
        
        if productos:
            catalogo_data.append({
                'info': dict(cat),
                'productos': [dict(prod) for prod in productos]
            })
            
    conn.close()
    
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
    """
    Actualiza el indicador de inventario (En Stock / Agotado) de un producto.
    """
    data = request.get_json()
    prod_id = data.get('id')
    nuevo_stock = data.get('stock') 
    
    conn = get_db()
    try:
        conn.execute('UPDATE catalogo_productos SET stock = ? WHERE id = ?', (nuevo_stock, prod_id))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()