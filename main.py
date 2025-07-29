from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
import pandas as pd
import numpy as np
import requests
from pvlib import location, modelchain, pvsystem, temperature, solarposition, irradiance
from pvlib.pvsystem import retrieve_sam
import json
from datetime import datetime, timedelta
import logging
import traceback
import math
import asyncio
from datetime import datetime, date
import os
from pathlib import Path
from dotenv import load_dotenv
from contextlib import asynccontextmanager
import aiohttp

load_dotenv()

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    google_api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not google_api_key:
        logger.warning("⚠️  GOOGLE_MAPS_API_KEY no encontrada en .env - funcionalidad de geocodificación y Solar API deshabilitada")
    else:
        logger.info("✅ Google Maps API Key cargada correctamente desde .env")
        
    yield  # Aquí FastAPI levanta la app
    
    # Si hubiera necesidad, insertar código de shutdown aquí

app = FastAPI(title="Sistema de Dimensionado Fotovoltaico Profesional", version="3.0.0", lifespan=lifespan)

# ======================== CONFIGURACIÓN SOLAR API ========================
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
SOLAR_API_BASE_URL = "https://solar.googleapis.com/v1"
GEOCODING_API_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# ======================== MODELOS PYDANTIC ========================
class SimulateRequest(BaseModel):
    # Nuevos campos para dirección
    address: Optional[str] = Field(None, description="Dirección postal para geocodificación automática")
    
    # Campos existentes (ahora opcionales si se usa address)
    lat: Optional[float] = Field(None, ge=-90, le=90, description="Latitud en grados decimales")
    lon: Optional[float] = Field(None, ge=-180, le=180, description="Longitud en grados decimales")
    annual_consumption_kwh: Optional[float] = Field(4200, gt=0, description="Consumo anual en kWh")
    roof_area_m2: Optional[float] = Field(None, gt=0, description="Área disponible del tejado en m²")
    roof_tilt: Optional[float] = Field(None, ge=0, le=90, description="Inclinación del tejado en grados")
    roof_azimuth: Optional[float] = Field(None, ge=0, le=360, description="Azimut del tejado en grados")
    installation_type: Optional[str] = Field("optimal", description="Tipo: 'optimal', 'coplanar', 'fixed'")
    coverage_percentage: Optional[float] = Field(85, ge=30, le=120, description="Porcentaje de consumo a cubrir")
    shading_factor: Optional[float] = Field(0.95, ge=0.7, le=1.0, description="Factor de sombreado")
    electricity_price: Optional[float] = Field(0.28, gt=0, description="Precio electricidad €/kWh")
    surplus_price: Optional[float] = Field(0.055, gt=0, description="Precio compensación excedentes €/kWh")
    
    # Control de uso de Solar API
    use_solar_api: Optional[bool] = Field(True, description="Usar Google Solar API si está disponible")

class GeometryAnalysis(BaseModel):
    optimal_tilt: float
    optimal_azimuth: float
    installation_tilt: float
    installation_azimuth: float
    with_support_structure: bool
    annual_irradiation_optimal: float
    annual_irradiation_real: float
    orientation_losses_percent: float
    shading_losses_percent: float
    
class TechnicalSpecs(BaseModel):
    module_model: str
    module_power_wp: int
    module_efficiency_percent: float
    module_area_m2: float
    inverter_model: str
    inverter_power_kw: float
    inverter_efficiency_percent: float
    dc_ac_ratio: float

class SystemConfiguration(BaseModel):
    total_modules: int
    modules_per_string: int
    strings_parallel: int
    total_power_kwp: float
    total_area_m2: float
    array_configuration: str

class ElectricalAnalysis(BaseModel):
    string_voltage_vmp_v: float
    string_voltage_voc_v: float
    array_current_imp_a: float
    max_system_voltage_v: float
    mppt_compatibility: bool
    dc_cable_losses_percent: float
    ac_cable_losses_percent: float

class EnergyProduction(BaseModel):
    annual_production_kwh: float
    monthly_production: List[float]
    specific_yield_kwh_kwp: float
    performance_ratio: float
    capacity_factor_percent: float

class EconomicAnalysis(BaseModel):
    system_cost_eur: float
    annual_savings_eur: float
    payback_years: float
    npv_25_years_eur: float
    irr_percent: float
    lcoe_eur_kwh: float
    
class MonthlyAnalysis(BaseModel):
    month: int
    month_name: str
    production_kwh: float
    consumption_kwh: float
    self_consumption_kwh: float
    grid_injection_kwh: float
    grid_consumption_kwh: float
    self_consumption_rate_percent: float
    self_sufficiency_rate_percent: float
    economic_savings_eur: Optional[float] = None

class AutoconsumptionAnalysis(BaseModel):
    annual_consumption_kwh: float
    annual_self_consumption_kwh: float
    annual_grid_injection_kwh: float
    annual_grid_purchase_kwh: float
    self_consumption_rate_percent: float
    self_sufficiency_rate_percent: float
    monthly_analysis: List[MonthlyAnalysis]

class SimulateResponse(BaseModel):
    location_info: Dict
    geometry_analysis: GeometryAnalysis
    technical_specs: TechnicalSpecs
    system_config: SystemConfiguration
    electrical_analysis: ElectricalAnalysis
    energy_production: EnergyProduction
    autoconsumption_analysis: AutoconsumptionAnalysis
    economic_analysis: EconomicAnalysis

class SolarApiData(BaseModel):
    """Datos obtenidos de Google Solar API"""
    source: str = "google_solar_api"
    roof_segments: List[Dict] = []
    solar_potential_kwh_per_year: Optional[float] = None
    carbon_offset_factor_kg_per_mwh: Optional[float] = None
    panel_capacity_watts: Optional[float] = None
    panels_count: Optional[int] = None
    max_array_panels_count: Optional[int] = None
    max_array_area_meters2: Optional[float] = None
    coverage_percent: Optional[float] = None
    attribution_required: bool = True

# ======================== CONSTANTES Y CONFIGURACIÓN ========================
# Base de datos de módulos fotovoltaicos actuales
PV_MODULES = {
    "ja_solar_545": {
        "model": "JA Solar JAM72S30-545/MR",
        "power_stc": 545,  # W
        "area_m2": 2.172,
        "efficiency": 25.1,  # %
        "v_mp": 41.85,  # V
        "i_mp": 13.02,  # A
        "v_oc": 50.15,  # V
        "i_sc": 13.85,  # A
        "temp_coef_power": -0.34,  # %/°C
        "temp_coef_vmp": -0.30,   # %/°C - Coeficiente Vmp
        "temp_coef_voc": -0.135,  # V/°C
        "temp_coef_isc": 0.06,    # %/°C
        "noct": 45,  # °C
        "cells_in_series": 144
    }
}

# Base de datos de inversores
INVERTERS = {
    3.0: {"model": "Huawei SUN2000-3KTL-L1", "efficiency": 98.3, "mppt_min": 90, "mppt_max": 560, "max_dc": 4.5},
    5.0: {"model": "Huawei SUN2000-5KTL-L1", "efficiency": 98.4, "mppt_min": 90, "mppt_max": 560, "max_dc": 7.5},
    6.0: {"model": "Huawei SUN2000-6KTL-L1", "efficiency": 98.4, "mppt_min": 90, "mppt_max": 560, "max_dc": 9.0},
    8.0: {"model": "Huawei SUN2000-8KTL", "efficiency": 98.5, "mppt_min": 200, "mppt_max": 950, "max_dc": 12.0},
    10.0: {"model": "Huawei SUN2000-10KTL-L1", "efficiency": 98.3, "mppt_min": 90, "mppt_max": 560, "max_dc": 15.0},
    12.0: {"model": "Huawei SUN2000-12KTL", "efficiency": 98.5, "mppt_min": 200, "mppt_max": 950, "max_dc": 18.0},
    15.0: {"model": "Huawei SUN2000-15KTL", "efficiency": 98.6, "mppt_min": 200, "mppt_max": 950, "max_dc": 22.5}
}

# Pérdidas del sistema (valores realistas)
SYSTEM_LOSSES = {
    "temperature": 0.10,      # 10% - Pérdidas por temperatura
    "irradiance": 0.03,       # 3% - Pérdidas por baja irradiancia
    "spectral": 0.015,        # 1.5% - Pérdidas espectrales
    "soiling": 0.02,          # 2% - Suciedad
    "shading": 0.03,          # 3% - Sombreado parcial
    "mismatch": 0.02,         # 2% - Desajuste entre módulos
    "ohmic_dc": 0.015,        # 1.5% - Pérdidas óhmicas DC
    "ohmic_ac": 0.01,         # 1% - Pérdidas óhmicas AC
    "inverter": 0.02,         # 2% - Pérdidas del inversor
    "availability": 0.01      # 1% - Disponibilidad del sistema
}

TARGET_YEAR = 2024
LOCAL_TZ = 'Europe/Madrid'

# Precios del sistema (España 2025)
SYSTEM_COSTS = {
    "cost_per_wp": 1.4,       # €/Wp instalado
    "maintenance_annual": 20, # €/kWp/año
    "insurance_annual": 8,    # €/kWp/año
    "degradation_annual": 0.007  # 0.5%/año
}

# ======================== FUNCIONES DE GEOCODIFICACIÓN ========================
async def geocode_address(address: str) -> tuple[float, float]:
    """Geocodifica una dirección usando Google Maps API"""
  
    if not GOOGLE_MAPS_API_KEY:
        raise HTTPException(
            status_code=400,
            detail="Geocodificación no disponible: GOOGLE_MAPS_API_KEY no configurada en .env"
        )
    
    params = {
        'address': address,
        'key': GOOGLE_MAPS_API_KEY,
        'region': 'es',  # Bias hacia España
        'language': 'es'
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(GEOCODING_API_URL, params=params, timeout=10) as response:
                response.raise_for_status()
                data = await response.json()
        
        if data['status'] != 'OK' or not data.get('results'):
            raise HTTPException(
                status_code=400, 
                detail=f"No se pudo geocodificar la dirección: {data.get('status', 'ERROR')}"
            )
        
        location = data['results'][0]['geometry']['location']
        formatted_address = data['results'][0]['formatted_address']
        
        logger.info(f"Dirección geocodificada: '{address}' → '{formatted_address}' ({location['lat']}, {location['lng']})")
        
        return location['lat'], location['lng']
        
    except aiohttp.ClientError as e:
        logger.error(f"Error en geocodificación: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error de geocodificación: {str(e)}")

# ======================== FUNCIONES DE SOLAR API ========================
async def get_building_insights(latitude: float, longitude: float) -> Optional[SolarApiData]:
    """Obtiene datos del edificio desde Google Solar API"""

    if not GOOGLE_MAPS_API_KEY:
        logger.warning("Solar API no disponible: GOOGLE_MAPS_API_KEY no configurada")
        return None

    url = f"{SOLAR_API_BASE_URL}/buildingInsights:findClosest"
    params = {
        'location.latitude': latitude,
        'location.longitude': longitude,
        'requiredQuality': 'MEDIUM',  # MEDIUM, HIGH, or LOW
        'key': GOOGLE_MAPS_API_KEY
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=15) as response:
                if response.status == 404:
                    logger.info(f"No hay cobertura Solar API para {latitude}, {longitude}")
                    return None
                
                response.raise_for_status()
                data = await response.json()
        
        # Procesar respuesta
        solar_data = process_solar_api_response(data)
        logger.info(f"Solar API: encontrados {len(solar_data.roof_segments)} segmentos de tejado")
        
        return solar_data
        
    except aiohttp.ClientError as e:
        logger.warning(f"Error Solar API: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error procesando Solar API: {str(e)}")
        return None

def process_solar_api_response(data: dict) -> SolarApiData:
    """Procesa la respuesta de Solar API y extrae datos relevantes"""
    
    roof_segments = []
    solar_potential = data.get('solarPotential', {})
    
    # Procesar segmentos de tejado
    for segment in solar_potential.get('roofSegmentStats', []):
        # Convertir azimuth de Solar API (0°=Norte, 180°=Sur) al estándar fotovoltaico
        azimuth_solar_api = segment.get('azimuthDegrees', 180)
        azimuth_pv_standard = (azimuth_solar_api + 180) % 360  # 0°=Norte → 180°=Sur
        
        segment_data = {
            'segment_index': segment.get('segmentIndex', 0),
            'tilt_degrees': segment.get('pitchDegrees', 30),
            'azimuth_degrees': azimuth_pv_standard,  # Convertido
            'area_meters2': segment.get('stats', {}).get('areaMeters2', 0),
            'sunlight_quantiles': segment.get('stats', {}).get('sunlightQuantiles', []),
            'panels_count': segment.get('panelsCount', 0)
        }
        roof_segments.append(segment_data)
    
    # Datos del potencial solar
    max_array_area = None
    max_panels = None
    
    if 'maxArrayAreaMeters2' in solar_potential:
        max_array_area = solar_potential['maxArrayAreaMeters2']
    
    if 'maxArrayPanelsCount' in solar_potential:
        max_panels = solar_potential['maxArrayPanelsCount']
    
    return SolarApiData(
        roof_segments=roof_segments,
        solar_potential_kwh_per_year=solar_potential.get('maxSunshineHoursPerYear'),
        carbon_offset_factor_kg_per_mwh=solar_potential.get('carbonOffsetFactorKgPerMwh'),
        panel_capacity_watts=solar_potential.get('panelCapacityWatts', 400),
        panels_count=solar_potential.get('panelsCount'),
        max_array_panels_count=max_panels,
        max_array_area_meters2=max_array_area,
        coverage_percent=None  # Se calculará después
    )

def select_best_roof_segment(solar_data: SolarApiData) -> Optional[dict]:
    """Selecciona el mejor segmento de tejado para la instalación"""
    
    if not solar_data.roof_segments:
        return None
    
    # Criterios de selección: mayor área + mejor orientación (cerca de Sur = 180°)
    best_segment = None
    best_score = -1
    
    for segment in solar_data.roof_segments:
        area = segment.get('area_meters2', 0)
        azimuth = segment.get('azimuth_degrees', 180)
        
        # Penalización por desviación del sur (180°)
        azimuth_deviation = min(abs(azimuth - 180), 360 - abs(azimuth - 180))
        orientation_factor = max(0.3, 1 - azimuth_deviation / 90)  # Máx penalización 70%
        
        # Score combinado
        score = area * orientation_factor
        
        if score > best_score and area > 10:  # Mín 10m²
            best_score = score
            best_segment = segment
    
    logger.info(f"Mejor segmento: {best_segment['area_meters2']:.1f}m², "
                f"{best_segment['tilt_degrees']:.1f}°, {best_segment['azimuth_degrees']:.1f}°")
    
    return best_segment

# ======================== FUNCIONES DE DATOS CLIMÁTICOS ========================
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

# ======================== FUNCIONES DE DIMENSIONADO ========================
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
# ======================== ENDPOINT PRINCIPAL ========================
@app.get("/")
async def root():
    return {"message": "Sistema de Dimensionado Fotovoltaico Profesional", "version": "3.0.0"}

@app.post("/simulate", response_model=SimulateResponse)
async def simulate_pv_system(request: SimulateRequest):
    """Endpoint principal para dimensionado fotovoltaico profesional"""
    
    try:
        logger.info(f"Iniciando simulación para lat={request.lat}, lon={request.lon}")
        
        # Variables para respuesta
        solar_api_data = None
        data_source = "manual"
        attribution_text = None
        
        # =================== FASE 1: DETERMINAR COORDENADAS ===================
        if request.address:
            logger.info(f"Geocodificando dirección: {request.address}")
            lat, lon = await geocode_address(request.address)
            data_source = "geocoded"
        elif request.lat is not None and request.lon is not None:
            lat, lon = request.lat, request.lon
        else:
            raise HTTPException(
                status_code=400, 
                detail="Debe proporcionar 'address' o coordenadas 'lat'/'lon'"
            )
        
        # =================== FASE 2: INTENTAR SOLAR API ===================
        roof_tilt_final = request.roof_tilt
        roof_azimuth_final = request.roof_azimuth  
        roof_area_final = request.roof_area_m2
        installation_type_final = request.installation_type
        
        if roof_tilt_final is not None and roof_azimuth_final is not None and roof_area_final is not None:
            use_solar = False
        else:
            use_solar = request.use_solar_api
        
        if installation_type_final != "optimal" and use_solar and GOOGLE_MAPS_API_KEY:
            logger.info("Consultando Google Solar API...")
            solar_api_data = await get_building_insights(lat, lon)
            
            if solar_api_data:
                data_source = "google_solar_api"
                attribution_text = "Source: Includes solar data from Google"
                
                # Seleccionar mejor segmento de tejado
                best_segment = select_best_roof_segment(solar_api_data)
                
                if best_segment:
                    # Usar datos de Solar API si no se proporcionaron manualmente
                    if roof_tilt_final is None:
                        roof_tilt_final = best_segment['tilt_degrees']
                    if roof_azimuth_final is None:
                        roof_azimuth_final = best_segment['azimuth_degrees']
                    if roof_area_final is None:
                        roof_area_final = best_segment['area_meters2']
                    
                    # Si el tejado es muy plano, cambiar a configuración óptima
                    if roof_tilt_final < 10:
                        installation_type_final = "optimal"
                        logger.info("Tejado plano detectado via Solar API, usando configuración óptima")
                    else:
                        installation_type_final = "coplanar"
                    
                    logger.info(f"Usando datos Solar API: tilt={roof_tilt_final}°, "
                               f"azimuth={roof_azimuth_final}°, area={roof_area_final}m²")
                else:
                    logger.warning("Solar API no encontró segmentos de tejado útiles")
            else:
                logger.info("Solar API sin cobertura, usando método tradicional")
        
        # =================== FASE 3: SIMULACIÓN TRADICIONAL ===================
        # Continuar con la lógica existente usando los valores finales
        logger.info(f"Iniciando simulación para lat={lat}, lon={lon}")
        
        # 1. Obtener datos climáticos
        weather_data, optimal_tilt, optimal_azimuth = get_pvgis_data(lat, lon)
        
        weather_data.index = (
            weather_data.index
            .tz_convert(LOCAL_TZ)
            .map(lambda ts: ts.replace(year=TARGET_YEAR))
        )
        
        # 2. Determinar geometría final
        installation_tilt, installation_azimuth, with_support = determine_installation_geometry(
            lat, roof_tilt_final, roof_azimuth_final, installation_type_final, optimal_tilt
        )
        
        # 3. CALCULAR IRRADIACIÓN PARA GEOMETRÍAS ÓPTIMA Y REAL
        site = location.Location(lat, lon, tz='Europe/Madrid')
        
        # Irradiación óptima
        solar_pos = solarposition.get_solarposition(weather_data.index, lat, lon)
        poa_optimal = irradiance.get_total_irradiance(
            optimal_tilt, 180, solar_pos['apparent_zenith'], solar_pos['azimuth'],
            weather_data['dni'], weather_data['ghi'], weather_data['dhi']
        )
        annual_irradiation_optimal = poa_optimal['poa_global'].sum() / 1000  # kWh/m²
        
        # Irradiación real
        poa_real = irradiance.get_total_irradiance(
            installation_tilt, installation_azimuth, 
            solar_pos['apparent_zenith'], solar_pos['azimuth'],
            weather_data['dni'], weather_data['ghi'], weather_data['dhi']
        )
        annual_irradiation_real = poa_real['poa_global'].sum() / 1000  # kWh/m²
        
        # 4. CALCULAR PÉRDIDAS POR ORIENTACIÓN
        orientation_losses = calculate_orientation_losses(
            lat, installation_tilt, installation_azimuth, optimal_tilt
        )
        shading_losses = (1 - request.shading_factor) * 100
        
        # 5. SELECCIONAR MÓDULO Y ESTIMAR RENDIMIENTO
        module = select_pv_module()
        estimated_specific_yield = annual_irradiation_real * 0.80  # Factor más conservador sin doble descuento
        
        # 6. DIMENSIONAR SISTEMA
        n_modules, system_kwp, total_area = calculate_system_size(
            request.annual_consumption_kwh, request.coverage_percentage,
            estimated_specific_yield, request.roof_area_m2, module
        )
        
        # 7. SELECCIONAR INVERSOR
        inverter = select_inverter(system_kwp)
        
        # 8. CONFIGURAR STRINGS (ahora con temperaturas reales)
        modules_per_string, strings_parallel, total_modules_final = calculate_string_configuration(
            n_modules, module, inverter, weather_data
        )
        
        # Actualizar valores finales
        final_kwp = total_modules_final * module["power_stc"] / 1000
        final_area = total_modules_final * module["area_m2"]
        dc_ac_ratio = final_kwp / inverter["ac_power"]
        
        # 9. SIMULAR PRODUCCIÓN CON PVLIB
        production_results = calculate_pv_production(
            weather_data, lat, lon,
            installation_tilt, installation_azimuth,
            modules_per_string, strings_parallel, module, inverter
        )
        
        # 10. GENERAR PERFIL DE CONSUMO
        consumption_profile = generate_consumption_profile(
            request.annual_consumption_kwh,
            year=TARGET_YEAR,
            tz=LOCAL_TZ
        )
        consumption_profile = consumption_profile[~consumption_profile.index.duplicated(keep="first")]

        # 11. ANALIZAR AUTOCONSUMO
        autoconsumption_results = analyze_autoconsumption(
            production_results['hourly_production'], consumption_profile
        )
        
        # 11.5 AÑADIR ANÁLISIS ECONÓMICO MENSUAL
        enhanced_monthly_analysis = add_economic_analysis_to_monthly(
            autoconsumption_results['monthly_analysis'],
            request.electricity_price, 
            request.surplus_price
        )

        
        # 12. ANÁLISIS ECONÓMICO (actualizar la llamada)
        economic_results = calculate_economics(
            final_kwp, 
            autoconsumption_results['annual_self_consumption'],
            autoconsumption_results['annual_grid_injection'],
            autoconsumption_results['annual_grid_purchase'],
            request.electricity_price, 
            request.surplus_price
        )
        
        # 13. CÁLCULOS ELÉCTRICOS DETALLADOS
        string_voltage_vmp = modules_per_string * module["v_mp"]
        string_voltage_voc = modules_per_string * module["v_oc"]
        array_current_imp = strings_parallel * module["i_mp"]
        max_system_voltage = string_voltage_voc * 1.25  # Factor de seguridad
        
        # Verificar compatibilidad MPPT
        mppt_compatible = (inverter["mppt_min"] <= string_voltage_vmp <= inverter["mppt_max"])
        
        # Pérdidas de cableado (estimadas)
        dc_cable_losses = 1.5  # %
        ac_cable_losses = 1.0  # %
        
        # 14. CONSTRUIR RESPUESTA
        response = SimulateResponse(
            location_info={
                "latitude": lat,
                "longitude": lon,
                "timezone": "Europe/Madrid",
                "data_source": data_source,
                "calculation_date": datetime.now().isoformat()
            },
            
            geometry_analysis=GeometryAnalysis(
                optimal_tilt=optimal_tilt,
                optimal_azimuth=optimal_azimuth,
                installation_tilt=installation_tilt,
                installation_azimuth=installation_azimuth,
                with_support_structure=with_support,
                annual_irradiation_optimal=round(annual_irradiation_optimal, 0),
                annual_irradiation_real=round(annual_irradiation_real, 0),
                orientation_losses_percent=round(orientation_losses, 2),
                shading_losses_percent=round(shading_losses, 2)
            ),
            
            technical_specs=TechnicalSpecs(
                module_model=module["model"],
                module_power_wp=module["power_stc"],
                module_efficiency_percent=round(module["efficiency"], 1),
                module_area_m2=module["area_m2"],
                inverter_model=inverter["model"],
                inverter_power_kw=inverter["ac_power"],
                inverter_efficiency_percent=round(inverter["efficiency"], 1),
                dc_ac_ratio=round(dc_ac_ratio, 2)
            ),
            
            system_config=SystemConfiguration(
                total_modules=total_modules_final,
                modules_per_string=modules_per_string,
                strings_parallel=strings_parallel,
                total_power_kwp=round(final_kwp, 2),
                total_area_m2=round(final_area, 1),
                array_configuration=f"{strings_parallel}S × {modules_per_string}P"
            ),
            
            electrical_analysis=ElectricalAnalysis(
                string_voltage_vmp_v=round(string_voltage_vmp, 1),
                string_voltage_voc_v=round(string_voltage_voc, 1),
                array_current_imp_a=round(array_current_imp, 1),
                max_system_voltage_v=round(max_system_voltage, 1),
                mppt_compatibility=mppt_compatible,
                dc_cable_losses_percent=dc_cable_losses,
                ac_cable_losses_percent=ac_cable_losses
            ),
            
            energy_production=EnergyProduction(
                annual_production_kwh=round(production_results['annual_production'], 0),
                monthly_production=production_results['monthly_production'],
                specific_yield_kwh_kwp=round(production_results['specific_yield'], 0),
                performance_ratio=round(production_results['performance_ratio'], 3),
                capacity_factor_percent=round(production_results['capacity_factor'], 1)
            ),
            
            autoconsumption_analysis=AutoconsumptionAnalysis(
                annual_consumption_kwh=round(autoconsumption_results['annual_consumption'], 0),
                annual_self_consumption_kwh=round(autoconsumption_results['annual_self_consumption'], 0),
                annual_grid_injection_kwh=round(autoconsumption_results['annual_grid_injection'], 0),
                annual_grid_purchase_kwh=round(autoconsumption_results['annual_grid_purchase'], 0),
                self_consumption_rate_percent=round(autoconsumption_results['self_consumption_rate'], 1),
                self_sufficiency_rate_percent=round(autoconsumption_results['self_sufficiency_rate'], 1),
                monthly_analysis=enhanced_monthly_analysis  # ← USAR LA VERSIÓN MEJORADA
            ),
            
            economic_analysis=EconomicAnalysis(
                system_cost_eur=round(economic_results['system_cost'], 0),
                annual_savings_eur=round(economic_results['annual_savings'], 0),
                payback_years=round(economic_results['payback_years'], 1),
                npv_25_years_eur=round(economic_results['npv_25_years'], 0),
                irr_percent=round(economic_results['irr_percent'], 1),
                lcoe_eur_kwh=round(economic_results['lcoe_eur_kwh'], 3)
            )
        )
        
        if solar_api_data:
            response.location_info["google_solar_data"] = {
                "roof_segments_found": len(solar_api_data.roof_segments),
                "max_array_area_m2": solar_api_data.max_array_area_meters2,
                "max_panels_count": solar_api_data.max_array_panels_count,
                "attribution": attribution_text
            }
        
        logger.info(
            f"Ángulos finales usados para simulación: tilt={installation_tilt}°, "
            f"azimuth={installation_azimuth}°"
        )
        logger.info(f"Simulación completada: {total_modules_final} módulos, {final_kwp:.1f} kWp")
        logger.info(f"Producción: {production_results['annual_production']:.0f} kWh/año")
        logger.info(f"Autoconsumo: {autoconsumption_results['self_consumption_rate']:.1f}%")
        logger.info(f"Payback: {economic_results['payback_years']:.1f} años")
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error en simulación: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")

# ======================== ENDPOINTS ADICIONALES ========================
@app.get("/health")
async def health_check():
    """Endpoint de salud del servicio"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.get("/modules")
async def get_available_modules():
    """Devuelve los módulos fotovoltaicos disponibles"""
    return {"modules": PV_MODULES}

@app.get("/inverters")
async def get_available_inverters():
    """Devuelve los inversores disponibles"""
    return {"inverters": INVERTERS}

@app.post("/quick-estimate")
async def quick_estimate(lat: float, lon: float, annual_consumption_kwh: float = 4200):
    """Estimación rápida usando pvlib sin doble descuento"""
    try:
        from pvlib.iotools import get_pvgis_tmy
        
        # Obtener datos básicos
        _, meta = get_pvgis_tmy(lat, lon, outputformat='json', optimalangles=True)
        
        # Extraer parámetros básicos
        optimal_info = meta.get('optimal', {})
        if not optimal_info:
            optimal_info = meta.get('optimalangles', {})
        
        optimal_tilt = optimal_info.get('slope', abs(lat) - 10)
        
        # Obtener irradiación anual directamente
        irradiation_url = "https://re.jrc.ec.europa.eu/api/v5_2/PVcalc"
        params = {
            'lat': lat, 'lon': lon, 'outputformat': 'json',
            'peakpower': 1, 'loss': 14, 'angle': optimal_tilt, 'aspect': 180
        }
        
        response = requests.get(irradiation_url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        # Extraer producción específica (ya incluye pérdidas del 14%)
        annual_production_kwh_kwp = data['outputs']['totals']['fixed']['E_y']
        
        # Estimación del sistema
        estimated_kwp = annual_consumption_kwh * 0.85 / annual_production_kwh_kwp
        estimated_modules = math.ceil(estimated_kwp * 1000 / 545)  # Módulo 545W
        estimated_cost = estimated_kwp * 1000 * 1.0  # 1€/Wp
        
        return {
            "location": {"lat": lat, "lon": lon},
            "optimal_tilt": optimal_tilt,
            "specific_production_kwh_kwp": annual_production_kwh_kwp,
            "estimated_system_kwp": round(estimated_kwp, 1),
            "estimated_modules": estimated_modules,
            "estimated_cost_eur": round(estimated_cost, 0),
            "estimated_annual_production_kwh": round(estimated_kwp * annual_production_kwh_kwp, 0)
        }
        
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en estimación: {str(e)}")

@app.post("/geocode")
async def geocode_endpoint(address: str):
    """Endpoint independiente para geocodificación"""
    try:
        if not GOOGLE_MAPS_API_KEY:
            return {
                "address": address,
                "error": "GOOGLE_MAPS_API_KEY no configurada en .env",
                "success": False
            }
        lat, lon = await geocode_address(address)
        return {
            "address": address,
            "latitude": lat,
            "longitude": lon,
            "success": True
        }
    except HTTPException as e:
        return {
            "address": address,
            "error": e.detail,
            "success": False
        }

@app.post("/solar-insights")
async def solar_insights_endpoint(lat: float, lon: float):
    """Endpoint independiente para Solar API"""
    try:
        if not GOOGLE_MAPS_API_KEY:
            return {
                "latitude": lat,
                "longitude": lon,
                "error": "GOOGLE_MAPS_API_KEY no configurada en .env",
                "success": False
            }

        solar_data = await get_building_insights(lat, lon)
        if solar_data:
            return {
                "latitude": lat,
                "longitude": lon,
                "solar_data": solar_data.dict(),
                "attribution": "Source: Includes solar data from Google",
                "success": True
            }
        else:
            return {
                "latitude": lat,
                "longitude": lon,
                "message": "No hay cobertura Solar API para esta ubicación",
                "success": False
            }
    except Exception as e:
        return {
            "latitude": lat,
            "longitude": lon,
            "error": str(e),
            "success": False
        }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)