from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
import pandas as pd
import numpy as np
import requests
import pvlib
from pvlib import location, modelchain, pvsystem, temperature
import json
from datetime import datetime
import logging
import traceback
import math

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

app = FastAPI(title="Fotovoltaico Pre-dimensionado API Profesional", version="2.0.0")

# Modelos Pydantic
class SimulateRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Latitud en grados decimales")
    lon: float = Field(..., ge=-180, le=180, description="Longitud en grados decimales")
    roof_area_m2: Optional[float] = Field(None, gt=0, description="Área disponible del tejado en m²")
    kwp_target: Optional[float] = Field(None, gt=0, description="Potencia objetivo en kWp")
    annual_consumption_kwh: Optional[float] = Field(4200, gt=0, description="Consumo anual de la vivienda en kWh")
    coverage_percentage: Optional[float] = Field(80, ge=30, le=120, description="Porcentaje de consumo a cubrir")
    consumption_profile: Optional[str] = Field("residential", description="Perfil de consumo: residential, commercial")

class MonthlyData(BaseModel):
    month: int
    month_name: str
    production_kwh: float
    consumption_kwh: float
    self_consumption_kwh: float
    grid_injection_kwh: float
    grid_consumption_kwh: float
    economic_savings_eur: float

class SystemConfiguration(BaseModel):
    modules_per_string: int
    strings_in_parallel: int
    total_modules: int
    array_configuration: str
    total_area_m2: float
    
class ModuleSpecs(BaseModel):
    model: str
    power_wp: int
    voltage_vmp: float
    current_imp: float
    voltage_voc: float
    current_isc: float
    area_m2: float
    efficiency: float
    temp_coef_power: float

class InverterSpecs(BaseModel):
    model: str
    power_ac_kw: float
    power_dc_max_kw: float
    efficiency: float
    mppt_trackers: int
    input_voltage_range: str

class ElectricalCalculations(BaseModel):
    dc_ac_ratio: float
    string_voltage_vmp: float
    string_voltage_voc: float
    total_current_imp: float
    max_system_voltage: float
    mppt_voltage_range_ok: bool
    
class EnergyAnalysis(BaseModel):
    annual_production_kwh: float
    annual_consumption_kwh: float
    annual_self_consumption_kwh: float
    annual_grid_injection_kwh: float
    self_consumption_rate_percent: float
    self_sufficiency_rate_percent: float
    monthly_analysis: List[MonthlyData]
    specific_yield_kwh_kwp: float
    performance_ratio: float
    capacity_factor: float

class EconomicAnalysis(BaseModel):
    system_cost_eur: float
    annual_savings_eur: float
    electricity_bill_reduction_eur: float
    surplus_compensation_eur: float
    payback_years: float
    roi_25_years_eur: float
    electricity_price_eur_kwh: float
    surplus_price_eur_kwh: float

class TechnicalLosses(BaseModel):
    soiling_percent: float
    cables_percent: float
    mismatch_percent: float
    connections_percent: float
    lid_percent: float
    nameplate_percent: float
    availability_percent: float
    inverter_percent: float
    total_losses_percent: float

class SolarGeometry(BaseModel):
    optimal_tilt_deg: float
    azimuth_deg: float
    annual_irradiation_kwh_m2: float
    peak_sun_hours: float

class SimulateResponse(BaseModel):
    project_info: Dict
    module_specs: ModuleSpecs
    inverter_specs: InverterSpecs
    system_config: SystemConfiguration
    electrical_calcs: ElectricalCalculations
    solar_geometry: SolarGeometry
    energy_analysis: EnergyAnalysis
    economic_analysis: EconomicAnalysis
    system_losses: TechnicalLosses

# Constantes realistas del sistema
MODULE_SPECS = {
    "model": "JA Solar JAM72S30-545/MR",
    "power_stc": 545,  # W - Módulo más realista actual
    "area_m2": 2.172,  # m² - Área real de módulo 545W
    "v_mp": 41.85,     # V
    "i_mp": 13.02,     # A
    "v_oc": 50.15,     # V
    "i_sc": 13.85,     # A
    "alpha_sc": 0.0006,  # A/°C
    "beta_voc": -0.135,  # V/°C
    "gamma_pmp": -0.34,  # %/°C
    "cells_in_series": 144
}

# Pérdidas técnicas reales
SYSTEM_LOSSES = {
    "soiling": 0.02,       # 2% - Suciedad
    "cables": 0.015,       # 1.5% - Cables DC
    "mismatch": 0.02,      # 2% - Desajuste módulos
    "connections": 0.005,  # 0.5% - Conexiones
    "lid": 0.01,           # 1% - Light Induced Degradation
    "nameplate": 0.005,    # 0.5% - Tolerancia fabricación
    "availability": 0.01,  # 1% - Disponibilidad sistema
    "inverter": 0.03       # 3% - Pérdidas inversor
}

# Precios realistas España 2025
ELECTRICITY_PRICES = {
    "consumption_eur_kwh": 0.28,  # Precio compra electricidad
    "surplus_eur_kwh": 0.055,     # Compensación excedentes
    "system_cost_eur_wp": 1.1     # Coste instalación €/Wp
}

# Catálogo inversores realista
INVERTER_CATALOG = {
    3: dict(model="Huawei SUN2000-3KTL-L1",  rend=0.983, ac_kw=3.0,  dc_max_kw=4.5,
            mppt=2, v_mppt_min=90,  v_mppt_max=560, i_mppt_max=12.5, v_dc_max=600),
    5: dict(model="Huawei SUN2000-5KTL-L1",  rend=0.984, ac_kw=5.0,  dc_max_kw=7.5,
            mppt=2, v_mppt_min=90,  v_mppt_max=560, i_mppt_max=12.5, v_dc_max=600),
    6: dict(model="Huawei SUN2000-6KTL-L1",  rend=0.984, ac_kw=6.0,  dc_max_kw=9.0,
            mppt=2, v_mppt_min=90,  v_mppt_max=560, i_mppt_max=12.5, v_dc_max=600),
    8: dict(model="Huawei SUN2000-8KTL",     rend=0.985, ac_kw=8.0,  dc_max_kw=12.0,
            mppt=2, v_mppt_min=200, v_mppt_max=950, i_mppt_max=18,   v_dc_max=1000),
   10: dict(model="Huawei SUN2000-10KTL-L1", rend=0.983, ac_kw=10.0, dc_max_kw=15.0,
            mppt=2, v_mppt_min=90,  v_mppt_max=560, i_mppt_max=11,   v_dc_max=600),
   12: dict(model="Huawei SUN2000-12KTL",    rend=0.985, ac_kw=12.0, dc_max_kw=13.5,
            mppt=2, v_mppt_min=200, v_mppt_max=950, i_mppt_max=18,   v_dc_max=1000)
}

def get_inverter_by_ac(ac_kw: float) -> dict:
    """
    Devuelve un dict con los campos que el resto del programa espera.
    """
    raw = INVERTER_CATALOG[ac_kw].copy()

    # Alias internos
    raw['efficiency']    = raw['rend']          # 0-1
    raw['max_dc_kw']     = raw['dc_max_kw']
    raw['power_ac_kw']   = raw['ac_kw']
    raw['voltage_range'] = f"{raw['v_mppt_min']}-{raw['v_mppt_max']}V"
    return raw

def get_pvgis_tmy_data(lat: float, lon: float) -> pd.DataFrame:
    """Descarga datos TMY desde PVGIS con manejo de errores mejorado"""
    try:
        url = "https://re.jrc.ec.europa.eu/api/v5_2/tmy"
        params = {
            'lat': lat, 'lon': lon, 'outputformat': 'json',
            'usehorizon': 1, 'startyear': 2005, 'endyear': 2020
        }
        
        logger.info(f"Descargando datos TMY para lat={lat}, lon={lon}")
        response = requests.get(url, params=params, timeout=45)
        response.raise_for_status()
        
        data = response.json()
        
        if 'outputs' not in data or 'tmy_hourly' not in data['outputs']:
            raise ValueError("Respuesta PVGIS inválida")
            
        hourly_data = data['outputs']['tmy_hourly']
        df = pd.DataFrame(hourly_data)
        
        # Procesar timestamps correctamente
        idx = pd.to_datetime(df['time(UTC)'], format='%Y%m%d:%H%M', utc=True)
        idx = (idx - pd.Timedelta(minutes=30)).dt.floor('h')

        idx = idx.dt.tz_convert('Europe/Madrid')
        idx = idx.map(lambda ts: ts.replace(year=2020))

        mask = ~idx.duplicated(keep='first')
        df   = df.loc[mask].reset_index(drop=True)
        idx  = pd.DatetimeIndex(idx[mask])

        df.index = idx


        # Renombrar columnas
        df = df.rename(columns={
            'G(h)': 'ghi', 'Gb(n)': 'dni', 'Gd(h)': 'dhi',
            'T2m': 'temp_air', 'WS10m': 'wind_speed', 'RH': 'relative_humidity'
        })
        
        # Convertir a numérico
        numeric_cols = ['ghi', 'dni', 'dhi', 'temp_air', 'wind_speed', 'relative_humidity']
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        
        # Validar datos
        df = df.dropna(subset=['ghi', 'dni', 'dhi', 'temp_air'])
        
        if len(df) < 8000:  # Menos de 11 meses de datos
            raise ValueError("Datos climáticos insuficientes")
            
        logger.info(f"Datos TMY procesados: {len(df)} registros válidos")
        return df
        
    except Exception as e:
        logger.error(f"Error descargando datos PVGIS: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error obteniendo datos climáticos: {str(e)}")

def calculate_optimal_system_size(annual_consumption_kwh: float, coverage_percentage: float, 
                                 specific_yield_estimate: float, roof_area_m2: Optional[float] = None) -> tuple:
    """Dimensiona sistema correctamente basado en consumo objetivo"""
    
    # Energía objetivo a generar
    target_energy_kwh = annual_consumption_kwh * (coverage_percentage / 100)
    
    # Potencia necesaria
    required_kwp = target_energy_kwh / specific_yield_estimate
    
    # Número de módulos necesarios
    module_power_kw = MODULE_SPECS["power_stc"] / 1000
    n_modules_needed = max(1, round(required_kwp / module_power_kw))
    
    # Verificar limitación por área de tejado
    if roof_area_m2:
        max_modules_by_area = int(roof_area_m2 / MODULE_SPECS["area_m2"] * 0.8)  # 80% aprovechamiento
        if n_modules_needed > max_modules_by_area:
            n_modules_needed = max_modules_by_area
            logger.warning(f"Sistema limitado por área: {n_modules_needed} módulos máximo")
    
    # Cálculos finales
    actual_kwp = n_modules_needed * module_power_kw
    total_area = n_modules_needed * MODULE_SPECS["area_m2"]
    expected_production = actual_kwp * specific_yield_estimate
    actual_coverage = (expected_production / annual_consumption_kwh) * 100
    
    logger.info(f"Dimensionado objetivo: {n_modules_needed} módulos, {actual_kwp:.2f} kWp, cobertura {actual_coverage:.1f}%")
    return n_modules_needed, actual_kwp, total_area, actual_coverage

def calculate_string_configuration_professional(
        n_modules_init: int,
        inverter_specs: dict,
        target_dcac_min: float = 1.1,
        target_dcac_max: float = 1.3) -> tuple:
    """
    Calcula la disposición de strings:
      • módulos_por_string
      • strings_en_parallel
      • módulos_totales
      • dc_ac_ratio real
    Cumple límites de tensión fría/caliente y potencia FV máxima.
    """
    mod_kw = MODULE_SPECS['power_stc'] / 1000

    # 1. Ajusta nº de módulos para alcanzar el ratio DC/AC mínimo
    n_modules = n_modules_init
    while (n_modules * mod_kw) / inverter_specs['power_ac_kw'] < target_dcac_min:
        n_modules += 1

    # 2. Límites de tensión del MPPT
    v_min = inverter_specs['v_mppt_min']
    v_max = inverter_specs['v_mppt_max']

    v_mp_hot  = MODULE_SPECS['v_mp'] + MODULE_SPECS['beta_voc'] * (70 - 25)   # 70 °C célula
    v_oc_cold = MODULE_SPECS['v_oc'] + MODULE_SPECS['beta_voc'] * (-10 - 25)  # –10 °C ambiente

    min_mps = max(1, int(v_min / v_mp_hot * 1.05))   # margen 5 %
    max_mps = int(v_max / v_oc_cold * 0.95)

    best_cfg, waste_best = None, float('inf')
    for mps in range(min_mps, max_mps + 1):
        strings  = math.ceil(n_modules / mps)
        tot_mod  = mps * strings
        waste    = abs(tot_mod - n_modules)
        dc_kw    = tot_mod * mod_kw

        if dc_kw > inverter_specs['max_dc_kw'] * 1.1:   # margen 10 %
            continue
        if waste < waste_best:
            best_cfg, waste_best = (mps, strings, tot_mod), waste

    mps, strings, tot_mod = best_cfg
    dcac_ratio = (tot_mod * mod_kw) / inverter_specs['power_ac_kw']

    logger.info("Strings seleccionados: %d×%d | módulos: %d | DC/AC = %.2f",
                strings, mps, tot_mod, dcac_ratio)

    return mps, strings, tot_mod, dcac_ratio


def select_inverter_professional(kwp_dc: float) -> tuple:
    """
    Devuelve:
      • potencia AC nominal elegida (float)
      • dict normalizado del inversor (get_inverter_by_ac)
    Criterio: ratio DC/AC óptimo 1.1-1.3 y sin exceder la potencia FV máxima.
    """
    target_min, target_max = 1.1, 1.3
    best_ac, best_score = None, float('inf')

    for ac_kw, spec in INVERTER_CATALOG.items():
        if kwp_dc > spec['dc_max_kw']:          # límite FV del inversor
            continue

        ratio = kwp_dc / ac_kw
        score = (abs(ratio - 1.2)               # cuanto más cerca de 1.2, mejor
                 if target_min <= ratio <= target_max
                 else 10 + abs(ratio - 1.2))    # penaliza fuera de rango

        if score < best_score:
            best_ac, best_score = ac_kw, score

    # si nada cumple, usa el inversor más pequeño
    if best_ac is None:
        best_ac = min(INVERTER_CATALOG)

    return best_ac, get_inverter_by_ac(best_ac)

def generate_consumption_profile_hourly(
        annual_consumption_kwh: float,
        profile_type: str = "residential",
        base_year: int = 2023,                 # ← nuevo argumento
        tz: str = "Europe/Madrid"
    ) -> pd.Series:

    # 1. Índice horario de un año
    dates = pd.date_range(
        f"{base_year}-01-01", f"{base_year}-12-31 23:00:00",
        freq="h", tz=tz
    )

    if profile_type == "residential":
        daily_pattern = np.array([
            0.6, 0.5, 0.4, 0.4, 0.4, 0.5,
            0.7, 0.9, 1.1, 1.0, 0.9, 0.8,
            0.9, 1.0, 1.1, 1.2, 1.3, 1.4,
            1.6, 1.8, 1.5, 1.2, 1.0, 0.8
        ])

        # --- ¡aquí el cambio importante! ---
        day_of_year = dates.dayofyear.to_numpy()
        seasonal_factor = 1 + 0.3 * np.cos(2 * np.pi * (day_of_year - 21) / 365)

        weekend_morning = (dates.weekday.to_numpy() >= 5) & np.isin(dates.hour.to_numpy(),
                                                                    [7, 8, 9, 10, 11])
        weekly_factor = np.where(weekend_morning, 0.8, 1.0)

    else:  # commercial
        daily_pattern = np.array([
            0.3, 0.3, 0.3, 0.3, 0.4, 0.5,
            0.7, 1.2, 1.5, 1.6, 1.7, 1.6,
            1.4, 1.5, 1.6, 1.7, 1.8, 1.6,
            1.2, 0.9, 0.6, 0.5, 0.4, 0.3
        ])

        day_of_year = dates.dayofyear.to_numpy()
        seasonal_factor = 1 + 0.2 * np.cos(2 * np.pi * (day_of_year - 21) / 365)
        weekly_factor = np.where(dates.weekday.to_numpy() >= 5, 0.4, 1.0)

    # 2. Perfil horario sin normalizar
    hourly_pattern = np.tile(daily_pattern, len(dates) // 24 + 1)[:len(dates)]
    consumption_array = hourly_pattern * seasonal_factor * weekly_factor

    # 3. Normaliza al consumo anual deseado
    consumption_array = consumption_array / consumption_array.sum() * annual_consumption_kwh

    # 4. Devuelve una Serie con el mismo índice datetime
    return pd.Series(consumption_array, index=dates)

def simulate_pv_system_professional(
        weather_data: pd.DataFrame,
        lat: float,
        lon: float,
        modules_per_string: int,
        strings_in_parallel: int,
        annual_consumption_kwh: float,
        inverter_specs: dict,                 # 👈 nuevo parámetro
        consumption_profile: str = "residential"
    ) -> Dict:
    """
    Simulación profesional con pvlib y análisis de autoconsumo.
    • Usa modelo Sandia real si el inversor está en la base SAM.
    • Si no, cae a pvwatts con la eficiencia de la hoja de datos.
    """
    try:
        # ── 1. Configuración del sitio
        site = location.Location(lat, lon, tz="Europe/Madrid", altitude=100)
        tilt = abs(lat) - 10 if abs(lat) > 25 else 30      # tilt conservador
        tilt = max(15, min(tilt, 45))                      # 15-45 °
        azimuth = 180                                      # sur

        total_modules = modules_per_string * strings_in_parallel
        logger.info(
            f"Simulando: {total_modules} módulos "
            f"({strings_in_parallel}×{modules_per_string}), tilt={tilt:.1f}°"
        )

        # ── 2. Parámetros de módulo
        module_params = {
            "pdc0": MODULE_SPECS["power_stc"],
            "v_mp": MODULE_SPECS["v_mp"],
            "i_mp": MODULE_SPECS["i_mp"],
            "v_oc": MODULE_SPECS["v_oc"],
            "i_sc": MODULE_SPECS["i_sc"],
            "alpha_sc": MODULE_SPECS["alpha_sc"],
            "beta_voc": MODULE_SPECS["beta_voc"],
            "gamma_pdc": MODULE_SPECS["gamma_pmp"] / 100,
            "cells_in_series": MODULE_SPECS["cells_in_series"],
        }

        # ── 3. Modelo térmico (Faiman)
        temperature_params = {"u0": 25.0, "u1": 6.84}

        # ── 4. Array FV
        array = pvsystem.Array(
            mount=pvsystem.FixedMount(surface_tilt=tilt, surface_azimuth=azimuth),
            module_parameters=module_params,
            temperature_model_parameters=temperature_params,
            modules_per_string=modules_per_string,
            strings=strings_in_parallel,
        )

        # ── 5. Parámetros del inversor: Sandia ↔ pvwatts
        kwp_dc = total_modules * MODULE_SPECS["power_stc"] / 1000
        from pvlib.pvsystem import retrieve_sam

        inverter_model = "pvwatts"          # predeterminado (fallback)
        inverter_params: dict

        try:
            sandia_db = retrieve_sam("sandiainverter")
            # Búsqueda simple; ajusta si tu string de modelo varía
            inv_key = next(
                k for k in sandia_db if inverter_specs["model"] in k
            )
            inverter_params = sandia_db[inv_key]
            inverter_model = "sandia"
            logger.info(f"Inversor encontrado en SAM: «{inv_key}» (modelo Sandia)")
        except (StopIteration, FileNotFoundError):
            # Fallback pvwatts (solo 2 coeficientes reales)
            inverter_params = {
                "pdc0": kwp_dc * 1000,                        # W
                "eta_inv_nom": inverter_specs["efficiency"],  # 0-1
                "eta_inv_ref": inverter_specs["efficiency"],  # 0-1
            }
            logger.warning(
                "Inversor no hallado en SAM: "
                "usando modelo pvwatts con η = %.1f %%",
                inverter_specs["efficiency"] * 100,
            )

        # ── 6. Sistema PV completo
        system = pvsystem.PVSystem(
            arrays=[array],
            inverter_parameters=inverter_params,
        )

        # ── 7. Cadena de modelos
        mc = modelchain.ModelChain(
            system,
            site,
            aoi_model="physical",
            spectral_model="no_loss",
            temperature_model="faiman",
            losses_model="no_loss",   # pérdidas aplicadas manualmente
        )

        # ── 8. Ejecución
        logger.info("Ejecutando simulación pvlib…")
        mc.run_model(weather_data)

        # ── 9. Aplicar pérdidas técnicas totales
        total_loss_factor = np.prod([1 - v for v in SYSTEM_LOSSES.values()])
        ac_power_with_losses = mc.results.ac * total_loss_factor / 1000

        # ── 10. Perfil de consumo y alineación
        profile_year = weather_data.index[0].year
        consumption_hourly = generate_consumption_profile_hourly(
            annual_consumption_kwh, consumption_profile, base_year=profile_year, tz="Europe/Madrid"
        )

        common_index = ac_power_with_losses.index.intersection(consumption_hourly.index)
        common_index = pd.DatetimeIndex(common_index)
        
        production_aligned = ac_power_with_losses.reindex(common_index, fill_value=0)
        consumption_aligned = consumption_hourly.reindex(
            common_index, fill_value=consumption_hourly.mean()
        )

        # ── 11. Cálculos horario → anual
        self_consumption_hourly = np.minimum(production_aligned, consumption_aligned)
        grid_injection_hourly   = np.maximum(0, production_aligned - consumption_aligned)
        grid_consumption_hourly = np.maximum(0, consumption_aligned - production_aligned)

        # ── 12. Análisis mensual
        monthly_analysis = []
        month_names = [
            "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
            "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
        ]

        for month in range(1, 13):
            mask = common_index.month == month
            if mask.sum() == 0:
                continue

            prod_month = production_aligned[mask].sum()
            cons_month = consumption_aligned[mask].sum()
            self_cons  = self_consumption_hourly[mask].sum()
            grid_inj   = grid_injection_hourly[mask].sum() 
            grid_cons  = grid_consumption_hourly[mask].sum()

            savings_month = (
                self_cons * ELECTRICITY_PRICES["consumption_eur_kwh"] +
                grid_inj  * ELECTRICITY_PRICES["surplus_eur_kwh"]
            )

            monthly_analysis.append({
                "month": month,
                "month_name": month_names[month - 1],
                "production_kwh":        round(prod_month, 1),
                "consumption_kwh":       round(cons_month, 1),
                "self_consumption_kwh":  round(self_cons, 1),
                "grid_injection_kwh":    round(grid_inj, 1),
                "grid_consumption_kwh":  round(grid_cons, 1),
                "economic_savings_eur":  round(savings_month, 2),
            })

        # ── 13. Totales y KPIs
        annual_production       = production_aligned.sum()
        annual_consumption_real = consumption_aligned.sum()
        annual_self_consumption = self_consumption_hourly.sum()
        annual_grid_injection   = grid_injection_hourly.sum()

        self_consumption_rate = (
            annual_self_consumption / annual_production * 100
            if annual_production > 0 else 0
        )
        self_sufficiency_rate = (
            annual_self_consumption / annual_consumption_real * 100
            if annual_consumption_real > 0 else 0
        )

        specific_yield = annual_production / kwp_dc if kwp_dc > 0 else 0

        ghi_sum = weather_data["ghi"].sum() / 1000  # kWh/m²·año
        theoretical_energy = kwp_dc * ghi_sum
        performance_ratio = (
            annual_production / theoretical_energy
            if theoretical_energy > 0 else 0
        )
        capacity_factor = (
            annual_production / (kwp_dc * 8760) * 100
            if kwp_dc > 0 else 0
        )

        logger.info(
            "Simulación completada: %.0f kWh/año, autoconsumo %.1f %%",
            annual_production, self_consumption_rate
        )

        # ── 14. Salida
        return {
            "annual_production":         annual_production,
            "annual_consumption":        annual_consumption_real,
            "annual_self_consumption":   annual_self_consumption,
            "annual_grid_injection":     annual_grid_injection,
            "self_consumption_rate":     self_consumption_rate,
            "self_sufficiency_rate":     self_sufficiency_rate,
            "monthly_analysis":          monthly_analysis,
            "specific_yield":            specific_yield,
            "performance_ratio":         performance_ratio,
            "capacity_factor":           capacity_factor,
            "tilt":                      tilt,
            "azimuth":                   azimuth,
            "ghi_annual":                ghi_sum,
        }

    except Exception as e:
        logger.error("Error en simulación: %s", e)
        logger.error("TRACEBACK:\n%s", traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Error en simulación fotovoltaica: {e}"
        )

@app.get("/")
async def root():
    return {"message": "API Fotovoltaica Profesional", "version": "2.0.0"}

@app.post("/simulate", response_model=SimulateResponse)
async def simulate_professional(request: SimulateRequest):
    """Endpoint principal para dimensionado fotovoltaico profesional y realista"""
    try:
        logger.info(f"Iniciando simulación profesional para lat={request.lat}, lon={request.lon}")
        
        # 1. Obtener datos climáticos
        weather_data = get_pvgis_tmy_data(request.lat, request.lon)
        
        # 2. Cálculo preliminar de rendimiento específico
        ghi_annual = weather_data['ghi'].sum() / 1000  # kWh/m²/año
        estimated_specific_yield = ghi_annual * 0.75  # Factor conservador 75%
        
        # 3. Dimensionado del sistema
        if request.kwp_target:
            # Dimensionado por potencia objetivo
            module_power_kw = MODULE_SPECS["power_stc"] / 1000
            n_modules = max(1, round(request.kwp_target / module_power_kw))
            kwp_dc = n_modules * module_power_kw
            total_area = n_modules * MODULE_SPECS["area_m2"]
            expected_production = kwp_dc * estimated_specific_yield
            coverage_percentage = (expected_production / request.annual_consumption_kwh) * 100
            logger.info(f"Dimensionado por potencia: {n_modules} módulos, {kwp_dc:.2f} kWp")
        else:
            # Dimensionado por consumo (método recomendado)
            n_modules, kwp_dc, total_area, coverage_percentage = calculate_optimal_system_size(
                request.annual_consumption_kwh, 
                request.coverage_percentage,
                estimated_specific_yield, 
                request.roof_area_m2
            )
        
        # 4. Selección del inversor
        inverter_power_ac, inverter_specs = select_inverter_professional(kwp_dc)      

        # 5. Configuración de strings
        modules_per_string, strings_in_parallel, total_modules_actual, dc_ac_ratio = \
            calculate_string_configuration_professional(n_modules, inverter_specs)
        
        # Actualizar valores finales
        n_modules = total_modules_actual
        kwp_dc = n_modules * (MODULE_SPECS["power_stc"] / 1000)
        total_area = n_modules * MODULE_SPECS["area_m2"]
        
        # 6. Simulación del sistema
        simulation_results = simulate_pv_system_professional(
            weather_data,
            request.lat,
            request.lon,
            modules_per_string,
            strings_in_parallel,
            request.annual_consumption_kwh,
            inverter_specs,
            request.consumption_profile
        )
        
        # 7. Cálculos eléctricos detallados
        string_voltage_vmp = modules_per_string * MODULE_SPECS["v_mp"]
        string_voltage_voc = modules_per_string * MODULE_SPECS["v_oc"]
        total_current_imp = strings_in_parallel * MODULE_SPECS["i_mp"]
        max_system_voltage = string_voltage_voc * 1.25  # Factor seguridad 25%
        dc_ac_ratio = kwp_dc / inverter_power_ac
        
        # Verificar compatibilidad MPPT
        voltage_range = inverter_specs["voltage_range"].replace("V", "")
        v_min, v_max = map(float, voltage_range.split("-"))
        mppt_ok = v_min <= string_voltage_vmp <= v_max
        
        # 8. Análisis económico realista
        system_cost = kwp_dc * 1000 * ELECTRICITY_PRICES["system_cost_eur_wp"]  # €
        
        # Ahorro por electricidad no comprada (autoconsumo)
        electricity_bill_reduction = (
            simulation_results["annual_self_consumption"] * 
            ELECTRICITY_PRICES["consumption_eur_kwh"]
        )
        
        # Compensación por excedentes
        surplus_compensation = (
            simulation_results["annual_grid_injection"] * 
            ELECTRICITY_PRICES["surplus_eur_kwh"]
        )
        
        annual_savings = electricity_bill_reduction + surplus_compensation
        payback_years = system_cost / annual_savings if annual_savings > 0 else 999
        
        # ROI a 25 años (considerando degradación 0.5%/año)
        total_savings_25y = 0
        for year in range(1, 26):
            degradation_factor = (1 - 0.005) ** (year - 1)
            year_production = simulation_results["annual_production"] * degradation_factor
            year_self_consumption = min(year_production, request.annual_consumption_kwh * 1.02**(year-1))  # Crecimiento consumo 2%/año
            year_surplus = max(0, year_production - year_self_consumption)
            
            year_savings = (
                year_self_consumption * ELECTRICITY_PRICES["consumption_eur_kwh"] * 1.03**(year-1) +  # Inflación 3%
                year_surplus * ELECTRICITY_PRICES["surplus_eur_kwh"] * 1.02**(year-1)  # Inflación compensación 2%
            )
            total_savings_25y += year_savings
        
        roi_25_years = total_savings_25y - system_cost
        
        # 9. Pérdidas del sistema
        total_losses = 1.0
        for loss_value in SYSTEM_LOSSES.values():
            total_losses *= (1 - loss_value)
        total_losses_percent = (1 - total_losses) * 100
        
        # 10. Construcción de la respuesta
        response = SimulateResponse(
            project_info={
                "location": f"Lat: {request.lat:.4f}, Lon: {request.lon:.4f}",
                "annual_consumption_kwh": request.annual_consumption_kwh,
                "coverage_target_percent": request.coverage_percentage,
                "coverage_achieved_percent": round(coverage_percentage, 1),
                "roof_area_available_m2": request.roof_area_m2,
                "consumption_profile": request.consumption_profile,
                "calculation_date": datetime.now().isoformat(),
                "api_version": "2.0.0"
            },
            
            module_specs=ModuleSpecs(
                model=MODULE_SPECS["model"],
                power_wp=MODULE_SPECS["power_stc"],
                voltage_vmp=MODULE_SPECS["v_mp"],
                current_imp=MODULE_SPECS["i_mp"],
                voltage_voc=MODULE_SPECS["v_oc"],
                current_isc=MODULE_SPECS["i_sc"],
                area_m2=MODULE_SPECS["area_m2"],
                efficiency=round(MODULE_SPECS["power_stc"] / (MODULE_SPECS["area_m2"] * 1000) * 100, 1),
                temp_coef_power=MODULE_SPECS["gamma_pmp"]
            ),
            
            inverter_specs=InverterSpecs(
                model=inverter_specs['model'],
                power_ac_kw=inverter_power_ac,
                power_dc_max_kw=inverter_specs['dc_max_kw'],
                efficiency=inverter_specs['efficiency'],
                mppt_trackers=inverter_specs['mppt'],
                input_voltage_range=inverter_specs['voltage_range']
            ),
            
            system_config=SystemConfiguration(
                modules_per_string=modules_per_string,
                strings_in_parallel=strings_in_parallel,
                total_modules=n_modules,
                array_configuration=f"{strings_in_parallel}S × {modules_per_string}P",
                total_area_m2=round(total_area, 1)
            ),
            
            electrical_calcs=ElectricalCalculations(
                dc_ac_ratio=round(dc_ac_ratio, 2),
                string_voltage_vmp=round(string_voltage_vmp, 1),
                string_voltage_voc=round(string_voltage_voc, 1),
                total_current_imp=round(total_current_imp, 1),
                max_system_voltage=round(max_system_voltage, 1),
                mppt_voltage_range_ok=mppt_ok
            ),
            
            solar_geometry=SolarGeometry(
                optimal_tilt_deg=simulation_results["tilt"],
                azimuth_deg=simulation_results["azimuth"],
                annual_irradiation_kwh_m2=round(simulation_results["ghi_annual"], 0),
                peak_sun_hours=round(simulation_results["ghi_annual"] / 365, 1)
            ),
            
            energy_analysis=EnergyAnalysis(
                annual_production_kwh=round(simulation_results["annual_production"], 0),
                annual_consumption_kwh=round(simulation_results["annual_consumption"], 0),
                annual_self_consumption_kwh=round(simulation_results["annual_self_consumption"], 0),
                annual_grid_injection_kwh=round(simulation_results["annual_grid_injection"], 0),
                self_consumption_rate_percent=round(simulation_results["self_consumption_rate"], 1),
                self_sufficiency_rate_percent=round(simulation_results["self_sufficiency_rate"], 1),
                monthly_analysis=[MonthlyData(**item) for item in simulation_results["monthly_analysis"]],
                specific_yield_kwh_kwp=round(simulation_results["specific_yield"], 0),
                performance_ratio=round(simulation_results["performance_ratio"], 3),
                capacity_factor=round(simulation_results["capacity_factor"], 1)
            ),
            
            economic_analysis=EconomicAnalysis(
                system_cost_eur=round(system_cost, 0),
                annual_savings_eur=round(annual_savings, 0),
                electricity_bill_reduction_eur=round(electricity_bill_reduction, 0),
                surplus_compensation_eur=round(surplus_compensation, 0),
                payback_years=round(payback_years, 1),
                roi_25_years_eur=round(roi_25_years, 0),
                electricity_price_eur_kwh=ELECTRICITY_PRICES["consumption_eur_kwh"],
                surplus_price_eur_kwh=ELECTRICITY_PRICES["surplus_eur_kwh"]
            ),
            
            system_losses=TechnicalLosses(
                soiling_percent=SYSTEM_LOSSES["soiling"] * 100,
                cables_percent=SYSTEM_LOSSES["cables"] * 100,
                mismatch_percent=SYSTEM_LOSSES["mismatch"] * 100,
                connections_percent=SYSTEM_LOSSES["connections"] * 100,
                lid_percent=SYSTEM_LOSSES["lid"] * 100,
                nameplate_percent=SYSTEM_LOSSES["nameplate"] * 100,
                availability_percent=SYSTEM_LOSSES["availability"] * 100,
                inverter_percent=SYSTEM_LOSSES["inverter"] * 100,
                total_losses_percent=round(total_losses_percent, 1)
            )
        )
        
        logger.info(f"Simulación profesional completada: {n_modules} módulos, {kwp_dc:.1f} kWp DC, {inverter_power_ac} kW AC")
        logger.info(f"Producción anual: {simulation_results['annual_production']:.0f} kWh, Autoconsumo: {simulation_results['self_consumption_rate']:.1f}%")
        logger.info(f"Ahorro anual: {annual_savings:.0f} €, Payback: {payback_years:.1f} años")
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error inesperado en simulación profesional: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error interno del servidor: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)