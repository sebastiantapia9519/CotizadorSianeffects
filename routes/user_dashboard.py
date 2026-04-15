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
    conn = get_db()
    
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

        # --- 2. TARJETAS DE PODER Y DATOS PARA LA DONA ---
        kpis_row = conn.execute("""
            SELECT 
                IFNULL(SUM(total), 0) as ingresos_brutos,
                IFNULL(SUM(total - costo_total), 0) as ganancia_neta,
                IFNULL(SUM(costo_total), 0) as costos_produccion,
                IFNULL(SUM(saldo_pendiente), 0) as dinero_calle,
                COUNT(id) as total_ventas
            FROM ventas 
            WHERE user_id = ? AND substr(fecha, 1, 7) = ? AND estado IN ('pagado', 'anticipo')
        """, (user_id, periodo_str)).fetchone()
        
        kpis = dict(kpis_row) if kpis_row else {'ingresos_brutos': 0, 'ganancia_neta': 0, 'costos_produccion': 0, 'dinero_calle': 0, 'total_ventas': 0}

        # --- 3. GRÁFICA 1: DIARIA ---
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

        # --- 4. GRÁFICA 2: HISTÓRICA ---
        hace_6_meses = (datetime.strptime(hoy_str, '%Y-%m-%d') - relativedelta(months=5)).strftime('%Y-%m')
        
        grafica_hist_db = conn.execute("""
            SELECT substr(fecha, 1, 7) as mes, SUM(total) as ingresos, SUM(total - costo_total) as ganancia
            FROM ventas
            WHERE user_id = ? AND substr(fecha, 1, 7) >= ? AND estado IN ('pagado', 'anticipo')
            GROUP BY mes ORDER BY mes ASC
        """, (user_id, hace_6_meses)).fetchall()

        meses_hist, ing_hist, gan_hist = [], [], []
        for fila in grafica_hist_db:
            meses_hist.append(fila['mes'])
            ing_hist.append(round(fila['ingresos'], 2))
            gan_hist.append(round(fila['ganancia'], 2))

        # --- 5. TOP 5 PRODUCTOS ---
        top_productos = conn.execute("""
            SELECT vd.concepto, SUM(vd.cantidad) as cantidad_vendida, SUM(vd.subtotal) as total_generado
            FROM venta_detalles vd JOIN ventas v ON vd.venta_id = v.id
            WHERE v.user_id = ? AND substr(v.fecha, 1, 7) = ? AND v.estado IN ('pagado', 'anticipo')
            GROUP BY vd.concepto ORDER BY cantidad_vendida DESC LIMIT 5
        """, (user_id, periodo_str)).fetchall()

        # --- 6. ALERTAS DE STOCK BAJO (CON FIX DE TIPOS) ---
        # Primero revisamos si el usuario tiene el módulo de inventario encendido
        config_row = conn.execute("SELECT inventario_activo FROM configuracion WHERE user_id = ?", (user_id,)).fetchone()
        inventario_activo = config_row['inventario_activo'] if config_row else 0

        if inventario_activo:
            # Obligamos a SQLite a comparar todo como números reales (CAST AS REAL)
            stock_bajo = conn.execute("""
                SELECT nombre, stock_actual, IFNULL(stock_minimo, 5) as stock_minimo, IFNULL(unidad_medida, 'pza') as unidad_medida
                FROM materiales
                WHERE user_id = ? AND CAST(stock_actual AS REAL) <= CAST(IFNULL(stock_minimo, 5) AS REAL)
                ORDER BY CAST(stock_actual AS REAL) ASC
                LIMIT 5
            """, (user_id,)).fetchall()
        else:
            stock_bajo = []

    except Exception as e:
        current_app.logger.error(f"DASHBOARD_ERROR: Fallo al cargar panel para user {user_id} - {e}")
        kpis = {'ingresos_brutos': 0, 'ganancia_neta': 0, 'costos_produccion': 0, 'dinero_calle': 0, 'total_ventas': 0}
        fechas_diarias, ing_diarios, gan_diarias, meses_hist, ing_hist, gan_hist, top_productos, stock_bajo = [], [], [], [], [], [], [], []
        inventario_activo = 0
    finally:
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
    anio = request.args.get('anio', type=int)
    mes = request.args.get('mes', type=int)
    
    # Si por alguna razón no mandan año o mes, abortamos limpiamente
    if not anio or not mes:
        return jsonify({})

    # Formateamos el mes a dos dígitos (ej. 4 -> "04")
    mes_str = f"{mes:02d}"
    periodo_str = f"{anio}-{mes_str}"
    
    conn = get_db()
    try:
        # Traemos todas las ventas de ese mes exacto para este usuario
        ventas = conn.execute("""
            SELECT id, cliente, total, estado, fecha 
            FROM ventas 
            WHERE user_id = ? AND substr(fecha, 1, 7) = ?
        """, (user_id, periodo_str)).fetchall()
        
        calendario = {}
        
        # Agrupamos las ventas por día ("YYYY-MM-DD")
        for v in ventas:
            fecha_exacta = str(v['fecha'])[:10]
            
            if fecha_exacta not in calendario:
                calendario[fecha_exacta] = []
                
            calendario[fecha_exacta].append({
                'id': v['id'],
                'cliente': v['cliente'],
                'total': float(v['total']),
                'estado': v['estado']
            })
            
        return jsonify(calendario)
        
    except Exception as e:
        current_app.logger.error(f"CALENDAR_ERROR: Fallo al cargar calendario para user {user_id} - {e}")
        return jsonify({})
    finally:
        conn.close()