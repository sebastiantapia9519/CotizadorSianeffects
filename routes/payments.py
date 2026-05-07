import stripe
import os
from flask import Blueprint, request, redirect, jsonify

# Creamos el Blueprint
payments_bp = Blueprint('payments', __name__)

# Cargamos la llave desde el entorno (corrigiendo el typo)
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')

@payments_bp.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    # En el front-end mandaremos el 'price_id' según el plan elegido
    data = request.form
    price_id = data.get('price_id')
    
    # Aquí podrías obtener el ID del usuario desde la sesión para vincular el pago
    # user_id = session.get('user_id')

    try:
        checkout_session = stripe.checkout.Session.create(
            line_items=[
                {
                    'price': price_id,
                    'quantity': 1,
                },
            ],
            mode='subscription',
            # Importante: Estas URLs deben apuntar a tu dominio en Railway
            # o a localhost si estás probando localmente
            success_url=os.getenv('DOMAIN') + '/pago-exitoso?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=os.getenv('DOMAIN') + '/pago-cancelado',
            # Puedes pasar metadatos para saber a quién activar el PRO después
            metadata={
                'user_id': 'ID_DEL_USUARIO_AQUI'
            }
        )
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        return jsonify(error=str(e)), 403

