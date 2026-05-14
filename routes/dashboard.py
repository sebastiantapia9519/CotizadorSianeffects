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
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    nombres_meses = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    mes_sel = request.args.get('mes', "")
    anio_sel = request.args.get('anio', "")
    
    ahora = now_utc()
    semana_proxima = ahora + timedelta(days=7)
    
    try:
        # 1. TOTALES
        cursor.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1")
        total_usuarios = cursor.fetchone()['total']
        
        cursor.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1 AND subscription_end > %s", (ahora,))
        activos = cursor.fetchone()['total']
        
        vencidos = total_usuarios - activos
        
        cursor.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1 AND subscription_end BETWEEN %s AND %s", 
                       (ahora, semana_proxima))
        proximos_a_vencer = cursor.fetchone()['total']

        # 2. HEAVY USERS
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
        
        query_heavy += " GROUP BY u.id, u.username, u.company_name, u.subscription_end ORDER BY total_cotizaciones DESC LIMIT 5"
        cursor.execute(query_heavy, params_heavy)
        top_leales = cursor.fetchall()

        # 3. GRÁFICA CRECIMIENTO
        meses_labels, usuarios_data = [], []
        for i in range(-5, 1):
            fecha_dt = ahora_sql(meses=i, as_string=False)
            y, m = str(fecha_dt.year), f"{fecha_dt.month:02d}"
            meses_labels.append(f"{nombres_meses[int(m)-1]} {y}")
            
            cursor.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1 AND to_char(created_at, 'MM') = %s AND to_char(created_at, 'YYYY') = %s", (m, y))
            usuarios_data.append(cursor.fetchone()['total'])

        # 4. SEGMENTACIÓN
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

# =====================================================================
        # 5. MÉTRICAS FINANCIERAS Y STRIPE (BLINDADO)
        # =====================================================================
        
        PRECIO_MENSUAL = 149
        PRECIO_ANUAL = 1490

        # A. Distribución de planes y MRR (Salud a largo plazo)
        cursor.execute("""
            SELECT LOWER(plan_type) as plan_normalizado, COUNT(id) as cantidad 
            FROM usuarios 
            WHERE LOWER(estado_suscripcion) IN ('activo', 'activa') 
            AND role <= 1
            AND subscription_end > %s 
            GROUP BY LOWER(plan_type)
        """, (ahora,))
        desglose_planes = cursor.fetchall()
        
        mrr_total = 0
        planes_dict = {'mensual': 0, 'anual': 0, 'free': 0}
        
        for fila in desglose_planes:
            tipo = (fila['plan_normalizado'] or 'free').strip()
            cantidad = fila['cantidad']
            
            if tipo in planes_dict:
                planes_dict[tipo] += cantidad
            else:
                planes_dict['free'] = planes_dict.get('free', 0) + cantidad
            
            if tipo == 'mensual':
                mrr_total += (cantidad * PRECIO_MENSUAL)
            elif tipo == 'anual':
                mrr_total += (cantidad * (PRECIO_ANUAL / 12))

        # B. Churn (Cancelaciones en el periodo seleccionado)
        query_churn = "SELECT COUNT(id) as bajas FROM usuarios WHERE LOWER(estado_suscripcion) IN ('cancelada', 'cancelado') AND role <= 1"
        params_churn = []
        if mes_sel:
            query_churn += " AND to_char(fecha_cancelacion, 'MM') = %s"
            params_churn.append(mes_sel)
        if anio_sel:
            query_churn += " AND to_char(fecha_cancelacion, 'YYYY') = %s"
            params_churn.append(anio_sel)
            
        cursor.execute(query_churn, params_churn)
        churn_total = cursor.fetchone()['bajas']

        # C. Nuevas Activaciones PRO (En el periodo seleccionado)
        query_nuevos = "SELECT COUNT(id) as nuevos FROM logs_actividad WHERE accion LIKE 'Activación PRO%%' AND modulo = 'Pagos'"
        params_nuevos = []
        if mes_sel:
            query_nuevos += " AND to_char(created_at, 'MM') = %s"
            params_nuevos.append(mes_sel)
        if anio_sel:
            query_nuevos += " AND to_char(created_at, 'YYYY') = %s"
            params_nuevos.append(anio_sel)
            
        cursor.execute(query_nuevos, params_nuevos)
        nuevos_pro = cursor.fetchone()['nuevos']

        # D. FLUJO DE CAJA / INGRESOS REALES (Dinero en el banco este mes)
        query_ingresos = """
            SELECT accion 
            FROM logs_actividad 
            WHERE (accion LIKE 'Activación PRO%%' OR accion LIKE 'Renovación PRO%%') 
            AND modulo = 'Pagos'
        """
        params_ingresos = []
        if mes_sel:
            query_ingresos += " AND to_char(created_at, 'MM') = %s"
            params_ingresos.append(mes_sel)
        if anio_sel:
            query_ingresos += " AND to_char(created_at, 'YYYY') = %s"
            params_ingresos.append(anio_sel)
            
        cursor.execute(query_ingresos, params_ingresos)
        pagos_logs = cursor.fetchall()

        ingresos_brutos = 0
        for pago in pagos_logs:
            accion_texto = pago['accion'].lower()
            if 'anual' in accion_texto:
                ingresos_brutos += PRECIO_ANUAL
            elif 'mensual' in accion_texto:
                ingresos_brutos += PRECIO_MENSUAL

        return render_template(
            'dashboard/index.html',
            total_usuarios=total_usuarios, activos=activos, vencidos=vencidos, proximos_a_vencer=proximos_a_vencer,
            ahora_actual=ahora_sql(as_string=True),
            top_leales=top_leales, meses_labels=meses_labels, usuarios_data=usuarios_data,
            seg_labels=list(segmentos.keys()), seg_data=list(segmentos.values()),
            mes_sel=mes_sel, anio_sel=anio_sel,
            lista_meses=[(f"{i:02d}", nombres_meses[i-1]) for i in range(1, 13)],
            lista_anios=[2025, 2026, 2027, 2028],
            mrr_total=round(mrr_total, 2),
            ingresos_brutos=ingresos_brutos,
            planes_dict=planes_dict,
            churn_total=churn_total,
            nuevos_pro=nuevos_pro
        )

        # B. Churn (Cancelaciones)
        query_churn = "SELECT COUNT(id) as bajas FROM usuarios WHERE LOWER(estado_suscripcion) IN ('cancelada', 'cancelado') AND role <= 1"
        params_churn = []
        if mes_sel:
            query_churn += " AND to_char(fecha_cancelacion, 'MM') = %s"
            params_churn.append(mes_sel)
        if anio_sel:
            query_churn += " AND to_char(fecha_cancelacion, 'YYYY') = %s"
            params_churn.append(anio_sel)
            
        cursor.execute(query_churn, params_churn)
        churn_total = cursor.fetchone()['bajas']

        # C. Nuevas Activaciones PRO
        query_nuevos = "SELECT COUNT(id) as nuevos FROM logs_actividad WHERE accion LIKE 'Activación PRO%%' AND modulo = 'Pagos'"
        params_nuevos = []
        if mes_sel:
            query_nuevos += " AND to_char(created_at, 'MM') = %s"
            params_nuevos.append(mes_sel)
        if anio_sel:
            query_nuevos += " AND to_char(created_at, 'YYYY') = %s"
            params_nuevos.append(anio_sel)
            
        cursor.execute(query_nuevos, params_nuevos)
        nuevos_pro = cursor.fetchone()['nuevos']

        return render_template(
            'dashboard/index.html',
            total_usuarios=total_usuarios, activos=activos, vencidos=vencidos, proximos_a_vencer=proximos_a_vencer,
            ahora_actual=ahora_sql(as_string=True),
            top_leales=top_leales, meses_labels=meses_labels, usuarios_data=usuarios_data,
            seg_labels=list(segmentos.keys()), seg_data=list(segmentos.values()),
            mes_sel=mes_sel, anio_sel=anio_sel,
            lista_meses=[(f"{i:02d}", nombres_meses[i-1]) for i in range(1, 13)],
            lista_anios=[2025, 2026, 2027, 2028],
            mrr_total=round(mrr_total, 2),
            planes_dict=planes_dict,
            churn_total=churn_total,
            nuevos_pro=nuevos_pro
        )

    except Exception as e:
        current_app.logger.error(f"DASHBOARD_ERROR: Admin '{admin_name}' (ID: {admin_id}) fallo al cargar metricas globales - {e}")
        return f"Error en Dashboard: {e}", 500
    finally:
        cursor.close()
        conn.close()