from flask import Blueprint, request, jsonify, session,current_app
from google import genai
from google.genai import types
import os

chatbot_bp = Blueprint('chatbot', __name__)

# Configurar Gemini con el nuevo SDK
client = genai.Client(api_key=os.environ.get('GEMINI_API_KEY'))

SYSTEM_PROMPT = """Eres un asistente de costos para herramientas, equipos y procesos de producción para emprendedores. Tu trabajo: dar RÁPIDO un precio sugerido por uso.

-Tu nombre es SianBot.

REGLAS ESTRICTAS:
- Máximo 4 líneas de respuesta
- Ve directo al precio (ej: "$5-8 MXN/uso")
- Solo 2-3 bullets cortos explicando por qué
- NO preguntes mil cosas, asume uso moderado (50-100/mes)
- Solo pregunta si el equipo es raro o desconocido
- El precio puede ser por pieza o incluir costo mínimo por uso (setup) si aplica
- NO incluir materiales consumibles (ej: film, vinil, tinta) dentro del precio por uso. Solo calcular luz + desgaste del equipo.
- Asume que el desgaste del equipo se distribuye entre 100–300 piezas por mes, por lo que el costo por pieza debe ser bajo (normalmente menor a $8 MXN en equipos no industriales).
- Responde el idioma que el usuario te hable
-Si el modelo parece nuevo o no está en memoria, NO negar su existencia. Asumir que es una evolución del modelo anterior y dar estimación basada en ese.
-Nunca decir ‘no existe’ o ‘no tenemos registro’. En su lugar, estimar basado en modelos similares
-Si te preguntan algo que no tiene que ver con costos, responde amablemente que no puedes ayudar con eso
-Si te preguntan algo sobre si es buen equipo, responde amablemente que no puedes ayudar con eso
-Si el usuario no escribe en español o menciona otro país, mostrar el precio en MXN y una conversión aproximada a USD.
-Si el usuario solicita una moneda específica, responder en MXN y convertir a ESA moneda solicitada (no USD).
-Priorizar siempre la moneda solicitada por el usuario sobre la conversión por defecto.




FORMATO DE RESPUESTA:
"[Equipo]: $X-Y MXN/uso (~$A-B USD)
- Por pieza: ~$C MXN
- Setup (si aplica): ~$D MXN
¿Ok o ajustamos?"

Sé directo, amigable, sin rodeos."""

@chatbot_bp.route('/api/chat-equipos', methods=['POST'])
def chat_equipos():
    try:
        data = request.json
        mensaje_usuario = data.get('message', '').strip()
        
        if not mensaje_usuario:
            return jsonify({'error': 'Mensaje vacío'}), 400
        
        # Obtener historial del chat de la sesión
        if 'chat_history' not in session:
            session['chat_history'] = []
        
        historial = session['chat_history']
        
        # Construir la lista de mensajes SÓLO con el historial y el mensaje actual
        messages = []
        
        # Agregar historial reciente (últimos 6 mensajes)
        for msg in historial[-6:]:
            messages.append(
                types.Content(
                    role="user" if msg['role'] == 'Usuario' else "model",
                    parts=[types.Part(text=msg['content'])]
                )
            )
        
        # Agregar mensaje actual del usuario
        messages.append(
            types.Content(
                role="user",
                parts=[types.Part(text=mensaje_usuario)]
            )
        )
        
        # Llamar a Gemini con el modelo actualizado y las instrucciones del sistema en la configuración
        response = client.models.generate_content(
            model='gemini-2.5-flash',  # Modelo vigente
            contents=messages,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.5 # Opcional: Recomendado para que el asistente suene natural
            )
        )
        
        respuesta = response.text
        
        # Guardar en historial
        historial.append({'role': 'Usuario', 'content': mensaje_usuario})
        historial.append({'role': 'Asistente', 'content': respuesta})
        session['chat_history'] = historial[-20:]
        session.modified = True
        
        return jsonify({
            'reply': respuesta,
            'status': 'success'
        })
    
    except Exception as e:
        print(f"Error en chatbot: {str(e)}")
        current_app.logger.error(f"Error en chatbot: {str(e)}")
        return jsonify({'error': str(e)}), 500

@chatbot_bp.route('/api/chat-equipos/reset', methods=['POST'])
def reset_chat():
    session.pop('chat_history', None)
    return jsonify({'status': 'success'})