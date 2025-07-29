from fastapi import APIRouter, HTTPException
from datetime import datetime
import requests
from pvlib import location, modelchain, pvsystem, temperature, solarposition, irradiance
import traceback
import math
from app.models import *
from app.geocoding import geocode_address
from app.solar_api import get_building_insights, select_best_roof_segment
from app.simulation import *
from app.constants import GOOGLE_MAPS_API_KEY, PV_MODULES, INVERTERS

import logging
logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/")
async def root():
    return {"message": "Sistema de Dimensionado Fotovoltaico Profesional", "version": "3.0.0"}

@router.post("/simulate", response_model=SimulateResponse)
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
@router.get("/health")
async def health_check():
    """Endpoint de salud del servicio"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@router.get("/modules")
async def get_available_modules():
    """Devuelve los módulos fotovoltaicos disponibles"""
    return {"modules": PV_MODULES}

@router.get("/inverters")
async def get_available_inverters():
    """Devuelve los inversores disponibles"""
    return {"inverters": INVERTERS}

@router.post("/quick-estimate")
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

@router.post("/geocode")
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

@router.post("/solar-insights")
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
