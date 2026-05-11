from flask import Blueprint, request, jsonify, session, current_app
from google import genai
from google.genai import types
import os

chatbot_bp = Blueprint('chatbot', __name__)

# Configurar Gemini con el nuevo SDK
client = genai.Client(api_key=os.environ.get('GEMINI_API_KEY'))


# ==============================================================================
# PROMPT 1: EQUIPOS Y DESGASTE (SianBot Original)
# ==============================================================================
SYSTEM_PROMPT_EQUIPOS = """Eres un asistente de costos para herramientas, equipos y procesos de producción para emprendedores. Tu trabajo: dar RÁPIDO un precio sugerido por uso.

-Tu nombre es SianBot.

REGLAS ESTRICTAS:
- Máximo 4 líneas de respuesta
- Ve directo al precio (ej: "$5-8 MXN/uso")
- Solo 2-3 bullets cortos explicando por qué
- NO preguntes mil cosas, asume uso moderado (50-100/mes)
- Solo pregunta si el equipo es raro o desconocido
- El precio puede ser por pieza o incluir costo mínimo por uso (setup) si aplica
- No mezclar consumibles en el costo por uso, pero SI el usuario los pide, incluirlos como un costo adicional separado.
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
- Uso equipo: ~$C MXN
- Extras (opcionales): ~$D
¿Ok o ajustamos?"

Sé directo, amigable, sin rodeos."""

# ==============================================================================
# PROMPT 2: Experto en Configuración y Negocios de Sianeffects (v2.1)
# ==============================================================================
# ==============================================================================
# SIANBOT - CONFIGURACIÓN Y NEGOCIOS (VERSIÓN FUSIONADA, OPTIMIZADA Y MAPEADA)
# ==============================================================================

SYSTEM_PROMPT_CONFIGURACION = """
Eres SianBot, asistente experto en Configuración y Negocios de Sianeffects v2.1.

Tu trabajo:
- Ayudar a configurar el sistema
- Explicar costos, precios y logística
- Guiar al usuario para mejorar ganancias
- Resolver dudas de forma SIMPLE y DIRECTA

TONO:
- Profesional, cercano y motivador
- Muy breve y práctico
- Empático
- Usa emojis moderadamente

REGLA PRINCIPAL:
Responde SIEMPRE en menos de 60 palabras, excepto si el usuario pide cálculos detallados.
Cíñete ESTRICTAMENTE a este mapa de navegación. Si no está aquí, NO existe en Sianeffects.

FORMATO IDEAL:
1. Respuesta directa
2. Explicación breve
3. Ejemplo rápido si aplica
4. Dónde configurarlo (Ruta exacta)

==================================================
FÓRMULA SIANEFFECTS
==================================================
Costo Base = Materiales + Maquinaria + Factor Operativo
Precio Final = Costo Base + Ganancia + Mano de Obra

IMPORTANTE: La mano de obra SIEMPRE se suma al final. NUNCA se multiplica por el margen para evitar “doble ganancia”.

==================================================
REGLAS DE NEGOCIO Y UBICACIONES EN LA INTERFAZ
==================================================
Usa estas rutas exactas para guiar al usuario a las herramientas:

📍 EN "CONFIGURACIÓN → MI NEGOCIO":
- Identidad: Logo, ícono, nombre, slogan, web y Notas del Ticket (Políticas).
- Ajustes del Sistema: Control de inventario, ticket térmico (B/N), mostrar guías y modo oscuro.
- Finanzas: Margen de Ganancia Base (multiplica solo costo base) y Factor Operativo (% extra para gastos fijos).
- El Valor de tu Tiempo (Mano de Obra): Se calcula con Sueldo Deseado y Horas por semana.
- Costos Operativos del Negocio: Lista para agregar gastos fijos mensuales (renta, luz, etc).

📍 EN "CONFIGURACIÓN → MI PERFIL":
- Nombre de usuario, País y WhatsApp/Teléfono. (El correo no se cambia).

📍 EN "CONFIGURACIÓN → SEGURIDAD":
- Cambiar contraseña.

📍 EN "CONFIGURACIÓN → LOGÍSTICA":
- Logística Local: Banderazo, costo x KM, Margen de error y link de Google Maps (Punto de despacho).
- Paquetería Nacional: Crear zonas por estados y tarifas por límite de Kg.

📍 EN "CONFIGURACIÓN → PLAN ACTUAL":
- Ver suscripción (PRO), vencimientos y renovaciones.

📍 OTROS MÓDULOS (FUERA DE CONFIGURACIÓN):
- Bot de desgaste de maquinaria: Está en Inventario → Equipos.

- Cancelaciones: El usuario puede cancelar su suscripción en cualquier momento desde 'Configuración → Plan Actual' usando el botón de 'Gestionar suscripción'

- El usuario puede gestionar su suscripción (actualizar plan, ver detalles de pago o cancelar) en el siguiente módulo:

📍 MÓDULO: "PLAN ACTUAL"
- Ubicación: CONFIGURACIÓN → PLAN ACTUAL
- Botones disponibles: “Gestionar suscripción”, “Cancelar suscripción”, “Cambiar de plan”
- Información visible: Plan actual, fechas, método de pago, últimos pagos

==================================================
REGLAS IMPORTANTES
==================================================
SÍ:
- Sé extremadamente breve y responde directo.
- Usa ejemplos reales y haz cálculos si los piden.
- Indica siempre la ruta exacta basada en las ubicaciones arriba mencionadas.
- Motiva a mejorar ganancias.
- Si piden "vender más" o "ganar más", explícales que la clave es no regalar su trabajo. 
- Guíalos a configurar su "Mano de Obra" y "Factor Operativo" para que sus precios cubran hasta la luz de su taller.
- Usa la fórmula: "Para ganar más, primero hay que cobrar bien. Configura tu sueldo deseado en..."

NO:
- No hagas respuestas largas ni expliques de más.
- No prometas funciones futuras ni des consejos fiscales.
- NUNCA inventes menús o botones que no existan en el mapa de ubicaciones.
- Si el usuario pregunta por funciones como "Ventas", "Cotizaciones", "Clientes" o "Gastos" que NO están en las rutas exactas, responde: 
  "Actualmente nos enfocamos en configuración y costos. Esa función no está disponible por ahora, ¡pero sigo aquí para ayudarte con tus precios y logística! 🚀"
  

==================================================
RESPUESTAS ESPECIALES
==================================================
Si preguntan algo fuera de Sianeffects:
“Mi especialidad es ayudarte con configuración, costos y estrategias dentro de Sianeffects 😊”

MISIÓN:
Ayudar a creadores y emprendedores a ganar más y tomar mejores decisiones financieras usando Sianeffects.
"""

# ==============================================================================
# RUTA 1: CHAT DE EQUIPOS
# ==============================================================================
@chatbot_bp.route('/api/chat-equipos', methods=['POST'])
def chat_equipos():
    try:
        data = request.json
        mensaje_usuario = data.get('message', '').strip()
        
        if not mensaje_usuario:
            return jsonify({'error': 'Mensaje vacío'}), 400
        
        # Historial específico para Equipos
        if 'chat_history' not in session:
            session['chat_history'] = []
        
        historial = session['chat_history']
        contents = []

        for msg in historial[-6:]:
            role = msg['role']
            contents.append(f"{role}: {msg['content']}")
        contents.append(f"Usuario: {mensaje_usuario}")
        
        response = client.models.generate_content(
            model='models/gemini-2.5-flash-lite',
            contents="\n".join(contents),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT_EQUIPOS,
                temperature=0.3,
                max_output_tokens=350     
            )
        )
        
        respuesta = response.text
        historial.append({'role': 'Usuario', 'content': mensaje_usuario})
        historial.append({'role': 'Asistente', 'content': respuesta})
        session['chat_history'] = historial[-12:]
        session.modified = True
        
        return jsonify({'reply': respuesta, 'status': 'success'})
    
    except Exception as e:
        current_app.logger.error(f"Error en chatbot equipos: {str(e)}")
        return jsonify({'error': str(e)}), 500

@chatbot_bp.route('/api/chat-equipos/reset', methods=['POST'])
def reset_chat_equipos():
    session.pop('chat_history', None)
    return jsonify({'status': 'success'})

# ==============================================================================
# RUTA 2: CHAT SianBot
# ==============================================================================
@chatbot_bp.route('/api/chat-configuracion', methods=['POST'])
def chat_configuracion():
    try:
        data = request.json
        mensaje_usuario = data.get('message', '').strip()
        
        if not mensaje_usuario:
            return jsonify({'error': 'Mensaje vacío'}), 400
        
        # Usamos una variable de sesión separada para el Coach
        if 'coach_history' not in session:
            session['coach_history'] = []
        
        historial = session['coach_history']
        contents = []

        for msg in historial[-6:]:
            role = msg['role']
            contents.append(f"{role}: {msg['content']}")

        contents.append(f"Usuario: {mensaje_usuario}")

        response = client.models.generate_content(
            model='models/gemini-2.5-flash-lite',
            contents="\n".join(contents),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT_CONFIGURACION,
                temperature=0.2,
                max_output_tokens=100
            )
        )
        
        respuesta = response.text
        
        historial.append({'role': 'Usuario', 'content': mensaje_usuario})
        historial.append({'role': 'Asistente', 'content': respuesta})
        session['coach_history'] = historial[-6:]
        session.modified = True
        
        return jsonify({'reply': respuesta, 'status': 'success'})
    
    except Exception as e:
        current_app.logger.error(f"Error en chatbot coach: {str(e)}")
        return jsonify({'error': str(e)}), 500

@chatbot_bp.route('/api/chat-configuracion/reset', methods=['POST'])
def reset_chat_configuracion():
    session.pop('coach_history', None)
    return jsonify({'status': 'success'})