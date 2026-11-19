from dateutil.relativedelta import relativedelta
from db import get_db_connection
import stripe
import os
from flask import Blueprint, request, redirect, jsonify, current_app, render_template, url_for, session
from helpers import login_required
from db import get_db_connection as get_db
from services.mail_service import enviar_correo_sian
from utils.datetime_utils import now_utc
from datetime import timedelta, timezone, datetime
import json
import urllib.request

# Registramos el Blueprint para segmentar la lógica de pagos
payments_bp = Blueprint('payments', __name__)

# Configuración de llaves de Stripe
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
endpoint_secret = os.getenv('STRIPE_WEBHOOK_SECRET') 

# Días que gana una embajadora por cada referido con primer pago confirmado
# (la tabla ambassador_rewards tiene un CHECK days_awarded = 7)
DIAS_RECOMPENSA_EMBAJADORA = 7


def notificar_admin(texto):
    """Envía un aviso a Telegram. Nunca debe romper el webhook."""
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        return
    try:
        data = json.dumps({'chat_id': chat_id, 'text': texto}).encode('utf-8')
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            headers={'Content-Type': 'application/json'}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        current_app.logger.error(f"NOTIFY_ERROR: {e}")


def obtener_cupon_usado(session_id):
    """Devuelve el código de promoción (o cupón) usado en un Checkout, o None."""
    try:
        sesion = stripe.checkout.Session.retrieve(session_id, expand=['total_details.breakdown'])
        for d in (sesion.total_details.breakdown.discounts or []):
            disc = d.discount
            promo = getattr(disc, 'promotion_code', None)
            if promo:
                promo_id = promo if isinstance(promo, str) else promo.id
                return stripe.PromotionCode.retrieve(promo_id).code
            source = getattr(disc, 'source', None)
            coupon = getattr(source, 'coupon', None) if source else None
            if coupon:
                coupon_id = coupon if isinstance(coupon, str) else coupon.id
                cp = stripe.Coupon.retrieve(coupon_id)
                return cp.name or coupon_id
    except Exception as e:
        current_app.logger.error(f"COUPON_LOOKUP_ERROR: {e}")
    return None


def get_invoice_subscription_id(invoice_obj):
    """
    Obtiene el ID de suscripción de una Invoice sin importar la versión de API.
    - API >= 2025-03-31 (basil): invoice.parent.subscription_details.subscription
    - API anterior: invoice.subscription
    Devuelve None si la factura no pertenece a una suscripción.
    """
    try:
        sub = invoice_obj.parent.subscription_details.subscription
        if sub:
            return sub if isinstance(sub, str) else sub.id
    except (AttributeError, KeyError, TypeError):
        pass

    try:
        sub = invoice_obj.subscription
        if sub:
            return sub if isinstance(sub, str) else sub.id
    except (AttributeError, KeyError, TypeError):
        pass

    return None


def suscripcion_sigue_viva(stripe_subscription_id):
    """
    Consulta en Stripe si una suscripción sigue vigente.
    - Devuelve False si no existe o está cancelada/vencida.
    - Ante cualquier otro error (red, API) devuelve True para NO pisar datos por duda.
    """
    try:
        sub = stripe.Subscription.retrieve(stripe_subscription_id)
        return sub.status in ('active', 'trialing', 'past_due', 'unpaid')
    except Exception as e:
        if getattr(e, 'http_status', None) == 404 or getattr(e, 'code', None) == 'resource_missing':
            return False
        current_app.logger.error(f"SUB_LOOKUP_ERROR {stripe_subscription_id}: {e}")
        return True


def obtener_fin_periodo_stripe(sub):
    """
    Devuelve (unix) el fin del período actual de una suscripción, sin importar la versión de API.
    - API anterior: sub.current_period_end
    - API >= 2025-03-31 (basil): sub.items.data[i].current_period_end
    Si la suscripción está en 'trialing' (cobro ya movido por una recompensa), respeta trial_end.
    """
    fin = None
    try:
        fin = sub['current_period_end']
    except (KeyError, AttributeError, TypeError):
        fin = None

    if not fin:
        items = sub['items']['data']
        fines = [i['current_period_end'] for i in items if i.get('current_period_end')]
        fin = max(fines) if fines else None

    trial_end = sub.get('trial_end')
    if sub.get('status') == 'trialing' and trial_end:
        fin = max(fin or 0, trial_end)

    if not fin:
        raise ValueError("No se pudo determinar el fin del período de la suscripción")
    return int(fin)

# =============================================================================
# CREAR SESIÓN DE PAGO (CHECKOUT)
# =============================================================================
@payments_bp.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    """
    Inicia el flujo de pago enviando al cliente a la pasarela de Stripe.
    Previene la creación de suscripciones duplicadas si el usuario ya tiene una activa o fallida.
    """
    price_id = request.form.get('price_id')
    user_id = session.get('user_id') 
    
    try:
        # 1. Buscamos si el usuario ya existe como cliente en Stripe
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT stripe_customer_id FROM usuarios WHERE id = %s", (user_id,))
        user_db = cursor.fetchone()
        cursor.close()
        conn.close()

        customer_id = user_db['stripe_customer_id'] if user_db else None

        # 2. LÓGICA DE PROTECCIÓN: Prevenir suscripción doble
        if customer_id:
            # Consultamos las suscripciones de este cliente directo en Stripe
            subs = stripe.Subscription.list(customer=customer_id, status='all', limit=5)
            
            for sub in subs.data:
                # Si encontramos una suscripción que está activa, en prueba, atrasada o sin pagar
                if sub.status in ['active', 'trialing', 'past_due', 'unpaid']:
                    current_app.logger.info(f"Usuario {user_id} intentó duplicar suscripción. Redirigiendo al portal de facturación.")
                    # Lo mandamos directo al Portal de Facturación para que arregle su pago actual
                    portal_session = stripe.billing_portal.Session.create(
                        customer=customer_id,
                        return_url=url_for('configuracion.configuracion', _external=True) + "#list-suscripcion"
                    )
                    return redirect(portal_session.url, code=303)

        # 3. Preparamos los metadatos
        metadatos_sian = {
            'user_id': str(user_id),
            'plan_type': 'anual' if '1490' in str(price_id) else 'mensual'
        }

        # 4. Preparamos los parámetros del checkout
        checkout_params = {
            'line_items': [{'price': price_id, 'quantity': 1}],
            'mode': 'subscription',
            'allow_promotion_codes': True,
            'success_url': url_for('payments.pago_exitoso', _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            'cancel_url': url_for('payments.pago_cancelado', _external=True),
            'metadata': metadatos_sian,
            'subscription_data': {
                'metadata': metadatos_sian
            }
        }
        
        # Si ya tiene un ID de cliente, se lo pasamos para no duplicar perfiles en Stripe
        if customer_id:
            checkout_params['customer'] = customer_id

        checkout_session = stripe.checkout.Session.create(**checkout_params)
        
        if not checkout_session.url:
            raise ValueError("Stripe no devolvió una URL de checkout válida.")
            
        return redirect(checkout_session.url, code=303)
        
    except Exception as e:
        current_app.logger.error(f"STRIPE_ERROR en create-checkout-session: {e}")
        return jsonify(error=str(e)), 403


# =============================================================================
# WEBHOOK: El cerebro de la automatización (Recibe notificaciones de Stripe)
# =============================================================================
@payments_bp.route('/webhook', methods=['POST'])
def webhook():
    """
    Endpoint que escucha las notificaciones de Stripe en tiempo real.
    Delegará las acciones a las funciones correspondientes pasando siempre el ID de la suscripción.
    """
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except Exception as e:
        current_app.logger.error(f"WEBHOOK_SIGNATURE_ERROR: {e}")
        return jsonify(success=False), 400

    # 1. Pago inicial completado (Nueva suscripción)
    if event.type == 'checkout.session.completed':
        session_obj = event.data.object
        try:
            user_id = session_obj.metadata.user_id
            plan = session_obj.metadata.plan_type
            stripe_session_id = session_obj.id
            stripe_customer_id = session_obj.customer
            stripe_subscription_id = session_obj.subscription 
            
            if user_id:
                cupon = obtener_cupon_usado(stripe_session_id)
                procesar_pago_exitoso(user_id, plan, stripe_session_id, stripe_customer_id, stripe_subscription_id, monto=(session_obj.amount_total or 0) / 100, cupon=cupon)
        except Exception as e:
            current_app.logger.error(f"Falta un dato clave en webhook checkout.session.completed: {e}")

    # 2. Suscripción eliminada (Cancelación)
    elif event.type == 'customer.subscription.deleted':
        subscription_obj = event.data.object
        stripe_customer_id = subscription_obj.customer
        stripe_subscription_id = subscription_obj.id 
        procesar_cancelacion(stripe_customer_id, stripe_subscription_id)

    # 3. Pago recurrente fallido
    elif event.type == 'invoice.payment_failed':
        invoice_obj = event.data.object
        stripe_customer_id = invoice_obj.customer
        stripe_subscription_id = get_invoice_subscription_id(invoice_obj)
        if stripe_subscription_id:
            procesar_pago_fallido(stripe_customer_id, stripe_subscription_id)
        else:
            current_app.logger.info(f"IGNORADO: invoice.payment_failed sin suscripción asociada (customer {stripe_customer_id}).")

    # 4. Pago recurrente exitoso (Renovación)
    elif event.type == 'invoice.paid':
        invoice_obj = event.data.object
        motivos_validos = ['subscription_cycle', 'subscription_update', 'subscription_create']
        
        if invoice_obj.billing_reason in motivos_validos:
            stripe_customer_id = invoice_obj.customer
            stripe_subscription_id = get_invoice_subscription_id(invoice_obj)
            if stripe_subscription_id:
                procesar_resurreccion(stripe_customer_id, invoice_obj, stripe_subscription_id)
            else:
                current_app.logger.info(f"IGNORADO: invoice.paid sin suscripción asociada (customer {stripe_customer_id}).")

    return jsonify(success=True)

# =============================================================================
# FUNCIONES DE LÓGICA DE NEGOCIO Y BASE DE DATOS
# =============================================================================
def procesar_pago_exitoso(user_id, plan, stripe_session_id, stripe_customer_id, stripe_subscription_id, monto=None, cupon=None):
    """
    Activa al usuario y guarda el ID exacto de la nueva suscripción de Stripe en la BD.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT username, email, subscription_end FROM usuarios WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        
        if user:
            ahora = now_utc()
            current_end = user['subscription_end']
            
            if current_end and current_end.tzinfo is None:
                current_end = current_end.replace(tzinfo=timezone.utc)

            # Si sigue activo sumamos desde su vencimiento, si ya venció sumamos desde hoy
            base_date = current_end if (current_end and current_end > ahora) else ahora

            if plan == 'anual':
                nueva_fecha = base_date + relativedelta(years=1)
            else:
                nueva_fecha = base_date + relativedelta(months=1)
            
            nueva_fecha = nueva_fecha.replace(hour=23, minute=59, second=59)

            # Guardamos el estado y amarramos el stripe_subscription_id
            cursor.execute("""
                UPDATE usuarios 
                SET subscription_end = %s, 
                    estado_suscripcion = 'Activo',
                    stripe_customer_id = %s,
                    stripe_subscription_id = %s, 
                    plan_type = %s
                WHERE id = %s
            """, (nueva_fecha, stripe_customer_id, stripe_subscription_id, plan, user_id))
            
            dias_agregados = (nueva_fecha - (current_end if current_end else ahora)).days
            cursor.execute("""
                INSERT INTO logs_actividad (user_id, accion, modulo, detalle)
                VALUES (%s, %s, %s, %s)
            """, (user_id, f"Renovación {plan} exitosa", "Pagos", f"Stripe ID: {stripe_session_id} | Se sumaron {dias_agregados} días."))

            conn.commit()

            enviar_correo_sian(
                subject="¡Pago Confirmado! Bienvenido a Sianeffects PRO ✨",
                recipient=user['email'],
                template="pago_confirmado",
                sender_alias="pagos", 
                username=user['username']
            )
            
            current_app.logger.info(f"PAYMENT_SUCCESS: Usuario {user_id} actualizado a PRO ({plan}).")

            monto_txt = f"${monto:,.2f} MXN" if monto is not None else "—"
            notificar_admin(
                f"💰 Nueva suscripción PRO ({plan})\n"
                f"Usuario: {user['username']} (ID {user_id})\n"
                f"Email: {user['email']}\n"
                f"Monto: {monto_txt}\n"
                f"Cupón: {cupon or 'ninguno'}\n"
                f"Vigente hasta: {nueva_fecha.strftime('%d/%m/%Y')}"
            )

            # Respaldo: si invoice.paid llegó antes de guardar stripe_customer_id,
            # registramos y aplicamos aquí la recompensa de embajadora (es idempotente).
            if stripe_subscription_id and (monto or 0) > 0:
                try:
                    sub = stripe.Subscription.retrieve(stripe_subscription_id)
                    latest_inv = getattr(sub, 'latest_invoice', None)
                    inv_id = latest_inv if isinstance(latest_inv, str) else getattr(latest_inv, 'id', None)
                    if inv_id:
                        cursor.execute("SAVEPOINT sp_embajadora_checkout")
                        rec = registrar_recompensa_embajadora(cursor, int(user_id), inv_id)
                        cursor.execute("RELEASE SAVEPOINT sp_embajadora_checkout")
                        conn.commit()
                        if rec and rec.get('ambassador_user_id'):
                            aplicar_recompensas_embajadora(rec['ambassador_user_id'])
                except Exception as emb_err:
                    try:
                        cursor.execute("ROLLBACK TO SAVEPOINT sp_embajadora_checkout")
                    except Exception:
                        pass
                    current_app.logger.error(f"AMBASSADOR_CHECKOUT_FALLBACK_ERROR ({user_id}): {emb_err}")

    except Exception as e:
        if conn: conn.rollback()
        current_app.logger.error(f"PAYMENT_PROCESS_ERROR para usuario {user_id}: {e}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def procesar_cancelacion(stripe_customer_id, stripe_subscription_id):
    """
    Revoca el acceso PRO. Incluye retrocompatibilidad para usuarios con ID NULL en PRD.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, email, username, stripe_subscription_id FROM usuarios WHERE stripe_customer_id = %s", (stripe_customer_id,))
        user = cursor.fetchone()

        # RETROCOMPATIBILIDAD: Pasa si coinciden, O si en PRD todavía está vacío
        if user and (user['stripe_subscription_id'] == stripe_subscription_id or not user['stripe_subscription_id']):
            cursor.execute("""
                UPDATE usuarios 
                SET estado_suscripcion = 'Cancelado',
                    fecha_cancelacion = %s,
                    stripe_subscription_id = NULL
                WHERE id = %s
            """, (now_utc(), user['id']))

            cursor.execute("""
                INSERT INTO logs_actividad (user_id, accion, modulo, detalle)
                VALUES (%s, %s, %s, %s)
            """, (user['id'], "Suscripción Finalizada", "Pagos", "Cancelación procesada vía Webhook"))

            conn.commit()
            current_app.logger.info(f"SUBSCRIPTION_DELETED: El usuario {user['id']} ha cancelado su suscripción.")
            notificar_admin(f"❌ Cancelación\nUsuario: {user['username']} (ID {user['id']})\nEmail: {user['email']}")
        else:
            current_app.logger.info(f"IGNORADO: Cancelación de una suscripción antigua para customer {stripe_customer_id}")

    except Exception as e:
        if conn: conn.rollback()
        current_app.logger.error(f"CANCEL_PROCESS_ERROR: {e}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def procesar_pago_fallido(stripe_customer_id, stripe_subscription_id):
    """
    Bloquea temporalmente el acceso. Actualiza el ID en PRD si estaba vacío.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, email, username, stripe_subscription_id FROM usuarios WHERE stripe_customer_id = %s", (stripe_customer_id,))
        user = cursor.fetchone()

        # RETROCOMPATIBILIDAD
        if user and (user['stripe_subscription_id'] == stripe_subscription_id or not user['stripe_subscription_id']):
            cursor.execute("""
                UPDATE usuarios 
                SET estado_suscripcion = 'Pago Fallido',
                    stripe_subscription_id = %s -- Auto-sanado para usuarios de PRD
                WHERE id = %s
            """, (stripe_subscription_id, user['id']))

            cursor.execute("""
                INSERT INTO logs_actividad (user_id, accion, modulo, detalle)
                VALUES (%s, %s, %s, %s)
            """, (user['id'], "Fallo de Pago", "Pagos", "Intento de cobro automático rechazado"))

            conn.commit()
            current_app.logger.info(f"PAYMENT_FAILED: Cobro fallido para usuario {user['id']}.")
            notificar_admin(f"⚠️ Pago fallido\nUsuario: {user['username']} (ID {user['id']})\nEmail: {user['email']}")

            enviar_correo_sian(
                subject="💳 Acción Requerida: Problema con tu pago de Sianeffects",
                recipient=user['email'],
                template="pago_fallido", 
                sender_alias="pagos", 
                username=user['username']
            )
        else:
             current_app.logger.info(f"IGNORADO: Fallo de pago de una suscripción antigua para customer {stripe_customer_id}")

    except Exception as e:
        if conn: conn.rollback()
        current_app.logger.error(f"FAIL_PROCESS_ERROR: {e}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# =============================================================================
# EMBAJADORAS SIANEFFECTS
# Fase 1: registrar_recompensa_embajadora() -> dentro de la transacción del pago
#         del referido; solo escribe en el libro de recompensas (idempotente).
# Fase 2: aplicar_recompensas_embajadora()  -> después del commit; aplica los
#         días pendientes al acceso y, si hay PRO activo, mueve el cobro de Stripe.
# =============================================================================
def registrar_recompensa_embajadora(cursor, referred_user_id, invoice_id):
    """
    Registra (sin tocar todavía el acceso) la recompensa de 7 días por el primer pago
    confirmado de un referido. Debe llamarse dentro de una transacción abierta.

    Idempotencia:
      - Solo procesa referidos en estado 'registered'.
      - UNIQUE por referido y por factura (payment_reference) en ambassador_rewards.

    Devuelve None si no hubo recompensa nueva, o un dict
    {'reward_id', 'ambassador_user_id'} (ambassador_user_id puede ser None si la
    embajadora aún no está vinculada a una cuenta; la recompensa queda pendiente).
    """
    cursor.execute("""
        SELECT
            r.id AS referral_id,
            a.id AS ambassador_id,
            a.user_id AS ambassador_user_id
        FROM ambassador_referrals r
        JOIN ambassadors a ON a.id = r.ambassador_id
        WHERE r.referred_user_id = %s
          AND r.status = 'registered'
        LIMIT 1
        FOR UPDATE OF r
    """, (referred_user_id,))

    referral = cursor.fetchone()

    if not referral:
        return None

    # Una embajadora no gana por su propia cuenta
    if referral['ambassador_user_id'] is not None and referral['ambassador_user_id'] == referred_user_id:
        cursor.execute("""
            UPDATE ambassador_referrals
            SET status = 'rejected'
            WHERE id = %s
        """, (referral['referral_id'],))
        return None

    cursor.execute("""
        INSERT INTO ambassador_rewards (
            ambassador_id,
            referral_id,
            days_awarded,
            payment_reference
        )
        VALUES (%s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        RETURNING id
    """, (
        referral['ambassador_id'],
        referral['referral_id'],
        DIAS_RECOMPENSA_EMBAJADORA,
        invoice_id
    ))

    reward = cursor.fetchone()

    if not reward:
        return None

    cursor.execute("""
        UPDATE ambassador_referrals
        SET status = 'converted',
            first_payment_at = NOW()
        WHERE id = %s
    """, (referral['referral_id'],))

    return {
        'reward_id': reward['id'],
        'ambassador_user_id': referral['ambassador_user_id']
    }


def aplicar_recompensas_embajadora(ambassador_user_id):
    """
    Aplica al acceso de la embajadora sus recompensas pendientes (applied_at IS NULL) y,
    cuando contrata PRO, le suma los días de recompensa que ganó durante su prueba.

    - PRO activo con suscripción de Stripe: mueve el próximo cobro con trial_end
      (sin prorrateos ni cobros inmediatos) y sincroniza subscription_end con esa fecha.
      Aquí también se trasladan los días ganados en prueba (applied_mode='local') que
      todavía no se hayan consumido: cada recompensa local ocupa un tramo de días al final
      de su prueba (local_until), así que solo cuenta lo que falte por transcurrir.
    - Prueba / sin suscripción de Stripe: extiende solo usuarios.subscription_end y deja
      anotado hasta dónde llega cada recompensa (local_until).
    - Pago fallido u otro estado con suscripción: las recompensas esperan pendientes
      y se reintentan en el siguiente invoice.paid de la embajadora.

    Es seguro llamarla varias veces: lo ya aplicado o trasladado no se repite. En Stripe la
    suscripción guarda metadata.amb_last_reward_id para no mover dos veces los mismos días
    si un reintento ocurre después de un fallo al guardar en la BD.
    Nunca lanza excepciones: ante cualquier fallo las recompensas siguen pendientes.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, username, email, estado_suscripcion, stripe_subscription_id, subscription_end
            FROM usuarios
            WHERE id = %s
            FOR UPDATE
        """, (ambassador_user_id,))
        user = cursor.fetchone()

        if not user:
            conn.rollback()
            return False

        cursor.execute("""
            SELECT r.id, r.days_awarded
            FROM ambassador_rewards r
            JOIN ambassadors a ON a.id = r.ambassador_id
            WHERE a.user_id = %s
              AND r.applied_at IS NULL
            ORDER BY r.id
            FOR UPDATE OF r
        """, (ambassador_user_id,))
        pendientes = cursor.fetchall()

        sub_id = user['stripe_subscription_id']
        estado = (user['estado_suscripcion'] or '').strip().lower()

        if sub_id and estado in ('activo', 'active'):
            modo = 'stripe'
        elif sub_id:
            modo = 'espera'
        else:
            modo = 'local'

        # Días ganados en prueba que se trasladan al contratar PRO
        locales = []
        if modo == 'stripe':
            cursor.execute("""
                SELECT r.id,
                       r.days_awarded,
                       GREATEST(0, LEAST(
                           r.days_awarded,
                           CEIL(EXTRACT(EPOCH FROM (r.local_until - NOW())) / 86400.0)
                       ))::int AS dias_restantes
                FROM ambassador_rewards r
                JOIN ambassadors a ON a.id = r.ambassador_id
                WHERE a.user_id = %s
                  AND r.applied_mode = 'local'
                  AND r.carried_at IS NULL
                  AND r.local_until IS NOT NULL
                ORDER BY r.id
                FOR UPDATE OF r
            """, (ambassador_user_id,))
            locales = cursor.fetchall()

        if not pendientes and not locales:
            conn.rollback()
            return False

        if modo == 'espera':
            conn.rollback()
            current_app.logger.info(
                f"AMBASSADOR_APPLY_DEFERRED: usuario {ambassador_user_id} con estado '{user['estado_suscripcion']}'; "
                f"recompensas siguen pendientes."
            )
            return False

        ids = [p['id'] for p in pendientes]
        total_dias = sum(p['days_awarded'] for p in pendientes)
        dias_trial = 0
        base_date = None

        if modo == 'stripe':
            sub = stripe.Subscription.retrieve(sub_id)

            if sub.status not in ('active', 'trialing'):
                conn.rollback()
                current_app.logger.info(
                    f"AMBASSADOR_APPLY_DEFERRED: suscripción {sub_id} en estado Stripe '{sub.status}'; recompensas pendientes."
                )
                return False

            fin_stripe = obtener_fin_periodo_stripe(sub)

            meta = sub.get('metadata') or {}
            try:
                ya_movido = int(meta.get('amb_last_reward_id') or 0)
            except (TypeError, ValueError):
                ya_movido = 0

            movibles = [(p['id'], p['days_awarded']) for p in pendientes] + \
                       [(l['id'], l['dias_restantes']) for l in locales]

            dias_por_mover = sum(d for (i, d) in movibles if i > ya_movido)
            dias_trial = sum(l['dias_restantes'] for l in locales if l['id'] > ya_movido)

            if dias_por_mover > 0:
                nuevo_fin_unix = fin_stripe + dias_por_mover * 86400
                stripe.Subscription.modify(
                    sub_id,
                    trial_end=nuevo_fin_unix,
                    proration_behavior='none',
                    metadata={'amb_last_reward_id': str(max(i for (i, _) in movibles))}
                )
            else:
                # Stripe ya tenía estos días (reintento tras fallo de BD) o no hay nada que mover
                nuevo_fin_unix = fin_stripe

            nueva_fecha = datetime.fromtimestamp(nuevo_fin_unix, tz=timezone.utc)
            dias_aplicados = dias_por_mover
        else:
            ahora = now_utc()
            fecha_actual = user['subscription_end']

            if fecha_actual and fecha_actual.tzinfo is None:
                fecha_actual = fecha_actual.replace(tzinfo=timezone.utc)

            base_date = fecha_actual if (fecha_actual and fecha_actual > ahora) else ahora
            nueva_fecha = base_date + timedelta(days=total_dias)
            dias_aplicados = total_dias

        cursor.execute("""
            UPDATE usuarios
            SET subscription_end = %s
            WHERE id = %s
        """, (nueva_fecha, ambassador_user_id))

        if modo == 'stripe':
            if ids:
                cursor.execute("""
                    UPDATE ambassador_rewards
                    SET applied_at = NOW(),
                        applied_mode = 'stripe'
                    WHERE id = ANY(%s)
                """, (ids,))
            if locales:
                cursor.execute("""
                    UPDATE ambassador_rewards
                    SET carried_at = NOW()
                    WHERE id = ANY(%s)
                """, ([l['id'] for l in locales],))
        else:
            # Cada recompensa ocupa su propio tramo al final de la prueba
            acumulado = base_date
            for p in pendientes:
                acumulado = acumulado + timedelta(days=p['days_awarded'])
                cursor.execute("""
                    UPDATE ambassador_rewards
                    SET applied_at = NOW(),
                        applied_mode = 'local',
                        local_until = %s
                    WHERE id = %s
                """, (acumulado, p['id']))

        if pendientes or dias_aplicados > 0:
            detalle = f"Se agregaron {dias_aplicados} días ({modo})"
            if dias_trial:
                detalle += f", {dias_trial} trasladados de su prueba"
            detalle += f". Nuevo vencimiento: {nueva_fecha.strftime('%d/%m/%Y')}."
            cursor.execute("""
                INSERT INTO logs_actividad (user_id, accion, modulo, detalle)
                VALUES (%s, %s, %s, %s)
            """, (ambassador_user_id, 'Recompensa de embajadora', 'Embajadoras', detalle))

        conn.commit()

        if pendientes or dias_aplicados > 0:
            current_app.logger.info(
                f"AMBASSADOR_REWARD_APPLIED: usuario {ambassador_user_id} +{dias_aplicados} días ({modo}) hasta {nueva_fecha}."
            )
            notificar_admin(
                f"🎁 Recompensa de embajadora aplicada ({modo})\n"
                f"Usuario: {user['username']} (ID {ambassador_user_id})\n"
                f"Días: +{dias_aplicados}" + (f" (incluye {dias_trial} de su prueba)" if dias_trial else "") + "\n"
                f"Vigente / próximo cobro: {nueva_fecha.strftime('%d/%m/%Y')}"
            )
        return True

    except Exception as e:
        if conn: conn.rollback()
        current_app.logger.error(f"AMBASSADOR_APPLY_ERROR usuario {ambassador_user_id}: {e}")
        notificar_admin(
            f"⚠️ Recompensa de embajadora PENDIENTE de aplicar\n"
            f"Usuario ID: {ambassador_user_id}\n"
            f"Error: {e}"
        )
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


def procesar_resurreccion(stripe_customer_id, invoice_obj, stripe_subscription_id):
    """
    Rehabilita el acceso y sincroniza a los usuarios antiguos de PRD.
    Si el stripe_subscription_id guardado difiere del de la factura:
      - Si la guardada ya no existe/está cancelada en Stripe -> auto-sana y renueva con la nueva.
      - Si la guardada sigue viva -> ignora el pago y avisa por Telegram (posible duplicada).
    Además registra la recompensa de embajadora cuando es el primer pago de un referido y
    reintenta aplicar recompensas pendientes de este usuario.
    """
    conn = get_db()
    cursor = conn.cursor()
    procesado_ok = False
    por_aplicar = []
    try:
        billing_reason = getattr(invoice_obj, 'billing_reason', None)

        cursor.execute(
            "SELECT id, email, username, estado_suscripcion, stripe_subscription_id, subscription_end FROM usuarios WHERE stripe_customer_id = %s",
            (stripe_customer_id,)
        )
        user = cursor.fetchone()

        # Fallback por si invoice.paid llegó antes que checkout.session.completed
        if not user:
            meta_user_id = None
            try:
                meta_user_id = invoice_obj.parent.subscription_details.metadata.get('user_id')
            except Exception:
                pass
            if not meta_user_id:
                try:
                    meta_user_id = invoice_obj.lines.data[0].metadata.get('user_id')
                except Exception:
                    pass

            if meta_user_id:
                cursor.execute(
                    "SELECT id, email, username, estado_suscripcion, stripe_subscription_id, subscription_end FROM usuarios WHERE id = %s",
                    (int(meta_user_id),)
                )
                user = cursor.fetchone()
                if user:
                    cursor.execute(
                        "UPDATE usuarios SET stripe_customer_id = %s WHERE id = %s AND stripe_customer_id IS NULL",
                        (stripe_customer_id, user['id'])
                    )

        # Pago exitoso de un customer que no existe en nuestra BD
        if not user:
            current_app.logger.warning(f"SIN_USUARIO: Pago exitoso sin usuario para customer {stripe_customer_id} (sub {stripe_subscription_id}).")
            notificar_admin(
                f"⚠️ Pago exitoso SIN usuario asociado\n"
                f"Customer: {stripe_customer_id}\n"
                f"Suscripción: {stripe_subscription_id}\n"
                f"Motivo: {billing_reason}"
            )
            return

        sub_guardada = user['stripe_subscription_id']
        auto_sanado = False

        # La BD tiene otra suscripción distinta a la de esta factura
        if sub_guardada and sub_guardada != stripe_subscription_id:
            if suscripcion_sigue_viva(sub_guardada):
                current_app.logger.info(f"IGNORADO: Pago exitoso de una suscripción antigua para customer {stripe_customer_id}")
                notificar_admin(
                    f"⚠️ Pago IGNORADO (suscripción distinta a la registrada)\n"
                    f"Usuario: {user['username']} (ID {user['id']})\n"
                    f"Email: {user['email']}\n"
                    f"Customer: {stripe_customer_id}\n"
                    f"En BD: {sub_guardada} (sigue viva)\n"
                    f"Factura: {stripe_subscription_id}\n"
                    f"Motivo: {billing_reason}\n"
                    f"Revisar posible suscripción duplicada."
                )
                return
            auto_sanado = True
            current_app.logger.warning(
                f"AUTO_SANADO: Usuario {user['id']} tenía {sub_guardada} (ya no vigente); se reemplaza por {stripe_subscription_id}."
            )

        # Fecha fin: el mayor period.end de las líneas (con prorrateos la primera línea puede ser corta)
        periodos = [l.period.end for l in invoice_obj.lines.data if getattr(l, 'period', None)]
        period_end_unix = max(periodos) if periodos else invoice_obj.period_end
        nueva_fecha_fin = datetime.fromtimestamp(period_end_unix, tz=timezone.utc)

        # Una factura de $0 (p. ej. movimiento de cobro por recompensa) nunca debe acortar el acceso vigente
        fin_actual = user['subscription_end']
        if fin_actual and fin_actual.tzinfo is None:
            fin_actual = fin_actual.replace(tzinfo=timezone.utc)
        if (getattr(invoice_obj, 'amount_paid', 0) or 0) == 0 and fin_actual and fin_actual > nueva_fecha_fin:
            nueva_fecha_fin = fin_actual

        cursor.execute("""
            UPDATE usuarios 
            SET estado_suscripcion = 'Activo',
                subscription_end = %s,
                stripe_subscription_id = %s -- Auto-sanado para usuarios de PRD
            WHERE id = %s
        """, (nueva_fecha_fin, stripe_subscription_id, user['id']))

        # Recompensa de embajadora: únicamente primer pago confirmado.
        # Va en un SAVEPOINT para que un fallo de la recompensa NUNCA revierta
        # la activación del cliente que sí pagó.
        recompensa = None
        error_recompensa = None

        if (
            billing_reason == 'subscription_create'
            and (getattr(invoice_obj, 'amount_paid', 0) or 0) > 0
            and getattr(invoice_obj, 'status', None) == 'paid'
        ):
            invoice_id = getattr(invoice_obj, 'id', None)

            if invoice_id:
                cursor.execute("SAVEPOINT recompensa_embajadora")
                try:
                    recompensa = registrar_recompensa_embajadora(cursor, user['id'], invoice_id)
                    cursor.execute("RELEASE SAVEPOINT recompensa_embajadora")
                except Exception as e:
                    cursor.execute("ROLLBACK TO SAVEPOINT recompensa_embajadora")
                    recompensa = None
                    error_recompensa = str(e)
                    current_app.logger.error(f"AMBASSADOR_REWARD_ERROR (referido {user['id']}, factura {invoice_id}): {e}")

        detalle_log = "Cobro recurrente procesado por Stripe"
        if auto_sanado:
            detalle_log += f" | ID de suscripción corregido: {sub_guardada} -> {stripe_subscription_id}"
        cursor.execute("""
            INSERT INTO logs_actividad (user_id, accion, modulo, detalle)
            VALUES (%s, %s, %s, %s)
        """, (user['id'], "Renovación Automática Exitosa", "Pagos", detalle_log))

        conn.commit()
        current_app.logger.info(f"RESURRECTION: Usuario {user['id']} renovado automáticamente hasta {nueva_fecha_fin}.")

        # Avisos de recompensa (fuera de la transacción)
        if error_recompensa:
            notificar_admin(
                f"🚨 Error registrando recompensa de embajadora\n"
                f"Referido: {user['username']} (ID {user['id']})\n"
                f"Factura: {getattr(invoice_obj, 'id', None)}\n"
                f"Error: {error_recompensa}"
            )
        elif recompensa and not recompensa['ambassador_user_id']:
            notificar_admin(
                f"⚠️ Recompensa registrada pero la embajadora NO tiene cuenta vinculada (ambassadors.user_id)\n"
                f"Referido: {user['username']} (ID {user['id']})\n"
                f"Quedará pendiente hasta vincularla."
            )

        # 'subscription_create' ya lo avisa checkout.session.completed; aquí avisamos ciclo y cambios
        if billing_reason in ('subscription_cycle', 'subscription_update'):
            monto_renov = (getattr(invoice_obj, 'amount_paid', 0) or 0) / 100
            titulo = "🔄 Renovación PRO" if billing_reason == 'subscription_cycle' else "🔁 Actualización de suscripción PRO"
            extra = f"\nℹ️ ID de suscripción corregido (antes {sub_guardada})" if auto_sanado else ""
            notificar_admin(
                f"{titulo}\n"
                f"Usuario: {user['username']} (ID {user['id']})\n"
                f"Email: {user['email']}\n"
                f"Monto: ${monto_renov:,.2f} MXN\n"
                f"Vigente hasta: {nueva_fecha_fin.strftime('%d/%m/%Y')}"
                f"{extra}"
            )

        # Cuentas a las que hay que aplicar recompensas pendientes una vez cerrada la transacción:
        # este usuario (reintento por si es embajadora) y la embajadora que acaba de ganar días.
        por_aplicar.append(user['id'])
        if recompensa and recompensa['ambassador_user_id'] and recompensa['ambassador_user_id'] != user['id']:
            por_aplicar.append(recompensa['ambassador_user_id'])

        procesado_ok = True

    except Exception as e:
        if conn: conn.rollback()
        current_app.logger.error(f"RESURRECTION_PROCESS_ERROR: {e}")
        notificar_admin(f"🚨 Error procesando renovación\nCustomer: {stripe_customer_id}\nError: {e}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

    # Aplicación de recompensas: conexión propia, después de liberar la del pago
    if procesado_ok:
        for uid in por_aplicar:
            aplicar_recompensas_embajadora(uid)

# =============================================================================
# RUTAS DE INTERFAZ DE USUARIO Y REDIRECCIONES
# =============================================================================
@payments_bp.route('/pago-exitoso')
@login_required
def pago_exitoso():
    """
    Página de éxito a la que Stripe redirige tras un pago.
    La BD ya fue actualizada por el webhook, aquí solo refrescamos la sesión de Flask.
    """
    user_id = session.get('user_id')
    
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT role, estado_suscripcion FROM usuarios WHERE id = %s", (user_id,))
        user_db = cursor.fetchone()
        if user_db:
            session['role'] = user_db['role']
            if user_db['estado_suscripcion'] == 'Activo':
                session['is_pro_active'] = True
                session.pop('grace_period', None)
    except Exception as e:
        current_app.logger.error(f"Error refrescando sesión post-pago: {e}")
    finally:
        cursor.close()
        conn.close()

    return render_template('pago_exitoso.html')

@payments_bp.route('/billing-portal', methods=['POST'])
@login_required
def billing_portal():
    """
    Genera un enlace para el portal de facturación de Stripe y redirige al usuario.
    """
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT stripe_customer_id FROM usuarios WHERE id = %s', (session['user_id'],))
    user = cursor.fetchone()
    cursor.close()
    conn.close()

    if not user or not user['stripe_customer_id']:
        return redirect(url_for('configuracion.configuracion'))

    portal_session = stripe.billing_portal.Session.create(
        customer=user['stripe_customer_id'],
        return_url=url_for('configuracion.configuracion', _external=True) + "#list-suscripcion"
    )

    return redirect(portal_session.url)

@payments_bp.route('/pago-cancelado')
@login_required
def pago_cancelado():
    """
    Página a la que Stripe redirige si el usuario cancela el flujo de pago.
    """
    return render_template('pago_cancelado.html')