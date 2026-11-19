# =============================================================================
# routes/embajadoras.py — Embajadoras Sianeffects
# =============================================================================
# Cubre: panel de administración (CRUD completo, pausa, vinculación con cuenta,
#        reintento de recompensas, historial), creación/actualización de
#        cupones y Promotion Codes individuales en Stripe, y panel de lectura
#        para cada embajadora.
#
# La lógica de recompensas vive en routes/payments.py:
#   registrar_recompensa_embajadora() y aplicar_recompensas_embajadora().
#
# SEGURIDAD APLICADA:
#   - Rutas de administración protegidas con @admin_required
#   - Operaciones que modifican datos usan POST
#   - El panel de la embajadora solo muestra SUS datos y nunca expone nombre
#     ni correo de las personas invitadas
# =============================================================================

import os
import re
import secrets
import unicodedata
from datetime import timezone, datetime
from urllib.parse import quote

import stripe
from flask import (
    Blueprint, render_template, request, redirect, jsonify,
    url_for, flash, session, current_app, g
)

from db import get_db_connection as get_db
from helpers import admin_required, login_required
from utils.datetime_utils import utc_to_local
from routes.payments import aplicar_recompensas_embajadora, DIAS_RECOMPENSA_EMBAJADORA

embajadoras_bp = Blueprint('embajadoras', __name__)


# =============================================================================
# CONTEXTO GLOBAL — ¿La cuenta actual es embajadora? (para mostrar el menú)
# =============================================================================

@embajadoras_bp.app_context_processor
def inyectar_es_embajadora():
    """Expone `is_ambassador` a todas las plantillas. Nunca rompe el render."""
    try:
        if hasattr(g, '_es_embajadora'):
            return {'is_ambassador': g._es_embajadora}

        resultado = False
        user_id = session.get('user_id')
        if user_id and request.endpoint != 'static':
            conn = get_db()
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT 1 FROM ambassadors WHERE user_id = %s LIMIT 1",
                    (user_id,)
                )
                resultado = cursor.fetchone() is not None
            finally:
                cursor.close()
                conn.close()

        g._es_embajadora = resultado
        return {'is_ambassador': resultado}
    except Exception as e:
        try:
            current_app.logger.debug(f"AMBASSADOR_FLAG_ERROR: {e}")
        except Exception:
            pass
        return {'is_ambassador': False}


# =============================================================================
# UTILIDADES
# =============================================================================

def _a_local(dt):
    """Convierte un datetime de la BD (UTC) a hora local. Tolera naive y None."""
    if not dt:
        return None
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return utc_to_local(dt)
    except Exception:
        return dt


def _fmt(dt, con_hora=False):
    dt = _a_local(dt)
    if not dt:
        return '—'
    return dt.strftime('%d/%m/%Y %H:%M' if con_hora else '%d/%m/%Y')


def _normalizar_codigo(texto):
    """Mayúsculas, solo letras, números, guion y guion bajo."""
    return re.sub(r'[^A-Z0-9_-]', '', (texto or '').strip().upper())


def _base_codigo(nombre):
    ascii_name = unicodedata.normalize('NFKD', nombre or '').encode('ascii', 'ignore').decode()
    letras = re.sub(r'[^A-Za-z]', '', ascii_name).upper()
    return letras[:6] or 'SIAN'


def _generar_codigo(cursor, nombre):
    """Genera un código único tipo ANA123 sin distinguir mayúsculas."""
    base = _base_codigo(nombre)
    for _ in range(20):
        candidato = f"{base}{secrets.randbelow(900) + 100}"
        cursor.execute("SELECT 1 FROM ambassadors WHERE UPPER(code) = %s", (candidato,))
        if not cursor.fetchone():
            return candidato
    return f"{base}{secrets.token_hex(3).upper()}"


def _contar_pendientes(amb_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) AS n FROM ambassador_rewards WHERE ambassador_id = %s AND applied_at IS NULL",
            (amb_id,)
        )
        return cursor.fetchone()['n']
    finally:
        cursor.close()
        conn.close()


def _log_admin(cursor, detalle, accion):
    cursor.execute(
        "INSERT INTO logs_actividad (user_id, accion, modulo, detalle) VALUES (%s, %s, %s, %s)",
        (session.get('user_id'), accion, 'Embajadoras', detalle)
    )


def _configurar_stripe_key():
    if not stripe.api_key:
        stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
    return bool(stripe.api_key)


def _parsear_config_descuento(form_data, default_when_missing=False):
    """
    Lee y valida los campos de descuento estilo Stripe desde un formulario.
    Devuelve (config_dict, error_str).
    Si default_when_missing=True y no se envía valor, devuelve None (no crear cupón nuevo).
    """
    tipo = (form_data.get('discount_type') or 'percent').strip().lower()
    duracion = (form_data.get('discount_duration') or 'once').strip().lower()
    raw_valor = (form_data.get('discount_value') or '').strip()
    raw_meses = (form_data.get('duration_in_months') or '').strip()

    if tipo not in ('percent', 'amount'):
        return None, "Tipo de descuento inválido en Stripe."

    if duracion not in ('once', 'repeating', 'forever'):
        return None, "Duración de cupón inválida en Stripe."

    if not raw_valor:
        if default_when_missing:
            return None, None
        return None, "Indica el valor del descuento para el cupón de Stripe."

    try:
        valor = float(raw_valor)
    except ValueError:
        return None, "El valor del descuento debe ser numérico."

    if valor <= 0:
        return None, "El valor del descuento debe ser mayor a cero."

    meses = None
    if duracion == 'repeating':
        try:
            meses = int(raw_meses)
        except ValueError:
            return None, "Indica un número entero de meses para la duración repetitiva."
        if meses < 1 or meses > 120:
            return None, "La duración en meses debe estar entre 1 y 120."

    if tipo == 'percent':
        if valor > 100:
            return None, "El porcentaje de descuento no puede ser mayor a 100%."
        return {
            'type': 'percent',
            'percent_off': round(valor, 2),
            'duration': duracion,
            'duration_in_months': meses,
        }, None

    # Monto fijo en MXN -> centavos para Stripe
    amount_cents = int(round(valor * 100))
    if amount_cents < 100:
        return None, "El monto fijo de descuento debe ser de al menos $1.00 MXN."

    return {
        'type': 'amount',
        'amount_off': amount_cents,
        'currency': 'mxn',
        'duration': duracion,
        'duration_in_months': meses,
    }, None


def _crear_cupon_y_promo_stripe(codigo, amb_id, nombre, disc_cfg, activo=True):
    """
    Crea un Coupon individual en Stripe y le asocia un Promotion Code con `codigo`.
    """
    if not _configurar_stripe_key():
        return False, "No está configurada STRIPE_SECRET_KEY."

    coupon_params = {
        'name': f"Embajadora {nombre} ({codigo})"[:120],
        'duration': disc_cfg['duration'],
        'metadata': {
            'ambassador_id': str(amb_id),
            'code': codigo,
            'source': 'sianeffects_embajadoras',
        },
    }
    if disc_cfg['duration'] == 'repeating' and disc_cfg.get('duration_in_months'):
        coupon_params['duration_in_months'] = disc_cfg['duration_in_months']

    if disc_cfg['type'] == 'percent':
        coupon_params['percent_off'] = disc_cfg['percent_off']
    else:
        coupon_params['amount_off'] = disc_cfg['amount_off']
        coupon_params['currency'] = disc_cfg.get('currency', 'mxn')

    coupon = stripe.Coupon.create(**coupon_params)

    existentes = stripe.PromotionCode.list(code=codigo, limit=10)
    for promo in existentes.data:
        if promo.active:
            stripe.PromotionCode.modify(promo.id, active=False)

    stripe.PromotionCode.create(
        promotion={"coupon": coupon.id, "type": "coupon"},
        code=codigo,
        active=bool(activo),
        metadata={
            "ambassador_id": str(amb_id),
            "ambassador_name": nombre[:100],
            "coupon_id": coupon.id,
            "source": "sianeffects_embajadoras",
        },
    )
    return True, None


def _actualizar_estado_promo_stripe(codigo, activo):
    """Activa o desactiva en Stripe los Promotion Codes asociados a `codigo`."""
    if not codigo or not _configurar_stripe_key():
        return False, "Stripe no configurado."

    promos = stripe.PromotionCode.list(code=codigo, limit=10)
    for promo in promos.data:
        if promo.active != activo:
            stripe.PromotionCode.modify(promo.id, active=activo)
    return True, None


# =============================================================================
# ADMIN — LISTADO
# =============================================================================

@embajadoras_bp.route('/admin/embajadoras')
@admin_required
def admin_lista():
    q = request.args.get('q', '').strip()
    estado = request.args.get('estado', 'all')
    if estado not in {'all', 'active', 'paused', 'sin_cuenta'}:
        estado = 'all'

    condiciones = []
    params = []

    if q:
        like = f"%{q.lower()}%"
        condiciones.append("(LOWER(a.name) LIKE %s OR LOWER(a.email) LIKE %s OR LOWER(a.code) LIKE %s)")
        params.extend([like, like, like])

    if estado in ('active', 'paused'):
        condiciones.append("a.status = %s")
        params.append(estado)
    elif estado == 'sin_cuenta':
        condiciones.append("a.user_id IS NULL")

    where_sql = ("WHERE " + " AND ".join(condiciones)) if condiciones else ""

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            SELECT
                a.id, a.name, a.email, a.code, a.status, a.user_id, a.created_at,
                u.username AS username,
                u.subscription_end AS subscription_end,
                (SELECT COUNT(*) FROM ambassador_referrals r
                  WHERE r.ambassador_id = a.id) AS registrados,
                (SELECT COUNT(*) FROM ambassador_referrals r
                  WHERE r.ambassador_id = a.id AND r.status = 'converted') AS convertidos,
                COALESCE((SELECT SUM(w.days_awarded) FROM ambassador_rewards w
                  WHERE w.ambassador_id = a.id), 0) AS dias_ganados,
                COALESCE((SELECT SUM(w.days_awarded) FROM ambassador_rewards w
                  WHERE w.ambassador_id = a.id AND w.applied_at IS NULL), 0) AS dias_pendientes
            FROM ambassadors a
            LEFT JOIN usuarios u ON u.id = a.user_id
            {where_sql}
            ORDER BY a.created_at DESC
        """, params)
        embajadoras = [dict(row) for row in cursor.fetchall()]
        for a in embajadoras:
            a['vigente_hasta'] = _fmt(a['subscription_end'])

        cursor.execute("""
            SELECT
                (SELECT COUNT(*) FROM ambassadors) AS total,
                (SELECT COUNT(*) FROM ambassadors WHERE status = 'active') AS activas,
                (SELECT COUNT(*) FROM ambassadors WHERE user_id IS NULL) AS sin_cuenta,
                (SELECT COUNT(*) FROM ambassador_referrals) AS registros,
                (SELECT COUNT(*) FROM ambassador_referrals WHERE status = 'converted') AS convertidos,
                (SELECT COALESCE(SUM(days_awarded), 0) FROM ambassador_rewards) AS dias_ganados,
                (SELECT COUNT(*) FROM ambassador_rewards WHERE applied_at IS NULL) AS pendientes
        """)
        stats = dict(cursor.fetchone())

        return render_template(
            'admin_embajadoras.html',
            embajadoras=embajadoras,
            stats=stats,
            q=q,
            estado=estado,
        )

    except Exception as e:
        current_app.logger.error(f"AMBASSADOR_ADMIN_LIST_ERROR: {e}")
        flash(f"No se pudo cargar Embajadoras: {e}", "danger")
        return redirect(url_for('admin.dashboard'))
    finally:
        cursor.close()
        conn.close()

# =============================================================================
# ADMIN — AUTOCOMPLETADO DE CUENTAS DISPONIBLES
# =============================================================================

@embajadoras_bp.route('/admin/embajadoras/buscar-usuarios')
@admin_required
def admin_buscar_usuarios():
    q = (request.args.get('q') or '').strip().lower()
    if len(q) < 2:
        return jsonify({'usuarios': []})

    like = f"%{q}%"
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT u.id, u.email, u.username, u.company_name
            FROM usuarios u
            WHERE (
                LOWER(u.email) LIKE %s
                OR LOWER(u.username) LIKE %s
                OR LOWER(COALESCE(u.company_name, '')) LIKE %s
            )
            AND NOT EXISTS (
                SELECT 1 FROM ambassadors a
                WHERE a.user_id = u.id OR LOWER(a.email) = LOWER(u.email)
            )
            ORDER BY u.email ASC
            LIMIT 8
        """, (like, like, like))

        resultados = []
        for row in cursor.fetchall():
            nombre_sugerido = (row['company_name'] or row['username'] or '').strip()
            resultados.append({
                'id': row['id'],
                'email': row['email'],
                'username': row['username'] or '',
                'nombre_sugerido': nombre_sugerido,
            })

        return jsonify({'usuarios': resultados})
    except Exception as e:
        current_app.logger.error(f"AMBASSADOR_USER_SEARCH_ERROR: {e}")
        return jsonify({'usuarios': []}), 500
    finally:
        cursor.close()
        conn.close()


# =============================================================================
# ADMIN — ALTA
# =============================================================================

@embajadoras_bp.route('/admin/embajadoras/crear', methods=['POST'])
@admin_required
def admin_crear():
    if session.get('role', 0) < 1:
        flash("No tienes permisos para dar de alta embajadoras.", "danger")
        return redirect(url_for('embajadoras.admin_lista'))

    email = (request.form.get('email') or '').strip().lower()
    nombre = (request.form.get('name') or '').strip()
    codigo = _normalizar_codigo(request.form.get('code'))
    disc_cfg, disc_err = _parsear_config_descuento(request.form, default_when_missing=True)

    if disc_err:
        flash(disc_err, "warning")
        return redirect(url_for('embajadoras.admin_lista'))

    if not email:
        flash("Escribe el correo de la cuenta de Sianeffects de la embajadora.", "warning")
        return redirect(url_for('embajadoras.admin_lista'))

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT id, username, email, company_name FROM usuarios WHERE LOWER(email) = %s",
            (email,)
        )
        user = cursor.fetchone()
        if not user:
            flash("No hay una cuenta con ese correo. La embajadora debe registrarse primero en Sianeffects.", "warning")
            return redirect(url_for('embajadoras.admin_lista'))

        cursor.execute(
            "SELECT id FROM ambassadors WHERE user_id = %s OR LOWER(email) = %s",
            (user['id'], user['email'].lower())
        )
        if cursor.fetchone():
            flash("Esa cuenta ya es embajadora.", "warning")
            return redirect(url_for('embajadoras.admin_lista'))

        nombre = (nombre or user['company_name'] or user['username'] or 'Embajadora')[:150]

        if codigo:
            if len(codigo) < 4:
                flash("El código debe tener al menos 4 caracteres (letras, números, guion o guion bajo).", "warning")
                return redirect(url_for('embajadoras.admin_lista'))
            cursor.execute("SELECT 1 FROM ambassadors WHERE UPPER(code) = %s", (codigo,))
            if cursor.fetchone():
                flash(f"El código {codigo} ya existe. Elige otro.", "warning")
                return redirect(url_for('embajadoras.admin_lista'))
        else:
            codigo = _generar_codigo(cursor, nombre)

        codigo = codigo[:40]
        cursor.execute("""
            INSERT INTO ambassadors (name, email, code, status, user_id)
            VALUES (%s, %s, %s, 'active', %s)
            RETURNING id
        """, (nombre, user['email'], codigo, user['id']))
        amb_id = cursor.fetchone()['id']

        _log_admin(cursor, f"Alta de embajadora '{nombre}' (ID:{amb_id}) con código {codigo}", "Alta de embajadora")
        conn.commit()

        if disc_cfg:
            try:
                ok_promo, err_promo = _crear_cupon_y_promo_stripe(codigo, amb_id, nombre, disc_cfg, activo=True)
                if ok_promo:
                    flash(f"Embajadora creada. Su cupón y código {codigo} quedaron activos en Stripe.", "success")
                else:
                    flash(f"Embajadora creada con código {codigo}, pero no se creó en Stripe: {err_promo}", "warning")
            except Exception as stripe_err:
                current_app.logger.error(f"STRIPE_COUPON_CREATE_ERROR ({codigo}): {stripe_err}")
                flash(f"Embajadora creada con código {codigo}, pero falló la creación del cupón en Stripe.", "warning")
        else:
            flash(f"Embajadora creada. Su código es {codigo} (sin cupón nuevo creado en Stripe).", "success")

        return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"AMBASSADOR_CREATE_ERROR: {e}")
        flash("No se pudo crear la embajadora. Revisa el registro de errores.", "danger")
        return redirect(url_for('embajadoras.admin_lista'))
    finally:
        cursor.close()
        conn.close()


# =============================================================================
# ADMIN — DETALLE
# =============================================================================

@embajadoras_bp.route('/admin/embajadoras/<int:amb_id>')
@admin_required
def admin_detalle(amb_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT a.id, a.name, a.email, a.code, a.status, a.user_id, a.created_at,
                   u.username, u.email AS user_email, u.estado_suscripcion,
                   u.subscription_end, u.stripe_subscription_id
            FROM ambassadors a
            LEFT JOIN usuarios u ON u.id = a.user_id
            WHERE a.id = %s
        """, (amb_id,))
        row = cursor.fetchone()
        if not row:
            flash("No se encontró la embajadora.", "warning")
            return redirect(url_for('embajadoras.admin_lista'))
        amb = dict(row)
        amb['vigente_hasta'] = _fmt(amb['subscription_end'])
        amb['creada'] = _fmt(amb['created_at'])

        cursor.execute("""
            SELECT r.id, r.status, r.registered_at, r.first_payment_at,
                   u.username, u.email
            FROM ambassador_referrals r
            LEFT JOIN usuarios u ON u.id = r.referred_user_id
            WHERE r.ambassador_id = %s
            ORDER BY r.registered_at DESC
        """, (amb_id,))
        referidos = []
        for r in cursor.fetchall():
            d = dict(r)
            d['registrado'] = _fmt(d['registered_at'], True)
            d['primer_pago'] = _fmt(d['first_payment_at'], True) if d['first_payment_at'] else '—'
            referidos.append(d)

        cursor.execute("""
            SELECT w.id, w.days_awarded, w.payment_reference, w.awarded_at,
                   w.applied_at, w.applied_mode, w.carried_at
            FROM ambassador_rewards w
            WHERE w.ambassador_id = %s
            ORDER BY w.id DESC
        """, (amb_id,))
        recompensas = []
        for w in cursor.fetchall():
            d = dict(w)
            d['otorgada'] = _fmt(d['awarded_at'], True)
            if not d['applied_at']:
                d['estado_txt'], d['estado_clase'] = 'Pendiente', 'warning'
            elif d['applied_mode'] == 'stripe':
                d['estado_txt'], d['estado_clase'] = 'Cobro movido', 'success'
            else:
                d['estado_txt'], d['estado_clase'] = 'Acceso extendido', 'info'
            d['trasladada'] = bool(d['carried_at'])
            recompensas.append(d)

        resumen = {
            'registrados': len(referidos),
            'convertidos': sum(1 for r in referidos if r['status'] == 'converted'),
            'dias_ganados': sum(w['days_awarded'] for w in recompensas),
            'dias_pendientes': sum(w['days_awarded'] for w in recompensas if not w['applied_at']),
        }

        return render_template(
            'admin_embajadora_detalle.html',
            amb=amb,
            referidos=referidos,
            recompensas=recompensas,
            resumen=resumen,
        )

    except Exception as e:
        current_app.logger.error(f"AMBASSADOR_ADMIN_DETAIL_ERROR ({amb_id}): {e}")
        flash("No se pudo cargar el detalle de la embajadora.", "danger")
        return redirect(url_for('embajadoras.admin_lista'))
    finally:
        cursor.close()
        conn.close()


# =============================================================================
# ADMIN — EDITAR DATOS, CÓDIGO Y CUPÓN EN STRIPE
# =============================================================================

@embajadoras_bp.route('/admin/embajadoras/<int:amb_id>/editar', methods=['POST'])
@admin_required
def admin_editar(amb_id):
    if session.get('role', 0) < 1:
        flash("No tienes permisos para editar embajadoras.", "danger")
        return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

    nombre = (request.form.get('name') or '').strip()[:150]
    email = (request.form.get('email') or '').strip().lower()[:150]
    codigo = _normalizar_codigo(request.form.get('code'))[:40]
    disc_cfg, disc_err = _parsear_config_descuento(request.form, default_when_missing=True)

    if disc_err:
        flash(disc_err, "warning")
        return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

    if not nombre:
        flash("El nombre de la embajadora no puede ir vacío.", "warning")
        return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

    if not email or '@' not in email:
        flash("Escribe un correo válido para la embajadora.", "warning")
        return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

    if len(codigo) < 4:
        flash("El código debe tener al menos 4 caracteres (letras, números, guion o guion bajo).", "warning")
        return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT id, name, email, code, status FROM ambassadors WHERE id = %s",
            (amb_id,)
        )
        actual = cursor.fetchone()
        if not actual:
            flash("No se encontró la embajadora.", "warning")
            return redirect(url_for('embajadoras.admin_lista'))

        cursor.execute(
            "SELECT 1 FROM ambassadors WHERE UPPER(code) = %s AND id <> %s",
            (codigo, amb_id)
        )
        if cursor.fetchone():
            flash(f"El código {codigo} ya pertenece a otra embajadora.", "warning")
            return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

        codigo_anterior = (actual['code'] or '').upper()
        codigo_cambio = codigo_anterior != codigo
        esta_activa = actual['status'] == 'active'

        cursor.execute("""
            UPDATE ambassadors
            SET name = %s, email = %s, code = %s, updated_at = NOW()
            WHERE id = %s
        """, (nombre, email, codigo, amb_id))

        cambios = []
        if actual['name'] != nombre:
            cambios.append(f"nombre '{actual['name']}' -> '{nombre}'")
        if (actual['email'] or '').lower() != email:
            cambios.append(f"correo '{actual['email']}' -> '{email}'")
        if codigo_cambio:
            cambios.append(f"código '{actual['code']}' -> '{codigo}'")
        if disc_cfg:
            cambios.append(f"cupón Stripe ({disc_cfg['type']})")

        detalle_log = f"Embajadora ID:{amb_id} actualizada: " + (", ".join(cambios) if cambios else "sin cambios")
        _log_admin(cursor, detalle_log, "Edición de embajadora")
        conn.commit()

        if disc_cfg:
            try:
                if codigo_cambio:
                    _actualizar_estado_promo_stripe(codigo_anterior, False)
                ok_promo, err_promo = _crear_cupon_y_promo_stripe(
                    codigo, amb_id, nombre, disc_cfg, activo=esta_activa
                )
                if ok_promo:
                    flash("Datos guardados y nuevo cupón/código creado en Stripe.", "success")
                else:
                    flash(f"Datos guardados, pero no se creó el cupón en Stripe: {err_promo}", "warning")
            except Exception as stripe_err:
                current_app.logger.error(f"STRIPE_COUPON_EDIT_ERROR ({codigo}): {stripe_err}")
                flash("Datos guardados, pero hubo un error al crear el cupón en Stripe.", "warning")
        elif codigo_cambio:
            try:
                _actualizar_estado_promo_stripe(codigo_anterior, False)
                _actualizar_estado_promo_stripe(codigo, esta_activa)
                flash("Datos actualizados y estado de códigos sincronizado en Stripe.", "success")
            except Exception as stripe_err:
                current_app.logger.error(f"STRIPE_PROMO_CODE_CHANGE_ERROR ({codigo}): {stripe_err}")
                flash("Datos actualizados, pero hubo un error al sincronizar el código en Stripe.", "warning")
        else:
            flash("Datos de la embajadora actualizados.", "success")

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"AMBASSADOR_EDIT_ERROR ({amb_id}): {e}")
        flash("No se pudieron guardar los cambios.", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))


# =============================================================================
# ADMIN — ELIMINAR EMBAJADORA (solo si no tiene historial asociado)
# =============================================================================

@embajadoras_bp.route('/admin/embajadoras/<int:amb_id>/eliminar', methods=['POST'])
@admin_required
def admin_eliminar(amb_id):
    if session.get('role', 0) < 1:
        flash("No tienes permisos para eliminar embajadoras.", "danger")
        return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, name, code FROM ambassadors WHERE id = %s", (amb_id,))
        amb = cursor.fetchone()
        if not amb:
            flash("No se encontró la embajadora.", "warning")
            return redirect(url_for('embajadoras.admin_lista'))

        codigo_borrado = (amb['code'] or '').upper()

        cursor.execute("DELETE FROM ambassador_rewards WHERE ambassador_id = %s", (amb_id,))
        cursor.execute("DELETE FROM ambassador_referrals WHERE ambassador_id = %s", (amb_id,))
        cursor.execute("DELETE FROM ambassadors WHERE id = %s", (amb_id,))

        _log_admin(
            cursor,
            f"Eliminada embajadora '{amb['name']}' (ID:{amb_id}, código {amb['code']})",
            "Eliminación de embajadora"
        )
        conn.commit()

        try:
            _actualizar_estado_promo_stripe(codigo_borrado, False)
        except Exception as stripe_err:
            current_app.logger.error(f"STRIPE_PROMO_DISABLE_ERROR ({codigo_borrado}): {stripe_err}")

        flash(f"Embajadora '{amb['name']}' eliminada.", "success")
        return redirect(url_for('embajadoras.admin_lista'))

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"AMBASSADOR_DELETE_ERROR ({amb_id}): {e}")
        flash("No se pudo eliminar la embajadora.", "danger")
        return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))
    finally:
        cursor.close()
        conn.close()


# =============================================================================
# ADMIN — PAUSAR / REACTIVAR CÓDIGO
# =============================================================================

@embajadoras_bp.route('/admin/embajadoras/<int:amb_id>/estado', methods=['POST'])
@admin_required
def admin_estado(amb_id):
    if session.get('role', 0) < 1:
        flash("No tienes permisos para cambiar el estado.", "danger")
        return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

    nuevo = (request.form.get('status') or '').strip()
    if nuevo not in ('active', 'paused'):
        flash("Estado inválido.", "warning")
        return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE ambassadors
            SET status = %s, updated_at = NOW()
            WHERE id = %s
            RETURNING name, code
        """, (nuevo, amb_id))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            flash("No se encontró la embajadora.", "warning")
            return redirect(url_for('embajadoras.admin_lista'))

        _log_admin(
            cursor,
            f"Embajadora '{row['name']}' (ID:{amb_id}), código {row['code']}: {'reactivada' if nuevo == 'active' else 'pausada'}",
            "Estado de embajadora"
        )
        conn.commit()

        try:
            _actualizar_estado_promo_stripe((row['code'] or '').upper(), nuevo == 'active')
        except Exception as stripe_err:
            current_app.logger.error(f"STRIPE_PROMO_TOGGLE_ERROR ({row['code']}): {stripe_err}")

        flash(
            f"El código {row['code']} quedó {'activo' if nuevo == 'active' else 'en pausa'}.",
            "success"
        )
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"AMBASSADOR_STATUS_ERROR ({amb_id}): {e}")
        flash("No se pudo cambiar el estado.", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))


# =============================================================================
# ADMIN — VINCULAR CON UNA CUENTA
# =============================================================================

@embajadoras_bp.route('/admin/embajadoras/<int:amb_id>/vincular', methods=['POST'])
@admin_required
def admin_vincular(amb_id):
    if session.get('role', 0) < 1:
        flash("No tienes permisos para vincular cuentas.", "danger")
        return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

    email = (request.form.get('email') or '').strip().lower()
    if not email:
        flash("Escribe el correo de la cuenta que quieres vincular.", "warning")
        return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

    user_id = None
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, user_id, name FROM ambassadors WHERE id = %s", (amb_id,))
        amb = cursor.fetchone()
        if not amb:
            flash("No se encontró la embajadora.", "warning")
            return redirect(url_for('embajadoras.admin_lista'))
        if amb['user_id']:
            flash("Esta embajadora ya tiene una cuenta vinculada.", "info")
            return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

        cursor.execute("SELECT id, username FROM usuarios WHERE LOWER(email) = %s", (email,))
        user = cursor.fetchone()
        if not user:
            flash("No hay una cuenta de Sianeffects con ese correo.", "warning")
            return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

        cursor.execute(
            "SELECT id FROM ambassadors WHERE user_id = %s AND id <> %s",
            (user['id'], amb_id)
        )
        if cursor.fetchone():
            flash("Esa cuenta ya está vinculada a otra embajadora.", "warning")
            return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

        cursor.execute(
            "UPDATE ambassadors SET user_id = %s, updated_at = NOW() WHERE id = %s",
            (user['id'], amb_id)
        )
        _log_admin(
            cursor,
            f"Embajadora '{amb['name']}' (ID:{amb_id}) vinculada a '{user['username']}' (ID:{user['id']})",
            "Vinculación de embajadora"
        )
        conn.commit()
        user_id = user['id']
        flash(f"Cuenta de {user['username']} vinculada.", "success")

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"AMBASSADOR_LINK_ERROR ({amb_id}): {e}")
        flash("No se pudo vincular la cuenta.", "danger")
    finally:
        cursor.close()
        conn.close()

    # Las recompensas que esperaban una cuenta se aplican ahora
    if user_id:
        aplicar_recompensas_embajadora(user_id)

    return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))


# =============================================================================
# ADMIN — REINTENTAR RECOMPENSAS PENDIENTES
# =============================================================================

@embajadoras_bp.route('/admin/embajadoras/<int:amb_id>/reintentar', methods=['POST'])
@admin_required
def admin_reintentar(amb_id):
    if session.get('role', 0) < 1:
        flash("No tienes permisos para aplicar recompensas.", "danger")
        return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT user_id FROM ambassadors WHERE id = %s", (amb_id,))
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if not row:
        flash("No se encontró la embajadora.", "warning")
        return redirect(url_for('embajadoras.admin_lista'))
    if not row['user_id']:
        flash("Primero vincula una cuenta de Sianeffects para poder aplicar sus recompensas.", "warning")
        return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

    try:
        antes = _contar_pendientes(amb_id)
        aplicar_recompensas_embajadora(row['user_id'])
        despues = _contar_pendientes(amb_id)
    except Exception as e:
        current_app.logger.error(f"AMBASSADOR_RETRY_ERROR ({amb_id}): {e}")
        flash("No se pudo ejecutar el reintento.", "danger")
        return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))

    if antes == 0:
        flash("No había recompensas pendientes.", "info")
    elif despues == 0:
        flash(f"Se aplicaron {antes} recompensa(s) pendiente(s).", "success")
    else:
        flash(
            f"Quedan {despues} recompensa(s) pendiente(s). Revisa el aviso de Telegram o el registro de errores.",
            "warning"
        )

    return redirect(url_for('embajadoras.admin_detalle', amb_id=amb_id))


# =============================================================================
# PANEL DE LA EMBAJADORA (solo lectura)
# =============================================================================

@embajadoras_bp.route('/embajadora')
@login_required
def panel():
    user_id = session.get('user_id')

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, name, code, status
            FROM ambassadors
            WHERE user_id = %s
            LIMIT 1
        """, (user_id,))
        amb = cursor.fetchone()
        if not amb:
            flash("Esta cuenta no es de embajadora.", "info")
            return redirect(url_for('main.index'))
        amb = dict(amb)

        cursor.execute("""
            SELECT status, registered_at, first_payment_at
            FROM ambassador_referrals
            WHERE ambassador_id = %s
            ORDER BY registered_at ASC
        """, (amb['id'],))
        invitaciones = []
        for n, r in enumerate(cursor.fetchall(), start=1):
            estado = r['status']
            if estado == 'converted':
                txt, clase = 'Contrató PRO', 'success'
            elif estado == 'rejected':
                txt, clase = 'No aplica', 'secondary'
            else:
                txt, clase = 'Se registró', 'info'
            invitaciones.append({
                'numero': n,
                'estado_txt': txt,
                'estado_clase': clase,
                'registrado': _fmt(r['registered_at']),
                'primer_pago': _fmt(r['first_payment_at']) if r['first_payment_at'] else '—',
            })
        invitaciones.reverse()

        cursor.execute("""
            SELECT days_awarded, awarded_at, applied_at
            FROM ambassador_rewards
            WHERE ambassador_id = %s
            ORDER BY id DESC
        """, (amb['id'],))
        recompensas = []
        for w in cursor.fetchall():
            recompensas.append({
                'dias': w['days_awarded'],
                'fecha': _fmt(w['awarded_at']),
                'aplicada': bool(w['applied_at']),
            })

        cursor.execute("""
            SELECT estado_suscripcion, subscription_end, stripe_subscription_id
            FROM usuarios
            WHERE id = %s
        """, (user_id,))
        cuenta = cursor.fetchone() or {}
        estado_cuenta = (cuenta.get('estado_suscripcion') or '').strip().lower()
        con_cobro = bool(cuenta.get('stripe_subscription_id')) and estado_cuenta in ('activo', 'active')

        resumen = {
            'registrados': sum(1 for i in invitaciones if i['estado_txt'] != 'No aplica'),
            'convertidos': sum(1 for i in invitaciones if i['estado_txt'] == 'Contrató PRO'),
            'dias_ganados': sum(r['dias'] for r in recompensas),
            'dias_en_proceso': sum(r['dias'] for r in recompensas if not r['aplicada']),
        }

        enlace = url_for('auth.registro', ref=amb['code'], _external=True)
        mensaje = f"Prueba Sianeffects gratis con mi código {amb['code']}: {enlace}"

        return render_template(
            'embajadora_panel.html',
            amb=amb,
            enlace=enlace,
            wa_url="https://wa.me/?text=" + quote(mensaje),
            resumen=resumen,
            invitaciones=invitaciones,
            recompensas=recompensas,
            vigente_hasta=_fmt(cuenta.get('subscription_end')),
            con_cobro=con_cobro,
            dias_por_referido=DIAS_RECOMPENSA_EMBAJADORA,
        )

    except Exception as e:
        current_app.logger.error(f"AMBASSADOR_PANEL_ERROR (usuario {user_id}): {e}")
        flash("No pudimos cargar tu panel. Intenta de nuevo.", "danger")
        return redirect(url_for('main.index'))
    finally:
        cursor.close()
        conn.close()

# =============================================================================
# PAGINA PRINCIPAL EMBAJADORAS INFO
# =============================================================================
@embajadoras_bp.route('/programa-embajadoras')
def programa_embajadoras():
    return render_template(
        'embajadoras_sianeffects.html',
        year=datetime.now().year,
    )