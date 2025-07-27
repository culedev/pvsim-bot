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

class MonthlyData(BaseModel):
    month: int
    energy_kwh: float

class SimulateResponse(BaseModel):
    # Dimensionado
    n_modules: int
    kwp: float
    kwp_dc: float
    inverter_power_kw: float
    
    # Producción anual
    energy_kwh_year: float
    specific_yield: float  # kWh/kWp
    performance_ratio: float
    
    # Datos mensuales
    monthly: List[MonthlyData]
    
    # Detalles técnicos
    dc_ac_ratio: float
    module_area_total_m2: float
    
    # Metadatos
    location_info: Dict[str, float]
    calculation_timestamp: str

# Constantes del sistema (mock data como solicitado)
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
    "lid": 0.015,         # 1.5% Light Induced Degradation
    "nameplate": 0.01,    # 1% tolerancia nameplate
    "availability": 0.02  # 2% disponibilidad
}

DC_AC_RATIO_TARGET = 1.15

def get_pvgis_tmy_data(lat: float, lon: float) -> pd.DataFrame:
    """
    Descarga datos TMY (Typical Meteorological Year) desde PVGIS para Europa.
    """
    try:
        # URL para PVGIS TMY API (Europa)
        url = "https://re.jrc.ec.europa.eu/api/v5_2/tmy"
        
        params = {
            'lat': lat,
            'lon': lon,
            'outputformat': 'json',
            'usehorizon': 1,
            'userhorizon': '',
            'startyear': 2005,
            'endyear': 2016
        }
        
        logger.info(f"Descargando datos TMY para lat={lat}, lon={lon}")
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        
        # Extraer datos horarios
        hourly_data = data['outputs']['tmy_hourly']
        
        # Crear DataFrame
        df = pd.DataFrame(hourly_data)
        
        # Convertir timestamp a datetime
        df.index = pd.to_datetime(df['time(UTC)'], format='%Y%m%d:%H%M')
        
        # Renombrar columnas para pvlib
        df = df.rename(columns={
            'G(h)': 'ghi',      # Global Horizontal Irradiance
            'Gb(n)': 'dni',     # Direct Normal Irradiance
            'Gd(h)': 'dhi',     # Diffuse Horizontal Irradiance
            'T2m': 'temp_air',  # Temperatura del aire
            'WS10m': 'wind_speed', # Velocidad del viento
            'RH': 'relative_humidity'
        })
        
        # Convertir a tipos numéricos
        numeric_cols = ['ghi', 'dni', 'dhi', 'temp_air', 'wind_speed', 'relative_humidity']
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        
        # Filtrar valores válidos
        df = df.dropna(subset=['ghi', 'dni', 'dhi', 'temp_air'])
        
        logger.info(f"Datos TMY descargados: {len(df)} registros")
        return df
        
    except Exception as e:
        logger.error(f"Error descargando datos PVGIS: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error obteniendo datos climáticos: {str(e)}")

def calculate_optimal_tilt(latitude: float) -> float:
    """
    Calcula la inclinación óptima basada en la latitud.
    Para España, regla empírica: tilt = lat - 10° (con límites)
    """
    optimal_tilt = abs(latitude) - 10
    return max(15, min(optimal_tilt, 45))  # Límites prácticos 15-45°

def calculate_system_size(roof_area_m2: Optional[float], kwp_target: Optional[float]) -> tuple:
    """
    Determina el número de módulos basado en área disponible o potencia objetivo.
    Retorna: (n_modules, kwp_dc, total_area_used)
    """
    module_power_kw = MODULE_SPECS["power_stc"] / 1000
    module_area = MODULE_SPECS["area_m2"]
    
    if kwp_target:
        # Dimensionar por potencia objetivo
        n_modules = int(kwp_target / module_power_kw)
        kwp_dc = n_modules * module_power_kw
        total_area = n_modules * module_area
        logger.info(f"Dimensionado por potencia: {n_modules} módulos, {kwp_dc:.2f} kWp")
        
    elif roof_area_m2:
        # Dimensionar por área disponible
        n_modules = int(roof_area_m2 / module_area)
        kwp_dc = n_modules * module_power_kw
        total_area = n_modules * module_area
        logger.info(f"Dimensionado por área: {n_modules} módulos, {kwp_dc:.2f} kWp")
        
    else:
        raise HTTPException(status_code=400, detail="Debe especificar roof_area_m2 o kwp_target")
    
    if n_modules <= 0:
        raise HTTPException(status_code=400, detail="No es posible instalar módulos con los parámetros dados")
    
    return n_modules, kwp_dc, total_area

def get_inverter_power(kwp_dc: float) -> float:
    """
    Selecciona potencia del inversor basada en ratio DC/AC objetivo.
    Mock de catálogo de inversores comunes.
    """
    target_ac_power = kwp_dc / DC_AC_RATIO_TARGET
    
    # Potencias estándar de inversores (kW)
    standard_powers = [3, 5, 6, 8, 10, 12, 15, 17, 20, 25, 30, 40, 50, 60, 75, 100]
    
    # Seleccionar el inversor más cercano (ligeramente superior)
    for power in standard_powers:
        if power >= target_ac_power:
            return power
    
    # Si es muy grande, usar múltiplos de 100kW
    return int(target_ac_power / 100 + 1) * 100

def simulate_pv_system(weather_data: pd.DataFrame, lat: float, lon: float, 
                      n_modules: int, kwp_dc: float) -> Dict:
    """
    Simula el sistema fotovoltaico usando pvlib con cálculos detallados.
    """
    try:
        # Crear ubicación
        site = location.Location(lat, lon, tz='Europe/Madrid', altitude=100)
        
        # Calcular inclinación óptima
        tilt = calculate_optimal_tilt(lat)
        azimuth = 180  # Sur
        
        logger.info(f"Configuración: tilt={tilt}°, azimuth={azimuth}°")
        
        # Parámetros del módulo (modelo CEC - California Energy Commission)
        module_params = {
            'pdc0': MODULE_SPECS["power_stc"],  # Potencia en STC (W)
            'v_mp': MODULE_SPECS["v_mp"],       # Voltaje en MPP (V)
            'i_mp': MODULE_SPECS["i_mp"],       # Corriente en MPP (A)
            'v_oc': MODULE_SPECS["v_oc"],       # Voltaje circuito abierto (V)
            'i_sc': MODULE_SPECS["i_sc"],       # Corriente cortocircuito (A)
            'alpha_sc': MODULE_SPECS["alpha_sc"], # Coef. temp. Isc (A/°C)
            'beta_voc': MODULE_SPECS["beta_voc"], # Coef. temp. Voc (V/°C)
            'gamma_pdc': MODULE_SPECS["gamma_pmp"] / 100, # Coef. temp. potencia (%/°C -> decimal)
            'cells_in_series': MODULE_SPECS["cells_in_series"]
        }
        
        # Parámetros de temperatura (modelo PVSYST)
        temperature_params = {
            'u_c': 29.0,      # Coeficiente de pérdida de calor convectivo (W/m²/°C)
            'u_v': 0.0,       # Coeficiente de pérdida de calor por viento (W/m²/°C/m/s)
            'eta_m': 0.1,     # Eficiencia del módulo (decimal)
            'alpha_absorption': 0.9  # Coeficiente de absorción
        }
        
        # Potencia del inversor
        inverter_power = get_inverter_power(kwp_dc)
        
        # Parámetros del inversor (modelo genérico)
        inverter_params = {
            'pdc0': inverter_power * 1000,      # Potencia DC nominal (W)
            'pac0': inverter_power * 1000 * 0.96, # Potencia AC nominal (W) - eficiencia 96%
            'eta_inv_nom': 0.96,                 # Eficiencia nominal
            'eta_inv_ref': 0.9637               # Eficiencia de referencia
        }
        
        # Crear array fotovoltaico
        array = pvsystem.Array(
            mount=pvsystem.FixedMount(surface_tilt=tilt, surface_azimuth=azimuth),
            module_parameters=module_params,
            temperature_model_parameters=temperature_params,
            modules_per_string=int(np.sqrt(n_modules)),
            strings=int(n_modules / int(np.sqrt(n_modules)))
        )
        
        # Sistema fotovoltaico
        system = pvsystem.PVSystem(arrays=[array], inverter_parameters=inverter_params)
        
        # Crear modelo de cadena con parámetros específicos
        mc = modelchain.ModelChain(
            system, site,
            aoi_model='physical',            # Modelo AOI físico
            spectral_model='no_loss',        # Sin pérdidas espectrales
            temperature_model='pvsyst',      # Modelo de temperatura PVSYST (más simple)
            losses_model='pvwatts'           # Modelo de pérdidas PVWatts
        )
        
        # Ejecutar simulación
        logger.info("Ejecutando simulación con pvlib...")
        mc.run_model(weather_data)
        
        # Aplicar pérdidas adicionales del sistema
        total_losses = 1.0
        for loss_name, loss_value in SYSTEM_LOSSES.items():
            total_losses *= (1 - loss_value)
        
        logger.info(f"Pérdidas totales del sistema: {(1-total_losses)*100:.1f}%")
        
        # Potencia AC con pérdidas
        ac_power_with_losses = mc.results.ac * total_losses
        
        # Energía anual (kWh)
        annual_energy = ac_power_with_losses.sum() / 1000  # W to kWh
        
        # Cálculos de rendimiento
        specific_yield = annual_energy / kwp_dc  # kWh/kWp
        
        # Performance Ratio (PR)
        # PR = Energía real / Energía teórica en STC
        ghi_sum = weather_data['ghi'].sum() / 1000  # kWh/m²
        theoretical_energy = kwp_dc * ghi_sum / 1  # kWh (1 kW/m² STC)
        performance_ratio = annual_energy / theoretical_energy if theoretical_energy > 0 else 0
        
        # Datos mensuales
        monthly_energy = ac_power_with_losses.resample('M').sum() / 1000
        monthly_data = []
        for month, energy in enumerate(monthly_energy, 1):
            monthly_data.append({
                "month": month,
                "energy_kwh": float(energy)
            })
        
        logger.info(f"Simulación completada: {annual_energy:.0f} kWh/año, PR={performance_ratio:.3f}")
        
        return {
            "annual_energy": float(annual_energy),
            "specific_yield": float(specific_yield),
            "performance_ratio": float(performance_ratio),
            "monthly_data": monthly_data,
            "inverter_power": inverter_power,
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
    """
    Endpoint principal para simulación de sistema fotovoltaico.
    Incluye descarga de datos climáticos, dimensionado y cálculos detallados.
    """
    try:
        logger.info(f"Iniciando simulación para lat={request.lat}, lon={request.lon}")
        
        # 1. Obtener datos climáticos TMY
        weather_data = get_pvgis_tmy_data(request.lat, request.lon)
        
        # 2. Dimensionar sistema
        n_modules, kwp_dc, total_area = calculate_system_size(
            request.roof_area_m2, request.kwp_target
        )
        
        # 3. Simular sistema fotovoltaico
        simulation_results = simulate_pv_system(
            weather_data, request.lat, request.lon, n_modules, kwp_dc
        )
        
        # 4. Preparar respuesta
        dc_ac_ratio = kwp_dc / simulation_results["inverter_power"]
        
        response = SimulateResponse(
            # Dimensionado
            n_modules=n_modules,
            kwp=simulation_results["inverter_power"],  # Potencia AC nominal
            kwp_dc=kwp_dc,
            inverter_power_kw=simulation_results["inverter_power"],
            
            # Producción anual
            energy_kwh_year=simulation_results["annual_energy"],
            specific_yield=simulation_results["specific_yield"],
            performance_ratio=simulation_results["performance_ratio"],
            
            # Datos mensuales
            monthly=[MonthlyData(**item) for item in simulation_results["monthly_data"]],
            
            # Detalles técnicos
            dc_ac_ratio=dc_ac_ratio,
            module_area_total_m2=total_area,
            
            # Metadatos
            location_info={
                "latitude": request.lat,
                "longitude": request.lon,
                "tilt": simulation_results["tilt"],
                "azimuth": simulation_results["azimuth"]
            },
            calculation_timestamp=datetime.now().isoformat()
        )
        
        logger.info("Simulación completada exitosamente")
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error inesperado: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error interno del servidor: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)