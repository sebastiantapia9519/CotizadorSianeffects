from flask import Blueprint, render_template
from db import get_db_connection
from utils.datetime_utils import now_utc
from helpers import admin_required
from utils.datetime_utils import now_utc, ahora_sql

# 1. Quitamos el url_prefix para evitar confusiones
dashboard_bp = Blueprint('dashboard', __name__)

# 2. Ponemos la ruta completa aquí directo
@dashboard_bp.route('/dashboard')
@admin_required
def index():
    conn = get_db_connection()
    # Fecha actual en formato SQL
    ahora_str = ahora_sql()
    # Fecha para considerar "Próximos a vencer" (en los siguientes 7 días)
    semana_proxima_str = ahora_sql(dias=7)
    


    try:
        # 1. TOTALES DE NEGOCIO
        total_usuarios = conn.execute("SELECT COUNT(id) as total FROM usuarios WHERE role = 0").fetchone()['total']
        
        activos = conn.execute(
            "SELECT COUNT(id) as total FROM usuarios WHERE role = 0 AND subscription_end > ?", 
            (ahora_str,)
        ).fetchone()['total']
        
        vencidos = total_usuarios - activos

        proximos_a_vencer = conn.execute(
            "SELECT COUNT(id) as total FROM usuarios WHERE role = 0 AND subscription_end BETWEEN ? AND ?", 
            (ahora_str, semana_proxima_str)
        ).fetchone()['total']

        # 2. RANKING DE HEAVY USERS (Engagement Real)
        top_leales = conn.execute('''
            SELECT 
                u.username, 
                u.company_name, 
                u.subscription_end, 
                COUNT(v.id) as total_cotizaciones
            FROM usuarios u
            LEFT JOIN ventas v ON u.id = v.user_id
            WHERE u.role = 0 
            GROUP BY u.id
            ORDER BY total_cotizaciones DESC 
            LIMIT 5
        ''').fetchall()

        # 3. DATOS PARA GRÁFICAS (Mantenemos los de crecimiento y origen)
        # --- VELOCIDAD DE ADQUISICIÓN (Histórico Mensual) ---
        # Agrupamos por año y mes para que cuando pasen los años, la gráfica siga teniendo sentido
        crecimiento_raw = conn.execute('''
            SELECT 
                strftime('%Y', created_at) as anio,
                strftime('%m', created_at) as mes,
                COUNT(id) as conteo 
            FROM usuarios 
            WHERE role = 0 
            GROUP BY anio, mes 
            ORDER BY anio ASC, mes ASC
        ''').fetchall()
        
        # Formateamos las etiquetas para que digan "Ene 2026", "Feb 2026", etc.
        meses_nombres = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
        meses_labels = [f"{meses_nombres[int(r['mes'])-1]} {r['anio']}" for r in crecimiento_raw]
        usuarios_data = [r['conteo'] for r in crecimiento_raw]
        
        origen_raw = conn.execute('''
            SELECT origen_registro, COUNT(id) as conteo 
            FROM usuarios WHERE role = 0 GROUP BY origen_registro
        ''').fetchall()

        return render_template(
            'dashboard/index.html',
            total_usuarios=total_usuarios,
            activos=activos,
            vencidos=vencidos,
            proximos_a_vencer=proximos_a_vencer,
            ahora_actual=ahora_str,
            top_leales=top_leales,
            meses_labels=[r['mes'] for r in crecimiento_raw],
            usuarios_data=[r['conteo'] for r in crecimiento_raw],
            origen_labels=[r['origen_registro'].capitalize() for r in origen_raw],
            origen_data=[r['conteo'] for r in origen_raw]
        )

    except Exception as e:
        print(f"Error en Dashboard: {e}")
        return "Hubo un error cargando el dashboard. Revisa la consola.", 500
    finally:
        conn.close()