from datetime import timedelta
import re

from flask import Blueprint, current_app, render_template, request, session
from psycopg2.extras import RealDictCursor

from db import get_db_connection
from helpers import admin_required
from utils.datetime_utils import ahora_sql, now_utc

dashboard_bp = Blueprint('dashboard', __name__)

PRECIO_MENSUAL = 149
PRECIO_ANUAL = 1490
PRECIOS_PLAN = {
    'mensual': PRECIO_MENSUAL,
    'anual': PRECIO_ANUAL,
}


def _period_filter(column, mes_sel, anio_sel):
    clauses = []
    params = []
    if mes_sel:
        clauses.append(f"to_char({column}, 'MM') = %s")
        params.append(mes_sel)
    if anio_sel:
        clauses.append(f"to_char({column}, 'YYYY') = %s")
        params.append(anio_sel)
    return clauses, params


def _infer_plan(*textos):
    texto = " ".join(str(t or "") for t in textos).lower()
    if 'anual' in texto:
        return 'anual'
    if 'mensual' in texto:
        return 'mensual'
    return 'desconocido'


def _extraer_monto(detalle, plan):
    detalle = detalle or ''
    match = re.search(r'\$\s*([0-9]+(?:[.,][0-9]{1,2})?)', detalle)
    if match:
        return float(match.group(1).replace(',', '.'))
    return PRECIOS_PLAN.get(plan, 0)


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
        periodo_clauses, periodo_params = _period_filter('created_at', mes_sel, anio_sel)
        periodo_label = "Histórico total"
        if mes_sel and anio_sel:
            periodo_label = f"{nombres_meses[int(mes_sel) - 1]} {anio_sel}"
        elif mes_sel:
            periodo_label = f"{nombres_meses[int(mes_sel) - 1]} - todos los años"
        elif anio_sel:
            periodo_label = f"Año {anio_sel}"

        cursor.execute("SELECT COUNT(id) as total FROM usuarios WHERE role <= 1")
        total_usuarios = cursor.fetchone()['total'] or 0

        cursor.execute(
            "SELECT COUNT(id) as total FROM usuarios WHERE role <= 1 AND subscription_end > %s",
            (ahora,)
        )
        activos = cursor.fetchone()['total'] or 0
        vencidos = max(total_usuarios - activos, 0)

        cursor.execute(
            """
            SELECT COUNT(id) as total
            FROM usuarios
            WHERE role <= 1 AND subscription_end BETWEEN %s AND %s
            """,
            (ahora, semana_proxima)
        )
        proximos_a_vencer = cursor.fetchone()['total'] or 0

        query_nuevos_usuarios = """
            SELECT id, username, email, company_name, created_at, plan_type,
                   estado_suscripcion, subscription_end
            FROM usuarios
            WHERE role <= 1
        """
        params_nuevos_usuarios = []
        if periodo_clauses:
            query_nuevos_usuarios += " AND " + " AND ".join(periodo_clauses)
            params_nuevos_usuarios.extend(periodo_params)
        query_nuevos_usuarios += " ORDER BY created_at DESC LIMIT 10"
        cursor.execute(query_nuevos_usuarios, params_nuevos_usuarios)
        nuevos_usuarios = cursor.fetchall()
        nuevos_usuarios_total = len(nuevos_usuarios)

        if periodo_clauses:
            cursor.execute(
                "SELECT COUNT(id) as total FROM usuarios WHERE role <= 1 AND " + " AND ".join(periodo_clauses),
                periodo_params
            )
            nuevos_usuarios_total = cursor.fetchone()['total'] or 0

        cursor.execute("""
            SELECT LOWER(COALESCE(plan_type, 'free')) as plan_normalizado, COUNT(id) as cantidad
            FROM usuarios
            WHERE LOWER(COALESCE(estado_suscripcion, '')) IN ('activo', 'activa')
              AND role <= 1
              AND subscription_end > %s
            GROUP BY LOWER(COALESCE(plan_type, 'free'))
        """, (ahora,))
        desglose_planes = cursor.fetchall()

        planes_dict = {'mensual': 0, 'anual': 0, 'free': 0}
        mrr_total = 0
        for fila in desglose_planes:
            tipo = (fila['plan_normalizado'] or 'free').strip().lower()
            cantidad = fila['cantidad'] or 0
            if tipo not in ('mensual', 'anual'):
                tipo = 'free'
            planes_dict[tipo] += cantidad
            if tipo == 'mensual':
                mrr_total += cantidad * PRECIO_MENSUAL
            elif tipo == 'anual':
                mrr_total += cantidad * (PRECIO_ANUAL / 12)

        cursor.execute("""
            SELECT id, username, email, company_name, LOWER(COALESCE(plan_type, 'free')) as plan_type,
                   estado_suscripcion, subscription_end, created_at,
                   (
                       SELECT MIN(l.created_at)
                       FROM logs_actividad l
                       WHERE l.user_id = usuarios.id
                         AND l.modulo = 'Pagos'
                         AND (
                             l.accion ILIKE 'Activación PRO%%'
                             OR l.accion ILIKE 'Renovación PRO%%'
                             OR l.accion ILIKE 'Renovación %%exitosa'
                             OR l.accion ILIKE 'Renovación Automática Exitosa'
                         )
                   ) as fecha_suscripcion
            FROM usuarios
            WHERE LOWER(COALESCE(estado_suscripcion, '')) IN ('activo', 'activa')
              AND role <= 1
              AND subscription_end > %s
              AND LOWER(COALESCE(plan_type, 'free')) IN ('mensual', 'anual')
            ORDER BY
                CASE LOWER(COALESCE(plan_type, 'free')) WHEN 'mensual' THEN 1 WHEN 'anual' THEN 2 ELSE 3 END,
                subscription_end ASC
        """, (ahora,))
        suscripciones_activas = []
        for usuario in cursor.fetchall():
            plan = (usuario['plan_type'] or 'free').lower()
            cobro_plan = PRECIOS_PLAN.get(plan, 0)
            aporte_mensual = cobro_plan if plan == 'mensual' else round(cobro_plan / 12, 2)
            suscripciones_activas.append({
                **usuario,
                'cobro_plan': cobro_plan,
                'aporte_mensual': aporte_mensual,
                'fecha_suscripcion_real': usuario['fecha_suscripcion'] or usuario['created_at'],
            })

        query_pagos = """
            SELECT l.id, l.user_id, l.accion, l.detalle, l.created_at,
                   u.username, u.email, u.company_name, u.plan_type
            FROM logs_actividad l
            JOIN usuarios u ON u.id = l.user_id
            WHERE l.modulo = 'Pagos'
              AND (
                l.accion ILIKE 'Activación PRO%%'
                OR l.accion ILIKE 'Renovación PRO%%'
                OR l.accion ILIKE 'Renovación %%exitosa'
                OR l.accion ILIKE 'Renovación Automática Exitosa'
              )
        """
        params_pagos = []
        pago_clauses, pago_params = _period_filter('l.created_at', mes_sel, anio_sel)
        if pago_clauses:
            query_pagos += " AND " + " AND ".join(pago_clauses)
            params_pagos.extend(pago_params)
        query_pagos += " ORDER BY l.created_at DESC"
        cursor.execute(query_pagos, params_pagos)
        pagos_db = cursor.fetchall()

        pagos_unicos = {}
        nuevos_pro = 0
        renovaciones = 0

        for pago in pagos_db:
            plan = _infer_plan(pago.get('accion'), pago.get('detalle'), pago.get('plan_type'))
            fecha = pago.get('created_at')
            fecha_key = fecha.strftime('%Y-%m-%d') if fecha else 'sin-fecha'
            key = (pago.get('user_id'), plan, fecha_key)
            if 'activación' in (pago.get('accion') or '').lower():
                pagos_unicos[key] = pago
            else:
                renovaciones += 1

        nuevos_pro = len(pagos_unicos)
        ingresos_brutos = round(mrr_total, 2)

        query_churn = """
            SELECT u.id, u.username, u.email, u.company_name, u.plan_type, u.fecha_cancelacion
            FROM usuarios u
            WHERE LOWER(COALESCE(u.estado_suscripcion, '')) IN ('cancelada', 'cancelado')
              AND u.role <= 1
        """
        params_churn = []
        churn_clauses, churn_params = _period_filter('u.fecha_cancelacion', mes_sel, anio_sel)
        if churn_clauses:
            query_churn += " AND " + " AND ".join(churn_clauses)
            params_churn.extend(churn_params)
        query_churn += " ORDER BY u.fecha_cancelacion DESC NULLS LAST LIMIT 10"
        cursor.execute(query_churn, params_churn)
        churn_reciente = cursor.fetchall()

        if churn_clauses:
            cursor.execute(
                """
                SELECT COUNT(id) as total
                FROM usuarios
                WHERE LOWER(COALESCE(estado_suscripcion, '')) IN ('cancelada', 'cancelado')
                  AND role <= 1
                  AND """ + " AND ".join(churn_clauses),
                churn_params
            )
            churn_total = cursor.fetchone()['total'] or 0
        else:
            churn_total = len(churn_reciente)
            cursor.execute("""
                SELECT COUNT(id) as total
                FROM usuarios
                WHERE LOWER(COALESCE(estado_suscripcion, '')) IN ('cancelada', 'cancelado')
                  AND role <= 1
            """)
            churn_total = cursor.fetchone()['total'] or 0

        query_heavy = """
            SELECT u.username, u.company_name, u.subscription_end,
                   (u.subscription_end > %s) as suscripcion_activa,
                   COUNT(v.id) as total_cotizaciones
            FROM usuarios u
            LEFT JOIN ventas v ON u.id = v.user_id
            WHERE u.role <= 1
        """
        params_heavy = [ahora]
        venta_clauses, venta_params = _period_filter('v.fecha', mes_sel, anio_sel)
        if venta_clauses:
            query_heavy += " AND " + " AND ".join(venta_clauses)
            params_heavy.extend(venta_params)
        query_heavy += """
            GROUP BY u.id, u.username, u.company_name, u.subscription_end, suscripcion_activa
            ORDER BY total_cotizaciones DESC
            LIMIT 5
        """
        cursor.execute(query_heavy, params_heavy)
        top_leales = cursor.fetchall()

        meses_labels, usuarios_data, ingresos_data = [], [], []
        for i in range(-5, 1):
            fecha_dt = ahora_sql(meses=i, as_string=False)
            y, m = str(fecha_dt.year), f"{fecha_dt.month:02d}"
            meses_labels.append(f"{nombres_meses[int(m) - 1]} {y}")

            cursor.execute("""
                SELECT COUNT(id) as total
                FROM usuarios
                WHERE role <= 1 AND to_char(created_at, 'MM') = %s AND to_char(created_at, 'YYYY') = %s
            """, (m, y))
            usuarios_data.append(cursor.fetchone()['total'] or 0)

            ingresos_data.append(round(mrr_total, 2) if i == 0 else 0)

        segmentos = {"Sin uso (0-2)": 0, "En adopción (3-14)": 0, "Power users (15+)": 0}
        query_segmentos = """
            SELECT COUNT(v.id) as total_v
            FROM usuarios u
            LEFT JOIN ventas v ON u.id = v.user_id
            WHERE u.role <= 1
        """
        params_seg = []
        if venta_clauses:
            query_segmentos += " AND " + " AND ".join(venta_clauses)
            params_seg.extend(venta_params)
        query_segmentos += " GROUP BY u.id"
        cursor.execute(query_segmentos, params_seg)
        for user in cursor.fetchall():
            cots = user['total_v'] or 0
            if cots <= 2:
                segmentos["Sin uso (0-2)"] += 1
            elif cots <= 14:
                segmentos["En adopción (3-14)"] += 1
            else:
                segmentos["Power users (15+)"] += 1

        return render_template(
            'dashboard/index.html',
            total_usuarios=total_usuarios,
            activos=activos,
            vencidos=vencidos,
            proximos_a_vencer=proximos_a_vencer,
            ahora_actual=ahora,
            top_leales=top_leales,
            meses_labels=meses_labels,
            usuarios_data=usuarios_data,
            ingresos_data=ingresos_data,
            seg_labels=list(segmentos.keys()),
            seg_data=list(segmentos.values()),
            mes_sel=mes_sel,
            anio_sel=anio_sel,
            periodo_label=periodo_label,
            lista_meses=[(f"{i:02d}", nombres_meses[i - 1]) for i in range(1, 13)],
            lista_anios=[2025, 2026, 2027, 2028],
            mrr_total=round(mrr_total, 2),
            ingresos_brutos=round(ingresos_brutos, 2),
            planes_dict=planes_dict,
            suscripciones_activas=suscripciones_activas,
            churn_total=churn_total,
            churn_reciente=churn_reciente,
            nuevos_pro=nuevos_pro,
            renovaciones=renovaciones,
            pagos_total=nuevos_pro,
            nuevos_usuarios=nuevos_usuarios,
            nuevos_usuarios_total=nuevos_usuarios_total,
            precios_plan={'mensual': PRECIO_MENSUAL, 'anual': PRECIO_ANUAL}
        )

    except Exception as e:
        current_app.logger.error(
            f"DASHBOARD_ERROR: Admin '{admin_name}' (ID: {admin_id}) fallo al cargar metricas globales - {e}"
        )
        return f"Error en Dashboard: {e}", 500
    finally:
        cursor.close()
        conn.close()
