from flask import Blueprint, render_template, request, current_app, session
from db import get_db_connection
from helpers import admin_required
from utils.datetime_utils import now_utc, ahora_sql

dashboard_bp = Blueprint('dashboard', __name__)

@dashboard_bp.route('/dashboard')
@admin_required
def index():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. DEFINICIÓN PREVENTIVA (Evita NameErrors)
    nombres_meses = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    meses_labels = []
    usuarios_data = []
    top_leales = []
    origen_labels = []
    origen_data = []
    
    # Captura de filtros
    mes_sel = request.args.get('mes', "")
    anio_sel = request.args.get('anio', "")
    
    ahora_str = ahora_sql()
    semana_proxima_str = ahora_sql(dias=7)

    try:
        # 2. TOTALES (Históricos)
        cursor.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1")
        total_usuarios = cursor.fetchone()['total']
        
        cursor.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1 AND subscription_end > %s", (ahora_str,))
        activos = cursor.fetchone()['total']
        
        vencidos = total_usuarios - activos
        
        cursor.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1 AND subscription_end BETWEEN %s AND %s", (ahora_str, semana_proxima_str))
        proximos_a_vencer = cursor.fetchone()['total']

        # 3. HEAVY USERS (Filtrado Dinámico)
        query_heavy = '''
            SELECT u.username, u.company_name, u.subscription_end, COUNT(v.id) as total_cotizaciones
            FROM usuarios u
            LEFT JOIN ventas v ON u.id = v.user_id 
        '''
        params_heavy = []
        if mes_sel:
            query_heavy += " AND to_char(v.fecha, 'MM') = %s"
            params_heavy.append(mes_sel)
        if anio_sel:
            query_heavy += " AND to_char(v.fecha, 'YYYY') = %s"
            params_heavy.append(anio_sel)
        
        query_heavy += " WHERE u.role <= 1 GROUP BY u.id ORDER BY total_cotizaciones DESC LIMIT 5"
        cursor.execute(query_heavy, params_heavy)
        top_leales = cursor.fetchall()

        # 4. GRÁFICA CRECIMIENTO (Histórico 6 meses)
        for i in range(-5, 1):
            fecha_mes_str = ahora_sql(meses=i)
            y, m = fecha_mes_str[:4], fecha_mes_str[5:7]
            meses_labels.append(f"{nombres_meses[int(m)-1]} {y}")
            
            cursor.execute("SELECT COUNT(id) FROM usuarios WHERE role <= 1 AND to_char(created_at, 'MM') = %s AND to_char(created_at, 'YYYY') = %s", (m, y))
            conteo = cursor.fetchone()[0]
            usuarios_data.append(conteo)

        # 5. ORIGEN (Filtrado Dinámico)
        query_origen = "SELECT origen_registro, COUNT(id) as conteo FROM usuarios WHERE role <= 1"
        params_origen = []
        if mes_sel:
            query_origen += " AND to_char(created_at, 'MM') = %s"
            params_origen.append(mes_sel)
        if anio_sel:
            query_origen += " AND to_char(created_at, 'YYYY') = %s"
            params_origen.append(anio_sel)
        
        query_origen += " GROUP BY origen_registro"
        cursor.execute(query_origen, params_origen)
        origen_raw = cursor.fetchall()
        
        # Filtro de seguridad por si existen registros con origen en null
        origen_labels = [r['origen_registro'].capitalize() for r in origen_raw if r['origen_registro']]
        origen_data = [r['conteo'] for r in origen_raw if r['origen_registro']]

        # 6. SELECTORES
        lista_meses = [(f"{i:02d}", nombres_meses[i-1]) for i in range(1, 13)]
        lista_anios = [2025, 2026, 2027, 2028]

        # --- 5. SEGMENTACIÓN POR ACTIVIDAD (Thresholds) ---
        segmentos = {"Zombies (0-2)": 0, "Exploradores (3-14)": 0, "Power Users (15+)": 0}
        
        query_segmentos = "SELECT COUNT(v.id) as total FROM usuarios u LEFT JOIN ventas v ON u.id = v.user_id"
        params_seg = []
        if mes_sel and anio_sel:
            query_segmentos += " AND to_char(v.fecha, 'MM') = %s AND to_char(v.fecha, 'YYYY') = %s"
            params_seg = [mes_sel, anio_sel]
        
        query_segmentos += " WHERE u.role <= 1 GROUP BY u.id"
        cursor.execute(query_segmentos, params_seg)
        usuarios_actividad = cursor.fetchall()

        for user in usuarios_actividad:
            cots = user['total']
            if cots <= 2:
                segmentos["Zombies (0-2)"] += 1
            elif 3 <= cots <= 14:
                segmentos["Exploradores (3-14)"] += 1
            else:
                segmentos["Power Users (15+)"] += 1

        seg_labels = list(segmentos.keys())
        seg_data = list(segmentos.values())

        return render_template(
            'dashboard/index.html',
            total_usuarios=total_usuarios, activos=activos, vencidos=vencidos, proximos_a_vencer=proximos_a_vencer,
            ahora_actual=ahora_str, top_leales=top_leales, meses_labels=meses_labels, usuarios_data=usuarios_data,
            origen_labels=origen_labels, origen_data=origen_data,
            seg_labels=seg_labels, seg_data=seg_data,
            mes_sel=mes_sel, anio_sel=anio_sel, lista_meses=lista_meses, lista_anios=lista_anios
        )

    except Exception as e:
        current_app.logger.error(f"DASHBOARD_ERROR: Fallo al cargar métricas para el Admin ID {session.get('user_id')} - {e}")
        return f"Error: {e}", 500
    finally:
        cursor.close()
        conn.close()