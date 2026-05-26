import os

# Base de datos de modulos
PV_MODULES = {
    "ja_solar_545": {
        "model": "JA Solar JAM72S30-545/MR",
        "power_stc": 545,          # W
        "area_m2": 2.587,          # m² = 2.279 m × 1.135 m
        "efficiency": 21.1,        # %
        "v_mp": 42.38,             # V
        "i_mp": 12.86,             # A
        "v_oc": 50.01,             # V
        "i_sc": 13.62,             # A
        "temp_coef_power": -0.350, # %/°C
        "temp_coef_vmp": -0.30,    # %/°C
        "temp_coef_voc": -0.275,   # %/°C
        "temp_coef_isc": 0.045,    # %/°C
        "noct": 45,                # °C
        "cells_in_series": 144,
        "maximum_series_fuse_rating_a": 25 # A
    }
}

# Base de datos de inversores
INVERTERS = {
    3.0: {
        "model": "Huawei SUN2000-3KTL-L1",
        "efficiency": 98.3,
        "mppt_min": 90,
        "mppt_max": 560,
        "max_dc": 4.5,
        "recommended_max_pv_power_kw": 4.5,
        "max_dc_voltage_v": 600,
        "max_input_current_per_mppt_a": 12.5,
        "max_short_circuit_current_per_mppt_a": 18.0,
        "mppt_count": 2,
        "strings_per_mppt": 1
    },
    3.1: {
        "model": "GoodWe GW3000-DNS-30",
        "efficiency": 97.9,
        "mppt_min": 40,
        "mppt_max": 560,
        "max_dc": 4.5,
        "recommended_max_pv_power_kw": 4.5,
        "max_dc_voltage_v": 600,
        "max_input_current_per_mppt_a": 16.0,
        "max_short_circuit_current_per_mppt_a": 23.0,
        "mppt_count": 2,
        "strings_per_mppt": 1,
        "nominal_ac_power_kw": 3.0
    },
    5.0: {
        "model": "Huawei SUN2000-5KTL-L1",
        "efficiency": 98.4,
        "mppt_min": 90,
        "mppt_max": 560,
        "max_dc": 7.5,
        "recommended_max_pv_power_kw": 7.5,
        "max_dc_voltage_v": 600,
        "max_input_current_per_mppt_a": 12.5,
        "max_short_circuit_current_per_mppt_a": 18.0,
        "mppt_count": 2,
        "strings_per_mppt": 1
    },
    6.0: {
        "model": "Huawei SUN2000-6KTL-L1",
        "efficiency": 98.4,
        "mppt_min": 90,
        "mppt_max": 560,
        "max_dc": 9.0,
        "recommended_max_pv_power_kw": 9.0,
        "max_dc_voltage_v": 600,
        "max_input_current_per_mppt_a": 12.5,
        "max_short_circuit_current_per_mppt_a": 18.0,
        "mppt_count": 2,
        "strings_per_mppt": 1
    },
    8.0: {
        "model": "Huawei SUN2000-8KTL-M1",
        "efficiency": 98.6,
        "mppt_min": 140,
        "mppt_max": 980,
        "max_dc": 12.0,
        "recommended_max_pv_power_kw": 12.0,
        "max_dc_voltage_v": 1100,
        "max_input_current_per_mppt_a": 11.0,
        "max_short_circuit_current_per_mppt_a": 15.0,
        "mppt_count": 2,
        "strings_per_mppt": 1
    },
    10.0: {
        "model": "Huawei SUN2000-10KTL-M1",
        "efficiency": 98.6,
        "mppt_min": 140,
        "mppt_max": 980,
        "max_dc": 15.0,
        "recommended_max_pv_power_kw": 15.0,
        "max_dc_voltage_v": 1100,
        "max_input_current_per_mppt_a": 11.0,
        "max_short_circuit_current_per_mppt_a": 15.0,
        "mppt_count": 2,
        "strings_per_mppt": 1
    },
    12.0: {
        "model": "Huawei SUN2000-12KTL-M2",
        "efficiency": 98.5,
        "mppt_min": 160,
        "mppt_max": 950,
        "max_dc": 18.0,
        "recommended_max_pv_power_kw": 18.0,
        "max_dc_voltage_v": 1080,
        "max_input_current_per_mppt_a": 22.0,
        "max_short_circuit_current_per_mppt_a": 30.0,
        "mppt_count": 2,
        "strings_per_mppt": 2
    },
    15.0: {
        "model": "Huawei SUN2000-15KTL-M2",
        "efficiency": 98.65,
        "mppt_min": 160,
        "mppt_max": 950,
        "max_dc": 22.5,
        "recommended_max_pv_power_kw": 22.5,
        "max_dc_voltage_v": 1080,
        "max_input_current_per_mppt_a": 22.0,
        "max_short_circuit_current_per_mppt_a": 30.0,
        "mppt_count": 2,
        "strings_per_mppt": 2
    }
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

# Precios del sistema (España 2025)
SYSTEM_COSTS = {
    "cost_per_wp": 1.4,       # €/Wp instalado
    "maintenance_annual": 20, # €/kWp/año
    "insurance_annual": 8,    # €/kWp/año
    "degradation_annual": 0.007  # 0.7%/año
}

# Tabla simplificada (puedes ampliar más si lo necesitas)
CABLE_AMPACITY_TABLE = {
    # (metodo, aislamiento, n_cond): {seccion_mm2: intensidad_admisible_A}
    ('A1', 'PVC', 2): {1.5: 14, 2.5: 18, 4: 24, 6: 31, 10: 42, 16: 57, 25: 76, 35: 96},
    ('A1', 'XLPE', 2): {1.5: 16, 2.5: 21, 4: 28, 6: 36, 10: 50, 16: 68, 25: 91, 35: 115},

    ('A2', 'PVC', 2): {1.5: 15, 2.5: 19, 4: 25, 6: 32, 10: 43, 16: 58, 25: 77, 35: 97},
    ('A2', 'XLPE', 2): {1.5: 17, 2.5: 22, 4: 29, 6: 37, 10: 51, 16: 70, 25: 93, 35: 117},

    ('B1', 'PVC', 2): {1.5: 16, 2.5: 21, 4: 28, 6: 36, 10: 50, 16: 68, 25: 89, 35: 112},
    ('B1', 'XLPE', 2): {1.5: 18, 2.5: 23, 4: 31, 6: 39, 10: 54, 16: 74, 25: 97, 35: 122},

    ('B2', 'PVC', 2): {1.5: 16, 2.5: 21, 4: 28, 6: 36, 10: 50, 16: 68, 25: 89, 35: 112},
    ('B2', 'XLPE', 2): {1.5: 18, 2.5: 23, 4: 31, 6: 39, 10: 54, 16: 74, 25: 97, 35: 122},

    ('C', 'PVC', 2): {1.5: 18, 2.5: 24, 4: 32, 6: 41, 10: 57, 16: 76, 25: 101, 35: 127},
    ('C', 'XLPE', 2): {1.5: 21, 2.5: 27, 4: 36, 6: 46, 10: 65, 16: 88, 25: 117, 35: 147},

    ('D', 'PVC', 2): {1.5: 16, 2.5: 21, 4: 28, 6: 36, 10: 50, 16: 68, 25: 89, 35: 112},
    ('D', 'XLPE', 2): {1.5: 18, 2.5: 23, 4: 31, 6: 39, 10: 54, 16: 74, 25: 97, 35: 122},

    ('E', 'PVC', 2): {1.5: 21, 2.5: 28, 4: 37, 6: 47, 10: 65, 16: 88, 25: 117, 35: 147},
    ('E', 'XLPE', 2): {1.5: 24, 2.5: 32, 4: 42, 6: 54, 10: 75, 16: 101, 25: 134, 35: 168},

    ('F', 'PVC', 2): {1.5: 23, 2.5: 30, 4: 40, 6: 51, 10: 70, 16: 96, 25: 127, 35: 159},
    ('F', 'XLPE', 2): {1.5: 27, 2.5: 36, 4: 47, 6: 61, 10: 87, 16: 119, 25: 158, 35: 197},
}
STANDARD_SECTIONS_MM2 = [1.5, 2.5, 4, 6, 10, 16, 25, 35, 50, 70, 95, 120, 150, 185, 240, 300]

STANDARD_GPV_FUSES_A = [10, 12, 15, 16, 20, 25, 32, 40, 50, 63]
STANDARD_AC_BREAKERS_A = [6, 10, 16, 20, 25, 32, 40, 50, 63]
MIN_RECOMMENDED_AC_SECTION_MM2 = 2.5
MIN_RECOMMENDED_DC_SECTION_MM2 = 2.5

RESISTIVITY = {
    'Cu': 0.01786,  # Ω·mm²/m (Copper)
    'Al': 0.02826,  # Ω·mm²/m (Aluminum)
}

TARGET_YEAR = 2024
LOCAL_TZ = 'Europe/Madrid'
FACTOR_CO2_GRID_KG_PER_KWH = 0.28

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
SOLAR_API_BASE_URL = "https://solar.googleapis.com/v1"
GEOCODING_API_URL = "https://maps.googleapis.com/maps/api/geocode/json"
APPS_SCRIPT_WEBAPP_URL = os.getenv("APPS_SCRIPT_WEBAPP_URL")