from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
import pandas as pd
import numpy as np
import requests
import pvlib
from pvlib import location, modelchain, pvsystem, temperature, tools
import json
from datetime import datetime
import logging

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Fotovoltaico Pre-dimensionado API", version="1.0.0")

# Modelos Pydantic
class SimulateRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Latitud en grados decimales")
    lon: float = Field(..., ge=-180, le=180, description="Longitud en grados decimales")
    roof_area_m2: Optional[float] = Field(None, gt=0, description="Área disponible del tejado en m²")
    kwp_target: Optional[float] = Field(None, gt=0, description="Potencia objetivo en kWp")
    annual_consumption_kwh: Optional[float] = Field(4200, gt=0, description="Consumo anual de la vivienda en kWh")
    coverage_percentage: Optional[float] = Field(80, ge=50, le=100, description="Porcentaje de consumo a cubrir")

class MonthlyData(BaseModel):
    month: int
    energy_kwh: float

class SystemConfiguration(BaseModel):
    modules_per_string: int
    strings_in_parallel: int
    total_modules: int
    array_configuration: str
    
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
    
class EnergyAnalysis(BaseModel):
    annual_production_kwh: float
    monthly_production: List[MonthlyData]
    specific_yield_kwh_kwp: float
    performance_ratio: float
    capacity_factor: float
    annual_savings_eur: float
    payback_years: float

class TechnicalLosses(BaseModel):
    soiling_percent: float
    cables_percent: float
    mismatch_percent: float
    connections_percent: float
    lid_percent: float
    nameplate_percent: float
    availability_percent: float
    total_losses_percent: float

class SolarGeometry(BaseModel):
    optimal_tilt_deg: float
    azimuth_deg: float
    annual_irradiation_kwh_m2: float
    peak_sun_hours: float

class SimulateResponse(BaseModel):
    # Información del proyecto
    project_info: Dict
    
    # Especificaciones del módulo
    module_specs: ModuleSpecs
    
    # Especificaciones del inversor  
    inverter_specs: InverterSpecs
    
    # Configuración del sistema
    system_config: SystemConfiguration
    
    # Cálculos eléctricos
    electrical_calcs: ElectricalCalculations
    
    # Geometría solar
    solar_geometry: SolarGeometry
    
    # Análisis energético
    energy_analysis: EnergyAnalysis
    
    # Pérdidas técnicas
    system_losses: TechnicalLosses
    
    # Compatibilidad (deprecated)
    n_modules: int
    kwp: float
    energy_kwh_year: float
    specific_yield: float
    performance_ratio: float
    monthly: List[MonthlyData]

# Constantes del sistema
MODULE_SPECS = {
    "power_stc": 430,  # W
    "area_m2": 1.9,    # m²
    "v_mp": 34.8,      # V
    "i_mp": 12.36,     # A
    "v_oc": 42.1,      # V
    "i_sc": 13.15,     # A
    "alpha_sc": 0.0006,  # A/°C
    "beta_voc": -0.127,  # V/°C
    "gamma_pmp": -0.35,  # %/°C
    "cells_in_series": 72
}

# Parámetros del sistema
SYSTEM_LOSSES = {
    "soiling": 0.03,      # 3%
    "cables": 0.02,       # 2%
    "mismatch": 0.02,     # 2%
    "connections": 0.005, # 0.5%
    "lid": 0.015,         # 1.5%
    "nameplate": 0.01,    # 1%
    "availability": 0.02  # 2%
}

DC_AC_RATIO_TARGET = 1.15

# Catálogo de inversores
INVERTER_CATALOG = {
    3: {"model": "SMA Sunny Boy 3.0", "efficiency": 0.967, "mppt": 2, "voltage_range": "125-750V"},
    5: {"model": "Fronius Primo 5.0", "efficiency": 0.966, "mppt": 2, "voltage_range": "120-800V"},
    6: {"model": "Huawei SUN2000-6KTL", "efficiency": 0.984, "mppt": 2, "voltage_range": "140-980V"},
    8: {"model": "SMA Sunny Boy 8.0", "efficiency": 0.966, "mppt": 2, "voltage_range": "125-750V"},
    10: {"model": "Fronius Symo 10.0", "efficiency": 0.967, "mppt": 2, "voltage_range": "120-800V"},
    12: {"model": "Huawei SUN2000-12KTL", "efficiency": 0.984, "mppt": 2, "voltage_range": "140-980V"},
    15: {"model": "SMA Sunny Tripower 15.0", "efficiency": 0.983, "mppt": 4, "voltage_range": "125-750V"},
    20: {"model": "Fronius Symo 20.0", "efficiency": 0.967, "mppt": 2, "voltage_range": "120-800V"}
}

def get_pvgis_tmy_data(lat: float, lon: float) -> pd.DataFrame:
    """Descarga datos TMY desde PVGIS"""
    try:
        url = "https://re.jrc.ec.europa.eu/api/v5_2/tmy"
        params = {
            'lat': lat, 'lon': lon, 'outputformat': 'json',
            'usehorizon': 1, 'startyear': 2005, 'endyear': 2016
        }
        
        logger.info(f"Descargando datos TMY para lat={lat}, lon={lon}")
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        hourly_data = data['outputs']['tmy_hourly']
        df = pd.DataFrame(hourly_data)
        
        df.index = pd.to_datetime(df['time(UTC)'], format='%Y%m%d:%H%M')
        df = df.rename(columns={
            'G(h)': 'ghi', 'Gb(n)': 'dni', 'Gd(h)': 'dhi',
            'T2m': 'temp_air', 'WS10m': 'wind_speed', 'RH': 'relative_humidity'
        })
        
        numeric_cols = ['ghi', 'dni', 'dhi', 'temp_air', 'wind_speed', 'relative_humidity']
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        
        df = df.dropna(subset=['ghi', 'dni', 'dhi', 'temp_air'])
        logger.info(f"Datos TMY descargados: {len(df)} registros")
        return df
        
    except Exception as e:
        logger.error(f"Error descargando datos PVGIS: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error obteniendo datos climáticos: {str(e)}")

def calculate_optimal_tilt(latitude: float) -> float:
    """Calcula inclinación óptima basada en latitud"""
    optimal_tilt = abs(latitude) - 10
    return max(15, min(optimal_tilt, 45))

def calculate_system_size_by_consumption(annual_consumption_kwh: float, coverage_percentage: float, 
                                        specific_yield_kwh_kwp: float, roof_area_m2: Optional[float] = None) -> tuple:
    """Dimensiona sistema basado en consumo eléctrico"""
    target_energy_kwh = annual_consumption_kwh * (coverage_percentage / 100)
    required_kwp = target_energy_kwh / specific_yield_kwh_kwp
    
    module_power_kw = MODULE_SPECS["power_stc"] / 1000
    n_modules_needed = int(np.ceil(required_kwp / module_power_kw))
    
    if roof_area_m2:
        max_modules_by_area = int(roof_area_m2 / MODULE_SPECS["area_m2"])
        if n_modules_needed > max_modules_by_area:
            n_modules_needed = max_modules_by_area
            logger.warning(f"Sistema limitado por área del tejado")
    
    actual_kwp = n_modules_needed * module_power_kw
    total_area = n_modules_needed * MODULE_SPECS["area_m2"]
    actual_coverage = (actual_kwp * specific_yield_kwh_kwp) / annual_consumption_kwh * 100
    
    logger.info(f"Dimensionado: {n_modules_needed} módulos, {actual_kwp:.2f} kWp, cobertura {actual_coverage:.1f}%")
    return n_modules_needed, actual_kwp, total_area, actual_coverage

def calculate_string_configuration(n_modules: int, inverter_voltage_range: str) -> tuple:
    """Calcula configuración de strings"""
    voltage_parts = inverter_voltage_range.replace("V", "").split("-")
    v_min = float(voltage_parts[0])
    v_max = float(voltage_parts[1])
    
    v_mp_hot = MODULE_SPECS["v_mp"] + MODULE_SPECS["beta_voc"] * (70 - 25)
    v_oc_cold = MODULE_SPECS["v_oc"] + MODULE_SPECS["beta_voc"] * (-10 - 25)
    
    max_modules_per_string_hot = int(v_max / v_mp_hot)
    max_modules_per_string_cold = int(v_max / v_oc_cold)
    min_modules_per_string = int(np.ceil(v_min / v_mp_hot))
    
    modules_per_string = min(max_modules_per_string_hot, max_modules_per_string_cold)
    modules_per_string = max(modules_per_string, min_modules_per_string)
    
    strings_in_parallel = int(np.ceil(n_modules / modules_per_string))
    total_modules_actual = modules_per_string * strings_in_parallel
    
    logger.info(f"Configuración: {strings_in_parallel} strings × {modules_per_string} módulos")
    return modules_per_string, strings_in_parallel, total_modules_actual

def get_inverter_specs(kwp_dc: float) -> tuple:
    """Selecciona inversor del catálogo"""
    target_ac_power = kwp_dc / DC_AC_RATIO_TARGET
    available_powers = sorted(INVERTER_CATALOG.keys())
    
    selected_power = None
    for power in available_powers:
        if power >= target_ac_power:
            selected_power = power
            break
    
    if selected_power is None:
        selected_power = available_powers[-1]
    
    return selected_power, INVERTER_CATALOG[selected_power]

def simulate_pv_system(weather_data: pd.DataFrame, lat: float, lon: float, 
                      n_modules: int, kwp_dc: float) -> Dict:
    """Simula sistema fotovoltaico con pvlib"""
    try:
        site = location.Location(lat, lon, tz='Europe/Madrid', altitude=100)
        tilt = calculate_optimal_tilt(lat)
        azimuth = 180
        
        logger.info(f"Configuración: tilt={tilt}°, azimuth={azimuth}°")
        
        # Parámetros del módulo
        module_params = {
            'pdc0': MODULE_SPECS["power_stc"],
            'v_mp': MODULE_SPECS["v_mp"],
            'i_mp': MODULE_SPECS["i_mp"],
            'v_oc': MODULE_SPECS["v_oc"],
            'i_sc': MODULE_SPECS["i_sc"],
            'alpha_sc': MODULE_SPECS["alpha_sc"],
            'beta_voc': MODULE_SPECS["beta_voc"],
            'gamma_pdc': MODULE_SPECS["gamma_pmp"] / 100,
            'cells_in_series': MODULE_SPECS["cells_in_series"]
        }
        
        # Parámetros de temperatura
        temperature_params = {
            'u_c': 29.0,
            'u_v': 0.0,
            'eta_m': 0.1,
            'alpha_absorption': 0.9
        }
        
        # Inversor
        inverter_power_ac, inverter_specs = get_inverter_specs(kwp_dc)
        inverter_params = {
            'pdc0': inverter_power_ac * 1000 * DC_AC_RATIO_TARGET,
            'pac0': inverter_power_ac * 1000,
            'eta_inv_nom': inverter_specs["efficiency"],
            'eta_inv_ref': inverter_specs["efficiency"] * 0.98
        }
        
        # Array fotovoltaico
        array = pvsystem.Array(
            mount=pvsystem.FixedMount(surface_tilt=tilt, surface_azimuth=azimuth),
            module_parameters=module_params,
            temperature_model_parameters=temperature_params,
            modules_per_string=int(np.sqrt(n_modules)),
            strings=int(n_modules / int(np.sqrt(n_modules)))
        )
        
        # Sistema
        system = pvsystem.PVSystem(arrays=[array], inverter_parameters=inverter_params)
        
        # Modelo de cadena
        mc = modelchain.ModelChain(
            system, site,
            aoi_model='physical',
            spectral_model='no_loss',
            temperature_model='pvsyst',
            losses_model='pvwatts'
        )
        
        # Ejecutar simulación
        logger.info("Ejecutando simulación con pvlib...")
        mc.run_model(weather_data)
        
        # Aplicar pérdidas del sistema
        total_losses = 1.0
        for loss_value in SYSTEM_LOSSES.values():
            total_losses *= (1 - loss_value)
        
        ac_power_with_losses = mc.results.ac * total_losses
        annual_energy = ac_power_with_losses.sum() / 1000
        specific_yield = annual_energy / kwp_dc
        
        # Performance Ratio
        ghi_sum = weather_data['ghi'].sum() / 1000
        theoretical_energy = kwp_dc * ghi_sum / 1
        performance_ratio = annual_energy / theoretical_energy if theoretical_energy > 0 else 0
        
        # Datos mensuales
        monthly_energy = ac_power_with_losses.resample('M').sum() / 1000
        monthly_data = []
        for month, energy in enumerate(monthly_energy, 1):
            monthly_data.append({"month": month, "energy_kwh": float(energy)})
        
        logger.info(f"Simulación completada: {annual_energy:.0f} kWh/año")
        
        return {
            "annual_energy": float(annual_energy),
            "specific_yield": float(specific_yield),
            "performance_ratio": float(performance_ratio),
            "monthly_data": monthly_data,
            "inverter_power": inverter_power_ac,
            "tilt": tilt,
            "azimuth": azimuth
        }
        
    except Exception as e:
        logger.error(f"Error en simulación pvlib: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error en cálculos fotovoltaicos: {str(e)}")

@app.get("/")
async def root():
    return {"message": "Fotovoltaico Pre-dimensionado API", "version": "1.0.0"}

@app.post("/simulate", response_model=SimulateResponse)
async def simulate(request: SimulateRequest):
    """Endpoint principal para dimensionado fotovoltaico profesional"""
    try:
        logger.info(f"Iniciando simulación para lat={request.lat}, lon={request.lon}")
        
        # 1. Obtener datos climáticos
        weather_data = get_pvgis_tmy_data(request.lat, request.lon)
        
        # 2. Estimación preliminar
        tilt = calculate_optimal_tilt(request.lat)
        ghi_sum = weather_data['ghi'].sum() / 1000
        estimated_specific_yield = ghi_sum * 0.75
        
        # 3. Dimensionado
        if request.kwp_target:
            n_modules = int(request.kwp_target / (MODULE_SPECS["power_stc"] / 1000))
            kwp_dc = n_modules * (MODULE_SPECS["power_stc"] / 1000)
            total_area = n_modules * MODULE_SPECS["area_m2"]
            coverage_percentage = (kwp_dc * estimated_specific_yield / request.annual_consumption_kwh) * 100
        else:
            n_modules, kwp_dc, total_area, coverage_percentage = calculate_system_size_by_consumption(
                request.annual_consumption_kwh, request.coverage_percentage,
                estimated_specific_yield, request.roof_area_m2
            )
        
        # 4. Selección del inversor
        inverter_power_ac, inverter_specs = get_inverter_specs(kwp_dc)
        
        # 5. Configuración de strings
        modules_per_string, strings_in_parallel, total_modules_actual = calculate_string_configuration(
            n_modules, inverter_specs["voltage_range"]
        )
        
        # Actualizar valores
        n_modules = total_modules_actual
        kwp_dc = n_modules * (MODULE_SPECS["power_stc"] / 1000)
        total_area = n_modules * MODULE_SPECS["area_m2"]
        
        # 6. Simular sistema
        simulation_results = simulate_pv_system(weather_data, request.lat, request.lon, n_modules, kwp_dc)
        
        # 7. Cálculos eléctricos
        string_voltage_vmp = modules_per_string * MODULE_SPECS["v_mp"]
        string_voltage_voc = modules_per_string * MODULE_SPECS["v_oc"]
        total_current_imp = strings_in_parallel * MODULE_SPECS["i_mp"]
        max_system_voltage = string_voltage_voc * 1.2
        dc_ac_ratio = kwp_dc / inverter_power_ac
        
        # 8. Análisis económico
        electricity_price_eur_kwh = 0.25
        annual_savings = simulation_results["annual_energy"] * electricity_price_eur_kwh
        system_cost_eur = kwp_dc * 1200
        payback_years = system_cost_eur / annual_savings if annual_savings > 0 else 999
        
        # 9. Pérdidas totales
        total_losses = 1.0
        for loss_value in SYSTEM_LOSSES.values():
            total_losses *= (1 - loss_value)
        total_losses_percent = (1 - total_losses) * 100
        
        # 10. Capacity factor
        capacity_factor = simulation_results["annual_energy"] / (kwp_dc * 8760) * 100
        
        # 11. Respuesta estructurada
        response = SimulateResponse(
            project_info={
                "location": f"Lat: {request.lat:.4f}, Lon: {request.lon:.4f}",
                "annual_consumption_kwh": request.annual_consumption_kwh,
                "coverage_target_percent": request.coverage_percentage,
                "coverage_achieved_percent": round(coverage_percentage, 1),
                "roof_area_available_m2": request.roof_area_m2,
                "calculation_date": datetime.now().isoformat()
            },
            
            module_specs=ModuleSpecs(
                model="Módulo genérico 430W",
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
                model=inverter_specs["model"],
                power_ac_kw=inverter_power_ac,
                power_dc_max_kw=round(inverter_power_ac * 1.3, 1),
                efficiency=inverter_specs["efficiency"],
                mppt_trackers=inverter_specs["mppt"],
                input_voltage_range=inverter_specs["voltage_range"]
            ),
            
            system_config=SystemConfiguration(
                modules_per_string=modules_per_string,
                strings_in_parallel=strings_in_parallel,
                total_modules=n_modules,
                array_configuration=f"{strings_in_parallel}S × {modules_per_string}P"
            ),
            
            electrical_calcs=ElectricalCalculations(
                dc_ac_ratio=round(dc_ac_ratio, 2),
                string_voltage_vmp=round(string_voltage_vmp, 1),
                string_voltage_voc=round(string_voltage_voc, 1),
                total_current_imp=round(total_current_imp, 1),
                max_system_voltage=round(max_system_voltage, 1)
            ),
            
            solar_geometry=SolarGeometry(
                optimal_tilt_deg=simulation_results["tilt"],
                azimuth_deg=simulation_results["azimuth"],
                annual_irradiation_kwh_m2=round(ghi_sum, 0),
                peak_sun_hours=round(ghi_sum / 365, 1)
            ),
            
            energy_analysis=EnergyAnalysis(
                annual_production_kwh=round(simulation_results["annual_energy"], 0),
                monthly_production=[MonthlyData(**item) for item in simulation_results["monthly_data"]],
                specific_yield_kwh_kwp=round(simulation_results["specific_yield"], 0),
                performance_ratio=round(simulation_results["performance_ratio"], 3),
                capacity_factor=round(capacity_factor, 1),
                annual_savings_eur=round(annual_savings, 0),
                payback_years=round(payback_years, 1)
            ),
            
            system_losses=TechnicalLosses(
                soiling_percent=SYSTEM_LOSSES["soiling"] * 100,
                cables_percent=SYSTEM_LOSSES["cables"] * 100,
                mismatch_percent=SYSTEM_LOSSES["mismatch"] * 100,
                connections_percent=SYSTEM_LOSSES["connections"] * 100,
                lid_percent=SYSTEM_LOSSES["lid"] * 100,
                nameplate_percent=SYSTEM_LOSSES["nameplate"] * 100,
                availability_percent=SYSTEM_LOSSES["availability"] * 100,
                total_losses_percent=round(total_losses_percent, 1)
            ),
            
            # Compatibilidad
            n_modules=n_modules,
            kwp=inverter_power_ac,
            energy_kwh_year=simulation_results["annual_energy"],
            specific_yield=simulation_results["specific_yield"],
            performance_ratio=simulation_results["performance_ratio"],
            monthly=[MonthlyData(**item) for item in simulation_results["monthly_data"]]
        )
        
        logger.info(f"Dimensionado completado: {n_modules} módulos, {kwp_dc:.1f} kWp DC, {inverter_power_ac} kW AC")
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error inesperado: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error interno del servidor: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)