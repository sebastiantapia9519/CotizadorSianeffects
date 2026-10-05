from flask import Blueprint, request, jsonify, session, current_app
from google import genai
from google.genai import types
import os
import re
import time
import json
import math
import urllib.request
from helpers import admin_required, login_required

chatbot_bp = Blueprint('chatbot', __name__)

# Configurar Gemini con el nuevo SDK
client = genai.Client(api_key=os.environ.get('GEMINI_API_KEY'))

MAX_HISTORY_MESSAGES = 6
MAX_HISTORY_CHARS = 500

# Tipo de cambio USD->MXN con caché de 6 horas.
# La variable de entorno TIPO_CAMBIO_USD queda solo como respaldo si la API falla.
_tc_cache = {'valor': float(os.environ.get('TIPO_CAMBIO_USD', '18')), 'ts': 0}


def obtener_tipo_cambio_usd():
    ahora = time.time()
    if ahora - _tc_cache['ts'] < 6 * 3600:
        return _tc_cache['valor']
    try:
        with urllib.request.urlopen('https://open.er-api.com/v6/latest/USD', timeout=3) as r:
            data = json.loads(r.read().decode())
        mxn = float(data['rates']['MXN'])
        if 10 < mxn < 40:  # filtro de cordura
            _tc_cache['valor'] = mxn
        _tc_cache['ts'] = ahora
    except Exception as e:
        current_app.logger.warning(f"TIPO_CAMBIO_WARNING: no se pudo actualizar, uso {_tc_cache['valor']} - {e}")
        _tc_cache['ts'] = ahora - 6 * 3600 + 300  # reintenta en 5 minutos
    return _tc_cache['valor']


def compact_chat_history(history, max_messages=MAX_HISTORY_MESSAGES, max_chars=MAX_HISTORY_CHARS):
    compacted = []

    for msg in history[-max_messages:]:
        if not isinstance(msg, dict):
            continue

        content = str(msg.get('content', ''))
        if len(content) > max_chars:
            content = content[:max_chars] + '...'

        compacted.append({
            'role': msg.get('role', ''),
            'content': content
        })

    return compacted


def normalize_chat_message(message):
    return re.sub(r'[^\wáéíóúüñ\s]', '', message.lower()).strip()


def is_simple_greeting(message):
    normalized = normalize_chat_message(message)
    if not normalized:
        return False

    greeting_phrases = {
        'hola',
        'holaa',
        'buenos dias',
        'buen dia',
        'buenas tardes',
        'buenas noches',
        'saludame',
        'salúdame',
        'saludos',
        'hey',
        'hello',
        'hi',
    }

    return normalized in greeting_phrases


def get_social_reply(message, bot_type):
    normalized = normalize_chat_message(message)

    replies = {
        'equipos': {
            'greeting': (
                "¡Hola! Qué gusto verte por aquí 😊\n"
                "Cuando quieras, dime qué equipo estás usando y lo calculamos."
            ),
            'how_are_you': (
                "Estoy bien, lista para ayudarte a cobrar mejor tus equipos sin complicarte 😊\n"
                "¿Qué tienes en mente hoy?"
            ),
            'thanks': "Con gusto 😊 Aquí estoy cuando quieras revisar un equipo o costo.",
        },
        'configuracion': {
            'greeting': (
                "¡Hola! Qué bueno tenerte por aquí 😊\n"
                "Cuéntame qué quieres ajustar y lo vemos paso a paso."
            ),
            'how_are_you': (
                "Estoy bien, lista para ayudarte a ordenar costos, precios o configuración 😊\n"
                "¿Qué quieres revisar primero?"
            ),
            'thanks': "Con gusto 😊 Cuando quieras seguimos afinando tu configuración.",
        },
        'dashboard': {
            'greeting': (
                "¡Hola! Qué gusto leerte 😊\n"
                "Estoy aquí contigo para revisar el panel sin hacerlo pesado."
            ),
            'how_are_you': (
                "Estoy bien, gracias por preguntar 😊\n"
                "Lista para ayudarte a entender qué está pasando con tus ventas, con calma y sin enredos."
            ),
            'thanks': "Con gusto 😊 Aquí sigo si quieres revisar ventas, utilidad o algo que se vea raro en tu panel.",
        },
        'admin': {
            'greeting': (
                "¡Hola, Jefe! Ya estoy aquí 😊\n"
                "Podemos revisar crecimiento, riesgos o prioridades del día cuando quieras."
            ),
            'how_are_you': (
                "Estoy bien, Jefe, con el tablero listo para pensar contigo 😊\n"
                "¿Quieres que revisemos crecimiento, conversión o riesgos primero?"
            ),
            'thanks': "Con gusto, Jefe 😊 Cuando quieras seguimos con el análisis.",
        },
    }

    how_are_you_phrases = {
        'como estas',
        'cómo estás',
        'como andas',
        'que tal',
        'qué tal',
        'todo bien',
        'how are you',
    }
    thanks_phrases = {
        'gracias',
        'muchas gracias',
        'mil gracias',
        'thanks',
        'thank you',
    }

    greeting_with_how_are_you = (
        normalized.startswith('hola como estas')
        or normalized.startswith('hola cómo estás')
        or normalized.startswith('hola que tal')
        or normalized.startswith('hola qué tal')
    )

    if normalized in how_are_you_phrases or greeting_with_how_are_you:
        return replies[bot_type]['how_are_you']
    if normalized in thanks_phrases:
        return replies[bot_type]['thanks']
    if is_simple_greeting(message):
        return replies[bot_type]['greeting']

    return None


def store_chat_reply(session_key, history, user_message, reply):
    history.append({'role': 'Usuario', 'content': user_message})
    history.append({'role': 'Asistente', 'content': reply})
    session[session_key] = compact_chat_history(history)
    session.modified = True


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
    habla_de_otro_campo = any(palabra in mensaje for palabra in [
        'margen', 'margen base', 'margen de ganancia', 'factor operativo'
    ])
    if habla_de_otro_campo:
        return None

    habla_de_salario = any(palabra in mensaje for palabra in [
        'salario', 'sueldo', 'ganar', 'ingreso', 'cobrar', 'valor de mi tiempo',
        'valor de tu tiempo', 'mano de obra', 'hora', 'costo por hora'
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
        "Sí. Para mano de obra, empieza gradual: si hoy cobras muy bajo, usa una meta realista y súbela por etapas.\n"
        "Guía rápida: ajuste suave $8k-$12k, ingreso extra $12k-$15k, vivir del negocio $18k-$25k.\n"
        "Ejemplo: $20,000 al mes y 40 hrs/semana = aprox. $115.47 por hora.\n"
        "Si tus cotizaciones suben demasiado, baja la meta inicial, no elimines tu mano de obra.\n"
        "📍 CONFIGURACIÓN → MI NEGOCIO → El Valor de tu Tiempo."
    )


def respuesta_margen_configuracion(mensaje_usuario, contexto_reciente=''):
    """Da una guía concreta cuando el usuario no sabe qué margen base poner."""
    texto = mensaje_usuario.lower()
    habla_de_margen = any(palabra in texto for palabra in [
        'margen base', 'margen de ganancia', 'margen', 'ganancia base',
        'porcentaje', 'por ciento', '%'
    ])
    pide_guia = any(frase in texto for frase in [
        'no se', 'no sé', 'que porcentaje', 'qué porcentaje', 'cuanto pongo',
        'cuánto pongo', 'que pongo', 'qué pongo', 'ayudame', 'ayúdame',
        'definirlo', 'recomiendas', 'recomiendas poner', 'empezar'
    ])

    if not habla_de_margen or not pide_guia:
        return None

    return (
        "Sí. Si vienes de cobrar bajo, pon 20% como margen base y úsalo fijo por una etapa.\n"
        "Guía rápida: 15%-20% si estás corrigiendo precios, 25%-30% si ya vendes estable, 35%+ si tu mercado ya acepta precios más altos.\n"
        "No incluye tu mano de obra; esa va aparte en El Valor de tu Tiempo.\n"
        "Elige uno y déjalo como regla general; no lo cambies por pedido. Revísalo solo cuando tengas datos reales o cambie tu estrategia.\n"
        "📍 CONFIGURACIÓN → MI NEGOCIO → Margen de Ganancia Base."
    )


def respuesta_factor_operativo_configuracion(mensaje_usuario, contexto_reciente=''):
    """Da una guía concreta cuando el usuario no sabe qué factor operativo poner."""
    texto = mensaje_usuario.lower()
    habla_de_factor = any(palabra in texto for palabra in [
        'factor operativo', 'gastos operativos', 'gastos fijos', 'operativo',
        'renta', 'luz', 'internet', 'agua'
    ])
    pide_guia = any(frase in texto for frase in [
        'no se', 'no sé', 'que porcentaje', 'qué porcentaje', 'cuanto pongo',
        'cuánto pongo', 'que pongo', 'qué pongo', 'ayudame', 'ayúdame',
        'definirlo', 'recomiendas', 'recomiendas poner', 'empezar'
    ])

    if not habla_de_factor or not pide_guia:
        return None

    return (
        "Sí. Si tus precios ya suben mucho, pon 5% de Factor Operativo como base fija para empezar.\n"
        "Guía rápida: 3%-5% para ajuste suave, 8%-12% si pagas luz, internet o herramientas, 15%+ si tienes renta o taller.\n"
        "Esto ayuda a que cada cotización cargue una parte de tus gastos fijos.\n"
        "No lo cambies por cotización; revísalo por temporada o cuando ya tengas tus gastos fijos mejor medidos.\n"
        "📍 CONFIGURACIÓN → MI NEGOCIO → Factor Operativo."
    )


def respuesta_precio_alto_configuracion(mensaje_usuario, contexto_reciente=''):
    """Acompaña cuando el usuario siente que sus cotizaciones subieron demasiado."""
    texto = f"{contexto_reciente} {mensaje_usuario}".lower()
    habla_de_precio_alto = any(frase in texto for frase in [
        'sube mucho', 'subió mucho', 'subio mucho', 'muy caro', 'se disparó',
        'se disparo', 'precio alto', 'precios altos', 'cotizacion alta',
        'cotización alta', 'cotizaciones altas', 'me sube bastante',
        'sube bastante', 'demasiado caro'
    ])

    if not habla_de_precio_alto:
        return None

    return (
        "Sí, puede pasar. Muchas veces el precio sube porque antes estabas absorbiendo mano de obra, gastos fijos o margen sin darte cuenta.\n"
        "Hazlo por etapas: costos reales primero, mano de obra mínima, margen 15%-20% y factor operativo 3%-5%.\n"
        "Cuando tus clientes se acostumbren y tengas más claridad, sube poco a poco.\n"
        "La meta no es encarecer de golpe; es dejar de vender con pérdida."
    )


# ==============================================================================
# EQUIPOS: tabla base + cálculo en Python (Gemini solo extrae datos)
# ==============================================================================
EQUIPOS_BASE = {
    'plotter':     {'precio': 5500,  'usos': 100, 'piezas': 0.50, 'luz': 0.10, 'pieza': 'cuchilla', 'nombre': 'Plotter de corte'},
    'plancha':     {'precio': 3000,  'usos': 100, 'piezas': 0.30, 'luz': 0.20, 'pieza': 'resistencia', 'nombre': 'Plancha o prensa térmica'},
    'sublimacion': {'precio': 6000,  'usos': 150, 'piezas': 1.50, 'luz': 0.10, 'pieza': 'cabezal', 'nombre': 'Impresora de sublimación'},
    'dtf':         {'precio': 20000, 'usos': 300, 'piezas': 4.00, 'luz': 0.60, 'pieza': 'cabezal e inyectores', 'nombre': 'Impresora DTF A3'},
    'uv':          {'precio': 90000, 'usos': 200, 'piezas': 7.00, 'luz': 1.00, 'pieza': 'cabezal y lámpara', 'nombre': 'Impresora UV'},
    'laser_diodo': {'precio': 8000,  'usos': 100, 'piezas': 0.30, 'luz': 0.10, 'pieza': 'módulo láser', 'nombre': 'Láser de diodo'},
    'laser_co2':   {'precio': 12000, 'usos': 100, 'piezas': 1.30, 'luz': 0.60, 'pieza': 'tubo y lentes', 'nombre': 'Láser CO2 40W'},
    'coser':       {'precio': 5000,  'usos': 150, 'piezas': 0.20, 'luz': 0.02, 'pieza': 'agujas', 'nombre': 'Máquina de coser'},
    'bordadora':   {'precio': 30000, 'usos': 150, 'piezas': 1.00, 'luz': 0.10, 'pieza': 'agujas', 'nombre': 'Bordadora'},
    'laminadora':  {'precio': 1500,  'usos': 100, 'piezas': 0.10, 'luz': 0.10, 'pieza': 'rodillos', 'nombre': 'Laminadora'},
    'guillotina':  {'precio': 1500,  'usos': 100, 'piezas': 0.30, 'luz': 0.00, 'pieza': 'cuchilla', 'nombre': 'Guillotina'},
}

# Campos que se guardan en sesión para poder recalcular con los botones o correcciones
EQUIPOS_CAMPOS_SESION = (
    'tipo', 'nombre', 'precio', 'usos_mes', 'usos_semana', 'moneda', 'consumibles',
    'precio_estimado', 'piezas_estimado', 'luz_estimado', 'pieza'
)

SYSTEM_PROMPT_EQUIPOS_EXTRACTOR = """Eres un clasificador. Lees el mensaje de un usuario de Sianeffects que habla de equipos de producción (plotters de corte, planchas, impresoras, láseres, máquinas de coser, etc.) y respondes SOLO un JSON, sin texto extra.

Campos:
- "intencion": "calcular" si pide o menciona un equipo para calcular su costo por uso, corrige datos del equipo anterior (precio, usos, moneda) o pide explicar el costo; "compra" si pregunta cuánto cuesta comprar un equipo o dónde comprarlo; "cotizar" si pregunta cómo cotizar, cobrar o vender; "otro" para todo lo demás (comparar equipos, si es bueno, temas ajenos).
- "tipo": uno de plotter, plancha, sublimacion, dtf, uv, laser_diodo, laser_co2, coser, bordadora, laminadora, guillotina, otro. Cricut y Silhouette son plotter. Usa "otro" si no encaja.
- "nombre": nombre del equipo como lo escribió el usuario, limpio y corto.
- "precio": precio que pagó el usuario en MXN, número; null si no lo dijo.
- "usos_mes": usos por mes que dijo el usuario, número; null si no lo dijo.
- "usos_semana": usos, piezas o trabajos por semana que dijo el usuario, número; si lo dijo por día, multiplícalo por 6; null si no lo dijo.
- "moneda": "usd" si pide dólares, escribe en inglés o menciona otro país; "otra" si pide una moneda distinta de MXN y USD; si no, "mxn".
- "consumibles": true si pide incluir vinil, tinta, papel u otro consumible.
- Solo si tipo es "otro": "precio_estimado" (precio típico del equipo nuevo en MXN), "piezas_estimado" (costo de piezas de desgaste por uso en MXN), "luz_estimado" (costo de luz por uso en MXN) y "pieza" (nombre de la pieza principal que se desgasta). Usa valores realistas y no hagas ninguna división.
Si el mensaje corrige datos del equipo anterior (ej. "y con 10 a la semana?" o "en dólares"), conserva tipo, nombre y los demás campos del "Último equipo" y cambia solo lo que el usuario cambió.
"""

RESP_EQUIPOS_COMPRA = "No te ayudo con precios de compra. Dime cuánto te costó y calculo su costo por uso."
RESP_EQUIPOS_COTIZAR = "Eso se hace en Cotizador. Aquí calculo el costo por uso de tus equipos."
RESP_EQUIPOS_OTRO = "No puedo ayudarte con eso. Solo calculo el costo por uso de equipos."
RESP_EQUIPOS_SIN_DATOS = "No pude calcularlo. Dime el equipo y, si puedes, cuánto costó y cuántas veces lo usas a la semana."


def _num(valor):
    try:
        valor = float(valor)
        return valor if valor > 0 else None
    except (TypeError, ValueError):
        return None


def _texto_semana(usos_mes):
    semanal = usos_mes / 4.33
    if semanal < 1:
        return "menos de 1 por semana"
    return f"~{round(semanal)} por semana"


def opciones_uso(datos):
    """Tres botones de uso (poco / normal / mucho), etiquetados por semana."""
    base = EQUIPOS_BASE.get(datos.get('tipo'))
    normal = base['usos'] if base else 100
    poco = max(5, round(normal * 0.25))
    mucho = normal * 2
    etiquetas = [('Pocas', poco), ('Normal', normal), ('Muchas', mucho)]
    return [
        {'label': f"{nombre} · {_texto_semana(usos)}", 'usos_mes': usos}
        for nombre, usos in etiquetas
    ]


def calcular_costo_equipo(datos, usos_override=None):
    base = EQUIPOS_BASE.get(datos.get('tipo'))

    # Usos que escribió el usuario (por mes o por semana). Si no hay, se usa el supuesto.
    usos_escrito = _num(datos.get('usos_mes'))
    if usos_escrito is None:
        semana = _num(datos.get('usos_semana'))
        if semana:
            usos_escrito = semana * 4.33
    if usos_escrito:
        usos_escrito = math.ceil(usos_escrito)  # usos al mes siempre redondeados hacia arriba

    precio_usuario = _num(datos.get('precio'))

    if base:
        precio = precio_usuario or base['precio']
        usos_base = base['usos']
        piezas, luz, pieza = base['piezas'], base['luz'], base['pieza']
        nombre = (datos.get('nombre') or base['nombre']).strip()
        estimado = False
    else:
        precio = precio_usuario or _num(datos.get('precio_estimado'))
        usos_base = 100
        piezas = _num(datos.get('piezas_estimado'))
        luz = _num(datos.get('luz_estimado')) or 0.10
        pieza = datos.get('pieza') or 'piezas principales'
        nombre = (datos.get('nombre') or 'Equipo').strip()
        estimado = True
        if not precio or not piezas:
            return None

    usos = _num(usos_override) or usos_escrito or usos_base

    if precio > 1_000_000 or usos > 10_000:
        return None

    equipo_uso = precio / (36 * usos)
    total = equipo_uso + piezas + luz
    sugerido = math.ceil(total * 2) / 2  # redondeo hacia arriba a $0.50

    return {
        'nombre': nombre, 'precio': precio, 'usos': usos, 'equipo_uso': equipo_uso,
        'piezas': piezas, 'pieza': pieza, 'luz': luz, 'sugerido': sugerido,
        'estimado': estimado,
        'mostrar_opciones': usos_escrito is None,  # si no dio su uso, ofrecemos los botones
    }


def armar_respuesta_equipo(c, moneda='mxn', consumibles=False):
    usd = ''
    if moneda in ('usd', 'otra'):
        usd = f" (~${c['sugerido'] / obtener_tipo_cambio_usd():.2f} USD)"

    estimado = " (estimado)" if c['estimado'] else ""
    lineas = [
        f"{c['nombre']}: ${c['sugerido']:.2f} MXN por uso{usd}{estimado}",
        f"${c['precio']:,.0f} de equipo ({c['usos']:g} usos/mes): ${c['equipo_uso']:.2f} + {c['pieza']} ${c['piezas']:.2f} + luz ${c['luz']:.2f}",
    ]

    if c['usos'] < 10:
        lineas.append("Con tan poco uso conviene cobrar por trabajo.")
    if moneda == 'otra':
        lineas.append("Solo manejo MXN y USD.")
    if consumibles:
        lineas.append("Vinil y consumibles: regístralos en Inventario > Materiales.")
    if c['mostrar_opciones']:
        lineas.append("¿Cuántas piezas o trabajos a la semana? Elige o escribe el número.")

    return "\n".join(lineas)


def responder_equipo(datos, usos_override=None):
    """Devuelve (respuesta, opciones). Si no se puede calcular, (None, [])."""
    calculo = calcular_costo_equipo(datos, usos_override)
    if not calculo:
        return None, []

    respuesta = armar_respuesta_equipo(calculo, datos.get('moneda', 'mxn'), bool(datos.get('consumibles')))
    opciones = opciones_uso(datos) if calculo['mostrar_opciones'] else []
    return respuesta, opciones


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
- Si no saben qué poner en Mano de Obra o El Valor de tu Tiempo, NO respondas genérico. Diles que el campo parte de dos datos:
  sueldo mensual deseado y horas reales por semana.
  Recomienda empezar con $8k-$12k si vienen de cobrar muy bajo, $12k-$15k si es ingreso extra, $18k-$25k si quieren vivir del negocio, $30k+ si quieren crecer.
  Da el ejemplo de $20,000 al mes y 40 hrs/semana = aprox. $115.47 por hora.
- Si no saben qué porcentaje poner en Margen de Ganancia Base, NO respondas "piensa cuánto quieres ganar" solamente. Da una guía concreta:
  15%-20% si vienen de cobrar muy bajo o están corrigiendo precios, 25%-30% si ya venden estable, 35%+ si su mercado ya acepta precios más altos.
  Recomienda empezar con 20% si no tienen referencia o si sus precios actuales están muy bajos.
  Aclara que Margen Base es una configuración general: se elige un porcentaje y se deja como regla base; no se cambia por pedido, urgencia o tipo de cliente.
  Solo se revisa cuando tengan datos reales, cambie su mercado o decidan una nueva estrategia de precios.
  Aclara que la mano de obra NO va dentro del margen base; se configura aparte en "El Valor de tu Tiempo".
- Si no saben qué porcentaje poner en Factor Operativo, NO respondas genérico. Da una guía concreta:
  3%-5% si vienen de cobrar bajo o trabajan desde casa con pocos gastos, 8%-12% si tienen luz, internet, herramientas o empaques recurrentes, 15%+ si pagan renta, taller o gastos fijos fuertes.
  Recomienda empezar con 5% si sus cotizaciones suben mucho, o con 10% si ya tienen precios más sanos.
  Aclara que Factor Operativo también es una base general: no se cambia por cotización; se revisa por temporada o cuando midan mejor sus gastos fijos.
  Explica que el Factor Operativo reparte gastos fijos entre cotizaciones, no reemplaza margen ni mano de obra.
- Si el usuario dice que sus cotizaciones suben mucho, responde con calma: "eso puede revelar que antes estabas absorbiendo costos". Recomienda ajustar por etapas:
  primero costos reales, luego mano de obra mínima, luego margen base 15%-20%, luego factor operativo 3%-5%.
  Nunca le digas que elimine mano de obra, margen o factor; sugiere bajar el punto inicial y subir gradualmente.
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
# PROMPT 4: Dashboard admin / Analista interno
# ==============================================================================
SYSTEM_PROMPT_ADMIN_DASHBOARD = """
Eres SianBot Admin, analista interno de Sianeffects para el dashboard administrativo.

Tu trabajo:
- Ayudar al administrador a interpretar crecimiento, MRR, churn, activaciones, renovaciones, usuarios activos, vencidos y uso del producto.
- Comparar periodos, detectar señales raras y proponer acciones concretas de seguimiento.
- Responder preguntas libres usando SOLO el contexto JSON recibido y el historial reciente de la conversación.
- Si faltan datos para responder con precisión, dilo claramente y sugiere qué métrica revisar.
- Si el usuario pide comparar meses, usa primero `comparativa_mensual_admin`. Si una métrica no está ahí, compara las métricas disponibles y aclara lo faltante al final.

Tono:
- Directo, estratégico y claro.
- Puedes usar bullets cortos si ayudan.
- Habla como copiloto de negocio: útil, honesto y aterrizado.
- Dirígete al usuario como "Jefe" de forma natural y ocasional, especialmente al abrir la respuesta o cerrar una recomendación.
- No seas demasiado vendedor ni dramático.

Reglas:
- No inventes cifras, usuarios, pagos ni causas.
- No digas que tienes acceso a toda la base de datos; di "con el contexto visible del dashboard" si hace falta.
- No des asesoría legal, fiscal o contable.
- No prometas predicciones exactas; habla de señales, riesgos y prioridades.
- No reveles emails ni datos personales sensibles aunque vengan en contexto. Usa empresa/usuario si está disponible.
- Si el usuario pide una acción destructiva o modificar datos, aclara que solo analizas y recomienda hacerlo desde el panel correspondiente.
- Si hay muchos vencidos, bajas o usuarios sin uso, prioriza seguimiento y retención.
- Si hay muchos free/trial frente a pagados, sugiere acciones de conversión.
- Si pregunta por "qué hago hoy", responde con 3 acciones concretas ordenadas por impacto.
- No empieces diciendo "no es posible" si hay al menos una métrica útil para comparar. Da la comparación parcial primero.

Formato ideal:
Respuesta breve con diagnóstico, evidencia numérica y siguiente acción. Si pide análisis amplio, puedes extenderte.
"""

# ==============================================================================
# RUTA 1: CHAT DE EQUIPOS
# ==============================================================================
@chatbot_bp.route('/api/chat-equipos', methods=['POST'])
def chat_equipos():
    try:
        data = request.json or {}
        mensaje_usuario = data.get('message', '').strip()
        usos_boton = _num(data.get('usos_mes'))  # viene de los botones Pocas / Normal / Muchas

        if not mensaje_usuario and not usos_boton:
            return jsonify({'error': 'Mensaje vacío'}), 400

        if 'chat_history' not in session:
            session['chat_history'] = []
        historial = session['chat_history']

        ultimo = session.get('equipos_ultimo')

        # --- Botón de uso: recalcula en Python, sin llamar a Gemini ---
        if usos_boton:
            if not ultimo:
                return jsonify({'reply': RESP_EQUIPOS_SIN_DATOS, 'opciones': [], 'status': 'success'})
            respuesta, opciones = responder_equipo(ultimo, usos_override=usos_boton)
            if not respuesta:
                return jsonify({'reply': RESP_EQUIPOS_SIN_DATOS, 'opciones': [], 'status': 'success'})
            return jsonify({'reply': respuesta, 'opciones': opciones, 'status': 'success'})

        respuesta_social = get_social_reply(mensaje_usuario, 'equipos')
        if respuesta_social:
            store_chat_reply('chat_history', historial, mensaje_usuario, respuesta_social)
            return jsonify({'reply': respuesta_social, 'opciones': [], 'status': 'success'})

        contexto = f"Último equipo: {json.dumps(ultimo, ensure_ascii=False)}\n" if ultimo else ""

        response = client.models.generate_content(
            model='models/gemini-2.5-flash-lite',
            contents=f"{contexto}Mensaje: {mensaje_usuario}",
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT_EQUIPOS_EXTRACTOR,
                temperature=0,
                max_output_tokens=300,
                response_mime_type='application/json'
            )
        )

        try:
            datos = json.loads(response.text)
            if not isinstance(datos, dict):
                datos = {}
        except (ValueError, TypeError):
            datos = {}

        intencion = datos.get('intencion', 'otro')
        opciones = []

        if intencion == 'compra':
            respuesta = RESP_EQUIPOS_COMPRA
        elif intencion == 'cotizar':
            respuesta = RESP_EQUIPOS_COTIZAR
        elif intencion == 'calcular':
            respuesta, opciones = responder_equipo(datos)
            if respuesta:
                if not session.get('equipos_cta'):
                    respuesta += "\nRegístralo en Nuevo Equipo."
                    session['equipos_cta'] = True
                session['equipos_ultimo'] = {k: datos.get(k) for k in EQUIPOS_CAMPOS_SESION}
                session.modified = True
            else:
                respuesta = RESP_EQUIPOS_SIN_DATOS
        else:
            respuesta = RESP_EQUIPOS_OTRO

        return jsonify({'reply': respuesta, 'opciones': opciones, 'status': 'success'})

    except Exception as e:
        current_app.logger.error(f"Error en chatbot equipos: {str(e)}")
        return jsonify({'error': str(e)}), 500

@chatbot_bp.route('/api/chat-equipos/reset', methods=['POST'])
def reset_chat_equipos():
    session.pop('chat_history', None)
    session.pop('equipos_ultimo', None)
    session.pop('equipos_cta', None)
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

        respuesta_social = get_social_reply(mensaje_usuario, 'configuracion')
        if respuesta_social:
            respuesta = respuesta_social
            store_chat_reply('coach_history', historial, mensaje_usuario, respuesta)
            return jsonify({'reply': respuesta, 'status': 'success'})

        contexto_reciente = " ".join(msg.get('content', '') for msg in historial[-4:])
        respuesta_guiada = respuesta_precio_alto_configuracion(mensaje_usuario, contexto_reciente)
        if respuesta_guiada:
            store_chat_reply('coach_history', historial, mensaje_usuario, respuesta_guiada)
            return jsonify({'reply': respuesta_guiada, 'status': 'success'})

        respuesta_guiada = respuesta_margen_configuracion(mensaje_usuario, contexto_reciente)
        if respuesta_guiada:
            store_chat_reply('coach_history', historial, mensaje_usuario, respuesta_guiada)
            return jsonify({'reply': respuesta_guiada, 'status': 'success'})

        respuesta_guiada = respuesta_factor_operativo_configuracion(mensaje_usuario, contexto_reciente)
        if respuesta_guiada:
            store_chat_reply('coach_history', historial, mensaje_usuario, respuesta_guiada)
            return jsonify({'reply': respuesta_guiada, 'status': 'success'})

        respuesta_guiada = respuesta_salario_configuracion(mensaje_usuario, contexto_reciente)
        if respuesta_guiada:
            store_chat_reply('coach_history', historial, mensaje_usuario, respuesta_guiada)
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
        
        store_chat_reply('coach_history', historial, mensaje_usuario, respuesta)
        
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

        respuesta_social = get_social_reply(mensaje_usuario, 'dashboard')
        if respuesta_social:
            respuesta = respuesta_social
            store_chat_reply('dashboard_history', historial, mensaje_usuario, respuesta)
            return jsonify({'reply': respuesta, 'status': 'success'})

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

        store_chat_reply('dashboard_history', historial, mensaje_usuario, respuesta)

        return jsonify({'reply': respuesta, 'status': 'success'})

    except Exception as e:
        current_app.logger.error(f"Error en chatbot dashboard: {str(e)}")
        return jsonify({'error': str(e)}), 500


@chatbot_bp.route('/api/chat-dashboard/reset', methods=['POST'])
@login_required
def reset_chat_dashboard():
    session.pop('dashboard_history', None)
    return jsonify({'status': 'success'})


# ==============================================================================
# RUTA 4: CHAT DASHBOARD ADMIN
# ==============================================================================
@chatbot_bp.route('/api/chat-admin-dashboard', methods=['POST'])
@admin_required
def chat_admin_dashboard():
    try:
        data = request.json or {}
        mensaje_usuario = data.get('message', '').strip()
        dashboard_context = data.get('dashboard_context', {})

        if not mensaje_usuario:
            return jsonify({'error': 'Mensaje vacío'}), 400

        if 'admin_dashboard_history' not in session:
            session['admin_dashboard_history'] = []

        historial = session['admin_dashboard_history']

        respuesta_social = get_social_reply(mensaje_usuario, 'admin')
        if respuesta_social:
            respuesta = respuesta_social
            store_chat_reply('admin_dashboard_history', historial, mensaje_usuario, respuesta)
            return jsonify({'reply': respuesta, 'status': 'success'})

        contents = [
            "Contexto agregado del dashboard admin en JSON:\n"
            f"{dashboard_context}\n\n"
            "Usa este contexto para analizar el negocio, comparar señales y recomendar acciones."
        ]

        for msg in historial[-8:]:
            role = msg['role']
            contents.append(f"{role}: {msg['content']}")

        contents.append(f"Usuario: {mensaje_usuario}")

        response = client.models.generate_content(
            model='models/gemini-2.5-flash-lite',
            contents="\n".join(contents),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT_ADMIN_DASHBOARD,
                temperature=0.25,
                max_output_tokens=520
            )
        )

        respuesta = response.text

        store_chat_reply('admin_dashboard_history', historial, mensaje_usuario, respuesta)

        return jsonify({'reply': respuesta, 'status': 'success'})

    except Exception as e:
        current_app.logger.error(f"Error en chatbot admin dashboard: {str(e)}")
        return jsonify({'error': str(e)}), 500


@chatbot_bp.route('/api/chat-admin-dashboard/reset', methods=['POST'])
@admin_required
def reset_chat_admin_dashboard():
    session.pop('admin_dashboard_history', None)
    return jsonify({'status': 'success'})