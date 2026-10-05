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


# ==============================================================================
# CONFIGURACIÓN GEMINI
# ==============================================================================

client = genai.Client(api_key=os.environ.get('GEMINI_API_KEY'))

MAX_HISTORY_MESSAGES = 6
MAX_HISTORY_CHARS = 500


# ==============================================================================
# TIPO DE CAMBIO
# ==============================================================================

# Tipo de cambio USD->MXN con caché de 6 horas.
# La variable de entorno TIPO_CAMBIO_USD queda solo como respaldo si la API falla.

_tc_cache = {
    'valor': float(os.environ.get('TIPO_CAMBIO_USD', '18')),
    'ts': 0
}


def obtener_tipo_cambio_usd():
    ahora = time.time()

    if ahora - _tc_cache['ts'] < 6 * 3600:
        return _tc_cache['valor']

    try:
        with urllib.request.urlopen(
            'https://open.er-api.com/v6/latest/USD',
            timeout=3
        ) as r:
            data = json.loads(r.read().decode())

        mxn = float(data['rates']['MXN'])

        if 10 < mxn < 40:
            _tc_cache['valor'] = mxn

        _tc_cache['ts'] = ahora

    except Exception as e:
        current_app.logger.warning(
            f"TIPO_CAMBIO_WARNING: no se pudo actualizar, "
            f"uso {_tc_cache['valor']} - {e}"
        )

        # Reintenta en 5 minutos.
        _tc_cache['ts'] = ahora - 6 * 3600 + 300

    return _tc_cache['valor']


# ==============================================================================
# HISTORIAL
# ==============================================================================

def compact_chat_history(
    history,
    max_messages=MAX_HISTORY_MESSAGES,
    max_chars=MAX_HISTORY_CHARS
):
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
    return re.sub(
        r'[^\wáéíóúüñ\s]',
        '',
        message.lower()
    ).strip()


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

            'thanks': (
                "Con gusto 😊 Aquí estoy cuando quieras revisar un equipo o costo."
            ),
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

            'thanks': (
                "Con gusto 😊 Cuando quieras seguimos afinando tu configuración."
            ),
        },

        'dashboard': {
            'greeting': (
                "¡Hola! Qué gusto leerte 😊\n"
                "Estoy aquí contigo para revisar el panel sin hacerlo pesado."
            ),

            'how_are_you': (
                "Estoy bien, gracias por preguntar 😊\n"
                "Lista para ayudarte a entender qué está pasando con tus ventas, "
                "con calma y sin enredos."
            ),

            'thanks': (
                "Con gusto 😊 Aquí sigo si quieres revisar ventas, utilidad "
                "o algo que se vea raro en tu panel."
            ),
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

            'thanks': (
                "Con gusto, Jefe 😊 Cuando quieras seguimos con el análisis."
            ),
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
    history.append({
        'role': 'Usuario',
        'content': user_message
    })

    history.append({
        'role': 'Asistente',
        'content': reply
    })

    session[session_key] = compact_chat_history(history)
    session.modified = True


# ==============================================================================
# CONFIGURACIÓN - UTILIDADES
# ==============================================================================

def extraer_monto_salario(mensaje):
    match_mil = re.search(
        r'\$?\s*(\d+(?:[.,]\d+)?)\s*mil\b',
        mensaje,
        re.IGNORECASE
    )

    if match_mil:
        return int(
            float(match_mil.group(1).replace(',', '.')) * 1000
        )

    match_monto = re.search(
        r'\$?\s*(\d{4,6}(?:[,.]\d{3})*)',
        mensaje
    )

    if match_monto:
        return int(
            match_monto.group(1)
            .replace(',', '')
            .replace('.', '')
        )

    return None


def extraer_horas_semanales(mensaje):
    match = re.search(
        r'(\d{1,2})\s*(?:h|hr|hrs|horas)',
        mensaje,
        re.IGNORECASE
    )

    if match:
        return int(match.group(1))

    return None


def respuesta_salario_configuracion(
    mensaje_usuario,
    contexto_reciente=''
):
    """Da una guía concreta cuando el usuario no sabe qué sueldo mensual poner."""

    mensaje = mensaje_usuario.lower()
    contexto = contexto_reciente.lower()

    habla_de_otro_campo = any(
        palabra in mensaje
        for palabra in [
            'margen',
            'margen base',
            'margen de ganancia',
            'factor operativo'
        ]
    )

    if habla_de_otro_campo:
        return None

    habla_de_salario = any(
        palabra in mensaje
        for palabra in [
            'salario',
            'sueldo',
            'ganar',
            'ingreso',
            'cobrar',
            'valor de mi tiempo',
            'valor de tu tiempo',
            'mano de obra',
            'hora',
            'costo por hora'
        ]
    ) or any(
        palabra in contexto
        for palabra in [
            'salario',
            'sueldo',
            'valor de tu tiempo',
            'mano de obra'
        ]
    )

    pide_guia = any(
        frase in mensaje
        for frase in [
            'no se',
            'no sé',
            'que valor',
            'qué valor',
            'cuanto pongo',
            'cuánto pongo',
            'que pongo',
            'qué pongo',
            'ayudame',
            'ayúdame',
            'calcular',
            'que valores',
            'qué valores',
            'valores debo poner'
        ]
    )

    salario = extraer_monto_salario(mensaje_usuario)
    horas = extraer_horas_semanales(mensaje_usuario) or 40

    if salario and habla_de_salario:
        horas_mes = horas * 4.33
        costo_hora = salario / horas_mes if horas_mes else 0

        texto_horas = (
            "Si trabajas otra cantidad de horas, cambia ese campo "
            "y Sianeffects recalcula tu hora."
            if extraer_horas_semanales(mensaje_usuario)
            else
            "Usé 40 hrs/semana como referencia; si trabajas menos, "
            "cambia ese campo y tu hora sube."
        )

        return (
            f"Perfecto. Pon Sueldo Deseado: ${salario:,.0f} MXN "
            f"y Horas por semana: {horas}.\n"
            f"Así tu hora vale aprox. ${costo_hora:,.2f} MXN.\n"
            f"{texto_horas}\n"
            "📍 CONFIGURACIÓN → MI NEGOCIO → El Valor de tu Tiempo."
        )

    if not habla_de_salario or not pide_guia:
        return None

    return (
        "Sí. Para mano de obra, empieza gradual: si hoy cobras muy bajo, "
        "usa una meta realista y súbela por etapas.\n"
        "Guía rápida: ajuste suave $8k-$12k, ingreso extra $12k-$15k, "
        "vivir del negocio $18k-$25k.\n"
        "Ejemplo: $20,000 al mes y 40 hrs/semana = aprox. $115.47 por hora.\n"
        "Si tus cotizaciones suben demasiado, baja la meta inicial, "
        "no elimines tu mano de obra.\n"
        "📍 CONFIGURACIÓN → MI NEGOCIO → El Valor de tu Tiempo."
    )


def respuesta_margen_configuracion(
    mensaje_usuario,
    contexto_reciente=''
):
    """Da una guía concreta cuando el usuario no sabe qué margen base poner."""

    texto = mensaje_usuario.lower()

    habla_de_margen = any(
        palabra in texto
        for palabra in [
            'margen base',
            'margen de ganancia',
            'margen',
            'ganancia base',
            'porcentaje',
            'por ciento',
            '%'
        ]
    )

    pide_guia = any(
        frase in texto
        for frase in [
            'no se',
            'no sé',
            'que porcentaje',
            'qué porcentaje',
            'cuanto pongo',
            'cuánto pongo',
            'que pongo',
            'qué pongo',
            'ayudame',
            'ayúdame',
            'definirlo',
            'recomiendas',
            'recomiendas poner',
            'empezar'
        ]
    )

    if not habla_de_margen or not pide_guia:
        return None

    return (
        "Sí. Si vienes de cobrar bajo, pon 20% como margen base y úsalo fijo por una etapa.\n"
        "Guía rápida: 15%-20% si estás corrigiendo precios, "
        "25%-30% si ya vendes estable, 35%+ si tu mercado ya acepta precios más altos.\n"
        "No incluye tu mano de obra; esa va aparte en El Valor de tu Tiempo.\n"
        "Elige uno y déjalo como regla general; no lo cambies por pedido. "
        "Revísalo solo cuando tengas datos reales o cambie tu estrategia de precios.\n"
        "📍 CONFIGURACIÓN → MI NEGOCIO → Margen de Ganancia Base."
    )


def respuesta_factor_operativo_configuracion(
    mensaje_usuario,
    contexto_reciente=''
):
    """Da una guía concreta cuando el usuario no sabe qué factor operativo poner."""

    texto = mensaje_usuario.lower()

    habla_de_factor = any(
        palabra in texto
        for palabra in [
            'factor operativo',
            'gastos operativos',
            'gastos fijos',
            'operativo',
            'renta',
            'luz',
            'internet',
            'agua'
        ]
    )

    pide_guia = any(
        frase in texto
        for frase in [
            'no se',
            'no sé',
            'que porcentaje',
            'qué porcentaje',
            'cuanto pongo',
            'cuánto pongo',
            'que pongo',
            'qué pongo',
            'ayudame',
            'ayúdame',
            'definirlo',
            'recomiendas',
            'recomiendas poner',
            'empezar'
        ]
    )

    if not habla_de_factor or not pide_guia:
        return None

    return (
        "Sí. Si tus precios ya suben mucho, pon 5% de Factor Operativo como base fija para empezar.\n"
        "Guía rápida: 3%-5% para ajuste suave, 8%-12% si pagas luz, "
        "internet o herramientas, 15%+ si tienes renta o taller.\n"
        "Esto ayuda a que cada cotización cargue una parte de tus gastos fijos.\n"
        "No lo cambies por cotización; revísalo por temporada o cuando ya tengas "
        "tus gastos fijos mejor medidos.\n"
        "📍 CONFIGURACIÓN → MI NEGOCIO → Factor Operativo."
    )


def respuesta_precio_alto_configuracion(
    mensaje_usuario,
    contexto_reciente=''
):
    """Acompaña cuando el usuario siente que sus cotizaciones subieron demasiado."""

    texto = f"{contexto_reciente} {mensaje_usuario}".lower()

    habla_de_precio_alto = any(
        frase in texto
        for frase in [
            'sube mucho',
            'subió mucho',
            'subio mucho',
            'muy caro',
            'se disparó',
            'se disparo',
            'precio alto',
            'precios altos',
            'cotizacion alta',
            'cotización alta',
            'cotizaciones altas',
            'me sube bastante',
            'sube bastante',
            'demasiado caro'
        ]
    )

    if not habla_de_precio_alto:
        return None

    return (
        "Sí, puede pasar. Muchas veces el precio sube porque antes estabas "
        "absorbiendo mano de obra, gastos fijos o margen sin darte cuenta.\n"
        "Hazlo por etapas: costos reales primero, mano de obra mínima, "
        "margen 15%-20% y factor operativo 3%-5%.\n"
        "Cuando tus clientes se acostumbren y tengas más claridad, sube poco a poco.\n"
        "La meta no es encarecer de golpe; es dejar de vender con pérdida."
    )


# ==============================================================================
# EQUIPOS
# ==============================================================================

EQUIPOS_BASE = {
    'plotter': {
        'precio': 5500,
        'usos': 100,
        'piezas': 0.50,
        'luz': 0.10,
        'pieza': 'cuchilla',
        'nombre': 'Plotter de corte'
    },

    'plancha': {
        'precio': 3000,
        'usos': 100,
        'piezas': 0.30,
        'luz': 0.20,
        'pieza': 'resistencia',
        'nombre': 'Plancha o prensa térmica'
    },

    'sublimacion': {
        'precio': 6000,
        'usos': 150,
        'piezas': 1.50,
        'luz': 0.10,
        'pieza': 'cabezal',
        'nombre': 'Impresora de sublimación'
    },

    'dtf': {
        'precio': 20000,
        'usos': 300,
        'piezas': 4.00,
        'luz': 0.60,
        'pieza': 'cabezal e inyectores',
        'nombre': 'Impresora DTF A3'
    },

    'uv': {
        'precio': 90000,
        'usos': 200,
        'piezas': 7.00,
        'luz': 1.00,
        'pieza': 'cabezal y lámpara',
        'nombre': 'Impresora UV'
    },

    'laser_diodo': {
        'precio': 8000,
        'usos': 100,
        'piezas': 0.30,
        'luz': 0.10,
        'pieza': 'módulo láser',
        'nombre': 'Láser de diodo'
    },

    'laser_co2': {
        'precio': 12000,
        'usos': 100,
        'piezas': 1.30,
        'luz': 0.60,
        'pieza': 'tubo y lentes',
        'nombre': 'Láser CO2 40W'
    },

    'coser': {
        'precio': 5000,
        'usos': 150,
        'piezas': 0.20,
        'luz': 0.02,
        'pieza': 'agujas',
        'nombre': 'Máquina de coser'
    },

    'bordadora': {
        'precio': 30000,
        'usos': 150,
        'piezas': 1.00,
        'luz': 0.10,
        'pieza': 'agujas',
        'nombre': 'Bordadora'
    },

    'laminadora': {
        'precio': 1500,
        'usos': 100,
        'piezas': 0.10,
        'luz': 0.10,
        'pieza': 'rodillos',
        'nombre': 'Laminadora'
    },

    'guillotina': {
        'precio': 1500,
        'usos': 100,
        'piezas': 0.30,
        'luz': 0.00,
        'pieza': 'cuchilla',
        'nombre': 'Guillotina'
    },
}


EQUIPOS_CAMPOS_SESION = (
    'tipo',
    'nombre',
    'precio',
    'usos_mes',
    'usos_semana',
    'moneda',
    'consumibles',
    'precio_estimado',
    'piezas_estimado',
    'luz_estimado',
    'pieza'
)


# ==============================================================================
# PROMPT EQUIPOS
# ==============================================================================

SYSTEM_PROMPT_EQUIPOS_EXTRACTOR = """
Eres el motor de interpretación del módulo "Equipos" de Sianeffects.

Tu salida será consumida directamente por Python para calcular el costo por uso
de un equipo.

IMPORTANTE:

- Responde SOLO con un JSON válido.
- No escribas explicaciones.
- No respondas directamente al usuario.
- No rechaces el mensaje.
- No inventes datos que el usuario no haya proporcionado, excepto cuando se
  indiquen reglas explícitas para estimaciones.
- Tu trabajo es ENTENDER lo que el usuario quiso decir y convertirlo en datos.
- Python se encargará de realizar los cálculos y de responder al usuario.


========================================
CONTEXTO DEL CHAT
========================================

Este chat pertenece al módulo "Equipos" de Sianeffects.

Su objetivo es ayudar al usuario a calcular cuánto le cuesta utilizar o desgastar
un equipo de producción por cada uso.

El usuario puede escribir de forma completamente natural.

El usuario NO necesita escribir una pregunta completa.

Si el asistente le pidió:

"Dime qué equipo tienes"

y el usuario responde:

"Cricut Explore 4"

eso debe interpretarse como una intención válida de CALCULAR el costo por uso
de ese equipo.

También pueden existir:

- errores ortográficos;
- nombres incompletos;
- marcas;
- modelos;
- abreviaturas;
- palabras mezcladas en español e inglés;
- descripciones informales.

Debes interpretar la intención del usuario, no exigir una frase específica.


========================================
REGLA PRINCIPAL DE INTERPRETACIÓN
========================================

Primero entiende qué quiso decir el usuario.

Después determina la intención.

Después extrae los datos disponibles.

NO debes decidir que la intención es "otro" simplemente porque:

- el usuario no hizo una pregunta;
- faltan datos como precio o usos;
- escribió solamente el nombre del equipo;
- escribió el nombre del equipo con errores;
- escribió solamente una marca;
- escribió solamente un modelo;
- escribió una descripción corta del equipo.

Si claramente está mencionando un equipo de producción dentro de este chat,
la intención normalmente debe ser "calcular".


========================================
INTENCIONES
========================================

El campo "intencion" SOLO puede ser:

- "calcular"
- "compra"
- "cotizar"
- "otro"


----------------------------------------
CALCULAR
----------------------------------------

Usa "calcular" cuando el usuario:

- menciona un equipo que tiene;
- indica el nombre o modelo de un equipo;
- proporciona datos de un equipo;
- corrige datos del equipo anterior;
- proporciona el precio que pagó;
- proporciona cuántos usos realiza;
- proporciona usos por día, semana o mes;
- pregunta cuánto le cuesta utilizar el equipo;
- pregunta cuánto debería considerar por desgaste;
- pregunta cuánto cuesta cada uso;
- pide calcular el costo de uso;
- pide explicar el costo de uso;
- agrega consumibles que quiere considerar;
- cambia la moneda del cálculo;
- continúa hablando del último equipo identificado.

NO necesita utilizar palabras como "calcular", "desgaste" o "costo por uso".

Ejemplos:

"Cricut Explore 4"
=> calcular

"cricut exolore 4"
=> calcular

"tengo una cricut"
=> calcular

"mi silhouette"
=> calcular

"silouette cameo 5"
=> calcular

"una impresora para sublimar"
=> calcular

"laser de 10w"
=> calcular

"me costó 8500"
=> calcular usando el último equipo

"la uso 20 veces por semana"
=> calcular usando el último equipo

"y en dólares"
=> calcular usando el último equipo

"también quiero considerar el vinil"
=> calcular usando el último equipo


----------------------------------------
COMPRA
----------------------------------------

Usa "compra" SOLO cuando el usuario está preguntando por comprar un equipo.

Ejemplos:

"¿Cuánto cuesta comprar una Cricut?"
=> compra

"¿Dónde puedo comprar una Cricut?"
=> compra

"quiero comprar una Cricut Explore 4"
=> compra

"¿En cuánto anda una Silhouette Cameo?"
=> compra

IMPORTANTE:

Si el usuario solamente escribe el nombre del equipo, NO es "compra".

"Cricut Explore 4"
=> calcular

"Silhouette Cameo 5"
=> calcular


----------------------------------------
COTIZAR
----------------------------------------

Usa "cotizar" cuando el usuario pregunta cómo cobrar, cotizar o vender un
trabajo/producto realizado con el equipo.

Ejemplos:

"¿Cuánto le cobro al cliente por este trabajo?"
=> cotizar

"¿Cuánto debería cobrar por una playera?"
=> cotizar

"¿Cómo cotizo un trabajo de sublimación?"
=> cotizar

"¿Cuánto cobro por cortar este diseño?"
=> cotizar

Esto es diferente a calcular cuánto cuesta utilizar el equipo.

"¿Cuánto me cuesta usar mi Cricut?"
=> calcular

"¿Cuánto le cobro al cliente por usar mi Cricut?"
=> cotizar


----------------------------------------
OTRO
----------------------------------------

Usa "otro" SOLO cuando el mensaje realmente no corresponde al propósito del
módulo de Equipos.

Ejemplos:

"¿Cuál es mejor, Cricut o Silhouette?"
=> otro

"¿La Cricut Explore 4 es buena?"
=> otro

"¿Qué color me recomiendas?"
=> otro

"hola"
=> otro

"gracias"
=> otro

"¿Cómo hago una página web?"
=> otro

IMPORTANTE:

NO uses "otro" únicamente porque faltan datos.

Si el usuario menciona claramente un equipo, intenta interpretarlo como
"calcular".


========================================
INTERPRETACIÓN DE ERRORES
========================================

Debes tener libertad para interpretar lo que el usuario quiso decir.

El usuario puede:

- escribir mal;
- omitir letras;
- cambiar letras;
- escribir sin acentos;
- usar abreviaturas;
- mezclar español e inglés;
- escribir el nombre incompleto;
- escribir solamente la marca;
- escribir solamente el modelo;
- escribir de forma informal;
- utilizar nombres comerciales;
- describir el equipo sin conocer su nombre exacto.

Debes corregir mentalmente errores evidentes y determinar la interpretación
más probable.

NO debes exigir que el usuario escriba correctamente el nombre del equipo.

Ejemplos:

"cricut exolore 4"
=> Cricut Explore 4

"cricut explore"
=> Cricut Explore

"criccut"
=> Cricut

"silouette"
=> Silhouette

"camio"
=> Cameo

"plotter cricut"
=> Plotter / Cricut

"impresora sublimacion"
=> Impresora de sublimación

"impresora para sublimar"
=> Impresora de sublimación

"laser 10w"
=> Láser

"maquina para cortar vinil"
=> Plotter de corte


========================================
EQUIPOS CONOCIDOS
========================================

El campo "tipo" debe ser uno de:

- "plotter"
- "plancha"
- "sublimacion"
- "dtf"
- "uv"
- "laser_diodo"
- "laser_co2"
- "coser"
- "bordadora"
- "laminadora"
- "guillotina"
- "otro"


REGLAS DE NORMALIZACIÓN:

- Cricut => plotter
- Silhouette => plotter
- Cricut Explore => plotter
- Cricut Maker => plotter
- Cricut Joy => plotter
- Silhouette Cameo => plotter
- Silhouette Portrait => plotter
- Silhouette Curio => plotter

Las marcas y modelos pueden venir escritos incorrectamente.
Corrige errores evidentes.


========================================
NOMBRE
========================================

"nombre" debe contener el nombre del equipo que el usuario quiso indicar.

Debe ser:

- limpio;
- corto;
- entendible;
- conservando marca y modelo cuando sean conocidos.

Ejemplos:

"Cricut Explore 4"
=> "Cricut Explore 4"

"cricut exolore 4"
=> "Cricut Explore 4"

"silouette camio 5"
=> "Silhouette Cameo 5"

"una cricut"
=> "Cricut"

"laser de 10w"
=> "Láser de 10W"

Si no se puede determinar el nombre exacto pero sí el tipo, utiliza el nombre
más razonable sin inventar un modelo específico.


========================================
PRECIO
========================================

Campo:

"precio"

Debe contener el precio que el usuario pagó por el equipo, en número.

Si el usuario NO indicó cuánto pagó:

"precio": null

NO inventes el precio pagado por el usuario.

Ejemplos:

"me costó 8500"
=> precio = 8500

"la compré en 12,000 pesos"
=> precio = 12000

"me salió en $8,500"
=> precio = 8500

Si el usuario solamente dice:

"Cricut Explore 4"

=> precio = null


========================================
USOS POR MES
========================================

Campo:

"usos_mes"

Debe contener la cantidad de usos por mes si el usuario la proporciona.

Ejemplos:

"la uso 20 veces al mes"
=> usos_mes = 20

"hago 50 trabajos al mes"
=> usos_mes = 50

Si no lo proporciona:

"usos_mes": null


========================================
USOS POR SEMANA
========================================

Campo:

"usos_semana"

Debe contener la cantidad de usos, piezas o trabajos por semana cuando el
usuario la indique.

Ejemplos:

"la uso 10 veces por semana"
=> usos_semana = 10

"hago 15 trabajos a la semana"
=> usos_semana = 15

"produzco 20 piezas por semana"
=> usos_semana = 20

Si el usuario indica usos por día y NO especifica cuántos días trabaja,
asume 6 días por semana.

Ejemplo:

"la uso 5 veces al día"
=> usos_semana = 30

Si el usuario dice:

"la uso 5 veces al día, 4 días a la semana"
=> usos_semana = 20

Si no proporciona usos:

"usos_semana": null


========================================
CORRECCIONES DEL ÚLTIMO EQUIPO
========================================

Si existe un "Último equipo", debes utilizarlo como contexto.

Si el usuario está corrigiendo o agregando información, conserva los datos
anteriores y modifica SOLO lo que el usuario cambió.

Ejemplo:

Último equipo:
{
  "tipo": "plotter",
  "nombre": "Cricut Explore 4",
  "precio": 8500,
  "usos_semana": 10
}

Usuario:
"ahora son 15 por semana"

Resultado conceptual:

{
  "tipo": "plotter",
  "nombre": "Cricut Explore 4",
  "precio": 8500,
  "usos_semana": 15
}

Otro ejemplo:

Último equipo:
{
  "tipo": "plotter",
  "nombre": "Cricut Explore 4",
  "precio": 8500,
  "usos_semana": 10
}

Usuario:
"me costó 9500"

Resultado conceptual:

{
  "tipo": "plotter",
  "nombre": "Cricut Explore 4",
  "precio": 9500,
  "usos_semana": 10
}

Otro ejemplo:

Último equipo:
{
  "tipo": "plotter",
  "nombre": "Cricut Explore 4",
  "precio": 8500,
  "usos_semana": 10
}

Usuario:
"y en dólares"

Resultado conceptual:

{
  "tipo": "plotter",
  "nombre": "Cricut Explore 4",
  "precio": 8500,
  "usos_semana": 10,
  "moneda": "usd"
}

No borres información anterior solamente porque el usuario no la repitió.


========================================
MONEDA
========================================

Campo:

"moneda"

Valores permitidos:

- "mxn"
- "usd"
- "otra"

Usa "usd" SOLO cuando el usuario indique explícitamente:

- dólares;
- USD;
- US dollars;
- dólares estadounidenses;
- precio en dólares;
- $US;
- una referencia inequívoca a moneda estadounidense.

NO asumas USD solamente porque:

- el texto esté en inglés;
- el equipo sea estadounidense;
- mencione Estados Unidos;
- la marca sea extranjera.

Si no especifica otra moneda:

"moneda": "mxn"

Si explícitamente indica otra moneda diferente de MXN o USD:

"moneda": "otra"


========================================
CONSUMIBLES
========================================

Campo:

"consumibles"

Debe ser true si el usuario pide considerar consumibles como:

- vinil;
- tinta;
- papel;
- tóner;
- material;
- consumibles;
- hojas;
- transfer;
- DTF;
- etc.

Debe ser false si no solicita incluir consumibles.

Ejemplo:

"quiero considerar también el vinil"
=> consumibles = true


========================================
EQUIPOS NO INCLUIDOS EN LA LISTA
========================================

Si el equipo claramente existe pero no corresponde a una categoría conocida,
utiliza:

"tipo": "otro"

En ese caso, si puedes identificar razonablemente el equipo, proporciona
estimaciones realistas para:

- "precio_estimado"
- "piezas_estimado"
- "luz_estimado"
- "pieza"

Las estimaciones deben ser números en MXN.

NO hagas divisiones.

Si no se puede identificar razonablemente el equipo:

"precio_estimado": null

"piezas_estimado": null

"luz_estimado": null

"pieza": null


========================================
REGLAS DE PRIORIDAD
========================================

Si un mensaje puede parecer ambiguo, utiliza esta prioridad:

1. Si el usuario está dando o corrigiendo información de un equipo
   => "calcular"

2. Si está mencionando un equipo dentro de este módulo
   => "calcular"

3. Si explícitamente quiere comprar un equipo
   => "compra"

4. Si pregunta cuánto cobrar, cotizar o vender un trabajo
   => "cotizar"

5. Si realmente no tiene relación con equipos/costos
   => "otro"


========================================
EJEMPLOS
========================================

Usuario:
"Cricut Explore 4"

Resultado:

{
  "intencion": "calcular",
  "tipo": "plotter",
  "nombre": "Cricut Explore 4",
  "precio": null,
  "usos_mes": null,
  "usos_semana": null,
  "moneda": "mxn",
  "consumibles": false,
  "precio_estimado": null,
  "piezas_estimado": null,
  "luz_estimado": null,
  "pieza": "cuchilla"
}


Usuario:
"cricut exolore 4"

Resultado:

{
  "intencion": "calcular",
  "tipo": "plotter",
  "nombre": "Cricut Explore 4",
  "precio": null,
  "usos_mes": null,
  "usos_semana": null,
  "moneda": "mxn",
  "consumibles": false,
  "precio_estimado": null,
  "piezas_estimado": null,
  "luz_estimado": null,
  "pieza": "cuchilla"
}


Usuario:
"tengo una cricut que me costó 8500"

Resultado:

{
  "intencion": "calcular",
  "tipo": "plotter",
  "nombre": "Cricut",
  "precio": 8500,
  "usos_mes": null,
  "usos_semana": null,
  "moneda": "mxn",
  "consumibles": false,
  "precio_estimado": null,
  "piezas_estimado": null,
  "luz_estimado": null,
  "pieza": "cuchilla"
}


Usuario:
"silouette camio 5, me costó 9000 y hago 20 trabajos a la semana"

Resultado:

{
  "intencion": "calcular",
  "tipo": "plotter",
  "nombre": "Silhouette Cameo 5",
  "precio": 9000,
  "usos_mes": null,
  "usos_semana": 20,
  "moneda": "mxn",
  "consumibles": false,
  "precio_estimado": null,
  "piezas_estimado": null,
  "luz_estimado": null,
  "pieza": "cuchilla"
}


Usuario:
"¿cuánto cuesta comprar una cricut explore 4?"

Resultado:

{
  "intencion": "compra",
  "tipo": "plotter",
  "nombre": "Cricut Explore 4",
  "precio": null,
  "usos_mes": null,
  "usos_semana": null,
  "moneda": "mxn",
  "consumibles": false,
  "precio_estimado": null,
  "piezas_estimado": null,
  "luz_estimado": null,
  "pieza": "cuchilla"
}


Usuario:
"¿cuánto le cobro al cliente por cortar 20 diseños?"

Resultado:

{
  "intencion": "cotizar",
  "tipo": "plotter",
  "nombre": null,
  "precio": null,
  "usos_mes": null,
  "usos_semana": 20,
  "moneda": "mxn",
  "consumibles": false,
  "precio_estimado": null,
  "piezas_estimado": null,
  "luz_estimado": null,
  "pieza": "cuchilla"
}


Usuario:
"¿la cricut es mejor que la silhouette?"

Resultado:

{
  "intencion": "otro",
  "tipo": "plotter",
  "nombre": "Cricut",
  "precio": null,
  "usos_mes": null,
  "usos_semana": null,
  "moneda": "mxn",
  "consumibles": false,
  "precio_estimado": null,
  "piezas_estimado": null,
  "luz_estimado": null,
  "pieza": "cuchilla"
}


========================================
FORMATO DE SALIDA
========================================

Siempre devuelve EXACTAMENTE un JSON válido con esta estructura:

{
  "intencion": "calcular|compra|cotizar|otro",
  "tipo": "plotter|plancha|sublimacion|dtf|uv|laser_diodo|laser_co2|coser|bordadora|laminadora|guillotina|otro",
  "nombre": null,
  "precio": null,
  "usos_mes": null,
  "usos_semana": null,
  "moneda": "mxn",
  "consumibles": false,
  "precio_estimado": null,
  "piezas_estimado": null,
  "luz_estimado": null,
  "pieza": null
}

No agregues campos adicionales.

No escribas Markdown.

No escribas explicaciones.

No escribas texto antes ni después del JSON.
"""


RESP_EQUIPOS_COMPRA = (
    "No te ayudo con precios de compra. Dime cuánto te costó "
    "y calculo su costo por uso."
)

RESP_EQUIPOS_COTIZAR = (
    "Eso se hace en Cotizador. Aquí calculo el costo por uso de tus equipos."
)

RESP_EQUIPOS_OTRO = (
    "No puedo ayudarte con eso. Solo calculo el costo por uso de equipos."
)

RESP_EQUIPOS_SIN_DATOS = (
    "No pude calcularlo. Dime el equipo y, si puedes, cuánto costó "
    "y cuántas veces lo usas a la semana."
)


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

    etiquetas = [
        ('Pocas', poco),
        ('Normal', normal),
        ('Muchas', mucho)
    ]

    return [
        {
            'label': f"{nombre} · {_texto_semana(usos)}",
            'usos_mes': usos
        }
        for nombre, usos in etiquetas
    ]


def calcular_costo_equipo(datos, usos_override=None):
    base = EQUIPOS_BASE.get(datos.get('tipo'))

    # Usos que escribió el usuario.
    # Si vienen por semana, se convierten a usos mensuales.
    usos_escrito = _num(datos.get('usos_mes'))

    if usos_escrito is None:
        semana = _num(datos.get('usos_semana'))

        if semana:
            usos_escrito = semana * 4.33

    if usos_escrito:
        usos_escrito = math.ceil(usos_escrito)

    precio_usuario = _num(datos.get('precio'))

    if base:
        precio = precio_usuario or base['precio']
        usos_base = base['usos']

        piezas = base['piezas']
        luz = base['luz']
        pieza = base['pieza']

        nombre = (
            datos.get('nombre')
            or base['nombre']
        ).strip()

        estimado = False

    else:
        precio = (
            precio_usuario
            or _num(datos.get('precio_estimado'))
        )

        usos_base = 100

        piezas = _num(datos.get('piezas_estimado'))
        luz = _num(datos.get('luz_estimado')) or 0.10
        pieza = datos.get('pieza') or 'piezas principales'

        nombre = (
            datos.get('nombre')
            or 'Equipo'
        ).strip()

        estimado = True

        if not precio or not piezas:
            return None

    usos = (
        _num(usos_override)
        or usos_escrito
        or usos_base
    )

    if precio > 1_000_000 or usos > 10_000:
        return None

    equipo_uso = precio / (36 * usos)

    total = equipo_uso + piezas + luz

    # Redondeo hacia arriba a $0.50
    sugerido = math.ceil(total * 2) / 2

    return {
        'nombre': nombre,
        'precio': precio,
        'usos': usos,
        'equipo_uso': equipo_uso,
        'piezas': piezas,
        'pieza': pieza,
        'luz': luz,
        'sugerido': sugerido,
        'estimado': estimado,
        # Cambia solo esta línea:
        'mostrar_opciones': (usos_escrito is None) and (usos_override is None),
    }


def armar_respuesta_equipo(
    c,
    moneda='mxn',
    consumibles=False
):
    usd = ''

    if moneda in ('usd', 'otra'):
        usd = (
            f" (~${c['sugerido'] / obtener_tipo_cambio_usd():.2f} USD)"
        )

    estimado = " (estimado)" if c['estimado'] else ""

    lineas = [
        (
            f"{c['nombre']}: "
            f"${c['sugerido']:.2f} MXN por uso"
            f"{usd}{estimado}"
        ),

        (
            f"${c['precio']:,.0f} de equipo "
            f"({c['usos']:g} usos/mes): "
            f"${c['equipo_uso']:.2f} + "
            f"{c['pieza']} ${c['piezas']:.2f} + "
            f"luz ${c['luz']:.2f}"
        ),
    ]

    if c['usos'] < 10:
        lineas.append(
            "Con tan poco uso conviene cobrar por trabajo."
        )

    if moneda == 'otra':
        lineas.append(
            "Solo manejo MXN y USD."
        )

    if consumibles:
        lineas.append(
            "Vinil y consumibles: regístralos en Inventario > Materiales."
        )

    if c['mostrar_opciones']:
        lineas.append(
            "¿Cuántas piezas o trabajos a la semana? "
            "Elige o escribe el número."
        )

    return "\n".join(lineas)


def responder_equipo(datos, usos_override=None):
    """Devuelve (respuesta, opciones). Si no se puede calcular, (None, [])."""

    calculo = calcular_costo_equipo(
        datos,
        usos_override
    )

    if not calculo:
        return None, []

    respuesta = armar_respuesta_equipo(
        calculo,
        datos.get('moneda', 'mxn'),
        bool(datos.get('consumibles'))
    )

    opciones = (
        opciones_uso(datos)
        if calculo['mostrar_opciones']
        else []
    )

    return respuesta, opciones


# ==============================================================================
# MERGE DE EQUIPO ANTERIOR
# ==============================================================================

def merge_equipo_anterior(datos_nuevos, ultimo):
    if not ultimo:
        return datos_nuevos

    # 1. Clonar los datos nuevos para NO perder 'intencion'
    resultado = datos_nuevos.copy()

    for campo in EQUIPOS_CAMPOS_SESION:
        anterior = ultimo.get(campo)
        nuevo = datos_nuevos.get(campo)

        # Si Gemini no proporcionó un dato nuevo, conserva el anterior.
        if nuevo is None and anterior is not None:
            resultado[campo] = anterior

    return resultado


# ==============================================================================
# PROMPT 2: CONFIGURACIÓN Y NEGOCIOS
# ==============================================================================

SYSTEM_PROMPT_CONFIGURACION = """
Eres SianBot, asistente experto en Configuración y Negocios de Sianeffects v2.1.

Tu trabajo:

- Ayudar a configurar el sistema
- Explicar costos, precios y logística
- Guiar al usuario para mejorar ganancias
- Resolver dudas de forma SIMPLE y DIRECTA
- Reforzar de forma natural que Sianeffects ayuda a no vender a ciegas porque
  ordena costos, precios, mano de obra y logística.

TONO:

- Profesional, cercano y motivador
- Muy breve y práctico
- Empático
- Usa emojis moderadamente
- Simple y humano, como si hablaras con una emprendedora ocupada.
- No saludes con "Hola" en cada respuesta si la conversación ya empezó.
- Haz que el usuario sienta alivio y control.
- No suenes vendedor ni manipulador.

REGLA PRINCIPAL:

Responde SIEMPRE en menos de 60 palabras, excepto si el usuario pide cálculos
detallados.

Cíñete ESTRICTAMENTE a este mapa de navegación.

No uses markdown, asteriscos, negritas, encabezados ni tablas.

FORMATO IDEAL:

1. Respuesta directa
2. Explicación breve
3. Ejemplo rápido si aplica
4. Dónde configurarlo

==================================================
FÓRMULA SIANEFFECTS
==================================================

Costo Base = Materiales + Maquinaria + Factor Operativo

Precio Final = Costo Base + Ganancia + Mano de Obra

IMPORTANTE:

La mano de obra SIEMPRE se suma al final.

NUNCA se multiplica por el margen para evitar "doble ganancia".

==================================================
REGLAS DE NEGOCIO Y UBICACIONES
==================================================

CONFIGURACIÓN → MI NEGOCIO:

- Identidad: Logo, ícono, nombre, slogan, web y Notas del Ticket.
- Ajustes del Sistema: Control de inventario, ticket térmico, mostrar guías
  y modo oscuro.
- Margen de Ganancia Base: Margen de Ganancia Base y Factor Operativo.
- El Valor de tu Tiempo: Sueldo Deseado y Horas por semana.
- Costos Operativos del Negocio: gastos fijos mensuales.

CONFIGURACIÓN → MI PERFIL:

- Nombre de usuario, País y WhatsApp/Teléfono.
- El correo no se cambia.

CONFIGURACIÓN → SEGURIDAD:

- Cambiar contraseña.

CONFIGURACIÓN → LOGÍSTICA:

- Logística Local: Banderazo, costo x KM, Margen de error y Google Maps.
- Paquetería Nacional: zonas por estados y tarifas por límite de Kg.

CONFIGURACIÓN → PLAN ACTUAL:

- Suscripción, vencimientos y renovaciones.

OTROS MÓDULOS:

- Bot de desgaste: Inventario → Equipos.
- Cancelación: Configuración → Plan Actual.
- Gestionar suscripción: Configuración → Plan Actual.

==================================================
REGLAS IMPORTANTES
==================================================

Si no saben qué sueldo poner:

- $8k-$12k si vienen de cobrar muy bajo.
- $12k-$15k si es ingreso extra.
- $18k-$25k si quieren vivir del negocio.
- $30k+ si quieren crecer.

Ejemplo:

$20,000 al mes / 173.2 horas = $115.47 por hora.

Si no saben qué margen poner:

- 15%-20% si están corrigiendo precios.
- 25%-30% si venden estable.
- 35%+ si el mercado acepta precios más altos.

Recomienda 20% como inicio si no tienen referencia.

La mano de obra NO va dentro del margen.

Si no saben qué Factor Operativo poner:

- 3%-5%: ajuste suave.
- 8%-12%: luz, internet, herramientas.
- 15%+: renta o taller.

Si las cotizaciones suben mucho:

Primero costos reales, luego mano de obra mínima, después margen 15%-20%
y finalmente factor operativo 3%-5%.

No elimines mano de obra, margen o factor.

Si hablan de vender más:

Explica que primero deben dejar de regalar su trabajo y configurar
correctamente costos, mano de obra y factor operativo.

NO inventes módulos, botones o funciones.

Si preguntan algo fuera de Sianeffects:

"Mi especialidad es ayudarte con configuración, costos y estrategias dentro
de Sianeffects 😊"

MISIÓN:

Ayudar a creadores y emprendedores a ganar más y tomar mejores decisiones
financieras usando Sianeffects.
"""


# ==============================================================================
# PROMPT 3: DASHBOARD
# ==============================================================================

SYSTEM_PROMPT_DASHBOARD = """
Eres SianBot, asistente financiero del dashboard "Mi Panel" de Sianeffects.

Tu trabajo:

- Explicar indicadores del dashboard financiero.
- Ayudar al usuario a entender qué vendió, cuánto cobró, qué tiene pendiente
  y qué utilidad estimada obtuvo.
- Convertir números en decisiones prácticas.
- Detectar oportunidades y alertas.

TONO:

- Profesional, cercano, directo y motivador.
- Responde en español salvo que el usuario escriba en otro idioma.
- Usa emojis moderados.
- No regañes.
- No saludes con "Hola" en cada respuesta si la conversación ya empezó.
- Evita celebrar de más cuando hables de dinero pendiente.
- No suenes vendedor.

REGLA PRINCIPAL:

Responde normalmente en menos de 70 palabras.

CONTEXTO DEL DASHBOARD:

Indicadores:

- Cobrado: pagos realmente recibidos.
- Utilidad Estimada: venta neta menos costos registrados.
- Por Cobrar: saldo pendiente.
- Tickets Activos: tickets pagados y con anticipo.
- Total Ticket: total facturado.
- Venta Neta: productos menos descuentos.
- Costos Producto: insumos, mano de obra y costos operativos.
- Cotizaciones / Anuladas: se muestran aparte.
- Cobros y Utilidad: gráfica.
- Radiografía de Ingresos: costos y utilidad.
- Productos Más Vendidos.
- Material por Agotarse.
- Calendario de Actividad.

FÓRMULA:

Venta Neta = Productos vendidos - Descuentos

Utilidad Estimada = Venta Neta - Costos Producto

Cobrado = Dinero recibido

Por Cobrar = Dinero pendiente de cobrar

ACLARACIÓN:

Cobrado y utilidad no son lo mismo.

Cobrado = flujo de efectivo.

Utilidad estimada = ganancia calculada sobre ventas registradas después
de restar costos.

Si utilidad estimada es mayor que cobrado, puede existir dinero pendiente
de cobrar.

NO digas que eso significa automáticamente que el negocio está excelente.

REGLAS:

- Usa el contexto JSON recibido.
- No inventes datos.
- Si hay baja utilidad, sugiere revisar costos, margen, mano de obra o descuentos.
- Si hay mucho por cobrar, sugiere seguimiento.
- Si no hay ventas, sugiere revisar cotizaciones y productos.
- Si hay inventario bajo, sugiere resurtir desde Inventario → Materiales.
- No des asesoría fiscal, contable o legal.
- No inventes módulos.

MISIÓN:

Ayudar al usuario a entender su dashboard y tomar mejores decisiones.
"""


# ==============================================================================
# PROMPT 4: ADMIN DASHBOARD
# ==============================================================================

SYSTEM_PROMPT_ADMIN_DASHBOARD = """
Eres SianBot Admin, analista interno de Sianeffects para el dashboard administrativo.

Tu trabajo:

- Interpretar crecimiento, MRR, churn, activaciones, renovaciones,
  usuarios activos, vencidos y uso del producto.
- Comparar periodos.
- Detectar señales raras.
- Proponer acciones concretas.
- Usar SOLO el contexto JSON recibido y el historial reciente.

Si el usuario pide comparar meses, usa primero comparativa_mensual_admin.

Si una métrica no está disponible, acláralo.

TONO:

- Directo.
- Estratégico.
- Claro.
- Útil y honesto.
- Puedes llamar al usuario "Jefe" de forma natural.

REGLAS:

- No inventes cifras.
- No inventes usuarios.
- No inventes causas.
- No digas que tienes acceso a toda la base de datos.
- No des asesoría legal, fiscal o contable.
- No prometas predicciones exactas.
- No reveles emails ni datos personales sensibles.
- Si hay muchos vencidos, prioriza retención.
- Si hay muchos free/trial frente a pagados, sugiere conversión.
- Si preguntan "qué hago hoy", responde con 3 acciones concretas.
- Si piden una acción destructiva, aclara que solo analizas.

MISIÓN:

Ser copiloto de negocio para interpretar el dashboard administrativo.
"""


# ==============================================================================
# RUTA 1: CHAT DE EQUIPOS
# ==============================================================================

@chatbot_bp.route('/api/chat-equipos', methods=['POST'])
def chat_equipos():
    try:
        data = request.json or {}

        mensaje_usuario = data.get('message', '').strip()

        usos_boton = _num(
            data.get('usos_mes')
        )

        if not mensaje_usuario and not usos_boton:
            return jsonify({
                'error': 'Mensaje vacío'
            }), 400

        if 'chat_history' not in session:
            session['chat_history'] = []

        historial = session['chat_history']

        ultimo = session.get('equipos_ultimo')


        # ------------------------------------------------------------------
        # BOTÓN DE USO
        # ------------------------------------------------------------------

        if usos_boton:

            if not ultimo:
                return jsonify({
                    'reply': RESP_EQUIPOS_SIN_DATOS,
                    'opciones': [],
                    'status': 'success'
                })

            respuesta, opciones = responder_equipo(
                ultimo,
                usos_override=usos_boton
            )

            if not respuesta:
                return jsonify({
                    'reply': RESP_EQUIPOS_SIN_DATOS,
                    'opciones': [],
                    'status': 'success'
                })

            # Guardar la elección del botón en la sesión
            ultimo['usos_mes'] = usos_boton
            session['equipos_ultimo'] = ultimo
            session.modified = True

            return jsonify({
                'reply': respuesta,
                'opciones': opciones,
                'status': 'success'
            })


        # ------------------------------------------------------------------
        # RESPUESTAS SOCIALES
        # ------------------------------------------------------------------

        respuesta_social = get_social_reply(
            mensaje_usuario,
            'equipos'
        )

        if respuesta_social:
            store_chat_reply(
                'chat_history',
                historial,
                mensaje_usuario,
                respuesta_social
            )

            return jsonify({
                'reply': respuesta_social,
                'opciones': [],
                'status': 'success'
            })


        # ------------------------------------------------------------------
        # CONTEXTO PARA GEMINI
        # ------------------------------------------------------------------

        contexto = ""

        if ultimo:
            contexto = (
                "Último equipo registrado:\n"
                f"{json.dumps(ultimo, ensure_ascii=False)}\n\n"
            )

        contents = f"""
El usuario está dentro del módulo "Equipos" de Sianeffects.

Este módulo sirve para calcular el costo por uso y desgaste de equipos.

El asistente puede pedirle al usuario que indique qué equipo tiene.
Por eso, si el usuario responde solamente con un nombre o modelo de equipo,
eso debe interpretarse como una solicitud válida para iniciar el cálculo.

{contexto}

Mensaje del usuario:
{mensaje_usuario}
"""


        # ------------------------------------------------------------------
        # GEMINI
        # ------------------------------------------------------------------

        response = client.models.generate_content(
            model='models/gemini-2.5-flash-lite',
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT_EQUIPOS_EXTRACTOR,
                temperature=0,
                max_output_tokens=500,
                response_mime_type='application/json'
            )
        )


        # ------------------------------------------------------------------
        # PARSEAR JSON
        # ------------------------------------------------------------------

        try:
            # Garantiza que sea un string aunque response.text sea None
            raw_text = (response.text or "").strip()
            
            if not raw_text:
                raise ValueError("Respuesta vacía o nula de Gemini")

            # Remover bloques markdown si Gemini los incluye
            if raw_text.startswith('```'):
                raw_text = re.sub(r'^```[a-zA-Z]*\n', '', raw_text)
                raw_text = re.sub(r'\n```$', '', raw_text).strip()

            datos = json.loads(raw_text)

            if not isinstance(datos, dict):
                datos = {}

        except (ValueError, TypeError, json.JSONDecodeError) as e:
            # Protegemos el logger también
            texto_error = response.text if hasattr(response, 'text') else 'Ninguno'
            current_app.logger.warning(
                f"EQUIPOS_GEMINI_JSON_INVALIDO: {texto_error} - Error: {e}"
            )
            datos = {}


        # ------------------------------------------------------------------
        # MERGE CON EL ÚLTIMO EQUIPO
        # ------------------------------------------------------------------

        if ultimo:
            datos = merge_equipo_anterior(
                datos,
                ultimo
            )


        # ------------------------------------------------------------------
        # INTENCIÓN
        # ------------------------------------------------------------------

        intencion = datos.get(
            'intencion',
            'otro'
        )

        opciones = []


        # ------------------------------------------------------------------
        # COMPRA
        # ------------------------------------------------------------------

        if intencion == 'compra':

            respuesta = RESP_EQUIPOS_COMPRA


        # ------------------------------------------------------------------
        # COTIZAR
        # ------------------------------------------------------------------

        elif intencion == 'cotizar':

            respuesta = RESP_EQUIPOS_COTIZAR


        # ------------------------------------------------------------------
        # CALCULAR
        # ------------------------------------------------------------------

        elif intencion == 'calcular':

            respuesta, opciones = responder_equipo(
                datos
            )

            if respuesta:

                if not session.get('equipos_cta'):
                    respuesta += "\nRegístralo en Nuevo Equipo."
                    session['equipos_cta'] = True

                # Guardamos la versión ya fusionada.
                session['equipos_ultimo'] = {
                    k: datos.get(k)
                    for k in EQUIPOS_CAMPOS_SESION
                }

                session.modified = True

            else:
                respuesta = RESP_EQUIPOS_SIN_DATOS


        # ------------------------------------------------------------------
        # OTRO
        # ------------------------------------------------------------------

        else:

            respuesta = RESP_EQUIPOS_OTRO


        return jsonify({
            'reply': respuesta,
            'opciones': opciones,
            'status': 'success'
        })


    except Exception as e:

        current_app.logger.exception(
            f"Error en chatbot equipos: {str(e)}"
        )

        return jsonify({
            'error': str(e)
        }), 500


@chatbot_bp.route('/api/chat-equipos/reset', methods=['POST'])
def reset_chat_equipos():

    session.pop('chat_history', None)
    session.pop('equipos_ultimo', None)
    session.pop('equipos_cta', None)

    return jsonify({
        'status': 'success'
    })


# ==============================================================================
# RUTA 2: CHAT CONFIGURACIÓN
# ==============================================================================

@chatbot_bp.route('/api/chat-configuracion', methods=['POST'])
def chat_configuracion():

    try:
        data = request.json or {}

        mensaje_usuario = data.get(
            'message',
            ''
        ).strip()

        if not mensaje_usuario:
            return jsonify({
                'error': 'Mensaje vacío'
            }), 400


        # Usamos una variable de sesión separada para el Coach.

        if 'coach_history' not in session:
            session['coach_history'] = []

        historial = session['coach_history']


        # ------------------------------------------------------------------
        # RESPUESTAS SOCIALES
        # ------------------------------------------------------------------

        respuesta_social = get_social_reply(
            mensaje_usuario,
            'configuracion'
        )

        if respuesta_social:

            respuesta = respuesta_social

            store_chat_reply(
                'coach_history',
                historial,
                mensaje_usuario,
                respuesta
            )

            return jsonify({
                'reply': respuesta,
                'status': 'success'
            })


        # ------------------------------------------------------------------
        # RESPUESTAS GUIADAS
        # ------------------------------------------------------------------

        contexto_reciente = " ".join(
            msg.get('content', '')
            for msg in historial[-4:]
        )


        respuesta_guiada = respuesta_precio_alto_configuracion(
            mensaje_usuario,
            contexto_reciente
        )

        if respuesta_guiada:

            store_chat_reply(
                'coach_history',
                historial,
                mensaje_usuario,
                respuesta_guiada
            )

            return jsonify({
                'reply': respuesta_guiada,
                'status': 'success'
            })


        respuesta_guiada = respuesta_margen_configuracion(
            mensaje_usuario,
            contexto_reciente
        )

        if respuesta_guiada:

            store_chat_reply(
                'coach_history',
                historial,
                mensaje_usuario,
                respuesta_guiada
            )

            return jsonify({
                'reply': respuesta_guiada,
                'status': 'success'
            })


        respuesta_guiada = respuesta_factor_operativo_configuracion(
            mensaje_usuario,
            contexto_reciente
        )

        if respuesta_guiada:

            store_chat_reply(
                'coach_history',
                historial,
                mensaje_usuario,
                respuesta_guiada
            )

            return jsonify({
                'reply': respuesta_guiada,
                'status': 'success'
            })


        respuesta_guiada = respuesta_salario_configuracion(
            mensaje_usuario,
            contexto_reciente
        )

        if respuesta_guiada:

            store_chat_reply(
                'coach_history',
                historial,
                mensaje_usuario,
                respuesta_guiada
            )

            return jsonify({
                'reply': respuesta_guiada,
                'status': 'success'
            })


        # ------------------------------------------------------------------
        # GEMINI
        # ------------------------------------------------------------------

        contents = []

        for msg in historial[-6:]:
            role = msg['role']
            contents.append(
                f"{role}: {msg['content']}"
            )

        contents.append(
            f"Usuario: {mensaje_usuario}"
        )


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


        store_chat_reply(
            'coach_history',
            historial,
            mensaje_usuario,
            respuesta
        )


        return jsonify({
            'reply': respuesta,
            'status': 'success'
        })


    except Exception as e:

        current_app.logger.exception(
            f"Error en chatbot coach: {str(e)}"
        )

        return jsonify({
            'error': str(e)
        }), 500


@chatbot_bp.route('/api/chat-configuracion/reset', methods=['POST'])
def reset_chat_configuracion():

    session.pop(
        'coach_history',
        None
    )

    return jsonify({
        'status': 'success'
    })


# ==============================================================================
# RUTA 3: CHAT DASHBOARD / MI PANEL
# ==============================================================================

@chatbot_bp.route('/api/chat-dashboard', methods=['POST'])
@login_required
def chat_dashboard():

    try:
        data = request.json or {}

        mensaje_usuario = data.get(
            'message',
            ''
        ).strip()

        dashboard_context = data.get(
            'dashboard_context',
            {}
        )

        if not mensaje_usuario:
            return jsonify({
                'error': 'Mensaje vacío'
            }), 400


        if 'dashboard_history' not in session:
            session['dashboard_history'] = []

        historial = session['dashboard_history']


        # ------------------------------------------------------------------
        # RESPUESTAS SOCIALES
        # ------------------------------------------------------------------

        respuesta_social = get_social_reply(
            mensaje_usuario,
            'dashboard'
        )

        if respuesta_social:

            respuesta = respuesta_social

            store_chat_reply(
                'dashboard_history',
                historial,
                mensaje_usuario,
                respuesta
            )

            return jsonify({
                'reply': respuesta,
                'status': 'success'
            })


        # ------------------------------------------------------------------
        # CONTEXTO
        # ------------------------------------------------------------------

        contents = []

        contexto_texto = (
            "Contexto visible del dashboard en JSON:\n"
            f"{dashboard_context}\n\n"
            "Usa este contexto solo para explicar el periodo actual "
            "y responder la duda del usuario."
        )

        contents.append(
            contexto_texto
        )


        for msg in historial[-6:]:

            role = msg['role']

            contents.append(
                f"{role}: {msg['content']}"
            )


        contents.append(
            f"Usuario: {mensaje_usuario}"
        )


        # ------------------------------------------------------------------
        # GEMINI
        # ------------------------------------------------------------------

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


        store_chat_reply(
            'dashboard_history',
            historial,
            mensaje_usuario,
            respuesta
        )


        return jsonify({
            'reply': respuesta,
            'status': 'success'
        })


    except Exception as e:

        current_app.logger.exception(
            f"Error en chatbot dashboard: {str(e)}"
        )

        return jsonify({
            'error': str(e)
        }), 500


@chatbot_bp.route('/api/chat-dashboard/reset', methods=['POST'])
@login_required
def reset_chat_dashboard():

    session.pop(
        'dashboard_history',
        None
    )

    return jsonify({
        'status': 'success'
    })


# ==============================================================================
# RUTA 4: CHAT DASHBOARD ADMIN
# ==============================================================================

@chatbot_bp.route('/api/chat-admin-dashboard', methods=['POST'])
@admin_required
def chat_admin_dashboard():

    try:
        data = request.json or {}

        mensaje_usuario = data.get(
            'message',
            ''
        ).strip()

        dashboard_context = data.get(
            'dashboard_context',
            {}
        )

        if not mensaje_usuario:
            return jsonify({
                'error': 'Mensaje vacío'
            }), 400


        if 'admin_dashboard_history' not in session:
            session['admin_dashboard_history'] = []

        historial = session['admin_dashboard_history']


        # ------------------------------------------------------------------
        # RESPUESTAS SOCIALES
        # ------------------------------------------------------------------

        respuesta_social = get_social_reply(
            mensaje_usuario,
            'admin'
        )

        if respuesta_social:

            respuesta = respuesta_social

            store_chat_reply(
                'admin_dashboard_history',
                historial,
                mensaje_usuario,
                respuesta
            )

            return jsonify({
                'reply': respuesta,
                'status': 'success'
            })


        # ------------------------------------------------------------------
        # CONTEXTO
        # ------------------------------------------------------------------

        contents = [
            "Contexto agregado del dashboard admin en JSON:\n"
            f"{dashboard_context}\n\n"
            "Usa este contexto para analizar el negocio, "
            "comparar señales y recomendar acciones."
        ]


        for msg in historial[-8:]:

            role = msg['role']

            contents.append(
                f"{role}: {msg['content']}"
            )


        contents.append(
            f"Usuario: {mensaje_usuario}"
        )


        # ------------------------------------------------------------------
        # GEMINI
        # ------------------------------------------------------------------

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


        store_chat_reply(
            'admin_dashboard_history',
            historial,
            mensaje_usuario,
            respuesta
        )


        return jsonify({
            'reply': respuesta,
            'status': 'success'
        })


    except Exception as e:

        current_app.logger.exception(
            f"Error en chatbot admin dashboard: {str(e)}"
        )

        return jsonify({
            'error': str(e)
        }), 500


@chatbot_bp.route('/api/chat-admin-dashboard/reset', methods=['POST'])
@admin_required
def reset_chat_admin_dashboard():

    session.pop(
        'admin_dashboard_history',
        None
    )

    return jsonify({
        'status': 'success'
    })