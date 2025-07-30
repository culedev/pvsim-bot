"""
simulation.py
==============
Lógica principal de simulación y dimensionado fotovoltaico profesional.
Todas las funciones críticas para obtener datos solares, calcular geometría,
dimensionar el sistema, simular producción, analizar autoconsumo y retorno económico.

Los logs están orientados a que cualquier usuario técnico o no técnico
pueda entender rápidamente los pasos clave, configuraciones adoptadas,
alertas de diseño y el rendimiento esperado de la instalación.
"""

import pandas as pd
import numpy as np
from fastapi import HTTPException
from pvlib import location, modelchain, pvsystem, temperature, solarposition, irradiance
from app.constants import *
from app.models import *

import logging
logger = logging.getLogger(__name__)

def get_pvgis_data(lat: float, lon: float) -> tuple:
    """
    Descarga y prepara los datos meteorológicos horarios (TMY) de PVGIS.
    Extrae además los ángulos óptimos de inclinación y azimut para la localización dada.
    """
    try:
        from pvlib.iotools import get_pvgis_tmy, get_pvgis_hourly

        logger.info(f"🟢 [PVGIS] Solicitando datos climáticos y ángulos óptimos para (lat={lat:.4f}, lon={lon:.4f})...")

        # 1. Descargar TMY horario (serie meteorológica típica de años recientes)
        weather_data, meta_tmy = get_pvgis_tmy(
            lat, lon,
            outputformat='json',
            usehorizon=True,
            startyear=2005,
            endyear=2020,
            coerce_year=None,
            timeout=60
        )
        weather_data = weather_data[~weather_data.index.duplicated(keep="first")]

        # 2. Descargar ángulos óptimos de inclinación y orientación
        _, meta_opt = get_pvgis_hourly(
            lat, lon,
            start=2020,
            end=2020,
            outputformat='json',
            usehorizon=True,
            optimalangles=True,
            components=False,
            timeout=30
        )
        optimal_info = meta_opt.get('optimal', {}) or meta_opt.get('optimalangles', {}) or meta_opt.get('optimalinclination', {})

        if not optimal_info:
            # Fallback: latitud - 10 y orientación sur
            optimal_tilt = max(15, min(45, abs(lat) - 10))
            optimal_azimuth = 180
            logger.warning(f"⚠️ No se encontraron ángulos óptimos, usando estimación por latitud ({optimal_tilt}°, {optimal_azimuth}°).")
        else:
            optimal_tilt = optimal_info.get('slope') or optimal_info.get('slope_opt') or max(15, min(45, abs(lat) - 10))
            optimal_azimuth = optimal_info.get('aspect') or optimal_info.get('azimuth_opt') or 180

        # 3. Validar y limpiar tabla meteorológica
        required_columns = ['ghi', 'dni', 'dhi', 'temp_air', 'wind_speed']
        missing_cols = [c for c in required_columns if c not in weather_data.columns]
        if missing_cols:
            logger.warning(f"⚠️ [PVGIS] Columnas faltantes en datos TMY: {missing_cols}")
            if 'wind_speed' in missing_cols:
                weather_data['wind_speed'] = 2.0  # Valor estándar

        for col in required_columns:
            if col in weather_data.columns:
                weather_data[col] = pd.to_numeric(weather_data[col], errors='coerce')

        weather_data = weather_data.dropna(subset=['ghi', 'dni', 'dhi', 'temp_air'])
        if len(weather_data) < 8000:
            logger.error(f"❌ [PVGIS] Serie meteorológica insuficiente: solo {len(weather_data)} registros.")
            raise ValueError("Datos meteorológicos insuficientes para simulación.")

        logger.info(f"🟢 [PVGIS] TMY descargado y validado: {len(weather_data)} registros horarios, tilt óptimo={optimal_tilt}°, azimut óptimo={optimal_azimuth}°")
        return weather_data, optimal_tilt, optimal_azimuth

    except Exception as e:
        logger.error(f"❌ [PVGIS] Error obteniendo datos: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error datos climáticos: {str(e)}")

def calculate_orientation_losses(lat: float, tilt: float, azimuth: float, 
                               optimal_tilt: float, optimal_azimuth: float = 180) -> float:
    """
    Calcula las pérdidas de producción por desviación de la inclinación y azimut respecto al óptimo (según CTE-HE5).
    """
    azimuth_deviation = abs(azimuth - 180)
    if azimuth_deviation > 180:
        azimuth_deviation = 360 - azimuth_deviation

    # Pérdida estimada (porcentaje)
    if tilt > 15:
        losses = 100 * (1.2e-4 * (tilt - lat + 10)**2 + 3.5e-5 * azimuth_deviation**2)
    else:
        losses = 100 * (1.2e-4 * (tilt - lat + 10)**2)
    losses_capped = max(0, min(losses, 50))

    logger.info(f"⚡️ [Geometría] Pérdidas por orientación/inclinación: {losses_capped:.2f}% (tilt={tilt}°, azimuth={azimuth}°)")
    return losses_capped

def determine_installation_geometry(
    lat: float,
    roof_tilt: Optional[float], 
    roof_azimuth: Optional[float], 
    installation_type: str,
    optimal_tilt: float
) -> tuple:
    """
    Decide la inclinación y azimut final de la instalación, considerando el tipo de montaje y si será necesario soporte.
    """
    with_support = False  # Por defecto: sin estructura extra

    if installation_type == "coplanar" and roof_tilt is not None:
        if roof_tilt < 10:
            final_tilt = optimal_tilt
            with_support = True
            logger.warning(f"⚠️  Tejado plano detectado ({roof_tilt:.1f}°): se usará inclinación óptima con estructura ({final_tilt:.1f}°).")
        else:
            final_tilt = roof_tilt
            logger.info(f"⚡️ Coplanar: inclinación tejado = {final_tilt:.1f}° (sin soporte extra).")
        final_azimuth = roof_azimuth if roof_azimuth is not None else 180
        logger.info(f"⚡️ Azimut usado: {final_azimuth:.1f}°")

    elif installation_type == "fixed" and roof_tilt is not None:
        final_tilt = roof_tilt
        final_azimuth = roof_azimuth if roof_azimuth is not None else 180
        logger.info(f"⚡️ Estructura fija: tilt={final_tilt:.1f}°, azimuth={final_azimuth:.1f}° (sin soporte).")
    else:
        final_tilt = optimal_tilt
        final_azimuth = 180
        with_support = True
        logger.info(f"ℹ⚡️ Sin datos geométricos: se usará configuración óptima (tilt={final_tilt:.1f}°, azimuth={final_azimuth}°).")

    return final_tilt, final_azimuth, with_support

def select_pv_module() -> dict:
    """
    Selecciona el módulo fotovoltaico por defecto del catálogo.
    """
    module = PV_MODULES["ja_solar_545"]
    logger.info(f"⚡️ Módulo seleccionado: {module['model']} ({module['power_stc']} Wp, {module['efficiency']}%, {module['area_m2']} m²)")
    return module

def calculate_system_size(annual_consumption: float, coverage_percent: float,
                         specific_yield_estimate: float, available_area: Optional[float],
                         module: dict) -> tuple:
    """
    Calcula el número óptimo de módulos, potencia total instalada (kWp) y área total ocupada, en base al consumo y rendimiento esperado.
    Limita el sistema si el área disponible es insuficiente.
    """
    target_energy = annual_consumption * (coverage_percent / 100)
    required_kwp = target_energy / specific_yield_estimate
    module_power_kw = module["power_stc"] / 1000
    n_modules = max(1, round(required_kwp / module_power_kw))

    # Comprobar si hay limitación por área
    if available_area:
        max_modules_by_area = int(available_area / module["area_m2"] * 0.75)  # 75% factor de aprovechamiento realista
        if n_modules > max_modules_by_area:
            logger.warning(f"⚠️  Área limitada: solo caben {max_modules_by_area} módulos (solicitados {n_modules})")
            n_modules = max_modules_by_area

    actual_kwp = n_modules * module_power_kw
    total_area = n_modules * module["area_m2"]

    logger.info(f"⚡️ Dimensionado: {n_modules} módulos seleccionados, {actual_kwp:.2f} kWp instalados, área total {total_area:.1f} m²")
    return n_modules, actual_kwp, total_area

def select_inverter(kwp_dc: float) -> dict:
    """
    Selecciona el inversor que mejor se adapta al campo fotovoltaico para un ratio DC/AC óptimo (1.2).
    """
    target_ratio = 1.2
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
        ac_power = min(INVERTERS.keys())
        best_inverter = {"ac_power": ac_power, **INVERTERS[ac_power]}
    logger.info(f"⚡ Inversor seleccionado: {best_inverter['model']} ({best_inverter['ac_power']} kW, eficiencia {best_inverter['efficiency']}%)")
    return best_inverter

def calculate_string_configuration(n_modules: int, module: dict, inverter: dict, 
                                weather_data: pd.DataFrame) -> tuple:
    """
    Calcula la configuración óptima de strings: módulos en serie y paralelo, considerando límites de tensión y potencia del inversor.
    Alerta si hay que usar una configuración de emergencia (no ideal).
    """
    t_air_min = weather_data['temp_air'].min() - 10  # Margen de seguridad 10°C
    t_air_max = weather_data['temp_air'].quantile(0.99)
    t_cell_max = t_air_max + (module['noct'] - 20) * 0.8

    v_mp_hot = module['v_mp'] * (1 + module['temp_coef_vmp']/100 * (t_cell_max - 25))
    v_oc_cold = module['v_oc'] * (1 + module['temp_coef_voc']/100 * (t_air_min - 25))

    min_modules_string = max(2, int(inverter["mppt_min"] / v_mp_hot * 1.1))
    max_modules_string = int(inverter["mppt_max"] / v_oc_cold * 0.9)

    logger.info(f"⚡️ [Strings] Temperaturas extremas: T_air_min={t_air_min:.1f}°C, T_cell_max={t_cell_max:.1f}°C")
    logger.info(f"⚡️ [Strings] Rangos recomendados: {min_modules_string}-{max_modules_string} módulos/serie, V_mp_hot={v_mp_hot:.1f}V, V_oc_cold={v_oc_cold:.1f}V")

    # Buscar la mejor combinación posible
    best_config = None
    min_waste = float('inf')
    possible_configs = []
    for modules_per_string in range(min_modules_string, min(max_modules_string + 1, n_modules + 1)):
        for strings_parallel in range(1, min(6, n_modules // modules_per_string + 2)):
            total_modules = modules_per_string * strings_parallel
            total_dc_power = total_modules * module["power_stc"] / 1000
            if total_dc_power > inverter["max_dc"]:
                continue
            string_vmp = modules_per_string * v_mp_hot
            string_voc = modules_per_string * v_oc_cold
            if not (inverter["mppt_min"] <= string_vmp <= inverter["mppt_max"]):
                continue
            waste = abs(total_modules - n_modules)
            possible_configs.append((modules_per_string, strings_parallel, total_modules, waste))
    if possible_configs:
        possible_configs.sort(key=lambda x: x[3])
        best = possible_configs[0]
        best_config = (best[0], best[1], best[2])
    else:
        modules_per_string = max(min_modules_string, 2)
        strings_parallel = 1
        total_modules = modules_per_string * strings_parallel
        best_config = (modules_per_string, strings_parallel, total_modules)
        logger.warning("⚠️  Configuración de emergencia aplicada: no se encontró combinación óptima.")

    logger.info(f"⚡️ [Strings] Configuración elegida: {best_config[1]} strings × {best_config[0]} módulos/serie = {best_config[2]} módulos totales")
    return best_config

def calculate_pv_production(weather_data: pd.DataFrame, lat: float, lon: float,
                           tilt: float, azimuth: float, modules_per_string: int,
                           strings_parallel: int, module: dict, inverter: dict) -> dict:
    """
    Simula la producción anual, mensual y horaria del sistema FV usando pvlib y aplica pérdidas realistas.
    Calcula además el rendimiento del sistema (PR y Capacity Factor).
    """
    try:
        site = location.Location(lat, lon, tz='Europe/Madrid')
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
        inverter_params = {
            'pdc0': strings_parallel * modules_per_string * module['power_stc'],
            'eta_inv_nom': inverter['efficiency'] / 100,
            'eta_inv_ref': inverter['efficiency'] / 100
        }
        temperature_params = {'u0': 25.0, 'u1': 6.84}
        array = pvsystem.Array(
            mount=pvsystem.FixedMount(surface_tilt=tilt, surface_azimuth=azimuth),
            module_parameters=module_params,
            temperature_model_parameters=temperature_params,
            modules_per_string=modules_per_string,
            strings=strings_parallel
        )
        system = pvsystem.PVSystem(arrays=[array], inverter_parameters=inverter_params)
        mc = modelchain.ModelChain(
            system, site,
            aoi_model='physical',
            spectral_model='no_loss',
            temperature_model='faiman',
            losses_model='no_loss'
        )
        mc.run_model(weather_data)

        # Aplicar pérdidas del sistema (sólo las NO simuladas ya por pvlib)
        total_loss_factor = 1.0
        for loss_name, loss_value in SYSTEM_LOSSES.items():
            if loss_name not in ['temperature', 'irradiance', 'inverter']:
                total_loss_factor *= (1 - loss_value)
        ac_power_with_losses = mc.results.ac * total_loss_factor
        production_hourly = ac_power_with_losses / 1000
        production_hourly = production_hourly[~production_hourly.index.duplicated(keep="first")]
        max_power_kw = production_hourly.max() 

        # Resumen mensual
        production_monthly = []
        for month in range(1, 13):
            month_mask = production_hourly.index.month == month
            month_production = production_hourly[month_mask].sum()
            production_monthly.append(round(month_production, 1))

        annual_production = production_hourly.sum()
        total_kwp = strings_parallel * modules_per_string * module['power_stc'] / 1000
        specific_yield = annual_production / total_kwp if total_kwp > 0 else 0

        # Rendimiento del sistema
        poa_global = mc.results.total_irrad['poa_global']
        poa_sum = poa_global.sum() / 1000
        theoretical_yield_poa = total_kwp * poa_sum
        performance_ratio = annual_production / theoretical_yield_poa if theoretical_yield_poa > 0 else 0
        capacity_factor = annual_production / (total_kwp * 8760) * 100 if total_kwp > 0 else 0

        logger.info(
            f"⚡️ Producción simulada: {annual_production:.0f} kWh/año, "
            f"específica={specific_yield:.0f} kWh/kWp, PR={performance_ratio:.2f}, CF={capacity_factor:.1f}%"
        )

        return {
            'annual_production': annual_production,
            'monthly_production': production_monthly,
            'specific_yield': specific_yield,
            'performance_ratio': performance_ratio,
            'capacity_factor': capacity_factor,
            'hourly_production': production_hourly,
            'max_power_kw': round(max_power_kw, 2)
        }

    except Exception as e:
        logger.error(f"❌ [Producción PV] Error en simulación: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error simulación: {str(e)}")

def generate_consumption_profile(annual_consumption: float, year: int = 2024, tz: str = "Europe/Madrid") -> pd.Series:
    """
    Genera un perfil de consumo horario sintético y realista para una vivienda media.
    El perfil se ajusta al consumo anual especificado y tiene estacionalidad y fines de semana.
    """
    start_date = f"{year}-01-01"
    end_date = f"{year}-12-31 23:00:00"
    dates = pd.date_range(start_date, end_date, freq="h", tz=tz, ambiguous="NaT", nonexistent="shift_forward")

    # Patrón diario tipo vivienda
    daily_pattern = np.array([
        0.4, 0.3, 0.3, 0.3, 0.3, 0.4,  # 0-5h: noche
        0.6, 0.8, 1.0, 0.9, 0.8, 0.7,  # 6-11h: mañana
        0.8, 0.9, 1.0, 1.1, 1.2, 1.3,  # 12-17h: tarde
        1.5, 1.7, 1.4, 1.1, 0.8, 0.6   # 18-23h: noche
    ])
    day_of_year = dates.dayofyear.values
    seasonal_factor = 1 + 0.3 * np.cos(2 * np.pi * (day_of_year - 21) / 365)
    is_weekend = dates.weekday.values >= 5
    weekend_factor = np.where(is_weekend, 1.1, 1.0)
    hourly_pattern = np.tile(daily_pattern, len(dates) // 24 + 1)[:len(dates)]
    consumption = hourly_pattern * seasonal_factor * weekend_factor
    consumption = consumption * annual_consumption / consumption.sum()
    logger.info(f"🟢 Perfil de consumo generado: {len(dates)} registros horarios, consumo anual={annual_consumption:.0f} kWh")
    return pd.Series(consumption, index=dates)

def analyze_autoconsumption(production_hourly: pd.Series, consumption_hourly: pd.Series) -> dict:
    """
    Calcula el balance horario y mensual entre producción y consumo.
    Proporciona métricas clave de autoconsumo y autosuficiencia.
    """
    common_index = production_hourly.index.intersection(consumption_hourly.index).unique()
    prod_aligned = production_hourly.reindex(common_index, fill_value=0)
    cons_aligned = consumption_hourly.reindex(common_index, fill_value=consumption_hourly.mean())

    self_consumption = np.minimum(prod_aligned, cons_aligned)
    grid_injection = np.maximum(0, prod_aligned - cons_aligned)
    grid_purchase = np.maximum(0, cons_aligned - prod_aligned)

    annual_production = prod_aligned.sum()
    annual_consumption = cons_aligned.sum()
    annual_self_consumption = self_consumption.sum()
    annual_grid_injection = grid_injection.sum()
    annual_grid_purchase = grid_purchase.sum()

    self_consumption_rate = (annual_self_consumption / annual_production * 100) if annual_production > 0 else 0
    self_sufficiency_rate = (annual_self_consumption / annual_consumption * 100) if annual_consumption > 0 else 0

    # Resumen visual del análisis de autoconsumo
    logger.info(
        f"⚡️ Autoconsumo anual: {annual_self_consumption:.0f} kWh "
        f"({self_consumption_rate:.1f}% producción cubierta), "
        f"autosuficiencia {self_sufficiency_rate:.1f}%"
    )
    if self_consumption_rate < 40:
        logger.warning(f"⚠️  Autoconsumo bajo: revisa si el campo FV está sobredimensionado o mal ajustado.")
    if self_sufficiency_rate < 30:
        logger.warning(f"⚠️  Autosuficiencia baja: posible campo FV insuficiente para el consumo anual objetivo.")

    # Detalle mensual
    month_names = [
        "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
        "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"
    ]
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
            'grid_consumption_kwh': round(month_purchase, 1),
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
    """
    Añade cálculo económico mensual: ahorro total mes a mes por autoconsumo y vertido a red.
    """
    enhanced_monthly = []
    for month_data in monthly_analysis:
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
    logger.info("🟢 Análisis económico mensual añadido a cada mes del año.")
    return enhanced_monthly

def calculate_economics(system_kwp: float, annual_self_consumption: float,
                       annual_grid_injection: float, annual_grid_purchase: float,
                       electricity_price: float, surplus_price: float) -> dict:
    """
    Calcula el análisis económico completo a 25 años: inversión inicial, OPEX, payback, NPV, TIR y LCOE.
    Lanza avisos si el payback es largo o el proyecto no es viable.
    """
    system_cost = system_kwp * 1000 * SYSTEM_COSTS["cost_per_wp"]
    annual_maintenance = system_kwp * SYSTEM_COSTS["maintenance_annual"]
    annual_insurance = system_kwp * SYSTEM_COSTS["insurance_annual"]
    annual_opex = annual_maintenance + annual_insurance

    savings_self_consumption = annual_self_consumption * electricity_price
    income_surplus = annual_grid_injection * surplus_price
    annual_savings_gross = savings_self_consumption + income_surplus
    annual_savings_net = annual_savings_gross - annual_opex

    payback_years = system_cost / annual_savings_net if annual_savings_net > 0 else 999
    logger.info(f"💶 [Eco] Coste total sistema: {system_cost:,.0f} € | Ahorro neto año 1: {annual_savings_net:,.0f} € | Payback: {payback_years:.1f} años")

    # Detalle financiero extendido
    discount_rate = 0.04
    inflation_electricity = 0.03
    inflation_surplus = 0.02
    inflation_opex = 0.025
    degradation_rate = SYSTEM_COSTS["degradation_annual"]

    # Flujo de caja a 25 años
    cash_flows = [-system_cost]
    for year in range(1, 26):
        degradation_factor = (1 - degradation_rate) ** (year - 1)
        year_self_consumption = annual_self_consumption * degradation_factor
        year_grid_injection = annual_grid_injection * degradation_factor
        year_electricity_price = electricity_price * (1 + inflation_electricity) ** (year - 1)
        year_surplus_price = surplus_price * (1 + inflation_surplus) ** (year - 1)
        year_opex = annual_opex * (1 + inflation_opex) ** (year - 1)
        year_savings_gross = (year_self_consumption * year_electricity_price +
                              year_grid_injection * year_surplus_price)
        year_cash_flow = year_savings_gross - year_opex
        cash_flows.append(year_cash_flow)
    npv = sum(cf / (1 + discount_rate) ** i for i, cf in enumerate(cash_flows))

    # TIR (IRR)
    def npv_at_rate(rate):
        if rate <= -1:
            return float('inf')
        try:
            return sum(cf / (1 + rate) ** i for i, cf in enumerate(cash_flows))
        except:
            return float('inf')
    if annual_savings_net <= 0:
        irr_percent = -100.0
    else:
        irr_low, irr_high = -0.99, 2.0
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
            irr_percent = (annual_savings_net / system_cost * 100) if system_cost > 0 else -100

    # LCOE
    total_energy_discounted = 0
    total_cost_discounted = system_cost
    for year in range(1, 26):
        degradation_factor = (1 - degradation_rate) ** (year - 1)
        year_energy = (annual_self_consumption + annual_grid_injection) * degradation_factor
        year_opex_inflated = annual_opex * (1 + inflation_opex) ** (year - 1)
        total_energy_discounted += year_energy / (1 + discount_rate) ** year
        total_cost_discounted += year_opex_inflated / (1 + discount_rate) ** year
    lcoe = total_cost_discounted / total_energy_discounted if total_energy_discounted > 0 else 999

    # Avisos de rentabilidad/riesgo
    if payback_years > 10:
        logger.warning(f"⚠️  Payback elevado (>10 años): revisa precio, dimensionado o costes.")
    if irr_percent < 4:
        logger.warning(f"⚠️  Rentabilidad baja: TIR estimada {irr_percent:.2f}%.")
    if lcoe > 0.20:
        logger.warning(f"⚠️  LCOE elevado: {lcoe:.3f} €/kWh.")

    logger.info(f"💶 KPIs finales: VAN={npv:,.0f} €, TIR={irr_percent:.2f}%, LCOE={lcoe:.3f} €/kWh (25 años)")
    return {
        'system_cost': system_cost,
        'annual_savings': annual_savings_net,
        'payback_years': payback_years,
        'npv_25_years': npv,
        'irr_percent': irr_percent,
        'lcoe_eur_kwh': lcoe
    }

def calculate_cable_losses(
    modules_per_string: int,
    strings_parallel: int,
    module_v_mp: float,
    module_i_mp: float,
    inverter_ac_power_kw: float,
    cable_length_dc_m: float = 15.0,           # ← Por defecto: 15 m DC (ajustable según instalación)
    cable_section_dc_mm2: int = None,             # ← Por defecto: 6 mm² (muy habitual FV string)
    cable_length_ac_m: float = 10.0,           # ← Por defecto: 10 m AC hasta cuadro/distribución
    cable_section_ac_mm2: int = None             # ← Por defecto: 10 mm² (instalación monofásica típica)
) -> dict:
    """
    Calcula caída de tensión y pérdidas por calentamiento en cableado DC (campo FV) y AC (salida inversor).
    Todos los parámetros pueden ajustarse según el proyecto real.

    - Si no se pasan longitudes ni secciones, se usan valores típicos domésticos recomendados.
    """

    # --- Resistividad estándar para cobre (Ω/km) según sección ---
    # Tablas IEC 60228 (puedes ampliar según tus necesidades)
    resistivity_table = {
        4:   4.61,    # Ω/km para 4 mm²
        6:   3.08,    # Ω/km para 6 mm²
        10:  1.83,    # Ω/km para 10 mm²
        16:  1.15,    # Ω/km para 16 mm²
        25:  0.727,   # Ω/km para 25 mm²
        35:  0.524,   # Ω/km para 35 mm²
    }
    cable_resistance_dc_ohm_per_km = resistivity_table.get(cable_section_dc_mm2, 3.08)   # default: 6 mm²
    cable_resistance_ac_ohm_per_km = resistivity_table.get(cable_section_ac_mm2, 1.83)   # default: 10 mm²

    # --- Cálculo de corrientes ---
    I_dc_string = module_i_mp    # Corriente máxima en cada string (A)
    I_dc_total = strings_parallel * I_dc_string
    I_ac = inverter_ac_power_kw * 1000 / 230.0   # Corriente en AC (monofásica 230 V)

    # --- Caída de tensión DC ---
    vd_dc = 2 * cable_length_dc_m * I_dc_string * cable_resistance_dc_ohm_per_km / 1000.0  # ida y vuelta (A)
    vd_percent_dc = vd_dc / (modules_per_string * module_v_mp) * 100.0    # % respecto tensión string

    # --- Caída de tensión AC ---
    vd_ac = 2 * cable_length_ac_m * I_ac * cable_resistance_ac_ohm_per_km / 1000.0
    vd_percent_ac = vd_ac / 230.0 * 100.0

    # --- Pérdidas por calentamiento ---
    # P_loss = I²·R  (para todo el tramo)
    P_loss_dc = I_dc_total**2 * (cable_resistance_dc_ohm_per_km * cable_length_dc_m / 1000.0)
    # Energía transportada: P_dc = módulos * Vmp * Imp (W)
    P_nominal_dc = modules_per_string * strings_parallel * module_v_mp * module_i_mp
    P_loss_dc_percent = (P_loss_dc / P_nominal_dc * 100.0) if P_nominal_dc > 0 else 0

    P_loss_ac = I_ac**2 * (cable_resistance_ac_ohm_per_km * cable_length_ac_m / 1000.0)
    P_nominal_ac = inverter_ac_power_kw * 1000
    P_loss_ac_percent = (P_loss_ac / P_nominal_ac * 100.0) if P_nominal_ac > 0 else 0
        
    logger.info(
        f"⚡️ [Cable] DC {cable_length_dc_m} m × {cable_section_dc_mm2} mm² "
        f"→ ΔV={vd_percent_dc:.2f}%  P_loss={P_loss_dc_percent:.2f}%.  "
        f"AC {cable_length_ac_m} m x {cable_section_ac_mm2} mm² "
        f"→ ΔV={vd_percent_ac:.2f}%  P_loss={P_loss_ac_percent:.2f}%."
    )
    return {
        "cable_length_dc_m": cable_length_dc_m,
        "cable_section_dc_mm2": cable_section_dc_mm2,
        "voltage_drop_dc_percent": round(vd_percent_dc, 2),
        "power_loss_dc_percent": round(P_loss_dc_percent, 2),
        "current_dc_a": round(I_dc_total, 2),
        "cable_length_ac_m": cable_length_ac_m,
        "cable_section_ac_mm2": cable_section_ac_mm2,
        "voltage_drop_ac_percent": round(vd_percent_ac, 2),
        "power_loss_ac_percent": round(P_loss_ac_percent, 2),
        "current_ac_a": round(I_ac, 2),
        "note": (
            "Default values: DC 6 mm²/15 m, AC 10 mm²/10 m. "
            "Adjust according to your case and local regulations "
            "(recommended voltage drop ≤ 1.5 % per segment)."
        ),    
    }

def calculate_professional_cable_section(
    current_a: float,
    allowed_voltage_drop_pct: float,
    length_m: float,
    nominal_voltage_v: float,
    method: str,
    insulation: str,
    n_conductors: int,
    material: str = 'Cu'
) -> dict:
    """
    Calcula la sección mínima de cable cumpliendo la UNE tanto por caída de tensión como por intensidad máxima.
    Devuelve sección recomendada, caída de tensión, intensidad admisible y motivo.
    """
    reasons = []
    resistivity = RESISTIVITY[material]
    for section in STANDARD_SECTIONS_MM2:
        # Buscar intensidad máxima según UNE
        ampacity = CABLE_AMPACITY_TABLE.get((method, insulation, n_conductors), {}).get(section)
        if not ampacity:
            continue
        # Cálculo caída de tensión (ida y vuelta)
        voltage_drop_v = 2 * length_m * current_a * resistivity / section
        voltage_drop_pct = voltage_drop_v / nominal_voltage_v * 100
        meets_voltage_drop = voltage_drop_pct <= allowed_voltage_drop_pct
        meets_ampacity = current_a <= ampacity
        logger.info(
            f"[Cable] {section}mm²: caída de tensión={voltage_drop_pct:.2f}% ({meets_voltage_drop}), "
            f"intensidad admisible={ampacity}A ({meets_ampacity})"
        )
        if meets_voltage_drop and meets_ampacity:
            return {
                "recommended_section_mm2": section,
                "voltage_drop_pct": round(voltage_drop_pct, 2),
                "ampacity_A": ampacity,
                "meets_voltage_drop": meets_voltage_drop,
                "meets_ampacity": meets_ampacity,
                "reason": "Cumple caída de tensión y UNE ampacidad",
            }
        if not meets_voltage_drop:
            reasons.append(f"{section}mm² no cumple caída de tensión ({voltage_drop_pct:.2f}% > {allowed_voltage_drop_pct}%)")
        if not meets_ampacity:
            reasons.append(f"{section}mm² no cumple intensidad ({current_a:.2f}A > {ampacity}A)")
    return {
        "recommended_section_mm2": None,
        "voltage_drop_pct": None,
        "ampacity_A": None,
        "meets_voltage_drop": False,
        "meets_ampacity": False,
        "reason": "; ".join(reasons)
    }


def analyze_cable_sections(
    current_dc_a, length_dc_m, v_dc,
    current_ac_a, length_ac_m, v_ac,
    method_dc, insulation_dc, n_cond_dc,
    method_ac, insulation_ac, n_cond_ac,
    allowed_vdrop_pct=1.5, material='Cu'
):
    """
    Analiza y selecciona la mejor sección de cable tanto en DC como en AC cumpliendo UNE y caída de tensión.
    """
    dc_result = calculate_professional_cable_section(
        current_a=current_dc_a,
        allowed_voltage_drop_pct=allowed_vdrop_pct,
        length_m=length_dc_m,
        nominal_voltage_v=v_dc,
        method=method_dc,
        insulation=insulation_dc,
        n_conductors=n_cond_dc,
        material=material
    )
    ac_result = calculate_professional_cable_section(
        current_a=current_ac_a,
        allowed_voltage_drop_pct=allowed_vdrop_pct,
        length_m=length_ac_m,
        nominal_voltage_v=v_ac,
        method=method_ac,
        insulation=insulation_ac,
        n_conductors=n_cond_ac,
        material=material
    )
    logger.info(
        f"⚡ Resultado DC: sección recomendada={dc_result['recommended_section_mm2']}mm² | "
        f"⚡ Resultado AC: sección recomendada={ac_result['recommended_section_mm2']}mm²"
    )
    return {
        "cable_dc": dc_result,
        "cable_ac": ac_result
    }
