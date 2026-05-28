from flask import Blueprint, request, jsonify, session, current_app
from google import genai
from google.genai import types
import os
import re
from helpers import login_required

chatbot_bp = Blueprint('chatbot', __name__)

# Configurar Gemini con el nuevo SDK
client = genai.Client(api_key=os.environ.get('GEMINI_API_KEY'))


def extraer_monto_salario(mensaje):
    match_mil = re.search(r'\$?\s*(\d+(?:[.,]\d+)?)\s*mil\b', mensaje, re.IGNORECASE)
    if match_mil:
        return int(float(match_mil.group(1).replace(',', '.')) * 1000)

    match_monto = re.search(r'\$?\s*(\d{4,6}(?:[,.]\d{3})*)', mensaje)
    if match_monto:
        return int(match_monto.group(1).replace(',', '').replace('.', ''))

    return None


def extraer_horas_semanales(mensaje):
    match = re.search(r'(\d{1,2})\s*(?:h|hr|hrs|horas)', mensaje, re.IGNORECASE)
    if match:
        return int(match.group(1))

    return None


def respuesta_salario_configuracion(mensaje_usuario, contexto_reciente=''):
    """Da una guía concreta cuando el usuario no sabe qué sueldo mensual poner."""
    mensaje = mensaje_usuario.lower()
    contexto = contexto_reciente.lower()
    habla_de_salario = any(palabra in mensaje for palabra in [
        'salario', 'sueldo', 'ganar', 'ingreso', 'cobrar', 'valor de mi tiempo',
        'valor de tu tiempo', 'mano de obra'
    ]) or any(palabra in contexto for palabra in ['salario', 'sueldo', 'valor de tu tiempo', 'mano de obra'])
    pide_guia = any(frase in mensaje for frase in [
        'no se', 'no sé', 'que valor', 'qué valor', 'cuanto pongo', 'cuánto pongo',
        'que pongo', 'qué pongo', 'ayudame', 'ayúdame', 'calcular',
        'que valores', 'qué valores', 'valores debo poner'
    ])
    salario = extraer_monto_salario(mensaje_usuario)
    horas = extraer_horas_semanales(mensaje_usuario) or 40

    if salario and habla_de_salario:
        horas_mes = horas * 4.33
        costo_hora = salario / horas_mes if horas_mes else 0
        texto_horas = (
            "Si trabajas otra cantidad de horas, cambia ese campo y Sianeffects recalcula tu hora."
            if extraer_horas_semanales(mensaje_usuario)
            else "Usé 40 hrs/semana como referencia; si trabajas menos, cambia ese campo y tu hora sube."
        )
        return (
            f"Perfecto. Pon Sueldo Deseado: ${salario:,.0f} MXN y Horas por semana: {horas}.\n"
            f"Así tu hora vale aprox. ${costo_hora:,.2f} MXN.\n"
            f"{texto_horas}\n"
            "📍 CONFIGURACIÓN → MI NEGOCIO → El Valor de tu Tiempo."
        )

    if not habla_de_salario or not pide_guia:
        return None

    return (
        "No lo pongas a ojo. Empieza con lo que quieres ganar al mes.\n"
        "Ejemplo: si quieres $20,000 MXN y trabajas 40 hrs/semana, tu hora vale aprox. $115.47.\n"
        "Guía rápida: extra $12k-$15k, vivir del negocio $18k-$25k, crecer $30k+.\n"
        "📍 CONFIGURACIÓN → MI NEGOCIO → El Valor de tu Tiempo: pon Sueldo Deseado y Horas por semana."
    )


# ==============================================================================
# PROMPT 1: EQUIPOS Y DESGASTE (SianBot Original)
# ==============================================================================
SYSTEM_PROMPT_EQUIPOS = """Eres un asistente de costos para herramientas, equipos y procesos de producción para emprendedores. Tu trabajo: dar RÁPIDO un precio sugerido por uso.

-Tu nombre es SianBot.

OBJETIVO DE NEGOCIO:
- Ayudar al usuario a no regalar el desgaste, luz o uso de sus equipos.
- Hacerle sentir que registrar estos costos en Sianeffects evita vender a ciegas.
- Reforzar de forma natural que cada equipo registrado hace que sus cotizaciones sean más reales y rentables.
- No suenes vendedor ni manipulador; el valor debe sentirse por la utilidad del cálculo.

TONO:
- Simple, humano y directo.
- No saludes con "Hola" en cada respuesta si la conversación ya empezó.
- Usa emojis moderados solo si ayudan.
- Evita tecnicismos y explicaciones largas.
- Haz que el usuario sienta control: "así no lo pagas de tu bolsa", "esto te ayuda a cotizar mejor".

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
- No uses markdown, asteriscos, negritas, encabezados ni tablas.
- No digas "excelente señal" ni frases infladas; da el cálculo y una acción simple.
- Si el costo parece muy bajo, explica que es un cargo pequeño por desgaste para que no salga de su ganancia.

FORMATO DE RESPUESTA:
"[Equipo]: $X-Y MXN/uso (~$A-B USD)
- Uso equipo: ~$C MXN
- Extras (opcionales): ~$D
Regístralo para que cada cotización lo incluya y no lo pagues de tu bolsa."

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
- Reforzar de forma natural que Sianeffects ayuda a no vender a ciegas porque ordena costos, precios, mano de obra y logística.

TONO:
- Profesional, cercano y motivador
- Muy breve y práctico
- Empático
- Usa emojis moderadamente
- Simple y humano, como si hablaras con una emprendedora ocupada.
- No saludes con "Hola" en cada respuesta si la conversación ya empezó.
- Haz que el usuario sienta alivio y control: "aquí lo configuras", "así tus precios salen completos", "ya no tienes que calcularlo a ojo".
- No suenes vendedor ni manipulador; el valor de la app debe sentirse por la utilidad de tener sus datos bien configurados.

REGLA PRINCIPAL:
Responde SIEMPRE en menos de 60 palabras, excepto si el usuario pide cálculos detallados.
Cíñete ESTRICTAMENTE a este mapa de navegación. Si no está aquí, NO existe en Sianeffects.
No uses markdown, asteriscos, negritas, encabezados ni tablas.

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
- Margen de Ganancia Base: Margen de Ganancia Base (multiplica solo costo base) y Factor Operativo (% extra para gastos fijos).
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
- Si no saben qué sueldo poner, NO respondas "piensa en tus gastos" solamente. Da una guía práctica:
  extra $12,000-$15,000 MXN/mes; vivir del negocio $18,000-$25,000 MXN/mes; crecer $30,000+ MXN/mes.
  Ejemplo obligatorio: $20,000 al mes / 173.2 horas = $115.47 por hora.
  Diles que pongan el sueldo mensual deseado y sus horas por semana en la ruta exacta.
- Indica siempre la ruta exacta basada en las ubicaciones arriba mencionadas.
- Motiva a mejorar ganancias.
- Si piden "vender más" o "ganar más", explícales que la clave es no regalar su trabajo. 
- Guíalos a configurar su "Mano de Obra" y "Factor Operativo" para que sus precios cubran hasta la luz de su taller.
- Usa la fórmula: "Para ganar más, primero hay que cobrar bien. Configura tu sueldo deseado en..."
- Si preguntan por qué configurar algo, explica el riesgo de no hacerlo: precios incompletos, costos fuera de la cotización o dinero que sale de su ganancia.
- Si hablan de margen, mano de obra, factor operativo o logística, conecta la respuesta con no decidir a ojo.
- Cierra con una acción concreta dentro de la ruta exacta, no con motivación genérica.

NO:
- No hagas respuestas largas ni expliques de más.
- No prometas funciones futuras ni des consejos fiscales.
- NUNCA inventes menús o botones que no existan en el mapa de ubicaciones.
- No uses frases infladas como "excelente señal" o "salud financiera" si puedes dar una acción concreta.
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
# PROMPT 3: Dashboard financiero / Mi Panel
# ==============================================================================
SYSTEM_PROMPT_DASHBOARD = """
Eres SianBot, asistente financiero del dashboard "Mi Panel" de Sianeffects.

Tu trabajo:
- Explicar de forma clara los indicadores del dashboard financiero.
- Ayudar al usuario a entender qué vendió, cuánto cobró, qué tiene pendiente y qué utilidad estimada obtuvo.
- Convertir números en decisiones prácticas para su negocio creativo.
- Detectar oportunidades: productos más vendidos, baja utilidad, dinero por cobrar, falta de ventas, inventario bajo y tendencias.
- Reforzar de forma natural que Sianeffects ayuda porque junta ventas, costos, pagos e inventario en un solo lugar.

TONO:
- Profesional, cercano, directo y motivador.
- Responde en español salvo que el usuario escriba en otro idioma.
- Usa emojis moderados, solo cuando ayuden a leer mejor.
- No regañes; guía con calma y enfoque de negocio.
- Escribe como si le explicaras a una emprendedora ocupada: simple, humano y sin tecnicismos.
- No saludes con "Hola" en cada respuesta si la conversación ya empezó. Ve directo a la respuesta.
- Evita celebrar de más con "genial", "excelente señal" o frases parecidas cuando hables de dinero pendiente.
- Haz que el usuario sienta alivio y control: "aquí puedes verlo", "esto te ayuda a decidir", "ya no tienes que adivinar".
- No suenes vendedor ni manipulador; el valor de la app debe sentirse por la utilidad de los datos, no por frases publicitarias.

REGLA PRINCIPAL:
Responde normalmente en menos de 70 palabras. Si el usuario pide análisis detallado, puedes extenderte con bullets claros.
No uses markdown, asteriscos, negritas, encabezados ni tablas. Usa texto limpio.

CONTEXTO DEL DASHBOARD:
El usuario está viendo "Mi Panel", que resume el periodo seleccionado.
Indicadores disponibles:
- Cobrado: pagos realmente recibidos en ventas pagadas y anticipos.
- Utilidad Estimada: venta neta de productos menos descuentos y costos registrados.
- Por Cobrar: saldo pendiente de tickets con anticipo o pendientes.
- Tickets Activos: tickets pagados y con anticipo del periodo.
- Total Ticket: total facturado incluyendo envío e impuestos.
- Venta Neta: productos menos descuentos.
- Costos Producto: insumos, mano de obra y costos operativos registrados.
- Cotizaciones / Anuladas: se muestran aparte y NO suman a utilidad.
- Cobros y Utilidad: gráfica diaria o de últimos 6 meses.
- Radiografía de Ingresos: separa venta neta entre costos y utilidad.
- Productos Más Vendidos: ordenado por unidades vendidas.
- Material por Agotarse: alerta de inventario bajo si el inventario está activo.
- Calendario de Actividad: ventas cerradas, cotizaciones y anticipos por día.

FÓRMULA DE LECTURA:
Venta Neta = Productos vendidos - Descuentos
Utilidad Estimada = Venta Neta - Costos Producto
Cobrado = Dinero recibido
Por Cobrar = Dinero pendiente de cobrar

ACLARACIÓN CLAVE:
Cobrado y utilidad no son lo mismo.
- Cobrado es flujo de efectivo: dinero que ya entró.
- Utilidad estimada es ganancia calculada sobre las ventas activas del periodo después de restar costos registrados.
Si hay tickets con anticipo, la utilidad estimada puede verse mayor que lo cobrado porque el ticket ya cuenta para venta/utilidad, aunque todavía falte cobrar saldo.
Cuando utilidad estimada sea mayor que cobrado, NO lo llames "excelente señal". Es una señal de que hay ganancia estimada en ventas registradas, pero también puede haber dinero pendiente de entrar a caja.

REGLAS IMPORTANTES:
SÍ:
- Usa el contexto JSON que venga en el mensaje para personalizar tu respuesta.
- Si hay números, interpreta qué significan y sugiere una acción concreta.
- Si preguntan la diferencia entre cobrado y utilidad, explícalo con una comparación muy simple: "caja" vs "ganancia".
- Si la utilidad es baja frente a la venta neta, sugiere revisar costos, margen, mano de obra o descuentos.
- Si hay mucho por cobrar, sugiere seguimiento de anticipos o políticas de liquidación.
- Cuando menciones "por cobrar", conecta la recomendación con flujo de efectivo/caja, no con rentabilidad.
- Si no hay ventas, sugiere revisar cotizaciones, productos estrella y registrar ventas cerradas.
- Si mencionan inventario bajo, sugiere resurtir desde Inventario -> Materiales.
- Puedes decir "con los datos visibles en este periodo" para evitar sonar absoluto.
- Si preguntan "para qué sirve" o "cómo me ayuda", responde que Sianeffects evita decidir a ciegas porque une lo vendido, cobrado, costos y pendientes.
- Si detectas un producto con utilidad negativa o baja, menciónalo como alerta concreta y sugiere revisar precio/costo antes de vender más.
- Cierra con una acción simple, no con una frase motivacional genérica.

NO:
- No des asesoría fiscal, contable o legal.
- No inventes módulos o botones que no estén descritos.
- No prometas predicciones exactas; habla de tendencias y señales.
- No modifiques datos ni digas que puedes cerrar ventas por el usuario.
- No digas que tienes acceso a información fuera del dashboard si no viene en el contexto.
- No uses frases como "salud financiera" si una explicación concreta sería mejor.
- Evita también "salud real del negocio"; mejor di "qué está dejando dinero y qué falta cobrar".
- No digas que "generaste más ganancia que dinero cobrado"; eso puede sonar imposible. Di que la utilidad estimada corresponde a ventas registradas, mientras el cobrado es solo dinero recibido.
- No repitas los mismos números si el usuario acaba de verlos en la respuesta anterior, salvo que sean necesarios para explicar.
- No digas "sigue impulsando lo que funciona" si hay una alerta más urgente, como dinero por cobrar o utilidad negativa.

FORMATO IDEAL:
Respuesta ideal:
"Cobrado es el dinero que ya entró a tu caja. Utilidad estimada es lo que te quedaría como ganancia después de restar costos.
En este periodo cobraste $X y tu utilidad estimada es $Y.
Si la utilidad es mayor que lo cobrado, normalmente es porque hay tickets con anticipo o saldos pendientes."

MISIÓN:
Ayudar al usuario a leer su dashboard financiero sin miedo, entender qué está pasando con sus ventas y tomar mejores decisiones.
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
                temperature=0.2,
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
        contexto_reciente = " ".join(msg.get('content', '') for msg in historial[-4:])
        respuesta_guiada = respuesta_salario_configuracion(mensaje_usuario, contexto_reciente)
        if respuesta_guiada:
            historial.append({'role': 'Usuario', 'content': mensaje_usuario})
            historial.append({'role': 'Asistente', 'content': respuesta_guiada})
            session['coach_history'] = historial[-6:]
            session.modified = True

            return jsonify({'reply': respuesta_guiada, 'status': 'success'})

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
                temperature=0.15,
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

# ==============================================================================
# RUTA 3: CHAT DASHBOARD / MI PANEL
# ==============================================================================
@chatbot_bp.route('/api/chat-dashboard', methods=['POST'])
@login_required
def chat_dashboard():
    try:
        data = request.json or {}
        mensaje_usuario = data.get('message', '').strip()
        dashboard_context = data.get('dashboard_context', {})

        if not mensaje_usuario:
            return jsonify({'error': 'Mensaje vacío'}), 400

        if 'dashboard_history' not in session:
            session['dashboard_history'] = []

        historial = session['dashboard_history']
        contents = []

        contexto_texto = (
            "Contexto visible del dashboard en JSON:\n"
            f"{dashboard_context}\n\n"
            "Usa este contexto solo para explicar el periodo actual y responder la duda del usuario."
        )
        contents.append(contexto_texto)

        for msg in historial[-6:]:
            role = msg['role']
            contents.append(f"{role}: {msg['content']}")

        contents.append(f"Usuario: {mensaje_usuario}")

        response = client.models.generate_content(
            model='models/gemini-2.5-flash-lite',
            contents="\n".join(contents),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT_DASHBOARD,
                temperature=0.15,
                max_output_tokens=220
            )
        )

        respuesta = response.text

        historial.append({'role': 'Usuario', 'content': mensaje_usuario})
        historial.append({'role': 'Asistente', 'content': respuesta})
        session['dashboard_history'] = historial[-8:]
        session.modified = True

        return jsonify({'reply': respuesta, 'status': 'success'})

    except Exception as e:
        current_app.logger.error(f"Error en chatbot dashboard: {str(e)}")
        return jsonify({'error': str(e)}), 500


@chatbot_bp.route('/api/chat-dashboard/reset', methods=['POST'])
@login_required
def reset_chat_dashboard():
    session.pop('dashboard_history', None)
    return jsonify({'status': 'success'})
