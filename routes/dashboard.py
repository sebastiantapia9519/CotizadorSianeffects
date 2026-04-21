from flask import Blueprint, render_template, request, current_app, session
from db import get_db_connection
from helpers import admin_required
from utils.datetime_utils import ahora_sql, now_utc
from datetime import datetime, timedelta
from psycopg2.extras import RealDictCursor

dashboard_bp = Blueprint('dashboard', __name__)

@dashboard_bp.route('/dashboard')
@admin_required
def index():
    admin_name = session.get('username', 'Admin_Desconocido')
    admin_id = session.get('user_id', 'N/A')
    
    conn = get_db_connection()
    # ACTIVAMOS EL MODO DICCIONARIO: Ahora los resultados son {'columna': valor}
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    nombres_meses = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    mes_sel = request.args.get('mes', "")
    anio_sel = request.args.get('anio', "")
    
    ahora = now_utc()
    semana_proxima = ahora + timedelta(days=7)
    
    try:
        # 1. TOTALES (Limpios y legibles)
        cursor.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1")
        total_usuarios = cursor.fetchone()['total']
        
        cursor.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1 AND subscription_end > %s", (ahora,))
        activos = cursor.fetchone()['total']
        
        vencidos = total_usuarios - activos
        
        cursor.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1 AND subscription_end BETWEEN %s AND %s", 
                       (ahora, semana_proxima))
        proximos_a_vencer = cursor.fetchone()['total']

        # 2. HEAVY USERS (Orden SQL corregido)
        query_heavy = '''
            SELECT u.username, u.company_name, u.subscription_end, COUNT(v.id) as total_cotizaciones
            FROM usuarios u
            LEFT JOIN ventas v ON u.id = v.user_id 
            WHERE u.role <= 1
        '''
        params_heavy = []
        if mes_sel:
            query_heavy += " AND to_char(v.fecha, 'MM') = %s"
            params_heavy.append(mes_sel)
        if anio_sel:
            query_heavy += " AND to_char(v.fecha, 'YYYY') = %s"
            params_heavy.append(anio_sel)
        
        # En Postgres, si agrupas, debes incluir todas las columnas seleccionadas
        query_heavy += " GROUP BY u.id, u.username, u.company_name, u.subscription_end ORDER BY total_cotizaciones DESC LIMIT 5"
        cursor.execute(query_heavy, params_heavy)
        top_leales = cursor.fetchall()

        # 3. GRÁFICA CRECIMIENTO
        meses_labels, usuarios_data = [], []
        for i in range(-5, 1):
            # Usamos as_string=False para obtener el objeto y extraer año/mes
            fecha_dt = ahora_sql(meses=i, as_string=False)
            y, m = str(fecha_dt.year), f"{fecha_dt.month:02d}"
            meses_labels.append(f"{nombres_meses[int(m)-1]} {y}")
            
            cursor.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1 AND to_char(created_at, 'MM') = %s AND to_char(created_at, 'YYYY') = %s", (m, y))
            usuarios_data.append(cursor.fetchone()['total'])

        # 4. ORIGEN
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
        origen_labels = [r['origen_registro'].capitalize() for r in origen_raw if r['origen_registro']]
        origen_data = [r['conteo'] for r in origen_raw if r['origen_registro']]

        # 5. SEGMENTACIÓN
        segmentos = {"Zombies (0-2)": 0, "Exploradores (3-14)": 0, "Power Users (15+)": 0}
        query_segmentos = "SELECT COUNT(v.id) as total_v FROM usuarios u LEFT JOIN ventas v ON u.id = v.user_id WHERE u.role <= 1"
        params_seg = []
        if mes_sel:
            query_segmentos += " AND to_char(v.fecha, 'MM') = %s"
            params_seg.append(mes_sel)
        if anio_sel:
            query_segmentos += " AND to_char(v.fecha, 'YYYY') = %s"
            params_seg.append(anio_sel)

        query_segmentos += " GROUP BY u.id"
        cursor.execute(query_segmentos, params_seg)
        usuarios_actividad = cursor.fetchall()

        for user in usuarios_actividad:
            cots = user['total_v']
            if cots <= 2: segmentos["Zombies (0-2)"] += 1
            elif 3 <= cots <= 14: segmentos["Exploradores (3-14)"] += 1
            else: segmentos["Power Users (15+)"] += 1

        # LOG DE AUDITORÍA SIN ACENTOS
        current_app.logger.info(f"DATA_ACCESS: Admin '{admin_name}' (ID: {admin_id}) consulto el dashboard global (Mes: {mes_sel or 'Todo'}, Anio: {anio_sel or 'Todo'})")

        return render_template(
            'dashboard/index.html',
            total_usuarios=total_usuarios, activos=activos, vencidos=vencidos, proximos_a_vencer=proximos_a_vencer,
            ahora_actual=ahora_sql(as_string=True), # Para el texto en el footer del dashboard
            top_leales=top_leales, meses_labels=meses_labels, usuarios_data=usuarios_data,
            origen_labels=origen_labels, origen_data=origen_data,
            seg_labels=list(segmentos.keys()), seg_data=list(segmentos.values()),
            mes_sel=mes_sel, anio_sel=anio_sel,
            lista_meses=[(f"{i:02d}", nombres_meses[i-1]) for i in range(1, 13)],
            lista_anios=[2025, 2026, 2027, 2028]
        )

    except Exception as e:
        current_app.logger.error(f"DASHBOARD_ERROR: Admin '{admin_name}' (ID: {admin_id}) fallo al cargar metricas globales - {e}")
        return f"Error en Dashboard: {e}", 500
    finally:
        cursor.close()
        conn.close()