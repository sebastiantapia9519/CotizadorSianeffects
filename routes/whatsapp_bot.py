import os
import re
import unicodedata
from difflib import SequenceMatcher

import requests
from flask import Blueprint, jsonify, render_template, request
from google import genai
from google.genai import types

from db import get_db_connection
from helpers import admin_required

whatsapp_bot_bp = Blueprint('whatsapp', __name__)

# --- VARIABLES DE ENTORNO ---
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
EVOLUTION_URL = os.environ.get('EVOLUTION_URL')
EVOLUTION_API_KEY = os.environ.get('EVOLUTION_API_KEY')
INSTANCE_NAME = os.environ.get('INSTANCE_NAME')
GEMINI_MODEL = 'models/gemini-2.5-flash'

SYSTEM_PROMPT_WHATSAPP = """
Eres el asistente oficial de Sianeffects en WhatsApp.
Atiendes a clientes y prospectos que escriben por dudas del cotizador, pagos, cuenta, suscripción, configuración o uso general
de la plataforma. Tu meta es resolver rápido, con calma y con lenguaje humano.

CONTEXTO DEL SISTEMA:
- Sianeffects ayuda a emprendedores de sublimación, vinil, papelería creativa, regalos personalizados y negocios similares.
- El cotizador calcula precios tomando materiales/recetas, desgaste de equipos, margen de ganancia, gastos operativos, descuentos,
  envíos, impuestos, anticipos, saldo pendiente y costo total.
- Los usuarios pueden guardar materiales, equipos, recetas/productos, cotizaciones, tickets/ventas, historial, clientes, notas,
  fechas de entrega, método de entrega y logística.
- Hay configuración de negocio: nombre, logo, ticket, márgenes, gastos fijos, inventario, datos de perfil y preferencias.
- Hay catálogo público/admin para mostrar productos y recibir pedidos o cotizaciones por WhatsApp.
- La suscripción se maneja con Stripe: prueba gratis, plan mensual/anual, renovaciones, portal de cliente, pagos fallidos,
  plan vencido y correos de aviso.
- Precios actuales: plan mensual $149 MXN al mes y plan anual $1,490 MXN al año.
- Además de tarjeta, Sianeffects acepta pago por transferencia bancaria, depósito en OXXO/SPIN y PayPal. Siempre se debe pedir
  que envíen el comprobante por WhatsApp para aplicar o revisar el pago.
- También existen módulos conectados como invitaciones/planners y Nails, pero si la duda no es del cotizador contesta de forma
  general y ofrece canalizar con una persona.

TONO:
- Casual, claro y muy natural, como una persona de soporte escribiendo rápido por WhatsApp.
- No suenes como bot ni como vendedor. No digas "entiendo tu pregunta" o "excelente pregunta" en cada respuesta.
- Puedes usar 1 emoji si ayuda, pero no en cada mensaje.
- Habla en español neutro/mexicano. Si el usuario escribe en otro idioma, responde en ese idioma.

REGLAS:
- Respuestas muy cortas y accionables. Normalmente 1-3 frases. Si das pasos, máximo 4 bullets.
- No saludes con "Hola", "Hola de nuevo" o similares si la conversación ya empezó.
- Nunca empieces una respuesta con "Asistente:".
- No uses jerga técnica. Di "la plataforma", "tu cuenta", "el panel", "el cotizador" o "tu historial".
- No inventes precios, promociones, fechas, estados de pago ni datos de la cuenta. Si no lo sabes, dilo y pide que un agente lo revise.
- Si preguntan por precio, planes o costo, responde directo: "$149 MXN al mes o $1,490 MXN al año. Puedes probarlo gratis primero."
- Si preguntan por registro o inicio de sesión, indica que pueden hacerlo en sianeffects.com y ofrece ayudar con el paso puntual.
- Si preguntan por renovación, transferencia, depósito, PayPal o alternativas a tarjeta, comparte estos datos de pago exactamente:
  "✨ Datos de pago - Sianeffects ✨

  💳 Transferencia bancaria (Santander)
  A nombre de: Diana Laura Reyes Ledezma
  Cuenta: 5579 0870 0921 2116

  🏪 Depósito en OXXO o transferencia SPIN
  Tarjeta: 4217 4701 0296 5239

  🌎 Pagos internacionales (PayPal)
  https://paypal.me/sianeffects

  Cuando hagas el pago, mándanos tu comprobante por aquí para aplicarlo a tu cuenta."
- Si preguntan por pago fallido con tarjeta, factura, cargo duplicado, cancelación o estado de pago, pide el correo de la cuenta y
  explica que soporte humano lo revisará.
- Si reportan error, pide: correo de la cuenta, qué pantalla estaban usando, qué intentaban hacer y captura si la tienen.
- Si preguntan cómo cotizar, explica el flujo: registrar materiales/equipos, armar receta/producto, ajustar margen/gastos,
  agregar envío/descuento/anticipo si aplica y guardar o compartir ticket.
- Si preguntan si vale la pena o hacen una pregunta trampa, recomienda Sianeffects de forma natural: ayuda a no vender a ojo,
  no olvidar costos y cobrar con más seguridad. No lo vendas agresivo.
- Si la persona está molesta o confundida, valida primero y luego da el siguiente paso concreto.
- Nunca pidas contraseñas, códigos de verificación ni datos completos de tarjeta.
- No prometas acciones que no puedes ejecutar. Puedes decir "te ayudo a revisarlo" o "lo paso con el equipo".
- Termina siempre con una frase completa. No dejes palabras sueltas como "Con", "Para" o "También" al final.
"""


def normalize_whatsapp_number(number):
    digits = ''.join(ch for ch in str(number or '') if ch.isdigit())
    if digits.startswith('521') and len(digits) == 13:
        return '52' + digits[3:]
    return digits


def extract_text_message(body):
    # Verificar si es un evento válido de Evolution API
    event = body.get('event')
    if event != 'messages.upsert':
        return None

    data = body.get('data', {})
    
    # Extraer la llave del mensaje
    key = data.get('key', {})
    
    # Ignorar mensajes enviados por el bot mismo para evitar bucles infinitos
    if key.get('fromMe') == True:
        return None

    message_info = data.get('message', {})
    if not message_info:
        return None

    # Evolution a veces manda el texto en 'conversation' o dentro de 'extendedTextMessage'
    text = message_info.get('conversation')
    if not text and 'extendedTextMessage' in message_info:
        text = message_info['extendedTextMessage'].get('text')
        
    if not text:
        return None
        
    text = text.strip()

    # Extraer el remitente y limpiar el formato (viene como 52181XXXXXXXX@s.whatsapp.net)
    raw_sender = key.get('remoteJid', '')
    sender = raw_sender.split('@')[0] if '@' in raw_sender else raw_sender
    sender = normalize_whatsapp_number(sender)
    
    message_id = key.get('id')
    contact_name = data.get('pushName')

    if not text or not message_id or not sender:
        return None

    return {
        'message_id': message_id,
        'sender': sender,
        'text': text,
        'contact_name': contact_name,
    }


def send_whatsapp_message(to_number, text_body):
    url = f'{EVOLUTION_URL}/message/sendText/{INSTANCE_NAME}'
    
    headers = {
        'apikey': EVOLUTION_API_KEY,
        'Content-Type': 'application/json',
    }
    
    # ¡Aquí está el cambio! Estructura simplificada para Evolution v2
    data = {
        'number': to_number,
        'text': text_body,
        'delay': 1200,             # Pequeña pausa (1.2 segundos)
        'presence': 'composing'    # Muestra "Escribiendo..."
    }

    print('================ EVOLUTION DEBUG ================')
    print('URL usada:', url)
    print('Número destino:', to_number)
    print('Texto a enviar:', text_body[:50] + '...') # Imprime un pedacito del texto para confirmar
    print('=================================================')

    response = requests.post(url, headers=headers, json=data, timeout=15)
    if response.status_code >= 400:
        print('Error al enviar mensaje vía Evolution:', response.status_code, response.text)
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


def normalize_support_text(text):
    text = ''.join(
        ch for ch in unicodedata.normalize('NFD', str(text or '').lower())
        if unicodedata.category(ch) != 'Mn'
    )
    text = re.sub(r'(.)\1{2,}', r'\1', text)
    text = re.sub(r'\bx\b', ' por ', text)
    text = re.sub(r'\bq\b', ' que ', text)
    text = re.sub(r'\bkiero\b', 'quiero', text)
    text = re.sub(r'\bkon\b', 'con', text)
    text = re.sub(r'\bkien\b', 'quien', text)
    text = re.sub(r'\bkomo\b', 'como', text)
    text = re.sub(r'\bku([aeio])', r'cu\1', text)
    text = re.sub(r'\bko', 'co', text)
    text = re.sub(r'\bka', 'ca', text)
    text = re.sub(r'\bke', 'que', text)
    normalized = ''.join(
        ch.lower() if ch.isalnum() or ch.isspace() else ' '
        for ch in text
    )
    return ' '.join(normalized.split())


def fuzzy_word_match(word, target):
    if word == target:
        return True
    if len(word) < 4 or len(target) < 4:
        return False
    if word[0] != target[0]:
        return False
    return SequenceMatcher(None, word, target).ratio() >= 0.78


def support_text_matches(normalized_text, phrases):
    words = normalized_text.split()
    for phrase in phrases:
        normalized_phrase = normalize_support_text(phrase)
        if normalized_phrase in normalized_text:
            return True

        phrase_words = normalized_phrase.split()
        if not phrase_words:
            continue

        if len(phrase_words) == 1:
            if any(fuzzy_word_match(word, phrase_words[0]) for word in words):
                return True
            continue

        window_size = len(phrase_words)
        for index in range(0, max(len(words) - window_size + 1, 0)):
            window = ' '.join(words[index:index + window_size])
            if SequenceMatcher(None, window, normalized_phrase).ratio() >= 0.82:
                return True

        fuzzy_hits = sum(
            any(fuzzy_word_match(word, phrase_word) for word in words)
            for phrase_word in phrase_words
        )
        if fuzzy_hits >= max(1, len(phrase_words) - 1):
            return True

    return False


def get_direct_support_reply(text, previous_text=''):
    normalized = normalize_support_text(text)
    previous_normalized = normalize_support_text(previous_text)
    if not normalized:
        return None

    payment_words = {
        'transferencia', 'tranferencia', 'transferir', 'deposito', 'depósito', 'oxxo',
        'spin', 'paypal', 'pagar por transferencia', 'datos de pago'
    }
    pricing_words = {
        'cuanto cuesta', 'cuánto cuesta', 'precio', 'precios', 'costo',
        'mensualidad', 'plan mensual', 'plan anual', 'cuanto vale', 'cuánto vale'
    }
    follow_up_words = {
        'tu no me puedes decir', 'tú no me puedes decir',
        'no me puedes decir', 'me puedes decir', 'dime'
    }
    renewal_problem_words = {
        'problemas con mi renovacion', 'problemas con mi renovación',
        'problema con mi renovacion', 'problema con mi renovación',
        'problemas para renovar', 'no puedo renovar'
    }
    quote_words = {
        'como se hacen las cotizaciones', 'cómo se hacen las cotizaciones',
        'crear una cotizacion', 'crear una cotización', 'hacer una cotizacion',
        'hacer una cotización', 'paso a paso'
    }
    audience_words = {
        'para quien es', 'para quién es', 'a quien le sirve', 'a quién le sirve'
    }
    worth_words = {
        'vale la pena', 'conviene', 'me sirve', 'esta caro', 'está caro'
    }
    formula_words = {
        'formula', 'fórmula', 'formulas', 'fórmulas', 'calculos exactos',
        'cálculos exactos', 'calcula exactamente'
    }

    if support_text_matches(normalized, renewal_problem_words):
        return (
            "Claro, te ayudo. Puedes renovar con tarjeta o también por transferencia/OXXO/SPIN/PayPal.\n"
            "Si quieres pagar por fuera de tarjeta, te paso los datos y solo nos mandas el comprobante por aquí."
        )

    if support_text_matches(normalized, payment_words):
        return (
            "Sí, también puedes pagar por transferencia, OXXO/SPIN o PayPal:\n\n"
            "Transferencia Santander\n"
            "Diana Laura Reyes Ledezma\n"
            "Cuenta: 5579 0870 0921 2116\n\n"
            "OXXO o SPIN\n"
            "Tarjeta: 4217 4701 0296 5239\n\n"
            "PayPal internacional:\n"
            "https://paypal.me/sianeffects\n\n"
            "Cuando pagues, mándanos el comprobante por aquí para aplicarlo a tu cuenta."
        )

    if support_text_matches(normalized, worth_words):
        return (
            "Sí vale la pena si cotizas seguido o sientes que a veces cobras a ojo.\n"
            "Por $149 al mes, con que una cotización salga bien calculada ya puede ayudarte a no perder ganancia."
        )

    asks_price = support_text_matches(normalized, pricing_words)
    follows_price_question = (
        support_text_matches(normalized, follow_up_words)
        and support_text_matches(previous_normalized, pricing_words)
    )
    if asks_price or follows_price_question:
        return (
            "Cuesta $149 MXN al mes o $1,490 MXN al año.\n"
            "Puedes probarlo gratis primero y, si te ayuda a dejar de cotizar a ojo, ya lo renuevas."
        )

    if support_text_matches(normalized, quote_words):
        return (
            "Lo ideal es registrar primero tus materiales; así Sianeffects calcula costos reales y no vendes a ojo.\n"
            "Flujo rápido: materiales/equipos -> receta/producto -> margen/gastos -> cotización con envío, descuento o anticipo."
        )

    if support_text_matches(normalized, audience_words):
        return (
            "Es para emprendedores que hacen productos personalizados: sublimación, vinil, papelería creativa, regalos y similares.\n"
            "Sirve cuando quieres saber cuánto te cuesta algo y cuánto cobrar sin hacerlo a tanteo."
        )

    if support_text_matches(normalized, formula_words):
        return (
            "La base es: materiales + desgaste/equipos + mano de obra + gastos operativos.\n"
            "Luego Sianeffects aplica tu margen de ganancia, y al final suma envío, resta descuentos y agrega impuestos si los activas.\n"
            "Así el precio no sale a ojo: sale de tus costos reales."
        )

    return None


def build_gemini_contents(history):
    contents = []
    for message in history:
        text = (message['texto'] or '').strip()
        if not text:
            continue

        if message['remitente'] == 'bot':
            role = 'model'
        else:
            role = 'user'

        contents.append(types.Content(
            role=role,
            parts=[types.Part.from_text(text=text)],
        ))
    return contents


def clean_ai_reply(text):
    reply = (text or '').strip()
    if not reply:
        return ''

    for prefix in ('Asistente:', 'Bot:', 'Sianeffects cotizador:'):
        if reply.startswith(prefix):
            reply = reply[len(prefix):].strip()

    sentence_marks = '.!?'
    last_sentence_end = max(reply.rfind(mark) for mark in sentence_marks)
    if last_sentence_end == -1 or last_sentence_end == len(reply) - 1:
        return reply

    trailing_fragment = reply[last_sentence_end + 1:].strip()
    if trailing_fragment and len(trailing_fragment.split()) <= 3:
        return reply[:last_sentence_end + 1].strip()

    return reply


def generate_ai_reply(history):
    if not GEMINI_API_KEY:
        raise RuntimeError('Falta GEMINI_API_KEY en variables de entorno.')

    if history:
        previous_text = history[-2]['texto'] if len(history) >= 2 else ''
        direct_reply = get_direct_support_reply(history[-1]['texto'], previous_text)
        if direct_reply:
            return direct_reply

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=build_gemini_contents(history),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT_WHATSAPP,
            temperature=0.35,
            max_output_tokens=300,
        ),
    )
    return clean_ai_reply(response.text) or 'Gracias por escribirnos. ¿Me cuentas un poquito más para ayudarte mejor?'


@whatsapp_bot_bp.route('/webhook', methods=['POST'])
def webhook():
    # Eliminamos el manejo de GET porque Evolution solo envía POST
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
