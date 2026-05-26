from flask import Blueprint, render_template, session, current_app, request, jsonify
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
    u_name = session.get('username', 'Anonimo')
    
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        # --- 1. CONFIGURACIÓN DE FILTROS ---
        hoy_str = hoy_local()
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

        # --- 2. TARJETAS DE PODER (CORREGIDO CON AGREGACIÓN CONDICIONAL) ---
        # Quitamos el WHERE estado IN... global, y metemos un CASE en cada suma
        # para que cada KPI decida qué sumar exactamente sin excluir a los demás.
        cursor.execute("""
            SELECT 
                COALESCE(SUM(CASE WHEN estado IN ('pagado', 'anticipo') THEN total ELSE 0 END), 0) as total_facturado,
                COALESCE(SUM(CASE WHEN estado IN ('pagado', 'anticipo') THEN (COALESCE(subtotal, 0) - COALESCE(descuento_monto, 0)) ELSE 0 END), 0) as venta_neta_productos,
                COALESCE(SUM(CASE WHEN estado IN ('pagado', 'anticipo') THEN COALESCE(monto_pagado, 0) ELSE 0 END), 0) as ingresos_cobrados,
                COALESCE(SUM(CASE WHEN estado IN ('pagado', 'anticipo') THEN ((COALESCE(subtotal, 0) - COALESCE(descuento_monto, 0)) - COALESCE(costo_total, 0)) ELSE 0 END), 0) as ganancia_neta,
                COALESCE(SUM(CASE WHEN estado IN ('pagado', 'anticipo') THEN COALESCE(costo_total, 0) ELSE 0 END), 0) as costos_produccion,
                COALESCE(SUM(CASE WHEN estado IN ('pendiente', 'anticipo') THEN COALESCE(saldo_pendiente, 0) ELSE 0 END), 0) as dinero_calle,
                COUNT(CASE WHEN estado IN ('pagado', 'anticipo') THEN 1 END) as total_ventas,
                COUNT(CASE WHEN estado = 'cotizacion' THEN 1 END) as total_cotizaciones,
                COUNT(CASE WHEN estado = 'cancelada' THEN 1 END) as total_canceladas
            FROM ventas 
            WHERE user_id = %s AND to_char(fecha, 'YYYY-MM') = %s AND estado != 'cancelado'
        """, (user_id, periodo_str))
        kpis_row = cursor.fetchone()
        
        kpis = dict(kpis_row) if kpis_row else {
            'total_facturado': 0, 'venta_neta_productos': 0, 'ingresos_cobrados': 0, 'ganancia_neta': 0,
            'costos_produccion': 0, 'dinero_calle': 0, 'total_ventas': 0,
            'total_cotizaciones': 0, 'total_canceladas': 0
        }

        # --- 3. GRÁFICA 1: DIARIA (PROTECCIÓN DE NULOS EN COSTOS) ---
        cursor.execute("""
            SELECT to_char(fecha, 'YYYY-MM-DD') as dia, 
                   SUM(COALESCE(monto_pagado, 0)) as ingresos, 
                   SUM((COALESCE(subtotal, 0) - COALESCE(descuento_monto, 0)) - COALESCE(costo_total, 0)) as ganancia
            FROM ventas
            WHERE user_id = %s AND to_char(fecha, 'YYYY-MM') = %s AND estado IN ('pagado', 'anticipo')
            GROUP BY dia ORDER BY dia ASC
        """, (user_id, periodo_str))
        grafica_diaria_db = cursor.fetchall()

        fechas_diarias, ing_diarios, gan_diarias = [], [], []
        for fila in grafica_diaria_db:
            fechas_diarias.append(f"Día {fila['dia'][-2:]}") 
            ing_diarios.append(round(float(fila['ingresos']), 2))
            gan_diarias.append(round(float(fila['ganancia']), 2))

        # --- 4. GRÁFICA 2: HISTÓRICA (CORREGIDO VIAJE EN EL TIEMPO) ---
        # Ahora los 6 meses se calculan desde el mes que el usuario seleccionó, no desde el día de hoy.
        fecha_seleccionada = datetime.strptime(f"{anio_sel}-{mes_sel.zfill(2)}-01", '%Y-%m-%d')
        hace_6_meses = (fecha_seleccionada - relativedelta(months=5)).strftime('%Y-%m')
        
        cursor.execute("""
            SELECT to_char(fecha, 'YYYY-MM') as mes, 
                   SUM(COALESCE(monto_pagado, 0)) as ingresos, 
                   SUM((COALESCE(subtotal, 0) - COALESCE(descuento_monto, 0)) - COALESCE(costo_total, 0)) as ganancia
            FROM ventas
            WHERE user_id = %s 
              AND to_char(fecha, 'YYYY-MM') >= %s 
              AND to_char(fecha, 'YYYY-MM') <= %s 
              AND estado IN ('pagado', 'anticipo')
            GROUP BY mes ORDER BY mes ASC
        """, (user_id, hace_6_meses, periodo_str))
        grafica_hist_db = cursor.fetchall()

        meses_hist, ing_hist, gan_hist = [], [], []
        for fila in grafica_hist_db:
            meses_hist.append(fila['mes'])
            ing_hist.append(round(float(fila['ingresos']), 2))
            gan_hist.append(round(float(fila['ganancia']), 2))

        # --- 5. TOP 5 PRODUCTOS ---
        cursor.execute("""
            SELECT
                vd.concepto,
                SUM(vd.cantidad) as cantidad_vendida,
                SUM(vd.subtotal) as total_generado,
                SUM(vd.cantidad * COALESCE(vd.costo_unitario, 0)) as costo_generado,
                SUM(vd.subtotal - (vd.cantidad * COALESCE(vd.costo_unitario, 0))) as utilidad_estimada,
                COUNT(DISTINCT v.id) as tickets
            FROM venta_detalles vd JOIN ventas v ON vd.venta_id = v.id
            WHERE v.user_id = %s AND to_char(v.fecha, 'YYYY-MM') = %s AND v.estado IN ('pagado', 'anticipo')
            GROUP BY vd.concepto
            ORDER BY cantidad_vendida DESC, total_generado DESC
            LIMIT 5
        """, (user_id, periodo_str))
        top_productos = cursor.fetchall()

        # --- 6. ALERTAS DE STOCK BAJO ---
        cursor.execute("SELECT inventario_activo FROM configuracion WHERE user_id = %s", (user_id,))
        config_row = cursor.fetchone()
        inventario_activo = config_row['inventario_activo'] if config_row else False

        if inventario_activo:
            cursor.execute("""
                SELECT nombre, stock_actual, COALESCE(stock_minimo, 5) as stock_minimo, COALESCE(unidad_medida, 'pza') as unidad_medida
                FROM materiales
                WHERE user_id = %s AND stock_actual <= COALESCE(stock_minimo, 5)
                ORDER BY stock_actual ASC
                LIMIT 5
            """, (user_id,))
            stock_bajo = cursor.fetchall()
        else:
            stock_bajo = []

        current_app.logger.info(f"DATA_ACCESS: Usuario '{u_name}' (ID: {user_id}) consulto su dashboard ({periodo_str})")

    except Exception as e:
        current_app.logger.error(f"DASHBOARD_ERROR: Usuario '{u_name}' (ID: {user_id}) fallo al cargar panel - {e}")
        kpis = {
            'total_facturado': 0, 'venta_neta_productos': 0, 'ingresos_cobrados': 0, 'ganancia_neta': 0,
            'costos_produccion': 0, 'dinero_calle': 0, 'total_ventas': 0,
            'total_cotizaciones': 0, 'total_canceladas': 0
        }
        fechas_diarias, ing_diarios, gan_diarias, meses_hist, ing_hist, gan_hist, top_productos, stock_bajo = [], [], [], [], [], [], [], []
        inventario_activo = False
    finally:
        cursor.close()
        conn.close()

    chart_data = {
        'diario': {'labels': fechas_diarias, 'ingresos': ing_diarios, 'ganancias': gan_diarias},
        'historico': {'labels': meses_hist, 'ingresos': ing_hist, 'ganancias': gan_hist}
    }

    return render_template(
        'dashboard/mi_panel.html', 
        kpis=kpis, 
        chart_data=chart_data, 
        top_productos=[dict(p) for p in top_productos],
        stock_bajo=[dict(s) for s in stock_bajo],
        inventario_activo=inventario_activo,
        mes_sel=mes_sel, anio_sel=anio_sel,
        lista_meses=lista_meses, lista_anios=lista_anios
    )

# ==============================================================================
# NUEVO ENDPOINT: CALENDARIO DE VENTAS (HEATMAP)
# ==============================================================================
@user_dash_bp.route('/api/historial/calendario')
@login_required
def api_calendario_historial():
    user_id = session['user_id']
    u_name = session.get('username', 'Anonimo')
    
    anio = request.args.get('anio', type=int)
    mes = request.args.get('mes', type=int)
    
    if not anio or not mes:
        return jsonify({})

    mes_str = f"{mes:02d}"
    periodo_str = f"{anio}-{mes_str}"
    
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, cliente, total, estado, fecha 
            FROM ventas 
            WHERE user_id = %s AND to_char(fecha, 'YYYY-MM') = %s
        """, (user_id, periodo_str))
        ventas = cursor.fetchall()
        
        calendario = {}
        
        for v in ventas:
            # En Postgres, la fecha extraída será un objeto datetime.
            # Lo convertimos a string con formato YYYY-MM-DD
            fecha_exacta = v['fecha'].strftime('%Y-%m-%d')
            
            if fecha_exacta not in calendario:
                calendario[fecha_exacta] = []
                
            calendario[fecha_exacta].append({
                'id': v['id'],
                'cliente': v['cliente'],
                'total': float(v['total']),
                'estado': v['estado']
            })
            
        current_app.logger.info(f"DATA_ACCESS: Usuario '{u_name}' (ID: {user_id}) consulto el calendario de ventas ({periodo_str})")
        return jsonify(calendario)
        
    except Exception as e:
        current_app.logger.error(f"CALENDAR_ERROR: Usuario '{u_name}' (ID: {user_id}) fallo al cargar calendario - {e}")
        return jsonify({})
    finally:
        cursor.close()
        conn.close()
