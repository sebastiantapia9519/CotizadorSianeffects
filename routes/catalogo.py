import random
import string
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from db import get_db_connection as get_db
from helpers import login_required

catalogo_bp = Blueprint('catalogo', __name__)

# =========================================================
# 1. GESTIÓN DE CATEGORÍAS (PANEL PRINCIPAL)
# =========================================================
@catalogo_bp.route('/admin/catalogo', methods=['GET', 'POST'])
@login_required
def admin_categorias():
    conn = get_db()
    
    # --- CREAR NUEVA CATEGORÍA ---
    if request.method == 'POST':
        nombre = request.form['nombre']
        orden = request.form.get('orden', 0)
        
        conn.execute('INSERT INTO categorias (nombre, orden) VALUES (?, ?)', (nombre, orden))
        conn.commit()
        flash('Categoría creada con éxito.', 'success')
        return redirect(url_for('catalogo.admin_categorias'))

    # --- VER CATEGORÍAS EXISTENTES ---
    categorias = conn.execute('SELECT * FROM categorias ORDER BY orden ASC, id DESC').fetchall()
    conn.close()
    
    return render_template('catalogo/admin_categorias.html', categorias=categorias)
# =========================================================
# 2. GESTIÓN DE PRODUCTOS (DENTRO DE UNA CATEGORÍA)
# =========================================================
@catalogo_bp.route('/admin/catalogo/<int:cat_id>', methods=['GET', 'POST'])
@login_required
def admin_productos(cat_id):
    conn = get_db()

    # Obtener la categoría actual
    categoria = conn.execute(
        'SELECT * FROM categorias WHERE id = ?', (cat_id,)
    ).fetchone()

    # ==============================
    # CREAR o EDITAR PRODUCTO
    # ==============================
    if request.method == 'POST':
        producto_id = request.form.get('producto_id')

        titulo = request.form['titulo']
        descripcion = request.form['descripcion']
        precio = request.form.get('precio', 0)

        media_url = request.form.get('media_url', '').strip()
        # Tomamos el tipo que dice el formulario, pero...
        media_type = request.form.get('media_type')

        # ... AQUI AGREGAMOS LA INTELIGENCIA:
        # Si la URL tiene una extensión clara, forzamos el tipo correcto
        # para evitar errores humanos.
        if media_url:
            ext = media_url.split('.')[-1].lower() # Obtiene lo que está después del último punto
            
            # Lista de extensiones comunes
            if ext in ['mp3', 'wav', 'ogg', 'm4a']:
                media_type = 'audio'
            elif ext in ['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg']:
                media_type = 'imagen' # Ojo: usa 'imagen' o 'image' según prefieras en tu BD. Tu código usaba 'imagen' en el HTML anterior.
            elif ext in ['mp4', 'mov', 'avi', 'webm', 'mkv']:
                media_type = 'video'

        # EDITAR
        if producto_id:
            conn.execute('''
                UPDATE catalogo_productos
                SET titulo = ?,
                    descripcion = ?,
                    precio = ?,
                    media_url = ?,
                    media_type = ?
                WHERE id = ?
            ''', (titulo, descripcion, precio, media_url, media_type, producto_id))

            conn.commit()
            flash('Producto actualizado correctamente.', 'success')

        # CREAR
        else:
            # (El resto de tu código de crear sigue igual...)
            nombre_limpio = ''.join(filter(str.isalpha, categoria['nombre']))
            prefix = nombre_limpio[:3].upper() if len(nombre_limpio) >= 2 else "PROD"

            while True:
                random_digits = ''.join(random.choices(string.digits, k=5))
                sku_generado = f"{prefix}-{random_digits}"

                existe = conn.execute(
                    'SELECT id FROM catalogo_productos WHERE sku = ?',
                    (sku_generado,)
                ).fetchone()

                if not existe:
                    break

            conn.execute('''
                INSERT INTO catalogo_productos
                (categoria_id, sku, titulo, descripcion, media_url, media_type, precio)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                cat_id,
                sku_generado,
                titulo,
                descripcion,
                media_url,
                media_type,
                precio
            ))

            conn.commit()
            flash(f'Producto agregado. SKU asignado: {sku_generado}', 'success')

        return redirect(url_for('catalogo.admin_productos', cat_id=cat_id))
    
    # ==============================
    # OBTENER PRODUCTOS
    # ==============================
    productos_db = conn.execute(
        'SELECT * FROM catalogo_productos WHERE categoria_id = ? ORDER BY id DESC',
        (cat_id,)
    ).fetchall()
    
    conn.close()

    # --- CORRECCIÓN: Convertir las filas a diccionarios para que funcione el JSON ---
    productos = [dict(row) for row in productos_db] 

    return render_template(
        'catalogo/admin_productos.html',
        categoria=categoria, # La categoría funciona bien como Row normal
        productos=productos  # Los productos ahora son diccionarios y tojson funcionará
    )

# =========================================================
# 3. INTERRUPTOR RÁPIDO (ON/OFF)
# =========================================================
@catalogo_bp.route('/api/catalogo/toggle', methods=['POST'])
@login_required
def toggle_status():
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
        print(f"Error toggle: {e}")
        return jsonify({'success': False, 'error': str(e)})
    finally:
        conn.close()

# =========================================================
# 4. EDITAR CATEGORÍA
# =========================================================
@catalogo_bp.route('/admin/catalogo/editar_categoria', methods=['POST'])
@login_required
def editar_categoria():
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
        flash('Categoría actualizada correctamente.', 'success')
    except Exception as e:
        flash(f'Error al editar: {e}', 'error')
    finally:
        conn.close()
        
    return redirect(url_for('catalogo.admin_categorias'))

# =========================================================
# 5. ELIMINAR ITEMS
# =========================================================
@catalogo_bp.route('/admin/catalogo/delete/<tipo>/<int:id_obj>')
@login_required
def delete_item(tipo, id_obj):
    conn = get_db()
    
    if tipo == 'categoria':
        conn.execute('DELETE FROM catalogo_productos WHERE categoria_id = ?', (id_obj,))
        conn.execute('DELETE FROM categorias WHERE id = ?', (id_obj,))
        flash('Categoría eliminada.', 'warning')
        dest = 'catalogo.admin_categorias'
        cat_arg = {}
    else:
        prod = conn.execute(
            'SELECT categoria_id FROM catalogo_productos WHERE id = ?',
            (id_obj,)
        ).fetchone()

        if prod:
            cat_id = prod['categoria_id']
            conn.execute('DELETE FROM catalogo_productos WHERE id = ?', (id_obj,))
            flash('Producto eliminado.', 'success')
            dest = 'catalogo.admin_productos'
            cat_arg = {'cat_id': cat_id}
        else:
            flash('Producto no encontrado', 'error')
            dest = 'catalogo.admin_categorias'
            cat_arg = {}
        
    conn.commit()
    conn.close()
    return redirect(url_for(dest, **cat_arg))

# =========================================================
# 6. VISTA PÚBLICA (CLIENTES)
# =========================================================
@catalogo_bp.route('/catalogo')
def ver_catalogo():
    conn = get_db()
    
    # 1. Categorías activas
    categorias = conn.execute(
        'SELECT * FROM categorias WHERE activo = 1 ORDER BY orden ASC'
    ).fetchall()
    
    catalogo_data = []
    
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
