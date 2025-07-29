import pandas as pd
import numpy as np
from fastapi import HTTPException
from pvlib import location, modelchain, pvsystem, temperature, solarposition, irradiance
from app.constants import PV_MODULES, INVERTERS, SYSTEM_LOSSES, SYSTEM_COSTS, TARGET_YEAR, LOCAL_TZ
from app.models import *
import logging

import logging
logger = logging.getLogger(__name__)


def get_pvgis_data(lat: float, lon: float) -> tuple:
    """Obtiene datos TMY y parámetros solares desde PVGIS usando pvlib."""
    try:
        from pvlib.iotools import get_pvgis_tmy, get_pvgis_hourly

        logger.info(f"Descargando datos TMY para lat={lat}, lon={lon}")

        # -------- 1. Serie TMY (sin optimalangles) -----------------
        weather_data, meta_tmy = get_pvgis_tmy(
            lat, lon,
            outputformat='json',
            usehorizon=True,
            startyear=2005,
            endyear=2020,
            coerce_year=None,
            timeout=60
        )
        
        # Quitar cualquier duplicado que origine PVGIS (cambios de DST)
        weather_data = weather_data[~weather_data.index.duplicated(keep="first")]


        # -------- 2. Ángulos óptimos (llamada rápida) --------------
        _, meta_opt = get_pvgis_hourly(
            lat, lon,
            start=2020,      # un solo año → respuesta pequeña
            end=2020,
            outputformat='json',
            usehorizon=True,
            optimalangles=True,
            components=False,
            timeout=30
        )

        optimal_info = meta_opt.get('optimal', {})  # {'slope': …, 'aspect': …}

        if not optimal_info:
            # Retrocompatibilidad con builds antiguas
            optimal_info = meta_opt.get('optimalangles', {}) or \
                           meta_opt.get('optimalinclination', {})

        if not optimal_info:
            # Fallback: aproximación por latitud
            optimal_tilt = max(15, min(45, abs(lat) - 10))
            optimal_azimuth = 180
            logger.warning(
                f"Usando ángulos estimados: tilt={optimal_tilt}°, azimuth={optimal_azimuth}°"
            )
        else:
            optimal_tilt = optimal_info.get('slope') or optimal_info.get('slope_opt') \
                           or max(15, min(45, abs(lat) - 10))
            optimal_azimuth = optimal_info.get('aspect') or optimal_info.get('azimuth_opt') \
                              or 180

        # -------- 3. Limpieza de la tabla climática ----------------
        required_columns = ['ghi', 'dni', 'dhi', 'temp_air', 'wind_speed']
        missing_cols = [c for c in required_columns if c not in weather_data.columns]
        if missing_cols:
            logger.warning(f"Columnas faltantes: {missing_cols}")
            if 'wind_speed' in missing_cols:
                weather_data['wind_speed'] = 2.0  # valor por defecto

        for col in required_columns:
            if col in weather_data.columns:
                weather_data[col] = pd.to_numeric(weather_data[col], errors='coerce')

        weather_data = weather_data.dropna(subset=['ghi', 'dni', 'dhi', 'temp_air'])
        if len(weather_data) < 8000:
            raise ValueError("Datos meteorológicos insuficientes")

        logger.info(
            f"Datos procesados: {len(weather_data)} registros, "
            f"tilt óptimo: {optimal_tilt}°, azimuth óptimo: {optimal_azimuth}°"
        )
        return weather_data, optimal_tilt, optimal_azimuth

    except Exception as e:
        logger.error(f"Error obteniendo datos PVGIS: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error datos climáticos: {str(e)}")


def calculate_orientation_losses(lat: float, tilt: float, azimuth: float, 
                               optimal_tilt: float, optimal_azimuth: float = 180) -> float:
    """Calcula pérdidas por orientación e inclinación según fórmulas del CTE"""
    # Convertir azimuth a desviación respecto al sur
    azimuth_deviation = abs(azimuth - 180)
    if azimuth_deviation > 180:
        azimuth_deviation = 360 - azimuth_deviation
    
    # Fórmulas del CTE-HE5
    if tilt > 15:
        losses = 100 * (1.2e-4 * (tilt - lat + 10)**2 + 3.5e-5 * azimuth_deviation**2)
    else:
        losses = 100 * (1.2e-4 * (tilt - lat + 10)**2)
    
    return max(0, min(losses, 50))  # Limitar entre 0-50%

def determine_installation_geometry(
    lat: float,
    roof_tilt: Optional[float], 
    roof_azimuth: Optional[float], 
    installation_type: str,
    optimal_tilt: float
) -> tuple:
    """
    Determines final installation geometry (tilt, azimuth)
    and whether support structure is required (for flat roofs).
    """
    with_support = False  # Default: no extra structure

    if installation_type == "coplanar" and roof_tilt is not None:
        if roof_tilt < 10:
            final_tilt = optimal_tilt
            with_support = True
            logger.info(f"Flat roof detected: {roof_tilt}° < 10° → using optimal tilt: {final_tilt:.1f}° with support structure.")
        else:
            final_tilt = roof_tilt
            logger.info(f"Coplanar installation with roof tilt: {final_tilt:.1f}° (no support needed).")
        final_azimuth = roof_azimuth if roof_azimuth is not None else 180
        logger.info(f"Using azimuth: {final_azimuth:.1f}°")

    elif installation_type == "fixed" and roof_tilt is not None:
        final_tilt = roof_tilt
        final_azimuth = roof_azimuth if roof_azimuth is not None else 180
        logger.info(f"Fixed structure with tilt: {final_tilt:.1f}°, azimuth: {final_azimuth:.1f}° (no support needed).")

    else:
        final_tilt = optimal_tilt
        final_azimuth = 180
        with_support = True
        logger.info(f"No geometry provided → using optimal: tilt={final_tilt:.1f}°, azimuth={final_azimuth}° with support structure.")

    return final_tilt, final_azimuth, with_support


def select_pv_module() -> dict:
    """Selecciona el módulo fotovoltaico por defecto"""
    return PV_MODULES["ja_solar_545"]

def calculate_system_size(annual_consumption: float, coverage_percent: float,
                         specific_yield_estimate: float, available_area: Optional[float],
                         module: dict) -> tuple:
    """Calcula el tamaño óptimo del sistema"""
    
    # Energía objetivo
    target_energy = annual_consumption * (coverage_percent / 100)
    
    # Potencia necesaria
    required_kwp = target_energy / specific_yield_estimate
    
    # Número de módulos
    module_power_kw = module["power_stc"] / 1000
    n_modules = max(1, round(required_kwp / module_power_kw))
    
    # Verificar limitación por área
    if available_area:
        max_modules_by_area = int(available_area / module["area_m2"] * 0.75)  # 75% aprovechamiento
        if n_modules > max_modules_by_area:
            n_modules = max_modules_by_area
            logger.warning(f"Sistema limitado por área: {n_modules} módulos")
    
    actual_kwp = n_modules * module_power_kw
    total_area = n_modules * module["area_m2"]
    
    return n_modules, actual_kwp, total_area

def select_inverter(kwp_dc: float) -> dict:
    """Selecciona el inversor óptimo para la potencia DC"""
    target_ratio = 1.2  # Ratio DC/AC objetivo
    
    best_inverter = None
    best_score = float('inf')
    
    for ac_power, specs in INVERTERS.items():
        if kwp_dc > specs["max_dc"]:
            continue
            
        ratio = kwp_dc / ac_power
        score = abs(ratio - target_ratio)
        
        if score < best_score:
            best_score = score
            best_inverter = {"ac_power": ac_power, **specs}
    
    if best_inverter is None:
        # Usar el inversor más pequeño disponible
        ac_power = min(INVERTERS.keys())
        best_inverter = {"ac_power": ac_power, **INVERTERS[ac_power]}
    
    return best_inverter

def calculate_string_configuration(n_modules: int, module: dict, inverter: dict, 
                                weather_data: pd.DataFrame) -> tuple:
    """Calcula la configuración óptima de strings CORREGIDA"""
    
    # Temperaturas extremas del sitio
    t_air_min = weather_data['temp_air'].min() - 10  # Margen de seguridad 10°C
    t_air_max = weather_data['temp_air'].quantile(0.99)  # Percentil 99%
    
    # Temperatura de célula máxima usando NOCT
    t_cell_max = t_air_max + (module['noct'] - 20) * 0.8  # Factor de corrección
    
    logger.info(f"Temperaturas: T_air_min={t_air_min:.1f}°C, T_cell_max={t_cell_max:.1f}°C")
    
    # Tensiones corregidas por temperatura
    v_mp_hot = module['v_mp'] * (1 + module['temp_coef_vmp']/100 * (t_cell_max - 25))
    v_oc_cold = module['v_oc'] * (1 + module['temp_coef_voc']/100 * (t_air_min - 25))
    
    # Límites de módulos por string (con márgenes de seguridad)
    min_modules_string = max(2, int(inverter["mppt_min"] / v_mp_hot * 1.1))  # Mínimo 2
    max_modules_string = int(inverter["mppt_max"] / v_oc_cold * 0.9)        # Margen 10%
    
    logger.info(f"Límites string: {min_modules_string}-{max_modules_string} módulos")
    logger.info(f"V_mp_hot={v_mp_hot:.1f}V, V_oc_cold={v_oc_cold:.1f}V")
    
    # NUEVA LÓGICA: Priorizar configuraciones válidas
    best_config = None
    min_waste = float('inf')
    
    # Intentar configuraciones desde las más eficientes
    possible_configs = []
    
    for modules_per_string in range(min_modules_string, min(max_modules_string + 1, n_modules + 1)):
        for strings_parallel in range(1, min(6, n_modules // modules_per_string + 2)):  # Max 6 strings
            total_modules = modules_per_string * strings_parallel
            
            # Verificar límite de potencia DC
            total_dc_power = total_modules * module["power_stc"] / 1000
            if total_dc_power > inverter["max_dc"]:
                continue
                
            # Verificar tensiones en rango MPPT
            string_vmp = modules_per_string * v_mp_hot
            string_voc = modules_per_string * v_oc_cold
            
            if not (inverter["mppt_min"] <= string_vmp <= inverter["mppt_max"]):
                continue
                
            waste = abs(total_modules - n_modules)
            possible_configs.append((modules_per_string, strings_parallel, total_modules, waste))
    
    if possible_configs:
        # Ordenar por desperdicio y seleccionar mejor
        possible_configs.sort(key=lambda x: x[3])
        best = possible_configs[0]
        best_config = (best[0], best[1], best[2])
    else:
        # Configuración de emergencia VÁLIDA
        modules_per_string = max(min_modules_string, 2)
        strings_parallel = 1
        total_modules = modules_per_string * strings_parallel
        best_config = (modules_per_string, strings_parallel, total_modules)
        logger.warning("Usando configuración de emergencia")
    
    logger.info(f"Configuración final: {best_config[1]}S × {best_config[0]}P = {best_config[2]} módulos")
    
    return best_config

def calculate_pv_production(weather_data: pd.DataFrame, lat: float, lon: float,
                           tilt: float, azimuth: float, modules_per_string: int,
                           strings_parallel: int, module: dict, inverter: dict) -> dict:
    """Simula la producción del sistema FV con pvlib"""
    
    try:
        # Configurar ubicación
        site = location.Location(lat, lon, tz='Europe/Madrid')
        
        # Parámetros del módulo para pvlib
        module_params = {
            'pdc0': module['power_stc'],
            'v_mp': module['v_mp'],
            'i_mp': module['i_mp'],
            'v_oc': module['v_oc'],
            'i_sc': module['i_sc'],
            'alpha_sc': module['temp_coef_isc'] / 100 * module['i_sc'],
            'beta_voc': module['temp_coef_voc'],
            'gamma_pdc': module['temp_coef_power'] / 100,
            'cells_in_series': module['cells_in_series']
        }
        
        # Parámetros del inversor
        inverter_params = {
            'pdc0': strings_parallel * modules_per_string * module['power_stc'],
            'eta_inv_nom': inverter['efficiency'] / 100,
            'eta_inv_ref': inverter['efficiency'] / 100
        }
        
        # Modelo térmico
        temperature_params = {'u0': 25.0, 'u1': 6.84}
        
        # Configurar array
        array = pvsystem.Array(
            mount=pvsystem.FixedMount(surface_tilt=tilt, surface_azimuth=azimuth),
            module_parameters=module_params,
            temperature_model_parameters=temperature_params,
            modules_per_string=modules_per_string,
            strings=strings_parallel
        )
        
        # Sistema PV
        system = pvsystem.PVSystem(arrays=[array], inverter_parameters=inverter_params)
        
        # Cadena de modelos
        mc = modelchain.ModelChain(
            system, site,
            aoi_model='physical',
            spectral_model='no_loss',
            temperature_model='faiman',
            losses_model='no_loss'
        )
        
        # Ejecutar simulación
        mc.run_model(weather_data)
        
        # Aplicar pérdidas adicionales del sistema
        total_loss_factor = 1.0
        for loss_name, loss_value in SYSTEM_LOSSES.items():
            if loss_name not in ['temperature', 'irradiance', 'inverter']:  # Ya incluidas en pvlib
                total_loss_factor *= (1 - loss_value)
        
        ac_power_with_losses = mc.results.ac * total_loss_factor
        
        # Convertir a kWh horarios
        production_hourly = ac_power_with_losses / 1000  # kW to kWh (datos horarios)
        production_hourly = production_hourly[~production_hourly.index.duplicated(keep="first")]
        
        # Análisis mensual
        production_monthly = []
        for month in range(1, 13):
            month_mask = production_hourly.index.month == month
            month_production = production_hourly[month_mask].sum()
            production_monthly.append(round(month_production, 1))
        
        # Cálculos anuales
        annual_production = production_hourly.sum()
        total_kwp = strings_parallel * modules_per_string * module['power_stc'] / 1000
        
        specific_yield = annual_production / total_kwp if total_kwp > 0 else 0
        
        # Performance Ratio usando POA (según IEC 61724-1)
        poa_global = mc.results.total_irrad['poa_global']
        poa_sum = poa_global.sum() / 1000  # kWh/m²
        theoretical_yield_poa = total_kwp * poa_sum
        performance_ratio = annual_production / theoretical_yield_poa if theoretical_yield_poa > 0 else 0
        
        # Capacity Factor
        capacity_factor = annual_production / (total_kwp * 8760) * 100 if total_kwp > 0 else 0
        
        logger.info(f"Producción anual calculada: {annual_production:.0f} kWh")
        
        return {
            'annual_production': annual_production,
            'monthly_production': production_monthly,
            'specific_yield': specific_yield,
            'performance_ratio': performance_ratio,
            'capacity_factor': capacity_factor,
            'hourly_production': production_hourly
        }
        
    except Exception as e:
        logger.error(f"Error en simulación PV: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error simulación: {str(e)}")

def generate_consumption_profile(annual_consumption: float, year: int = 2024, tz: str = "Europe/Madrid") -> pd.Series:
    """Genera perfil de consumo horario realista SIN timezone para evitar conflictos"""
    
    # Crear índice horario SIN timezone (igual que pvlib)
    start_date = f"{year}-01-01"
    end_date = f"{year}-12-31 23:00:00"
    dates = pd.date_range(start_date, end_date, freq="h", tz=tz, ambiguous="NaT", nonexistent="shift_forward")
    
    # Patrón diario típico residencial
    daily_pattern = np.array([
        0.4, 0.3, 0.3, 0.3, 0.3, 0.4,  # 0-5h: noche
        0.6, 0.8, 1.0, 0.9, 0.8, 0.7,  # 6-11h: mañana
        0.8, 0.9, 1.0, 1.1, 1.2, 1.3,  # 12-17h: tarde
        1.5, 1.7, 1.4, 1.1, 0.8, 0.6   # 18-23h: noche
    ])
    
    # Factor estacional
    day_of_year = dates.dayofyear.values
    seasonal_factor = 1 + 0.3 * np.cos(2 * np.pi * (day_of_year - 21) / 365)
    
    # Factor fin de semana
    is_weekend = dates.weekday.values >= 5
    weekend_factor = np.where(is_weekend, 1.1, 1.0)
    
    # Construir perfil horario
    hourly_pattern = np.tile(daily_pattern, len(dates) // 24 + 1)[:len(dates)]
    consumption = hourly_pattern * seasonal_factor * weekend_factor
    
    # Normalizar al consumo anual
    consumption = consumption * annual_consumption / consumption.sum()
    
    return pd.Series(consumption, index=dates)

def analyze_autoconsumption(production_hourly: pd.Series, consumption_hourly: pd.Series) -> dict:
    """Analiza el autoconsumo del sistema con análisis mensual detallado"""
    
    # Alinear índices temporales
    common_index = production_hourly.index.intersection(consumption_hourly.index).unique()
    prod_aligned = production_hourly.reindex(common_index, fill_value=0)
    cons_aligned = consumption_hourly.reindex(common_index, fill_value=consumption_hourly.mean())
    
    logger.info(f"Análisis autoconsumo: {len(common_index)} registros horarios alineados")
    logger.info(f"Producción total: {prod_aligned.sum():.0f} kWh")
    logger.info(f"Consumo total: {cons_aligned.sum():.0f} kWh")
    
    # Cálculos horarios
    self_consumption = np.minimum(prod_aligned, cons_aligned)
    grid_injection = np.maximum(0, prod_aligned - cons_aligned)
    grid_purchase = np.maximum(0, cons_aligned - prod_aligned)
    
    # Totales anuales
    annual_production = prod_aligned.sum()
    annual_consumption = cons_aligned.sum()
    annual_self_consumption = self_consumption.sum()
    annual_grid_injection = grid_injection.sum()
    annual_grid_purchase = grid_purchase.sum()
    
    # Ratios
    self_consumption_rate = (annual_self_consumption / annual_production * 100) if annual_production > 0 else 0
    self_sufficiency_rate = (annual_self_consumption / annual_consumption * 100) if annual_consumption > 0 else 0
    
    # Análisis mensual DETALLADO como antes
    month_names = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
                   "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
    
    monthly_analysis = []
    for month in range(1, 13):
        month_mask = common_index.month == month
        if month_mask.sum() == 0:
            continue
            
        month_prod = prod_aligned[month_mask].sum()
        month_cons = cons_aligned[month_mask].sum()
        month_self = self_consumption[month_mask].sum()
        month_injection = grid_injection[month_mask].sum()
        month_purchase = grid_purchase[month_mask].sum()
        
        month_data = {
            'month': month,
            'month_name': month_names[month-1],
            'production_kwh': round(month_prod, 1),
            'consumption_kwh': round(month_cons, 1),
            'self_consumption_kwh': round(month_self, 1),
            'grid_injection_kwh': round(month_injection, 1),
            'grid_consumption_kwh': round(month_purchase, 1),  # Como en el ejemplo
            'self_consumption_rate_percent': round(month_self/month_prod*100, 1) if month_prod > 0 else 0,
            'self_sufficiency_rate_percent': round(month_self/month_cons*100, 1) if month_cons > 0 else 0
        }
        monthly_analysis.append(month_data)
    
    return {
        'annual_consumption': annual_consumption,
        'annual_self_consumption': annual_self_consumption,
        'annual_grid_injection': annual_grid_injection,
        'annual_grid_purchase': annual_grid_purchase,
        'self_consumption_rate': self_consumption_rate,
        'self_sufficiency_rate': self_sufficiency_rate,
        'monthly_analysis': monthly_analysis
    }

def add_economic_analysis_to_monthly(monthly_analysis: List[dict], 
                                   electricity_price: float, 
                                   surplus_price: float) -> List[MonthlyAnalysis]:
    """Añade análisis económico mensual detallado"""
    
    enhanced_monthly = []
    for month_data in monthly_analysis:
        # Calcular ahorros económicos del mes
        self_consumption_savings = month_data['self_consumption_kwh'] * electricity_price
        surplus_income = month_data['grid_injection_kwh'] * surplus_price
        total_monthly_savings = self_consumption_savings + surplus_income
        
        enhanced_data = MonthlyAnalysis(
            month=month_data['month'],
            month_name=month_data['month_name'],
            production_kwh=month_data['production_kwh'],
            consumption_kwh=month_data['consumption_kwh'],
            self_consumption_kwh=month_data['self_consumption_kwh'],
            grid_injection_kwh=month_data['grid_injection_kwh'],
            grid_consumption_kwh=month_data['grid_consumption_kwh'],
            self_consumption_rate_percent=month_data['self_consumption_rate_percent'],
            self_sufficiency_rate_percent=month_data['self_sufficiency_rate_percent'],
            economic_savings_eur=round(total_monthly_savings, 2)
        )
        enhanced_monthly.append(enhanced_data)
    
    return enhanced_monthly

def calculate_economics(system_kwp: float, annual_self_consumption: float,
                       annual_grid_injection: float, annual_grid_purchase: float,
                       electricity_price: float, surplus_price: float) -> dict:
    """Calcula el análisis económico CORREGIDO"""
    
    # Costes del sistema
    system_cost = system_kwp * 1000 * SYSTEM_COSTS["cost_per_wp"]
    annual_maintenance = system_kwp * SYSTEM_COSTS["maintenance_annual"]
    annual_insurance = system_kwp * SYSTEM_COSTS["insurance_annual"]
    annual_opex = annual_maintenance + annual_insurance
    
    # Ahorros anuales año 1
    savings_self_consumption = annual_self_consumption * electricity_price
    income_surplus = annual_grid_injection * surplus_price
    annual_savings_gross = savings_self_consumption + income_surplus
    annual_savings_net = annual_savings_gross - annual_opex
    
    logger.info(f"Economía: autoconsumo={annual_self_consumption:.0f} kWh, "
                f"excedentes={annual_grid_injection:.0f} kWh")
    logger.info(f"Ahorro bruto: {annual_savings_gross:.0f} €, "
                f"OPEX: {annual_opex:.0f} €, neto: {annual_savings_net:.0f} €")
    
    # Periodo de retorno simple
    payback_years = system_cost / annual_savings_net if annual_savings_net > 0 else 999
    
    # Parámetros financieros
    discount_rate = 0.04  # 4%
    inflation_electricity = 0.03  # 3%
    inflation_surplus = 0.02  # 2%
    inflation_opex = 0.025  # 2.5%
    degradation_rate = SYSTEM_COSTS["degradation_annual"]
    
    # Calcular flujos de caja anuales para NPV y TIR
    cash_flows = [-system_cost]  # Año 0: inversión inicial
    
    for year in range(1, 26):  # 25 años
        # Degradación de la producción
        degradation_factor = (1 - degradation_rate) ** (year - 1)
        year_self_consumption = annual_self_consumption * degradation_factor
        year_grid_injection = annual_grid_injection * degradation_factor
        
        # Precios con inflación
        year_electricity_price = electricity_price * (1 + inflation_electricity) ** (year - 1)
        year_surplus_price = surplus_price * (1 + inflation_surplus) ** (year - 1)
        year_opex = annual_opex * (1 + inflation_opex) ** (year - 1)
        
        # Flujo de caja del año
        year_savings_gross = (year_self_consumption * year_electricity_price + 
                             year_grid_injection * year_surplus_price)
        year_cash_flow = year_savings_gross - year_opex
        cash_flows.append(year_cash_flow)
    
    # NPV
    npv = sum(cf / (1 + discount_rate) ** i for i, cf in enumerate(cash_flows))
    
    # TIR usando búsqueda binaria MEJORADA
    def npv_at_rate(rate):
        if rate <= -1:  # Evitar divisiones por 0
            return float('inf')
        try:
            return sum(cf / (1 + rate) ** i for i, cf in enumerate(cash_flows))
        except:
            return float('inf')
    
    # Verificar si es viable calcular TIR
    if annual_savings_net <= 0:
        irr_percent = -100.0  # Proyecto no viable
    else:
        # Búsqueda binaria para TIR
        irr_low, irr_high = -0.99, 2.0  # Rango -99% a 200%
        tolerance = 1e-6
        max_iterations = 100
        
        irr_percent = 0.0
        for _ in range(max_iterations):
            irr_mid = (irr_low + irr_high) / 2
            npv_mid = npv_at_rate(irr_mid)
            
            if abs(npv_mid) < tolerance:
                irr_percent = irr_mid * 100
                break
                
            if npv_mid > 0:
                irr_low = irr_mid
            else:
                irr_high = irr_mid
        else:
            # Fallback: aproximación simple
            irr_percent = (annual_savings_net / system_cost * 100) if system_cost > 0 else -100
    
    # LCOE real con inflación y degradación
    total_energy_discounted = 0
    total_cost_discounted = system_cost
    
    for year in range(1, 26):
        degradation_factor = (1 - degradation_rate) ** (year - 1)
        year_energy = (annual_self_consumption + annual_grid_injection) * degradation_factor
        year_opex_inflated = annual_opex * (1 + inflation_opex) ** (year - 1)
        
        total_energy_discounted += year_energy / (1 + discount_rate) ** year
        total_cost_discounted += year_opex_inflated / (1 + discount_rate) ** year
    
    lcoe = total_cost_discounted / total_energy_discounted if total_energy_discounted > 0 else 999
    
    return {
        'system_cost': system_cost,
        'annual_savings': annual_savings_net,
        'payback_years': payback_years,
        'npv_25_years': npv,
        'irr_percent': irr_percent,
        'lcoe_eur_kwh': lcoe
    }
