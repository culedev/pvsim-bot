from pydantic import BaseModel, Field
from typing import Optional, List, Dict

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
    pass

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