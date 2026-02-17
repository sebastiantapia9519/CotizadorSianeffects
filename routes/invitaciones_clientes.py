import json
import io
import zipfile
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file
from db import get_db_connection
from routes.invitaciones_publicas import s3_client, BUCKET_NAME

clientes_bp = Blueprint('invitaciones_clientes', __name__)

@clientes_bp.route('/mi-evento', methods=['GET', 'POST'])
def login_cliente():
    if request.method == 'POST':
        codigo = request.form.get('codigo', '').strip().upper()
        conn = get_db_connection()
        try:
            # Buscamos usando la tabla exacta
            inv = conn.execute("""
                SELECT id, slug, datos_cliente_json, camara_premium, tiene_modulo_invitados 
                FROM invitaciones 
                WHERE codigo_acceso_cliente = ?
            """, (codigo,)).fetchone()
            
            if inv:
                session['cliente_inv_id'] = inv['id']
                session['cliente_slug'] = inv['slug']
                datos = json.loads(inv['datos_cliente_json']) if inv['datos_cliente_json'] else {}
                session['cliente_nombre'] = datos.get('novios', 'Nuestro Evento')
                
                return redirect(url_for('invitaciones_clientes.dashboard_cliente'))
            else:
                flash("Código de acceso no válido.", "danger")
        finally:
            conn.close()
            
    return render_template('clientes/login.html')

@clientes_bp.route('/mi-evento/panel')
def dashboard_cliente():
    if 'cliente_inv_id' not in session:
        return redirect(url_for('invitaciones_clientes.login_cliente'))
        
    inv_id = session['cliente_inv_id']
    conn = get_db_connection()
    
    try:
        inv = conn.execute("SELECT * FROM invitaciones WHERE id = ?", (inv_id,)).fetchone()
        
        invitados = []
        if inv['tiene_modulo_invitados']:
             invitados = conn.execute("SELECT * FROM pases_invitados WHERE invitacion_id = ? ORDER BY nombre_familia ASC", (inv_id,)).fetchall()
        
        fotos = []
        if inv['camara_premium']:
             # OJO: usando tu tabla fotos_invitados
             fotos = conn.execute("SELECT url FROM fotos_invitados WHERE invitacion_id = ? ORDER BY fecha_creacion DESC", (inv_id,)).fetchall()

        return render_template('clientes/dashboard.html', 
                               inv=inv, 
                               invitados=invitados, 
                               fotos=fotos,
                               nombre_evento=session.get('cliente_nombre'))
    finally:
        conn.close()

@clientes_bp.route('/mi-evento/descargar-fotos')
def descargar_fotos_cliente():
    if 'cliente_inv_id' not in session:
        return redirect(url_for('invitaciones_clientes.login_cliente'))
        
    inv_id = session['cliente_inv_id']
    conn = get_db_connection()
    try:
        inv = conn.execute("SELECT slug FROM invitaciones WHERE id = ?", (inv_id,)).fetchone()
        fotos = conn.execute("SELECT url FROM fotos_invitados WHERE invitacion_id = ?", (inv_id,)).fetchall()

        if not fotos:
            flash("No hay fotos para descargar.", "warning")
            return redirect(url_for('invitaciones_clientes.dashboard_cliente'))

        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for i, foto in enumerate(fotos):
                try:
                    key = foto['url'].split('.dev/')[-1]
                    obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=key)
                    zf.writestr(f"recuerdo_{i+1}.jpg", obj['Body'].read())
                except:
                    continue

        memory_file.seek(0)
        return send_file(memory_file, mimetype='application/zip', as_attachment=True, download_name=f"fotos_{inv['slug']}.zip")
    finally:
        conn.close()

@clientes_bp.route('/mi-evento/salir')
def logout_cliente():
    session.clear()
    return redirect(url_for('invitaciones_clientes.login_cliente'))