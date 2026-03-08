import base64
import io
import uuid
import os
import json
import html
from flask import Blueprint, render_template, request, jsonify, session
from utils.datetime_utils import ahora_sql
from db import get_db_connection as get_db
import boto3
from botocore.config import Config
from helpers import admin_required # Importamos tu decorador de administración

invitaciones_publicas_bp = Blueprint('invitaciones_publicas', __name__)

# =========================================================
# CONFIGURACIÓN CLOUDFLARE R2
# =========================================================
ACCESS_KEY = os.getenv('R2_ACCESS_KEY')
SECRET_KEY = os.getenv('R2_SECRET_KEY')
ENDPOINT_URL = os.getenv('R2_ENDPOINT_URL')
BUCKET_NAME = os.getenv('R2_BUCKET_NAME')
PUBLIC_URL = os.getenv('R2_PUBLIC_URL')

s3_client = boto3.client(
    service_name='s3',
    endpoint_url=ENDPOINT_URL,
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    region_name='auto',
    config=Config(signature_version='s3v4')
)

# =========================================================
# RUTAS PÚBLICAS PARA LA CÁMARA DESECHABLE
# =========================================================

@invitaciones_publicas_bp.route('/rollo-invitados/<int:invitacion_id>')
def abrir_camara(invitacion_id):
    return render_template('invitaciones/camara.html', inv_id=invitacion_id)

@invitaciones_publicas_bp.route('/api/upload_rollo/<int:invitacion_id>', methods=['POST'])
def upload_rollo(invitacion_id):
    try:
        data = request.get_json()
        imagen_b64 = data.get('imagen')
        if not imagen_b64:
            return jsonify({'success': False, 'error': 'No se recibió ninguna imagen'}), 400

        if ',' in imagen_b64:
            imagen_b64 = imagen_b64.split(',')[1]

        img_bytes = base64.b64decode(imagen_b64)
        archivo_memoria = io.BytesIO(img_bytes)
        nombre_archivo = f"bodas/boda_{invitacion_id}/rollo_invitados/foto_{uuid.uuid4().hex[:8]}.jpg"
        
        s3_client.upload_fileobj(
            archivo_memoria, 
            BUCKET_NAME, 
            nombre_archivo,
            ExtraArgs={'ContentType': 'image/jpeg'}
        )
        
        url_final = f"{PUBLIC_URL}/{nombre_archivo}"
        conn = get_db()
        conn.execute(
            """
            INSERT INTO fotos_invitados (invitacion_id, url, fecha_creacion)
            VALUES (?, ?, ?)
            """,
            (invitacion_id, url_final, ahora_sql())
        )
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'mensaje': '¡Foto revelada!', 'url': url_final})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# =========================================================
# SISTEMA DE CONFIRMACIÓN (RSVP)
# =========================================================

@invitaciones_publicas_bp.route('/api/invitados/<int:invitado_id>/confirmar', methods=['POST'])
def api_confirmar_asistencia(invitado_id):
    try:
        data = request.get_json()
        nuevo_estado = data.get('estado') 
        if nuevo_estado not in ['Confirmado', 'Declinado']:
            return jsonify({'success': False, 'error': 'Estado no válido'}), 400

        conn = get_db()
        conn.execute(
            "UPDATE pases_invitados SET estado_asistencia = ? WHERE id = ?",
            (nuevo_estado, invitado_id)
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'mensaje': 'Confirmación guardada'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# =========================================================
# SISTEMA DE RECEPCIÓN (ESCÁNER)
# =========================================================

# --- RUTA PARA EL CLIENTE (Dashboard de Novios) ---
@invitaciones_publicas_bp.route('/recepcion/<slug>')
def recepcion_boda(slug): # <--- Cambiamos el nombre aquí para que coincida con el dashboard
    # Validamos la sesión manual del cliente
    if 'cliente_inv_id' not in session or session.get('cliente_slug') != slug:
        return "<h1>Acceso Denegado</h1><p>Debes ingresar con tu código de evento para usar el escáner.</p>", 403
        
    conn = get_db()
    inv = conn.execute("SELECT id, slug FROM invitaciones WHERE slug = ?", (slug,)).fetchone()
    conn.close()
    
    if not inv:
        return "Boda no encontrada", 404
    
    # IMPORTANTE: Pasamos el objeto 'inv' para que scanner.html sepa que es Modo Cliente
    return render_template('invitaciones/scanner.html', inv=inv)

# --- RUTA PARA TI (Administrador Maestro de SianEffects) ---
@invitaciones_publicas_bp.route('/admin/scanner-global')
@admin_required 
def scanner_global(): # <--- Este nombre debe coincidir con url_for('invitaciones_publicas.scanner_global')
    return render_template('invitaciones/scanner.html', inv=None)

# --- API DE VALIDACIÓN QR ---
@invitaciones_publicas_bp.route('/api/validar-qr', methods=['POST'])
def validar_qr():
    data = request.get_json()
    codigo = data.get('codigo')
    invitacion_id = data.get('invitacion_id') # Viene null desde el Scanner Maestro

    conn = get_db()
    
    if invitacion_id:
        # MODO CLIENTE: Solo valida pases de SU propia boda
        invitado = conn.execute("""
            SELECT p.*, i.slug as boda_nombre 
            FROM pases_invitados p
            JOIN invitaciones i ON p.invitacion_id = i.id
            WHERE p.codigo_qr_unique = ? AND p.invitacion_id = ?
        """, (codigo, invitacion_id)).fetchone()
    else:
        # MODO ADMIN: Valida cualquier código de cualquier boda en el sistema
        invitado = conn.execute("""
            SELECT p.*, i.slug as boda_nombre 
            FROM pases_invitados p
            JOIN invitaciones i ON p.invitacion_id = i.id
            WHERE p.codigo_qr_unique = ?
        """, (codigo,)).fetchone()

    if not invitado:
        conn.close()
        return jsonify({'success': False, 'error': 'Código QR no válido para este evento'})

    # Verificar si ya marcaron entrada
    if invitado['pases_usados'] >= invitado['pases_totales']:
        conn.close()
        return jsonify({
            'success': False, 
            'error': f"¡ALERTA! {invitado['nombre_familia']} ya ingresó. Evento: {invitado['boda_nombre']}"
        })

    # Marcar entrada (pases_usados = pases_totales)
    conn.execute("UPDATE pases_invitados SET pases_usados = pases_totales WHERE id = ?", (invitado['id'],))
    conn.commit()
    conn.close()

    return jsonify({
        'success': True,
        'familia': invitado['nombre_familia'],
        'pases': invitado['pases_totales'],
        'mesa': invitado['mesa'] if invitado['mesa'] else '0',
        'evento': invitado['boda_nombre']
    })

# =========================================================
# BUENOS DESEOS (GUESTBOOK)
# =========================================================

@invitaciones_publicas_bp.route('/api/buenos-deseos', methods=['POST'])
def guardar_buen_deseo():
    try:
        data = request.get_json()
        invitacion_id = data.get('invitacion_id')
        nombre = data.get('nombre')
        mensaje = data.get('mensaje')

        # 1. Validación estricta: que no vengan vacíos
        if not invitacion_id or not nombre or not mensaje:
            return jsonify({'success': False, 'error': 'Faltan datos. El nombre y mensaje son obligatorios.'}), 400

        # 2. Seguridad: Limpiamos los inputs para evitar inyección de código (XSS)
        nombre_limpio = html.escape(nombre.strip())
        mensaje_limpio = html.escape(mensaje.strip())

        # 3. Guardar en Base de Datos
        conn = get_db()
        conn.execute(
            """
            INSERT INTO buenos_deseos (invitacion_id, nombre, mensaje, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (invitacion_id, nombre_limpio, mensaje_limpio, ahora_sql())
        )
        conn.commit()
        conn.close()

        return jsonify({'success': True, 'mensaje': '¡Gracias por tus buenos deseos!'})
        
    except Exception as e:
        return jsonify({'success': False, 'error': f"Error interno: {str(e)}"}), 500