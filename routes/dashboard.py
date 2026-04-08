from flask import Blueprint, render_template
from db import get_db_connection
from helpers import admin_required
# Usamos solo lo que ya tienes en tu arsenal
from utils.datetime_utils import now_utc, ahora_sql 

dashboard_bp = Blueprint('dashboard', __name__)

@dashboard_bp.route('/dashboard')
@admin_required
def index():
    conn = get_db_connection()
    
    # 1. FECHAS BASE (Usando tus utils)
    ahora_str = ahora_sql()
    semana_proxima_str = ahora_sql(dias=7)

    try:
        # TOTALES DE NEGOCIO
        total_usuarios = conn.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1").fetchone()['total']
        activos = conn.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1 AND subscription_end > ?", (ahora_str,)).fetchone()['total']
        vencidos = total_usuarios - activos
        proximos_a_vencer = conn.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1 AND subscription_end BETWEEN ? AND ?", (ahora_str, semana_proxima_str)).fetchone()['total']

        # RANKING DE HEAVY USERS
        top_leales = conn.execute('''
            SELECT u.username, u.company_name, u.subscription_end, COUNT(v.id) as total_cotizaciones
            FROM usuarios u
            LEFT JOIN ventas v ON u.id = v.user_id
            WHERE u.role <= 1
            GROUP BY u.id
            ORDER BY total_cotizaciones DESC LIMIT 5
        ''').fetchall()

        # 2. LÓGICA DE GRÁFICA (RELLENO DE MESES)
        meses_labels = []
        usuarios_data = []
        nombres_meses = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
        
        # Generamos los últimos 6 meses usando tu función ahora_sql
        # i será: -5, -4, -3, -2, -1, 0
        for i in range(-5, 1):
            # Tu función ahora_sql permite pasar meses negativos
            # Usamos una fecha de referencia para extraer el mes y año
            fecha_mes_str = ahora_sql(meses=i) # Ejemplo: '2026-02-07 18:20:00'
            
            anio = fecha_mes_str[:4]
            mes_num = fecha_mes_str[5:7]
            
            label = f"{nombres_meses[int(mes_num)-1]} {anio}"
            meses_labels.append(label)
            
            # Buscamos en la BD usando el formato de tu función
            conteo = conn.execute('''
                SELECT COUNT(id) FROM usuarios 
                WHERE role <= 1 
                AND strftime('%m', created_at) = ? 
                AND strftime('%Y', created_at) = ?
            ''', (mes_num, anio)).fetchone()[0]
            
            usuarios_data.append(conteo)

        # ORIGEN DE CLIENTES
        origen_raw = conn.execute('SELECT origen_registro, COUNT(id) as conteo FROM usuarios WHERE role <= 1 GROUP BY origen_registro').fetchall()

        return render_template(
            'dashboard/index.html',
            total_usuarios=total_usuarios,
            activos=activos,
            vencidos=vencidos,
            proximos_a_vencer=proximos_a_vencer,
            ahora_actual=ahora_str,
            top_leales=top_leales,
            meses_labels=meses_labels,
            usuarios_data=usuarios_data,
            origen_labels=[r['origen_registro'].capitalize() for r in origen_raw],
            origen_data=[r['conteo'] for r in origen_raw]
        )

    except Exception as e:
        print(f"Error en Dashboard: {e}")
        return "Error interno", 500
    finally:
        conn.close()