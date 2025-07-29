import os

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

FACTOR_CO2_GRID_KG_PER_KWH = 0.35

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
SOLAR_API_BASE_URL = "https://solar.googleapis.com/v1"
GEOCODING_API_URL = "https://maps.googleapis.com/maps/api/geocode/json"