import base64
import io
import uuid
import os
import json
import html
from flask import Blueprint, render_template, request, jsonify, session, current_app
from utils.datetime_utils import ahora_sql
from db import get_db_connection as get_db
import boto3
from botocore.config import Config
from helpers import admin_required 

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
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO fotos_invitados (invitacion_id, url, fecha_creacion)
            VALUES (%s, %s, %s)
            """,
            (invitacion_id, url_final, ahora_sql())
        )
        conn.commit()
        cursor.close()
        conn.close()
        
        current_app.logger.info(f"GUEST_CAM_UPLOAD: Foto subida con éxito para la invitación ID {invitacion_id}.")
        return jsonify({'success': True, 'mensaje': '¡Foto revelada!', 'url': url_final})
    except Exception as e:
        current_app.logger.error(f"GUEST_CAM_ERROR: Fallo al procesar/subir foto de invitado para evento {invitacion_id} - {e}")
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
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE pases_invitados SET estado_asistencia = %s WHERE id = %s",
            (nuevo_estado, invitado_id)
        )
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'mensaje': 'Confirmación guardada'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# =========================================================
# SISTEMA DE RECEPCIÓN (ESCÁNER)
# =========================================================

# --- RUTA PARA EL CLIENTE (Dashboard de Novios) ---
@invitaciones_publicas_bp.route('/recepcion/<slug>')
def recepcion_boda(slug): 
    # Validamos la sesión manual del cliente
    if 'cliente_inv_id' not in session or session.get('cliente_slug') != slug:
        return "<h1>Acceso Denegado</h1><p>Debes ingresar con tu código de evento para usar el escáner.</p>", 403
        
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, slug FROM invitaciones WHERE slug = %s", (slug,))
    inv = cursor.fetchone()
    cursor.close()
    conn.close()
    
    if not inv:
        return "Boda no encontrada", 404
    
    # IMPORTANTE: Pasamos el objeto 'inv' para que scanner.html sepa que es Modo Cliente
    return render_template('invitaciones/scanner.html', inv=inv)

# --- RUTA PARA TI (Administrador Maestro de Sianeffects) ---
@invitaciones_publicas_bp.route('/admin/scanner-global')
@admin_required 
def scanner_global(): 
    return render_template('invitaciones/scanner.html', inv=None)

# --- API DE VALIDACIÓN QR ---
@invitaciones_publicas_bp.route('/api/validar-qr', methods=['POST'])
def validar_qr():
    import json
    data = request.get_json()
    codigo = data.get('codigo')
    invitacion_id = data.get('invitacion_id') 
    pases_a_ingresar = data.get('pases_a_ingresar') 

    conn = get_db()
    cursor = conn.cursor()
    
    # 1. Buscamos al invitado según el modo
    if invitacion_id:
        cursor.execute("""
            SELECT p.*, i.slug as boda_nombre 
            FROM pases_invitados p
            JOIN invitaciones i ON p.invitacion_id = i.id
            WHERE p.codigo_qr_unique = %s AND p.invitacion_id = %s
        """, (codigo, invitacion_id))
        invitado = cursor.fetchone()
    else:
        cursor.execute("""
            SELECT p.*, i.slug as boda_nombre 
            FROM pases_invitados p
            JOIN invitaciones i ON p.invitacion_id = i.id
            WHERE p.codigo_qr_unique = %s
        """, (codigo,))
        invitado = cursor.fetchone()

    if not invitado:
        cursor.close()
        conn.close()
        current_app.logger.warning(f"QR_SCAN_DENIED: Intento de acceso con QR inválido o ajeno al evento: '{codigo}'")
        return jsonify({'success': False, 'error': 'Código QR no válido para este evento'})

    # 2. Calculamos los pases disponibles reales
    pases_totales = invitado['pases_totales']
    pases_usados = invitado['pases_usados']
    pases_disponibles = pases_totales - pases_usados

    # Si ya entraron todos, bloqueamos
    if pases_disponibles <= 0:
        cursor.close()
        conn.close()
        current_app.logger.warning(f"QR_SCAN_EMPTY: La familia {invitado['nombre_familia']} intentó ingresar sin pases disponibles.")
        return jsonify({
            'success': False, 
            'error': f"¡ALERTA! La familia {invitado['nombre_familia']} ya ingresó todos sus pases ({pases_totales}/{pases_totales}). Evento: {invitado['boda_nombre']}"
        })

    # Extraer nombres de acompañantes de forma segura
    nombres_lista = []
    if invitado['nombres_acompanantes_json']:
        try:
            nombres_lista = json.loads(invitado['nombres_acompanantes_json'])
        except:
            nombres_lista = []

    # ---------------------------------------------------------
    # MODO A: Solo Consulta (Cuando escanean el QR por primera vez)
    # ---------------------------------------------------------
    if not pases_a_ingresar:
        cursor.close()
        conn.close()
        return jsonify({
            'success': True,
            'requiere_confirmacion': True, 
            'familia': invitado['nombre_familia'],
            'pases_totales': pases_totales,
            'pases_usados': pases_usados,
            'pases_disponibles': pases_disponibles,
            'mesa': invitado['mesa'] if invitado['mesa'] else '0',
            'evento': invitado['boda_nombre'],
            'nombres_acompanantes': nombres_lista
        })

    # ---------------------------------------------------------
    # MODO B: Confirmación (Cuando la hostess dice "entran 3")
    # ---------------------------------------------------------
    pases_a_ingresar = int(pases_a_ingresar)
    
    # Validamos que no intenten meter a más gente de la que tienen disponible
    if pases_a_ingresar > pases_disponibles:
        cursor.close()
        conn.close()
        return jsonify({'success': False, 'error': f'Solo le quedan {pases_disponibles} pases disponibles.'})

    # Sumamos los nuevos ingresos a los que ya estaban adentro
    nuevo_usados = pases_usados + pases_a_ingresar
    
    cursor.execute("UPDATE pases_invitados SET pases_usados = %s WHERE id = %s", (nuevo_usados, invitado['id']))
    conn.commit()
    cursor.close()
    conn.close()

    current_app.logger.info(f"QR_SCAN_SUCCESS: Ingresaron {pases_a_ingresar} personas de la familia {invitado['nombre_familia']} al evento {invitado['boda_nombre']}.")

    return jsonify({
        'success': True,
        'requiere_confirmacion': False,
        'mensaje': f'Se registraron {pases_a_ingresar} accesos. Quedan {pases_totales - nuevo_usados} pases libres.'
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
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO buenos_deseos (invitacion_id, nombre, mensaje)
            VALUES (%s, %s, %s)
            """,
            (invitacion_id, nombre_limpio, mensaje_limpio)
        )
        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({'success': True, 'mensaje': '¡Gracias por tus buenos deseos!'})
        
    except Exception as e:
        return jsonify({'success': False, 'error': f"Error interno: {str(e)}"}), 500