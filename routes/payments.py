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

# Registramos el Blueprint para segmentar la lógica de pagos
payments_bp = Blueprint('payments', __name__)

# Configuración de llaves de Stripe
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
endpoint_secret = os.getenv('STRIPE_WEBHOOK_SECRET') 


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
                procesar_pago_exitoso(user_id, plan, stripe_session_id, stripe_customer_id, stripe_subscription_id)
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
def procesar_pago_exitoso(user_id, plan, stripe_session_id, stripe_customer_id, stripe_subscription_id):
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

def procesar_resurreccion(stripe_customer_id, invoice_obj, stripe_subscription_id):
    """
    Rehabilita el acceso y sincroniza a los usuarios antiguos de PRD.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, email, username, estado_suscripcion, stripe_subscription_id FROM usuarios WHERE stripe_customer_id = %s", (stripe_customer_id,))
        user = cursor.fetchone()

        # RETROCOMPATIBILIDAD
        if user and (user['stripe_subscription_id'] == stripe_subscription_id or not user['stripe_subscription_id']):
            period_end_unix = invoice_obj.lines.data[0].period.end
            nueva_fecha_fin = datetime.fromtimestamp(period_end_unix, tz=timezone.utc)

            cursor.execute("""
                UPDATE usuarios 
                SET estado_suscripcion = 'Activo',
                    subscription_end = %s,
                    stripe_subscription_id = %s -- Auto-sanado para usuarios de PRD
                WHERE id = %s
            """, (nueva_fecha_fin, stripe_subscription_id, user['id']))

            cursor.execute("""
                INSERT INTO logs_actividad (user_id, accion, modulo, detalle)
                VALUES (%s, %s, %s, %s)
            """, (user['id'], "Renovación Automática Exitosa", "Pagos", "Cobro recurrente procesado por Stripe"))

            conn.commit()
            current_app.logger.info(f"RESURRECTION: Usuario {user['id']} renovado automáticamente hasta {nueva_fecha_fin}.")
        else:
             current_app.logger.info(f"IGNORADO: Pago exitoso de una suscripción antigua para customer {stripe_customer_id}")

    except Exception as e:
        if conn: conn.rollback()
        current_app.logger.error(f"RESURRECTION_PROCESS_ERROR: {e}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

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