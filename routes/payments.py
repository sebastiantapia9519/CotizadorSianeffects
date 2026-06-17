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

# =============================================================================
# CREAR SESIÓN DE PAGO (CHECKOUT)
# =============================================================================
@payments_bp.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    """
    Inicia el flujo de pago enviando al cliente a la pasarela de Stripe.
    """
    price_id = request.form.get('price_id')
    user_id = session.get('user_id') # Lo sacamos de forma segura de la sesión
    
    try:
        # 1. Buscamos si el usuario ya existe como cliente en Stripe
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT stripe_customer_id FROM usuarios WHERE id = %s", (user_id,))
        user_db = cursor.fetchone()
        cursor.close()
        conn.close()

        customer_id = user_db['stripe_customer_id'] if user_db else None

        # 2. Preparamos los metadatos (Stripe exige que todo sea STRING)
        metadatos_sian = {
            'user_id': str(user_id),
            'plan_type': 'anual' if '1490' in str(price_id) else 'mensual'
        }

        # 3. Preparamos los parámetros del checkout
        checkout_params = {
            'line_items': [{'price': price_id, 'quantity': 1}],
            'mode': 'subscription',
            'allow_promotion_codes': True,
            'success_url': url_for('payments.pago_exitoso', _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            'cancel_url': url_for('payments.pago_cancelado', _external=True),
            
            # Pegamos los metadatos a la Sesión (para tu Webhook)
            'metadata': metadatos_sian,
            
            # ¡EL TRUCO! Pegamos los metadatos a la Suscripción (para que los veas en el Dashboard)
            'subscription_data': {
                'metadata': metadatos_sian
            }
        }
        
        # Si ya tiene un ID, se lo pasamos para no duplicar clientes
        if customer_id:
            checkout_params['customer'] = customer_id

        checkout_session = stripe.checkout.Session.create(**checkout_params)
        
        # Validamos que Stripe realmente haya generado la URL para satisfacer a Pylance
        if not checkout_session.url:
            raise ValueError("Stripe no devolvió una URL de checkout válida.")
            
        return redirect(checkout_session.url, code=303)
        
    except Exception as e:
        current_app.logger.error(f"STRIPE_ERROR: {e}")
        return jsonify(error=str(e)), 403


# =============================================================================
# WEBHOOK: El cerebro de la automatización
# =============================================================================
@payments_bp.route('/webhook', methods=['POST'])
def webhook():
    """
    Endpoint que escucha las notificaciones de Stripe cuando el pago es real.
    """
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except Exception as e:
        current_app.logger.error(f"WEBHOOK_SIGNATURE_ERROR: {e}")
        return jsonify(success=False), 400

    # 1. Si el pago inicial se completó con éxito
    if event.type == 'checkout.session.completed':
        session_obj = event.data.object
        try:
            user_id = session_obj.metadata.user_id
            plan = session_obj.metadata.plan_type
            stripe_session_id = session_obj.id
            stripe_customer_id = session_obj.customer
            
            if user_id:
                procesar_pago_exitoso(user_id, plan, stripe_session_id, stripe_customer_id)
        except Exception as e:
            current_app.logger.error(f"Falta un dato clave en la respuesta de Stripe: {e}")

    # 2. Si la suscripción fue eliminada (Por el usuario, fin de plazo o disputas)
    elif event.type == 'customer.subscription.deleted':
        subscription_obj = event.data.object
        stripe_customer_id = subscription_obj.customer
        procesar_cancelacion(stripe_customer_id)

    # 3. Si un pago recurrente falla (Tarjeta rechazada, sin fondos, expirada)
    elif event.type == 'invoice.payment_failed':
        invoice_obj = event.data.object
        stripe_customer_id = invoice_obj.customer
        procesar_pago_fallido(stripe_customer_id)
        # 4. Si un pago recurrente se cobra con éxito (Renovación o "Resurrección")
    elif event.type == 'invoice.paid':
        invoice_obj = event.data.object
        
        # IMPORTANTE: Validamos que el motivo del cobro sea una renovación (subscription_cycle)
        # Esto evita que se ejecute en el primer pago, porque el primer pago ya lo maneja 'checkout.session.completed'
        if invoice_obj.billing_reason == 'subscription_cycle':
            stripe_customer_id = invoice_obj.customer
            procesar_resurreccion(stripe_customer_id, invoice_obj)

    return jsonify(success=True)

# =============================================================================
# LÓGICA DE ACTUALIZACIÓN DE USUARIO Y LOGS
# =============================================================================
def procesar_pago_exitoso(user_id, plan, stripe_session_id, stripe_customer_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        # 1. Traemos los datos actuales para ver si acumulamos tiempo
        cursor.execute("SELECT username, email, subscription_end FROM usuarios WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        
        if user:
            ahora = now_utc()
            current_end = user['subscription_end']
            
            # Normalizar timezone si es necesario
            if current_end and current_end.tzinfo is None:
                current_end = current_end.replace(tzinfo=timezone.utc)

            # LÓGICA CLAVE: ¿Desde cuándo sumamos?
            # Si el usuario sigue activo, sumamos desde su fecha de vencimiento. 
            # Si ya venció, sumamos desde hoy.
            base_date = current_end if (current_end and current_end > ahora) else ahora

            if plan == 'anual':
                nueva_fecha = base_date + relativedelta(years=1)
            else:
                nueva_fecha = base_date + relativedelta(months=1)
            
            # Forzar cierre del día 23:59:59
            nueva_fecha = nueva_fecha.replace(hour=23, minute=59, second=59)

            # 2. Actualizamos la BD (Igual que antes pero con la fecha exacta)
            cursor.execute("""
                UPDATE usuarios 
                SET subscription_end = %s, 
                    estado_suscripcion = 'Activo',
                    stripe_customer_id = %s,
                    plan_type = %s
                WHERE id = %s
            """, (nueva_fecha, stripe_customer_id, plan, user_id))
            
            # LOGGING MANUAL (Opcional pero recomendado para ver el acumulado)
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

@payments_bp.route('/pago-exitoso')
@login_required
def pago_exitoso():
    session_id = request.args.get('session_id')
    user_id = session.get('user_id')
    
    if session_id:
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id)
            if checkout_session.payment_status == 'paid':
                
                # Usamos getattr en lugar de .get() porque metadata es un StripeObject
                plan = getattr(checkout_session.metadata, 'plan_type', 'mensual')
                
                if plan == 'anual':
                    nueva_fecha = now_utc() + relativedelta(years=1)
                else:
                    nueva_fecha = now_utc() + relativedelta(months=1)
                
                # Propiedad directa
                stripe_customer_id = checkout_session.customer
                
                # ACTUALIZAR LA BASE DE DATOS DIRECTAMENTE
                conn = get_db()
                cursor = conn.cursor()
                try:
                    # Obtenemos el usuario antes de enviar el correo
                    cursor.execute("SELECT username, email FROM usuarios WHERE id = %s", (user_id,))
                    user_data = cursor.fetchone()

                    # Actualizamos la suscripción y el plan
                    cursor.execute("""
                        UPDATE usuarios 
                        SET subscription_end = %s, 
                            estado_suscripcion = 'Activo',
                            stripe_customer_id = %s,
                            plan_type = %s
                        WHERE id = %s
                    """, (nueva_fecha, stripe_customer_id, plan, user_id))
                    
                    # Registrar en logs
                    cursor.execute("""
                        INSERT INTO logs_actividad (user_id, accion, modulo, detalle)
                        VALUES (%s, %s, %s, %s)
                    """, (user_id, f"Activación PRO {plan} (verificación instantánea)", "Pagos", f"Session ID: {session_id}"))
                    
                    
                    conn.commit()
                    current_app.logger.info(f"INSTANT_ACTIVATION: Usuario {user_id} activado como PRO en BD")
                    
                except Exception as db_error:
                    if conn: conn.rollback()
                    current_app.logger.error(f"Error actualizando BD en pago_exitoso: {db_error}")
                finally:
                    cursor.close()
                    conn.close()
                
                # Actualizar sesión después de actualizar BD
                session['is_pro_active'] = True
                session.pop('grace_period', None)
                
        except Exception as e:
            current_app.logger.error(f"Error consultando Stripe en pago_exitoso: {e}")

    # Refrescar la sesión con los datos actualizados de la BD
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT role, estado_suscripcion FROM usuarios WHERE id = %s", (user_id,))
        user_db = cursor.fetchone()
        if user_db:
            session['role'] = user_db['role']
            if user_db['estado_suscripcion'] == 'Activo':
                session['is_pro_active'] = True
    except Exception as e:
        current_app.logger.error(f"Error refrescando sesión post-pago: {e}")
    finally:
        cursor.close()
        conn.close()

    return render_template('pago_exitoso.html')

@payments_bp.route('/billing-portal', methods=['POST'])
def billing_portal():
    # 1. Obtener el Customer ID de la BD
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT stripe_customer_id FROM usuarios WHERE id = %s', (session['user_id'],))
    user = cursor.fetchone()
    cursor.close()
    conn.close()

    if not user or not user['stripe_customer_id']:
        # Si no tiene ID de cliente, es que nunca ha pagado
        return redirect(url_for('configuracion.configuracion'))

    # 2. Crear sesión del portal de Stripe
    # return_url es a donde regresa el usuario al salir del portal
    portal_session = stripe.billing_portal.Session.create(
        customer=user['stripe_customer_id'],
        return_url=url_for('configuracion.configuracion', _external=True) + "#list-suscripcion"
    )

    return redirect(portal_session.url)

@payments_bp.route('/pago-cancelado')
@login_required
def pago_cancelado():
    return render_template('pago_cancelado.html')

# =============================================================================
# MANEJO DE CANCELACIONES Y FALLOS
# =============================================================================
def procesar_cancelacion(stripe_customer_id):
    """
    Busca al usuario por su ID de Stripe, revoca el acceso PRO
    y registra el momento exacto de la cancelación.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, email, username FROM usuarios WHERE stripe_customer_id = %s", (stripe_customer_id,))
        user = cursor.fetchone()

        if user:
            # 1. Marcamos el estado y la fecha de cancelación
            # Usamos now_utc() para mantener la consistencia con el resto de tu app
            cursor.execute("""
                UPDATE usuarios 
                SET estado_suscripcion = 'Cancelado',
                    fecha_cancelacion = %s
                WHERE id = %s
            """, (now_utc(), user['id']))

            # 2. Log de actividad para auditoría
            cursor.execute("""
                INSERT INTO logs_actividad (user_id, accion, modulo, detalle)
                VALUES (%s, %s, %s, %s)
            """, (user['id'], "Suscripción Finalizada", "Pagos", "Cancelación procesada vía Webhook"))

            conn.commit()
            current_app.logger.info(f"SUBSCRIPTION_DELETED: El usuario {user['id']} ha cancelado su suscripción.")

    except Exception as e:
        if conn: conn.rollback()
        current_app.logger.error(f"CANCEL_PROCESS_ERROR: {e}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def procesar_pago_fallido(stripe_customer_id):
    """
    Bloquea temporalmente el acceso si Stripe no pudo hacer el cargo del mes.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, email, username FROM usuarios WHERE stripe_customer_id = %s", (stripe_customer_id,))
        user = cursor.fetchone()

        if user:
            cursor.execute("""
                UPDATE usuarios 
                SET estado_suscripcion = 'Pago Fallido'
                WHERE id = %s
            """, (user['id'],))

            cursor.execute("""
                INSERT INTO logs_actividad (user_id, accion, modulo, detalle)
                VALUES (%s, %s, %s, %s)
            """, (user['id'], "Fallo de Pago", "Pagos", "Intento de cobro automático rechazado"))

            conn.commit()
            current_app.logger.info(f"PAYMENT_FAILED: Cobro fallido para usuario {user['id']}.")

            # Súper recomendado: Enviar correo avisando del fallo
            enviar_correo_sian(
                subject="💳 Acción Requerida: Problema con tu pago de Sianeffects",
                recipient=user['email'],
                template="pago_fallido", # Tendrías que crear este HTML en resend/templates
                sender_alias="pagos", 
                username=user['username']
            )

    except Exception as e:
        if conn: conn.rollback()
        current_app.logger.error(f"FAIL_PROCESS_ERROR: {e}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def procesar_resurreccion(stripe_customer_id, invoice_obj):
    """
    Rehabilita el acceso al usuario cuando Stripe cobra con éxito una renovación
    automática o un pago que había fallado previamente.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, email, username, estado_suscripcion FROM usuarios WHERE stripe_customer_id = %s", (stripe_customer_id,))
        user = cursor.fetchone()

        if user:
            # 1. Extraemos la fecha exacta de la factura de Stripe (viene en formato Unix)
            period_end_unix = invoice_obj.lines.data[0].period.end
            nueva_fecha_fin = datetime.fromtimestamp(period_end_unix, tz=timezone.utc)

            # 2. Actualizamos el estado a Activo y ponemos la nueva fecha
            cursor.execute("""
                UPDATE usuarios 
                SET estado_suscripcion = 'Activo',
                    subscription_end = %s
                WHERE id = %s
            """, (nueva_fecha_fin, user['id']))

            # 3. Registramos la victoria en los logs
            cursor.execute("""
                INSERT INTO logs_actividad (user_id, accion, modulo, detalle)
                VALUES (%s, %s, %s, %s)
            """, (user['id'], "Renovación Automática Exitosa", "Pagos", "Cobro recurrente procesado por Stripe"))

            conn.commit()
            current_app.logger.info(f"RESURRECTION: Usuario {user['id']} renovado automáticamente hasta {nueva_fecha_fin}.")

            # Opcional pero da mucha confianza: Puedes mandar un correo de "Hemos recibido tu pago de este mes"
            # enviar_correo_sian(...)

    except Exception as e:
        if conn: conn.rollback()
        current_app.logger.error(f"RESURRECTION_PROCESS_ERROR: {e}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()