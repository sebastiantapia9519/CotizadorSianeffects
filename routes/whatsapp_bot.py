import os
import requests
from flask import Blueprint, request, jsonify
from google import genai
from google.genai import types

# 1. Crear el Blueprint para mantener tu código ordenado
whatsapp_bot_bp = Blueprint('whatsapp', __name__)

# 2. Configurar Variables de Entorno (Asegúrate de tenerlas en tu servidor o archivo .env)
# La API key de Gemini ya la tenías
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# Los nuevos tokens que sacamos de Meta
WHATSAPP_TOKEN = os.environ.get('WHATSAPP_TOKEN')
WHATSAPP_PHONE_ID = os.environ.get('WHATSAPP_PHONE_ID')
VERIFY_TOKEN = os.environ.get('VERIFY_TOKEN', 'sianeffects_bot_seguro')

# Configurar el cliente de Gemini
client = genai.Client(api_key=GEMINI_API_KEY)

# 3. Almacén temporal para historiales de WhatsApp. 
# NOTA: Como WhatsApp no usa cookies (session), mapeamos el historial al número de teléfono.
# En un futuro, podrías guardar este diccionario en tu base de datos (PostgreSQL/Redis)
historial_whatsapp = {}

# 4. El Prompt específico para WhatsApp
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

def compact_chat_history(history, max_messages=6):
    """Mantiene el historial corto para no saturar los tokens, igual que en tu código original."""
    return history[-max_messages:]


def send_whatsapp_message(to_number, text_body):
    """Función para devolver el mensaje a Meta/WhatsApp"""
    url = f"https://graph.facebook.com/v18.0/{WHATSAPP_PHONE_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    
    data = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text_body}
    }
    
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        print(f"Mensaje enviado con éxito a {to_number}")
    except requests.exceptions.RequestException as e:
        print(f"Error al enviar mensaje a WhatsApp: {e}")
        if response is not None:
            print(f"Detalle: {response.text}")


@whatsapp_bot_bp.route('/webhook', methods=['GET', 'POST'])
def webhook():
    # --- PASO 1: Verificación de Meta ---
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')

        if mode and token:
            if mode == 'subscribe' and token == VERIFY_TOKEN:
                print("Webhook de WhatsApp verificado exitosamente.")
                return challenge, 200
            else:
                return 'Error de verificación', 403

    # --- PASO 2: Recibir Mensajes de Usuarios ---
    elif request.method == 'POST':
        body = request.get_json()

        if body.get('object'):
            try:
                # Meta manda mucha información anidada, navegamos hasta el mensaje
                entry = body.get('entry', [])[0]
                changes = entry.get('changes', [])[0]
                value = changes.get('value', {})
                
                if 'messages' in value:
                    mensaje_info = value['messages'][0]
                    numero_remitente = mensaje_info['from']
                    
                    # Solo procesar mensajes de texto
                    if 'text' in mensaje_info:
                        texto_mensaje = mensaje_info['text']['body'].strip()

                        # Si el número es de México y trae el '1' interno de WhatsApp (13 dígitos), se lo quitamos
                        if numero_remitente.startswith("521") and len(numero_remitente) == 13:
                            numero_remitente = "52" + numero_remitente[3:]
                        
                        print(f"WA -> Recibido de {numero_remitente}: {texto_mensaje}")

                        # 1. Recuperar o iniciar el historial de este usuario específico
                        if numero_remitente not in historial_whatsapp:
                            historial_whatsapp[numero_remitente] = []
                        
                        historial = historial_whatsapp[numero_remitente]

                        # 2. Preparar el contenido para Gemini
                        contents = []
                        for msg in historial:
                            contents.append(f"{msg['role']}: {msg['content']}")
                        contents.append(f"Usuario: {texto_mensaje}")

                        # 3. Llamar a Gemini (Con un poco más de 'temperature' para que sea más creativo y casual)
                        response = client.models.generate_content(
                            model='models/gemini-2.5-flash-lite',
                            contents="\n".join(contents),
                            config=types.GenerateContentConfig(
                                system_instruction=SYSTEM_PROMPT_WHATSAPP,
                                temperature=0.6, 
                                max_output_tokens=250
                            )
                        )
                        
                        respuesta_ia = response.text

                        # 4. Actualizar el historial
                        historial.append({'role': 'Usuario', 'content': texto_mensaje})
                        historial.append({'role': 'Asistente', 'content': respuesta_ia})
                        historial_whatsapp[numero_remitente] = compact_chat_history(historial)

                        # 5. Enviar la respuesta por WhatsApp al usuario
                        send_whatsapp_message(numero_remitente, respuesta_ia)

            except Exception as e:
                print(f"Error procesando el webhook de WhatsApp: {e}")

            # Siempre devolver 200 OK rápido para que Meta no crea que el servidor falló
            return jsonify({"status": "ok"}), 200
            
        else:
            return jsonify({"status": "error"}), 404