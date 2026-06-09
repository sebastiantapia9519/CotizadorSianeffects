import os

import requests
from flask import Blueprint, jsonify, render_template, request
from google import genai
from google.genai import types

from db import get_db_connection
from helpers import admin_required


whatsapp_bot_bp = Blueprint('whatsapp', __name__)

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
WHATSAPP_TOKEN = os.environ.get('WHATSAPP_TOKEN')
WHATSAPP_PHONE_ID = os.environ.get('WHATSAPP_PHONE_ID')
VERIFY_TOKEN = os.environ.get('VERIFY_TOKEN', 'sianeffects_bot_seguro')
GEMINI_MODEL = 'models/gemini-2.5-flash-lite'

SYSTEM_PROMPT_WHATSAPP = """
Eres el asistente oficial de Sianeffects en WhatsApp.
Tu objetivo es ayudar a emprendedores (crafters, sublimación, vinil, papelería creativa) a entender cómo el cotizador
de Sianeffects les ayuda a no perder dinero y cobrar correctamente.

TONO:
- Súper casual, relajado, empático y humano.
- Usa emojis para que se sienta como una plática real por WhatsApp.
- Eres el puente amigable entre su negocio y la herramienta.

REGLAS:
- Respuestas MUY cortas y al grano (es WhatsApp, nadie lee textos largos). Máximo 2-3 párrafos cortos.
- Cero jerga técnica. No hables de "SaaS", "APIs" o "Dashboards", diles "la plataforma" o "el cotizador".
- Si te preguntan precios o cómo registrarse, diles amablemente que pueden hacerlo directo en sianeffects.com.
- No inventes funciones. Tu meta es que entiendan el valor de usar la herramienta para sacar sus costos.
"""


def normalize_whatsapp_number(number):
    digits = ''.join(ch for ch in str(number or '') if ch.isdigit())
    if digits.startswith('521') and len(digits) == 13:
        return '52' + digits[3:]
    return digits


def extract_text_message(body):
    for entry in body.get('entry', []):
        for change in entry.get('changes', []):
            value = change.get('value') or {}
            messages = value.get('messages') or []
            if not messages:
                continue

            message = messages[0]
            if message.get('type') != 'text' or 'text' not in message:
                return None

            text = (message.get('text') or {}).get('body', '').strip()
            message_id = message.get('id')
            sender = normalize_whatsapp_number(message.get('from'))
            if not text or not message_id or not sender:
                return None

            contact_name = None
            contacts = value.get('contacts') or []
            if contacts:
                contact_name = (contacts[0].get('profile') or {}).get('name')

            return {
                'message_id': message_id,
                'sender': sender,
                'text': text,
                'contact_name': contact_name,
            }

    return None


def send_whatsapp_message(to_number, text_body):
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        raise RuntimeError('Faltan WHATSAPP_TOKEN o WHATSAPP_PHONE_ID en variables de entorno.')


    url = f'https://graph.facebook.com/v18.0/{WHATSAPP_PHONE_ID}/messages'
    headers = {
        'Authorization': f'Bearer {WHATSAPP_TOKEN}',
        'Content-Type': 'application/json',
    }
    data = {
        'messaging_product': 'whatsapp',
        'to': to_number,
        'type': 'text',
        'text': {'body': text_body},
    }

    print('================ WHATSAPP DEBUG ================')
    print('WHATSAPP_PHONE_ID usado:', WHATSAPP_PHONE_ID)
    print('URL usada:', url)
    print('Número destino:', to_number)
    print('Token existe:', bool(WHATSAPP_TOKEN))
    print('================================================')

    response = requests.post(url, headers=headers, json=data, timeout=15)
    if response.status_code >= 400:
        print('Error al enviar mensaje a WhatsApp:', response.status_code, response.text)
    response.raise_for_status()
    return response


def get_or_create_conversation(cursor, number, contact_name=None):
    cursor.execute("""
        SELECT id, numero_cliente, nombre_cliente, bot_activo, ultima_actividad
        FROM conversaciones
        WHERE numero_cliente = %s
        LIMIT 1
    """, (number,))
    conversation = cursor.fetchone()

    if conversation:
        cursor.execute("""
            UPDATE conversaciones
            SET ultima_actividad = CURRENT_TIMESTAMP,
                nombre_cliente = COALESCE(NULLIF(nombre_cliente, ''), %s)
            WHERE id = %s
        """, (contact_name, conversation['id']))
        return conversation

    cursor.execute("""
        INSERT INTO conversaciones (numero_cliente, nombre_cliente, bot_activo, ultima_actividad)
        VALUES (%s, %s, TRUE, CURRENT_TIMESTAMP)
        RETURNING id, numero_cliente, nombre_cliente, bot_activo, ultima_actividad
    """, (number, contact_name))
    return cursor.fetchone()


def message_already_processed(cursor, whatsapp_message_id):
    cursor.execute("""
        SELECT id
        FROM mensajes_whatsapp
        WHERE whatsapp_message_id = %s
        LIMIT 1
    """, (whatsapp_message_id,))
    return cursor.fetchone() is not None


def insert_whatsapp_message(cursor, conversation_id, sender, text, whatsapp_message_id=None):
    cursor.execute("""
        INSERT INTO mensajes_whatsapp
            (conversacion_id, remitente, texto, whatsapp_message_id, fecha_envio)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
        RETURNING id
    """, (conversation_id, sender, text, whatsapp_message_id))
    return cursor.fetchone()


def load_recent_history(cursor, conversation_id, limit=6):
    cursor.execute("""
        SELECT remitente, texto
        FROM mensajes_whatsapp
        WHERE conversacion_id = %s
        ORDER BY fecha_envio DESC, id DESC
        LIMIT %s
    """, (conversation_id, limit))
    return list(reversed(cursor.fetchall()))


def build_gemini_contents(history):
    contents = []
    for message in history:
        text = (message['texto'] or '').strip()
        if not text:
            continue

        if message['remitente'] == 'bot':
            role = 'model'
            label = 'Asistente'
        else:
            role = 'user'
            label = 'Usuario' if message['remitente'] == 'cliente' else 'Agente'

        contents.append(types.Content(
            role=role,
            parts=[types.Part.from_text(text=f'{label}: {text}')],
        ))
    return contents


def generate_ai_reply(history):
    if not GEMINI_API_KEY:
        raise RuntimeError('Falta GEMINI_API_KEY en variables de entorno.')

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=build_gemini_contents(history),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT_WHATSAPP,
            temperature=0.6,
            max_output_tokens=250,
        ),
    )
    return (response.text or '').strip() or 'Gracias por escribirnos. ¿Me cuentas un poquito más para ayudarte mejor?'


@whatsapp_bot_bp.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')

        if mode == 'subscribe' and token == VERIFY_TOKEN:
            print('Webhook de WhatsApp verificado exitosamente.')
            return challenge or '', 200
        return 'Error de verificación', 403

    body = request.get_json(silent=True) or {}
    incoming = extract_text_message(body)
    if incoming is None:
        return jsonify({'status': 'ignored'}), 200

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if message_already_processed(cursor, incoming['message_id']):
            return jsonify({'status': 'duplicate'}), 200

        conversation = get_or_create_conversation(
            cursor,
            incoming['sender'],
            incoming.get('contact_name'),
        )

        insert_whatsapp_message(
            cursor,
            conversation['id'],
            'cliente',
            incoming['text'],
            incoming['message_id'],
        )
        conn.commit()

        if not bool(conversation['bot_activo']):
            return jsonify({'status': 'paused_for_human'}), 200

        history = load_recent_history(cursor, conversation['id'], limit=6)
        reply = generate_ai_reply(history)

        insert_whatsapp_message(cursor, conversation['id'], 'bot', reply)
        conn.commit()

        try:
            send_whatsapp_message(incoming['sender'], reply)
        except Exception as exc:
            print('Error enviando respuesta de WhatsApp:', exc)

        return jsonify({'status': 'ok'}), 200
    except Exception as exc:
        conn.rollback()
        print(f'Error procesando el webhook de WhatsApp: {exc}')
        return jsonify({'status': 'error'}), 200
    finally:
        cursor.close()
        conn.close()


# ==============================================================================
# RUTAS PARA EL PANEL DE ADMINISTRACIÓN (LA BANDEJA DE ENTRADA)
# ==============================================================================

@whatsapp_bot_bp.route('/inbox', methods=['GET'])
@admin_required
def inbox():
    return render_template('whatsapp_inbox.html')


@whatsapp_bot_bp.route('/api/chats', methods=['GET'])
@admin_required
def get_chats():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT
                c.id,
                c.numero_cliente AS numero,
                COALESCE(NULLIF(c.nombre_cliente, ''), c.numero_cliente) AS nombre,
                COALESCE(c.bot_activo, TRUE) AS bot_activo,
                c.ultima_actividad,
                (
                    SELECT mw.texto
                    FROM mensajes_whatsapp mw
                    WHERE mw.conversacion_id = c.id
                    ORDER BY mw.fecha_envio DESC, mw.id DESC
                    LIMIT 1
                ) AS ultimo_mensaje
            FROM conversaciones c
            ORDER BY c.ultima_actividad DESC NULLS LAST, c.id DESC
        """)
        chats = []
        for row in cursor.fetchall():
            chats.append({
                'id': row['id'],
                'numero': row['numero'],
                'nombre': row['nombre'],
                'bot_activo': bool(row['bot_activo']),
                'ultima_act': format_datetime_short(row['ultima_actividad']),
                'ultimo_mensaje': row['ultimo_mensaje'] or '',
            })
        return jsonify(chats)
    finally:
        cursor.close()
        conn.close()


@whatsapp_bot_bp.route('/api/chats/<int:chat_id>/messages', methods=['GET'])
@admin_required
def get_messages(chat_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT remitente, texto, fecha_envio
            FROM mensajes_whatsapp
            WHERE conversacion_id = %s
            ORDER BY fecha_envio ASC, id ASC
        """, (chat_id,))
        messages = []
        for row in cursor.fetchall():
            messages.append({
                'remitente': row['remitente'],
                'texto': row['texto'] or '',
                'fecha_texto': format_date_label(row['fecha_envio']),
                'hace_tiempo': format_time_label(row['fecha_envio']),
            })
        return jsonify(messages)
    finally:
        cursor.close()
        conn.close()


@whatsapp_bot_bp.route('/api/chats/<int:chat_id>/toggle_bot', methods=['POST'])
@admin_required
def toggle_bot(chat_id):
    data = request.get_json(silent=True) or {}
    nuevo_estado = bool(data.get('bot_activo'))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE conversaciones
            SET bot_activo = %s,
                ultima_actividad = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING id, bot_activo
        """, (nuevo_estado, chat_id))
        updated = cursor.fetchone()
        if not updated:
            conn.rollback()
            return jsonify({'status': 'error', 'message': 'Conversación no encontrada'}), 404
        conn.commit()
        return jsonify({'status': 'success', 'bot_activo': bool(updated['bot_activo'])})
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


@whatsapp_bot_bp.route('/api/chats/<int:chat_id>/send_manual', methods=['POST'])
@admin_required
def send_manual_message(chat_id):
    data = request.get_json(silent=True) or {}
    texto = (data.get('texto') or '').strip()

    if not texto:
        return jsonify({'status': 'error', 'message': 'El mensaje está vacío'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT numero_cliente
            FROM conversaciones
            WHERE id = %s
            LIMIT 1
        """, (chat_id,))
        chat = cursor.fetchone()
        if not chat:
            return jsonify({'status': 'error', 'message': 'Conversación no encontrada'}), 404

        send_whatsapp_message(chat['numero_cliente'], texto)

        insert_whatsapp_message(cursor, chat_id, 'agente', texto)
        cursor.execute("""
            UPDATE conversaciones
            SET bot_activo = FALSE,
                ultima_actividad = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (chat_id,))
        conn.commit()
        return jsonify({'status': 'success', 'bot_activo': False})
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def format_datetime_short(value):
    if not value:
        return ''
    return value.strftime('%d/%m %H:%M')


def format_date_label(value):
    if not value:
        return ''
    return value.strftime('%d/%m/%Y')


def format_time_label(value):
    if not value:
        return ''
    return value.strftime('%H:%M')
