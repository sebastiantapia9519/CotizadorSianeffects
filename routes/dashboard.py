from flask import Blueprint, render_template, request, current_app, session
from db import get_db_connection
from helpers import admin_required
from utils.datetime_utils import now_utc, ahora_sql

dashboard_bp = Blueprint('dashboard', __name__)

@dashboard_bp.route('/dashboard')
@admin_required
def index():
    conn = get_db_connection()
    
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
        total_usuarios = conn.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1").fetchone()['total']
        activos = conn.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1 AND subscription_end > ?", (ahora_str,)).fetchone()['total']
        vencidos = total_usuarios - activos
        proximos_a_vencer = conn.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1 AND subscription_end BETWEEN ? AND ?", (ahora_str, semana_proxima_str)).fetchone()['total']

        # 3. HEAVY USERS (Filtrado Dinámico)
        query_heavy = '''
            SELECT u.username, u.company_name, u.subscription_end, COUNT(v.id) as total_cotizaciones
            FROM usuarios u
            LEFT JOIN ventas v ON u.id = v.user_id 
        '''
        params_heavy = []
        if mes_sel:
            query_heavy += " AND strftime('%m', v.fecha) = ?"
            params_heavy.append(mes_sel)
        if anio_sel:
            query_heavy += " AND strftime('%Y', v.fecha) = ?"
            params_heavy.append(anio_sel)
        
        query_heavy += " WHERE u.role <= 1 GROUP BY u.id ORDER BY total_cotizaciones DESC LIMIT 5"
        top_leales = conn.execute(query_heavy, params_heavy).fetchall()

        # 4. GRÁFICA CRECIMIENTO (Histórico 6 meses)
        for i in range(-5, 1):
            fecha_mes_str = ahora_sql(meses=i)
            y, m = fecha_mes_str[:4], fecha_mes_str[5:7]
            meses_labels.append(f"{nombres_meses[int(m)-1]} {y}")
            conteo = conn.execute("SELECT COUNT(id) FROM usuarios WHERE role <= 1 AND strftime('%m', created_at) = ? AND strftime('%Y', created_at) = ?", (m, y)).fetchone()[0]
            usuarios_data.append(conteo)

        # 5. ORIGEN (Filtrado Dinámico)
        query_origen = "SELECT origen_registro, COUNT(id) as conteo FROM usuarios WHERE role <= 1"
        params_origen = []
        if mes_sel:
            query_origen += " AND strftime('%m', created_at) = ?"
            params_origen.append(mes_sel)
        if anio_sel:
            query_origen += " AND strftime('%Y', created_at) = ?"
            params_origen.append(anio_sel)
        
        query_origen += " GROUP BY origen_registro"
        origen_raw = conn.execute(query_origen, params_origen).fetchall()
        origen_labels = [r['origen_registro'].capitalize() for r in origen_raw]
        origen_data = [r['conteo'] for r in origen_raw]

        # 6. SELECTORES
        lista_meses = [(f"{i:02d}", nombres_meses[i-1]) for i in range(1, 13)]
        lista_anios = [2025, 2026, 2027, 2028]

        # --- 5. SEGMENTACIÓN POR ACTIVIDAD (Thresholds) ---
        # Definimos tus nuevos umbrales: 0-2, 3-14, 15+
        segmentos = {"Zombies (0-2)": 0, "Exploradores (3-14)": 0, "Power Users (15+)": 0}
        
        # Consultamos el conteo de cotizaciones por usuario
        # Usamos el filtro de fecha si está activo para que la segmentación sea actual
        query_segmentos = "SELECT COUNT(v.id) as total FROM usuarios u LEFT JOIN ventas v ON u.id = v.user_id"
        params_seg = []
        if mes_sel and anio_sel:
            query_segmentos += " AND strftime('%m', v.fecha) = ? AND strftime('%Y', v.fecha) = ?"
            params_seg = [mes_sel, anio_sel]
        
        query_segmentos += " WHERE u.role <= 1 GROUP BY u.id"
        usuarios_actividad = conn.execute(query_segmentos, params_seg).fetchall()

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
        conn.close()