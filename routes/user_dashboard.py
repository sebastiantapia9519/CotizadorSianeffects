from flask import Blueprint, render_template, session, current_app, request
from helpers import login_required
from db import get_db_connection as get_db
from utils.datetime_utils import hoy_local
from datetime import datetime
from dateutil.relativedelta import relativedelta
import json

user_dash_bp = Blueprint('user_dash', __name__)

@user_dash_bp.route('/mi-panel')
@login_required
def mi_panel():
    user_id = session['user_id']
    conn = get_db()
    
    try:
        # --- 1. CONFIGURACIÓN DE FILTROS (MÁQUINA DEL TIEMPO) ---
        hoy_str = hoy_local() # Ej: '2026-04-12'
        anio_actual = hoy_str[:4]
        mes_actual = hoy_str[5:7]
        
        mes_sel = request.args.get('mes')
        anio_sel = request.args.get('anio')
        
        if not anio_sel: anio_sel = anio_actual
        if not mes_sel: mes_sel = mes_actual
            
        periodo_str = f"{anio_sel}-{mes_sel.zfill(2)}"
        
        lista_meses = [
            ('01', 'Enero'), ('02', 'Febrero'), ('03', 'Marzo'), ('04', 'Abril'), 
            ('05', 'Mayo'), ('06', 'Junio'), ('07', 'Julio'), ('08', 'Agosto'), 
            ('09', 'Septiembre'), ('10', 'Octubre'), ('11', 'Noviembre'), ('12', 'Diciembre')
        ]
        lista_anios = [str(y) for y in range(2025, int(anio_actual) + 2)]

        current_app.logger.info(f"DASHBOARD_LOAD: Usuario {user_id} consultando métricas de {periodo_str}")

        # --- 2. TARJETAS DE PODER (KPIs del periodo seleccionado) ---
        kpis = conn.execute("""
            SELECT 
                IFNULL(SUM(total), 0) as ingresos_brutos,
                IFNULL(SUM(total - costo_total), 0) as ganancia_neta,
                IFNULL(SUM(saldo_pendiente), 0) as dinero_calle,
                COUNT(id) as total_ventas
            FROM ventas 
            WHERE user_id = ? AND substr(fecha, 1, 7) = ? AND estado IN ('pagado', 'anticipo')
        """, (user_id, periodo_str)).fetchone()

        # --- 3. GRÁFICA 1: DIARIA (Mes seleccionado) ---
        grafica_diaria_db = conn.execute("""
            SELECT substr(fecha, 1, 10) as dia, SUM(total) as ingresos, SUM(total - costo_total) as ganancia
            FROM ventas
            WHERE user_id = ? AND substr(fecha, 1, 7) = ? AND estado IN ('pagado', 'anticipo')
            GROUP BY dia ORDER BY dia ASC
        """, (user_id, periodo_str)).fetchall()

        fechas_diarias, ing_diarios, gan_diarias = [], [], []
        for fila in grafica_diaria_db:
            fechas_diarias.append(f"Día {fila['dia'][-2:]}") 
            ing_diarios.append(round(fila['ingresos'], 2))
            gan_diarias.append(round(fila['ganancia'], 2))

        # --- 4. GRÁFICA 2: HISTÓRICA (Últimos 6 meses) ---
        # Calculamos la fecha de hace 5 meses para incluir el mes actual (6 meses totales)
        hace_6_meses = (datetime.strptime(hoy_str, '%Y-%m-%d') - relativedelta(months=5)).strftime('%Y-%m')
        
        grafica_hist_db = conn.execute("""
            SELECT substr(fecha, 1, 7) as mes, SUM(total) as ingresos, SUM(total - costo_total) as ganancia
            FROM ventas
            WHERE user_id = ? AND substr(fecha, 1, 7) >= ? AND estado IN ('pagado', 'anticipo')
            GROUP BY mes ORDER BY mes ASC
        """, (user_id, hace_6_meses)).fetchall()

        meses_hist, ing_hist, gan_hist = [], [], []
        for fila in grafica_hist_db:
            meses_hist.append(fila['mes']) # Ej: '2026-04'
            ing_hist.append(round(fila['ingresos'], 2))
            gan_hist.append(round(fila['ganancia'], 2))

        # --- 5. TOP 5 PRODUCTOS (Mes seleccionado) ---
        top_productos = conn.execute("""
            SELECT vd.concepto, SUM(vd.cantidad) as cantidad_vendida, SUM(vd.subtotal) as total_generado
            FROM venta_detalles vd JOIN ventas v ON vd.venta_id = v.id
            WHERE v.user_id = ? AND substr(v.fecha, 1, 7) = ? AND v.estado IN ('pagado', 'anticipo')
            GROUP BY vd.concepto ORDER BY cantidad_vendida DESC LIMIT 5
        """, (user_id, periodo_str)).fetchall()

    except Exception as e:
        current_app.logger.error(f"DASHBOARD_ERROR: Fallo al cargar panel para user {user_id} - {e}")
        kpis = {'ingresos_brutos': 0, 'ganancia_neta': 0, 'dinero_calle': 0, 'total_ventas': 0}
        fechas_diarias, ing_diarios, gan_diarias, meses_hist, ing_hist, gan_hist, top_productos = [], [], [], [], [], [], []
    finally:
        conn.close()

    # Empaquetamos todo para JS
    chart_data = {
        'diario': {'labels': fechas_diarias, 'ingresos': ing_diarios, 'ganancias': gan_diarias},
        'historico': {'labels': meses_hist, 'ingresos': ing_hist, 'ganancias': gan_hist}
    }

    return render_template(
        'dashboard/mi_panel.html', 
        kpis=kpis, 
        chart_data=chart_data, 
        top_productos=[dict(p) for p in top_productos],
        mes_sel=mes_sel, anio_sel=anio_sel,
        lista_meses=lista_meses, lista_anios=lista_anios
    )