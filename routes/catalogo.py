import random
import string
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from db import get_db_connection as get_db
from helpers import login_required

# Definimos el "Blueprint" (es como una mini-app dentro de tu app)
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

    # --- AGREGAR PRODUCTO NUEVO ---
    if request.method == 'POST':
        # 1. Generar SKU Automático y Único
       prefix = ''.join(filter(str.isalpha, categoria['nombre']))[:3].upper() 
       if len(prefix) < 2: prefix = "PROD"
        
       while True:
            random_digits = ''.join(random.choices(string.digits, k=5)) # 5 números al azar
            sku_generado = f"{prefix}-{random_digits}"
            
            # Verificar si ya existe
            existe = conn.execute('SELECT id FROM productos WHERE sku = ?', (sku_generado,)).fetchone()
            if not existe:
                break # ¡Es único! Salimos del ciclo

       titulo = request.form['titulo']
       descripcion = request.form['descripcion']
       precio = request.form.get('precio', 0)
        
       # DATOS QUE VIENEN DE CLOUDINARY
       media_url = request.form['media_url']   # El link de la foto/video
       media_type = request.form['media_type'] # 'image', 'video' o 'audio'
        
       conn.execute('''
            INSERT INTO productos (categoria_id, sku, titulo, descripcion, media_url, media_type, precio)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (cat_id, sku, titulo, descripcion, media_url, media_type, precio))
       conn.commit()
       flash('Producto agregado al catálogo.', 'success')
       return redirect(url_for('catalogo.admin_productos', cat_id=cat_id))

    # --- OBTENER DATOS ---
    categoria = conn.execute('SELECT * FROM categorias WHERE id = ?', (cat_id,)).fetchone()
    productos = conn.execute('SELECT * FROM productos WHERE categoria_id = ? ORDER BY id DESC', (cat_id,)).fetchall()
    conn.close()

    return render_template('catalogo/admin_productos.html', categoria=categoria, productos=productos)

# =========================================================
# 3. INTERRUPTOR RÁPIDO (ON/OFF) - API
# =========================================================
@catalogo_bp.route('/api/catalogo/toggle', methods=['POST'])
@login_required
def toggle_status():
    data = request.get_json()
    tipo = data.get('tipo') # 'categoria' o 'producto'
    id_obj = data.get('id')
    nuevo_estado = data.get('activo') # 1 o 0
    
    conn = get_db()
    tabla = 'categorias' if tipo == 'categoria' else 'productos'
    
    try:
        # Consulta dinámica segura
        query = f'UPDATE {tabla} SET activo = ? WHERE id = ?'
        conn.execute(query, (nuevo_estado, id_obj))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
    finally:
        conn.close()


# =========================================================
# EDITAR CATEGORÍA
# =========================================================
@catalogo_bp.route('/admin/catalogo/editar_categoria', methods=['POST'])
@login_required
def editar_categoria():
    conn = get_db()
    cat_id = request.form['cat_id']
    nombre = request.form['nombre']
    orden = request.form.get('orden', 0)
    
    try:
        conn.execute('UPDATE categorias SET nombre = ?, orden = ? WHERE id = ?', 
                     (nombre, orden, cat_id))
        conn.commit()
        flash('Categoría actualizada correctamente.', 'success')
    except Exception as e:
        flash(f'Error al editar: {e}', 'error')
    finally:
        conn.close()
        
    return redirect(url_for('catalogo.admin_categorias'))


# =========================================================
# 4. ELIMINAR (Limpieza)
# =========================================================
@catalogo_bp.route('/admin/catalogo/delete/<tipo>/<int:id_obj>')
@login_required
def delete_item(tipo, id_obj):
    conn = get_db()
    if tipo == 'categoria':
        # Borrar categoría y sus productos asociados
        conn.execute('DELETE FROM productos WHERE categoria_id = ?', (id_obj,))
        conn.execute('DELETE FROM categorias WHERE id = ?', (id_obj,))
        flash('Categoría eliminada.', 'warning')
        dest = 'catalogo.admin_categorias'
    else:
        # Borrar solo producto
        # (Necesitamos saber la categoría para redirigir, así que la buscamos primero)
        prod = conn.execute('SELECT categoria_id FROM productos WHERE id = ?', (id_obj,)).fetchone()
        cat_id = prod['categoria_id']
        conn.execute('DELETE FROM productos WHERE id = ?', (id_obj,))
        flash('Producto eliminado.', 'success')
        conn.commit()
        conn.close()
        return redirect(url_for('catalogo.admin_productos', cat_id=cat_id))
        
    conn.commit()
    conn.close()
    return redirect(url_for(dest))