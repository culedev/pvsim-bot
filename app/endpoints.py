from fastapi import APIRouter, HTTPException
from datetime import datetime
import requests
from pvlib import location, modelchain, pvsystem, temperature, solarposition, irradiance
import traceback
import math
import aiohttp
from app.models import *
from app.simulation import *
from app.geocoding import geocode_address
from app.solar_api import get_building_insights, select_best_roof_segment
from app.constants import (
    GOOGLE_MAPS_API_KEY,
    PV_MODULES,
    INVERTERS,
    FACTOR_CO2_GRID_KG_PER_KWH,
    APPS_SCRIPT_WEBAPP_URL,
    STANDARD_GPV_FUSES_A,
    STANDARD_AC_BREAKERS_A,
    MIN_RECOMMENDED_AC_SECTION_MM2,
    CABLE_AMPACITY_TABLE,
    MIN_RECOMMENDED_DC_SECTION_MM2
)

import logging
logger = logging.getLogger(__name__)

router = APIRouter()

async def call_google_apps_script_report(payload: dict) -> dict:
    """
    Envía el JSON de simulación al Web App de Google Apps Script
    para generar Google Sheet, Google Doc, CSV y PDF.
    """
    if not APPS_SCRIPT_WEBAPP_URL:
        raise HTTPException(
            status_code=500,
            detail="APPS_SCRIPT_WEBAPP_URL no está configurada en el entorno"
        )

    timeout = aiohttp.ClientTimeout(total=180)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                APPS_SCRIPT_WEBAPP_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                text = await response.text()

                if response.status >= 400:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Google Apps Script respondió con HTTP {response.status}: {text}"
                    )

                try:
                    result = await response.json(content_type=None)
                except Exception:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Google Apps Script no devolvió JSON válido: {text}"
                    )

                if not result.get("success"):
                    raise HTTPException(
                        status_code=502,
                        detail=f"Error generando documentos: {result.get('error', 'Error desconocido')}"
                    )

                return result

    except HTTPException:
        raise
    except aiohttp.ClientError as e:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo conectar con Google Apps Script: {str(e)}"
        )

@router.get("/")
async def root():
    """Mensaje de bienvenida y versión de la API."""
    logger.debug("Acceso a endpoint raíz '/'")
    return {"message": "Sistema de Dimensionado Fotovoltaico Profesional", "version": "3.0.0"}

@router.post("/simulate", response_model=SimulateResponse)
async def simulate_pv_system(request: SimulateRequest):
    """
    Endpoint principal para dimensionado profesional fotovoltaico.
    Orquesta todo el flujo: geocodificación, obtención de clima, cálculo de geometría,
    simulación de producción, análisis económico y construcción de respuesta.
    """
    try:
        logger.info(f"🟢 Nueva simulación: Entrada lat={request.lat}, lon={request.lon}, address='{request.address}'")
        
        # --- Variables de control de flujo ---
        solar_api_data = None
        data_source = "manual"
        attribution_text = None

        # ========== FASE 1: DETERMINAR COORDENADAS ==========
        if request.address:
            logger.info(f"🟢 Geocodificando dirección: '{request.address}'")
            lat, lon = await geocode_address(request.address)
            data_source = "geocoded"
        elif request.lat is not None and request.lon is not None:
            lat, lon = request.lat, request.lon
        else:
            logger.error("❌ Entrada incompleta: falta 'address' o 'lat/lon'")
            raise HTTPException(
                status_code=400, 
                detail="Debe proporcionar 'address' o coordenadas 'lat'/'lon'"
            )

        # ========== FASE 2: INTENTAR OBTENER DATOS DE SOLAR API ==========
        roof_tilt_final = request.roof_tilt
        roof_azimuth_final = request.roof_azimuth  
        roof_area_final = request.roof_area_m2
        installation_type_final = request.installation_type

        if roof_tilt_final is not None and roof_azimuth_final is not None and roof_area_final is not None:
            use_solar = False
        else:
            use_solar = request.use_solar_api

        if installation_type_final != "optimal" and use_solar and GOOGLE_MAPS_API_KEY:
            logger.info("🟢 Consultando Google Solar API para tejado y ángulos óptimos...")
            solar_api_data = await get_building_insights(lat, lon)

            if solar_api_data:
                data_source = "google_solar_api"
                attribution_text = "Source: Includes solar data from Google"
                best_segment = select_best_roof_segment(solar_api_data)
                if best_segment:
                    # Usar datos sugeridos por Solar API si faltan en la petición
                    logger.info(
                        f"⚡️ Mejor segmento: tilt={best_segment['tilt_degrees']}°, "
                        f"azimuth={best_segment['azimuth_degrees']}°, área={best_segment['area_meters2']}m²"
                    )
                    if roof_tilt_final is None:
                        roof_tilt_final = best_segment['tilt_degrees']
                    if roof_azimuth_final is None:
                        roof_azimuth_final = best_segment['azimuth_degrees']
                    if roof_area_final is None:
                        roof_area_final = best_segment['area_meters2']

                    if roof_tilt_final < 10:
                        installation_type_final = "optimal"
                        logger.info("⚡️ Tejado plano detectado (Solar API): Se fuerza configuración óptima")
                    else:
                        installation_type_final = "coplanar"
                else:
                    logger.warning("⚠️ Solar API sin segmentos útiles: se usará método tradicional")
            else:
                logger.info("⚠️ Solar API sin cobertura, simulación tradicional continua")

        # ========== FASE 3: SIMULACIÓN CON PVLIB Y LÓGICA CLÁSICA ==========
        logger.info(f"⚡️ Descargando datos meteorológicos (PVGIS) para lat={lat}, lon={lon}")
        weather_data, optimal_tilt, optimal_azimuth = get_pvgis_data(lat, lon)

        # Ajustar año a TARGET_YEAR y zona horaria (puede haber duplicados DST)
        weather_data.index = (
            weather_data.index
            .tz_convert(LOCAL_TZ)
            .map(lambda ts: ts.replace(year=TARGET_YEAR))
        )

        logger.debug(f"⚡️ Geometría final: roof_tilt={roof_tilt_final}, roof_azimuth={roof_azimuth_final}, tipo={installation_type_final}")
        installation_tilt, installation_azimuth, with_support = determine_installation_geometry(
            lat, roof_tilt_final, roof_azimuth_final, installation_type_final, optimal_tilt
        )

        # --- Cálculo de irradiaciones anual óptima y real ---
        site = location.Location(lat, lon, tz='Europe/Madrid')
        solar_pos = solarposition.get_solarposition(weather_data.index, lat, lon)
        poa_optimal = irradiance.get_total_irradiance(
            optimal_tilt, 180, solar_pos['apparent_zenith'], solar_pos['azimuth'],
            weather_data['dni'], weather_data['ghi'], weather_data['dhi']
        )
        annual_irradiation_optimal = poa_optimal['poa_global'].sum() / 1000
        poa_real = irradiance.get_total_irradiance(
            installation_tilt, installation_azimuth,
            solar_pos['apparent_zenith'], solar_pos['azimuth'],
            weather_data['dni'], weather_data['ghi'], weather_data['dhi']
        )
        annual_irradiation_real = poa_real['poa_global'].sum() / 1000

        orientation_losses = calculate_orientation_losses(
            lat, installation_tilt, installation_azimuth, optimal_tilt
        )
        shading_losses = (1 - request.shading_factor) * 100

        # --- Selección y dimensionado inicial del sistema ---
        module = select_pv_module()
        estimated_specific_yield = annual_irradiation_real * 0.80
        logger.info(f"⚡️ Irradiación óptima: {annual_irradiation_optimal:.0f} kWh/m² | Irradiación real: {annual_irradiation_real:.0f} kWh/m²")
        logger.info(f"⚡️ Pérdidas por orientación: {orientation_losses:.2f}% | Sombreado: {shading_losses:.2f}%")

        n_modules, system_kwp, total_area = calculate_system_size(
            request.annual_consumption_kwh, request.coverage_percentage,
            estimated_specific_yield, request.roof_area_m2, module
        )
        logger.info(f"⚡️ Dimensionado inicial: {n_modules} módulos, {system_kwp:.2f} kWp, {total_area:.1f} m²")

        inverter = select_inverter(system_kwp, module)
        string_config = calculate_string_configuration(
            n_modules, module, inverter, weather_data
        )

        modules_per_string = string_config["modules_per_string"]
        strings_parallel = string_config["strings_parallel"]
        total_modules_final = string_config["total_modules"]
        
        # --- Cálculo eléctrico de strings ---
        string_voltage_vmp = modules_per_string * module["v_mp"]
        string_voltage_voc_stc = modules_per_string * module["v_oc"]
        string_voltage_voc_cold = modules_per_string * string_config["v_oc_cold_module_v"]

        array_current_imp = strings_parallel * module["i_mp"]
        array_current_isc_stc = strings_parallel * module["i_sc"]

        # Corrección conservadora de Isc con temperatura de célula máxima.
        # Isc aumenta ligeramente con la temperatura.
        array_current_isc_corrected = strings_parallel * module["i_sc"] * (
            1 + module["temp_coef_isc"] / 100 * (string_config["t_cell_max_c"] - 25)
        )

        strings_per_mppt_used = math.ceil(strings_parallel / inverter["mppt_count"])

        current_imp_per_mppt = strings_per_mppt_used * module["i_mp"]
        current_isc_per_mppt = strings_per_mppt_used * module["i_sc"] * (
            1 + module["temp_coef_isc"] / 100 * (string_config["t_cell_max_c"] - 25)
        )

        mppt_voltage_compatible = inverter["mppt_min"] <= string_voltage_vmp <= inverter["mppt_max"]
        dc_voltage_compatible = string_voltage_voc_cold <= inverter["max_dc_voltage_v"]

        mppt_current_compatible = (
            current_imp_per_mppt <= inverter["max_input_current_per_mppt_a"]
        )

        mppt_short_circuit_current_compatible = (
            current_isc_per_mppt <= inverter["max_short_circuit_current_per_mppt_a"]
        )

        dc_input_compatible = (
            mppt_voltage_compatible
            and dc_voltage_compatible
            and mppt_current_compatible
            and mppt_short_circuit_current_compatible
        )
        
        if not dc_input_compatible:
            raise HTTPException(
                status_code=422,
                detail=(
                    "La configuración eléctrica calculada no es compatible con la entrada DC del inversor. "
                    f"Imp/MPPT={current_imp_per_mppt:.2f} A, "
                    f"Isc/MPPT={current_isc_per_mppt:.2f} A, "
                    f"límite Imp={inverter['max_input_current_per_mppt_a']:.2f} A, "
                    f"límite Isc={inverter['max_short_circuit_current_per_mppt_a']:.2f} A, "
                    f"configuración={modules_per_string}S{strings_parallel}P"
                )
            )
        
        # --- Cálculo profesional de secciones según UNE ---
        cable_section_results = analyze_cable_sections(
            current_dc_a=array_current_imp,
            length_dc_m=request.cable_length_dc_m,
            v_dc=string_voltage_vmp,
            current_ac_a=inverter["ac_power"] * 1000 / 230,
            length_ac_m=request.cable_length_ac_m,
            v_ac=230,
            method_dc="C", insulation_dc="PVC", n_cond_dc=2,
            method_ac="C", insulation_ac="PVC", n_cond_ac=2,
            allowed_vdrop_pct=1.5,
            material="Cu"
        )

        # --- Usar secciones recomendadas para cálculo de pérdidas ---
        calculated_dc_mm2 = cable_section_results['cable_dc']['recommended_section_mm2'] or 6
        recommended_dc_mm2 = max(calculated_dc_mm2, MIN_RECOMMENDED_DC_SECTION_MM2)

        recommended_dc_ampacity_a = CABLE_AMPACITY_TABLE.get(
            ("C", "PVC", 2),
            {}
        ).get(recommended_dc_mm2)

        calculated_ac_mm2 = cable_section_results['cable_ac']['recommended_section_mm2'] or 10
        recommended_ac_mm2 = max(calculated_ac_mm2, MIN_RECOMMENDED_AC_SECTION_MM2)

        recommended_ac_ampacity_a = CABLE_AMPACITY_TABLE.get(
            ("C", "PVC", 2),
            {}
        ).get(recommended_ac_mm2)

        cable_results = calculate_cable_losses(
            modules_per_string=modules_per_string,
            strings_parallel=strings_parallel,
            module_v_mp=module["v_mp"],
            module_i_mp=module["i_mp"],
            inverter_ac_power_kw=inverter["ac_power"],
            cable_length_dc_m=request.cable_length_dc_m,
            cable_section_dc_mm2=recommended_dc_mm2,
            cable_length_ac_m=request.cable_length_ac_m,
            cable_section_ac_mm2=recommended_ac_mm2
        )

        dc_cable_losses = cable_results['power_loss_dc_percent']
        ac_cable_losses = cable_results['power_loss_ac_percent']

        # Actualizar totales según configuración eléctrica final
        final_kwp = total_modules_final * module["power_stc"] / 1000
        final_area = total_modules_final * module["area_m2"]
        dc_ac_ratio = final_kwp / inverter["ac_power"]

        # --- Simulación de producción horaria anual ---
        production_results = calculate_pv_production(
            weather_data, lat, lon,
            installation_tilt, installation_azimuth,
            modules_per_string, strings_parallel, module, inverter, shading_factor=request.shading_factor
        )
        
        total_cable_losses_pct = (dc_cable_losses + ac_cable_losses) / 100
        adjusted_annual_production = production_results['annual_production'] * (1 - total_cable_losses_pct)
        adjusted_hourly_production = production_results['hourly_production'] * (1 - total_cable_losses_pct)

        adjusted_monthly_production = []
        for month in range(1, 13):
            month_mask = adjusted_hourly_production.index.month == month
            adjusted_monthly_production.append(
                round(float(adjusted_hourly_production[month_mask].sum()), 1)
            )

        adjusted_specific_yield = adjusted_annual_production / final_kwp if final_kwp > 0 else 0
        adjusted_capacity_factor = adjusted_annual_production / (final_kwp * 8760) * 100 if final_kwp > 0 else 0
        adjusted_max_power_kw = round(float(adjusted_hourly_production.max()), 2)

        # El PR original venía calculado antes de pérdidas de cableado.
        # Se ajusta con el mismo factor de pérdidas de cableado para mantener coherencia.

        adjusted_performance_ratio = production_results['performance_ratio'] * (1 - total_cable_losses_pct)
        co2_avoided_kg_per_year = adjusted_annual_production * FACTOR_CO2_GRID_KG_PER_KWH
        logger.info(f"⚡️ Producción simulada: {adjusted_annual_production:.0f} kWh/año")
        logger.info(f"⚡️ CO2 evitado: {co2_avoided_kg_per_year} kg CO2/kWh")

        # --- Perfil de consumo y autoconsumo ---
        consumption_profile = generate_consumption_profile(
            request.annual_consumption_kwh,
            year=TARGET_YEAR,
            tz=LOCAL_TZ
        )
        consumption_profile = consumption_profile[~consumption_profile.index.duplicated(keep="first")]

        consumption_profile_aligned = consumption_profile.reindex(
            adjusted_hourly_production.index
        )

        consumption_profile_aligned = consumption_profile_aligned.fillna(
            consumption_profile.mean()
        )
        if consumption_profile_aligned.sum() > 0:
            consumption_profile_aligned = (
                consumption_profile_aligned
                * request.annual_consumption_kwh
                / consumption_profile_aligned.sum()
            )

        autoconsumption_results = analyze_autoconsumption(
            adjusted_hourly_production,
            consumption_profile_aligned
        )
        enhanced_monthly_analysis = add_economic_analysis_to_monthly(
            autoconsumption_results['monthly_analysis'],
            request.electricity_price, 
            request.surplus_price
        )

        logger.info(f"⚡️ Autoconsumo: {autoconsumption_results['self_consumption_rate']:.1f}% | Autosuficiencia: {autoconsumption_results['self_sufficiency_rate']:.1f}%")

        # --- Análisis económico ---
        economic_results = calculate_economics(
            final_kwp, 
            autoconsumption_results['annual_self_consumption'],
            autoconsumption_results['annual_grid_injection'],
            autoconsumption_results['annual_grid_purchase'],
            request.electricity_price, 
            request.surplus_price
        )
        logger.info(
            f"💶 KPIs finales: Payback={economic_results['payback_years']:.2f} años, "
            f"VAN={economic_results['npv_25_years']:.0f} €, TIR={economic_results['irr_percent']:.2f}%, "
            f"LCOE={economic_results['lcoe_eur_kwh']:.3f} €/kWh (25 años)"
        )
        
        location_info = {
            "latitude": lat,
            "longitude": lon,
            "timezone": "Europe/Madrid",
            "data_source": data_source,
            "calculation_date": datetime.now().isoformat()
        }
        if request.address:
            location_info["address"] = request.address
            
        inputs_block = {
            "annual_consumption_kwh": request.annual_consumption_kwh,
            "coverage_percentage": request.coverage_percentage,
            "roof_area_m2": request.roof_area_m2,
            "roof_tilt": request.roof_tilt,
            "roof_azimuth": request.roof_azimuth,
            "installation_type": request.installation_type,
            "electricity_price": request.electricity_price,
            "surplus_price": request.surplus_price
        }
        
        # Series horarias para CSV/Sheet
        hourly_production_list = [round(float(x), 6) for x in adjusted_hourly_production.values]
        hourly_consumption_list = [
            round(float(x), 6)
            for x in consumption_profile_aligned.values
        ]
        
        # --- Protección DC mediante fusible gPV ---
        # Se toma como referencia la corriente de cortocircuito corregida por string,
        # no la corriente total del campo FV.
        string_isc_corrected = module["i_sc"] * (
            1 + module["temp_coef_isc"] / 100 * (string_config["t_cell_max_c"] - 25)
        )

        dc_fuse_min_a = string_isc_corrected * 1.25
        module_max_series_fuse_a = module.get("maximum_series_fuse_rating_a")

        recommended_fuse_dc_a = next(
            (fuse for fuse in STANDARD_GPV_FUSES_A if fuse >= dc_fuse_min_a),
            None
        )

        if recommended_fuse_dc_a is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"No existe fusible gPV comercial suficiente para la protección DC calculada. "
                    f"Iprot,DC={dc_fuse_min_a:.2f} A"
                )
            )

        if module_max_series_fuse_a is not None and recommended_fuse_dc_a > module_max_series_fuse_a:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"El fusible gPV seleccionado supera el maximum series fuse rating del módulo. "
                    f"Iprot,DC={dc_fuse_min_a:.2f} A, "
                    f"fusible seleccionado={recommended_fuse_dc_a} A, "
                    f"límite del módulo={module_max_series_fuse_a} A"
                )
            )
        # --- Protección AC mediante magnetotérmico comercial ---
        ac_breaker_reference_a = cable_results['current_ac_a'] * 1.25

        recommended_breaker_ac_a = next(
            (breaker for breaker in STANDARD_AC_BREAKERS_A if breaker >= ac_breaker_reference_a),
            None
        )

        if recommended_breaker_ac_a is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"No existe magnetotérmico AC comercial suficiente. "
                    f"Iprot,AC={ac_breaker_reference_a:.2f} A"
                )
            )
        # --- Ensamblaje de respuesta final ---
        response = SimulateResponse(
            location_info=location_info,
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
                array_configuration=f"{modules_per_string}S{strings_parallel}P"
            ),
            electrical_analysis=ElectricalAnalysis(
                string_voltage_vmp_v=round(string_voltage_vmp, 1),

                string_voltage_voc_stc_v=round(string_voltage_voc_stc, 1),
                string_voltage_voc_cold_v=round(string_voltage_voc_cold, 1),
                inverter_max_dc_voltage_v=round(inverter["max_dc_voltage_v"], 1),
                dc_voltage_compatible=dc_voltage_compatible,

                array_current_imp_a=round(array_current_imp, 1),
                array_current_isc_stc_a=round(array_current_isc_stc, 1),
                array_current_isc_corrected_a=round(array_current_isc_corrected, 1),

                current_imp_per_mppt_a=round(current_imp_per_mppt, 1),
                current_isc_per_mppt_a=round(current_isc_per_mppt, 1),
                inverter_max_input_current_per_mppt_a=round(inverter["max_input_current_per_mppt_a"], 1),
                inverter_max_short_circuit_current_per_mppt_a=round(inverter["max_short_circuit_current_per_mppt_a"], 1),

                mppt_voltage_compatible=mppt_voltage_compatible,
                mppt_current_compatible=mppt_current_compatible,
                mppt_short_circuit_current_compatible=mppt_short_circuit_current_compatible,
                dc_input_compatible=dc_input_compatible,

                mppt_count=inverter["mppt_count"],
                strings_per_mppt=inverter["strings_per_mppt"],
                strings_per_mppt_used=strings_per_mppt_used,

                dc_cable_losses_percent=dc_cable_losses,
                ac_cable_losses_percent=ac_cable_losses
            ),
            cable_analysis=CableAnalysis(
                cable_length_dc_m=cable_results['cable_length_dc_m'],
                cable_section_dc_mm2=cable_results['cable_section_dc_mm2'],
                voltage_drop_dc_percent=cable_results['voltage_drop_dc_percent'],
                power_loss_dc_percent=cable_results['power_loss_dc_percent'],
                current_dc_a=cable_results['current_dc_a'],
                cable_length_ac_m=cable_results['cable_length_ac_m'],
                cable_section_ac_mm2=cable_results['cable_section_ac_mm2'],
                voltage_drop_ac_percent=cable_results['voltage_drop_ac_percent'],
                power_loss_ac_percent=cable_results['power_loss_ac_percent'],
                current_ac_a=cable_results['current_ac_a'],
                note=cable_results['note']
            ),
            cable_section_analysis=CableSectionAnalysis(
                cable_dc=CableDetail(
                    chosen_section_mm2=recommended_dc_mm2,
                    voltage_drop_pct=cable_section_results['cable_dc']['voltage_drop_pct'],
                    ampacity_A=recommended_dc_ampacity_a,
                    resistivity_ohm_km=None,
                    material="Cu",
                    meets_voltage_drop=cable_section_results['cable_dc']['meets_voltage_drop'],
                    meets_ampacity=cable_section_results['cable_dc']['meets_ampacity'],
                    reason=cable_section_results['cable_dc']['reason']
                ),
                cable_ac=CableDetail(
                chosen_section_mm2=recommended_ac_mm2,
                voltage_drop_pct=cable_section_results['cable_ac']['voltage_drop_pct'],
                ampacity_A=recommended_ac_ampacity_a,
                resistivity_ohm_km=None,
                material="Cu",
                meets_voltage_drop=cable_section_results['cable_ac']['meets_voltage_drop'],
                meets_ampacity=cable_section_results['cable_ac']['meets_ampacity'],
                reason=(
                    cable_section_results['cable_ac']['reason']
                    if calculated_ac_mm2 >= MIN_RECOMMENDED_AC_SECTION_MM2
                    else "Se adopta 2.5 mm² como sección mínima recomendada en AC para coordinar mejor con la protección magnetotérmica."
                )
                )
            ),
            protections=CableProtections(
                calculated_fuse_dc_a=round(dc_fuse_min_a, 2),
                recommended_fuse_dc_a=recommended_fuse_dc_a,
                fuse_dc_type="gPV",
                module_max_series_fuse_rating_a=module_max_series_fuse_a,
                recommended_breaker_ac_a=recommended_breaker_ac_a
            ),
            energy_production=EnergyProduction(
                annual_production_kwh=round(adjusted_annual_production, 0),
                monthly_production=adjusted_monthly_production,
                specific_yield_kwh_kwp=round(adjusted_specific_yield, 0),
                performance_ratio=round(adjusted_performance_ratio, 3),
                capacity_factor_percent=round(adjusted_capacity_factor, 1),
                max_power_kw=adjusted_max_power_kw
            ),
            environmental_impact={
                "co2_avoided_kg_per_year": round(co2_avoided_kg_per_year, 1),
                "factor_kg_per_kwh": FACTOR_CO2_GRID_KG_PER_KWH
            },
            autoconsumption_analysis=AutoconsumptionAnalysis(
                annual_consumption_kwh=round(autoconsumption_results['annual_consumption'], 0),
                annual_self_consumption_kwh=round(autoconsumption_results['annual_self_consumption'], 0),
                annual_grid_injection_kwh=round(autoconsumption_results['annual_grid_injection'], 0),
                annual_grid_purchase_kwh=round(autoconsumption_results['annual_grid_purchase'], 0),
                self_consumption_rate_percent=round(autoconsumption_results['self_consumption_rate'], 1),
                self_sufficiency_rate_percent=round(autoconsumption_results['self_sufficiency_rate'], 1),
                monthly_analysis=enhanced_monthly_analysis
            ),
            economic_analysis=EconomicAnalysis(
                system_cost_eur=round(economic_results['system_cost'], 0),
                annual_savings_eur=round(economic_results['annual_savings'], 0),
                payback_years=round(economic_results['payback_years'], 1),
                npv_25_years_eur=round(economic_results['npv_25_years'], 0),
                irr_percent=round(economic_results['irr_percent'], 1),
                lcoe_eur_kwh=round(economic_results['lcoe_eur_kwh'], 3)
            ),
            inputs=inputs_block,
            hourly_production_ac_kwh=hourly_production_list,
            hourly_consumption_kwh=hourly_consumption_list
        )

        if solar_api_data:
            response.location_info["google_solar_data"] = {
                "roof_segments_found": len(solar_api_data.roof_segments),
                "max_array_area_m2": solar_api_data.max_array_area_meters2,
                "max_panels_count": solar_api_data.max_array_panels_count,
                "attribution": attribution_text
            }

        logger.info(f"✅ Simulación COMPLETADA [{total_modules_final} módulos | {final_kwp:.1f} kWp | {production_results['annual_production']:.0f} kWh/año]")
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error en simulación: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")

@router.post("/simulate-and-report")
async def simulate_pv_system_and_generate_report(request: SimulateRequest):
    """
    Ejecuta la simulación fotovoltaica y envía el resultado a Google Apps Script
    para generar automáticamente los documentos del proyecto.
    """
    simulation_response = await simulate_pv_system(request)

    if hasattr(simulation_response, "model_dump"):
        simulation_payload = simulation_response.model_dump()
    else:
        simulation_payload = simulation_response.dict()

    report_result = await call_google_apps_script_report(simulation_payload)

    return {
        "success": True,
        "simulation": simulation_payload,
        "report": {
            "folder_url": report_result.get("folder_url"),
            "sheet_url": report_result.get("sheet_url"),
            "doc_url": report_result.get("doc_url"),
            "csv_url": report_result.get("csv_url"),
            "pdf_url": report_result.get("pdf_url"),
        }
    }
# ======================== ENDPOINTS ADICIONALES ========================

@router.get("/health")
async def health_check():
    """Comprueba el estado del servicio."""
    logger.debug("Health check solicitado")
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@router.get("/modules")
async def get_available_modules():
    """Lista los módulos FV disponibles."""
    logger.debug("Consulta de módulos disponibles")
    return {"modules": PV_MODULES}

@router.get("/inverters")
async def get_available_inverters():
    """Lista los inversores FV disponibles."""
    logger.debug("Consulta de inversores disponibles")
    return {"inverters": INVERTERS}

@router.post("/quick-estimate")
async def quick_estimate(lat: float, lon: float, annual_consumption_kwh: float = 4200):
    """
    Estimación rápida (sin simulación detallada): solo energía y coste aprox.
    """
    try:
        from pvlib.iotools import get_pvgis_tmy
        _, meta = get_pvgis_tmy(lat, lon, outputformat='json', optimalangles=True)
        optimal_info = meta.get('optimal', {})
        if not optimal_info:
            optimal_info = meta.get('optimalangles', {})
        optimal_tilt = optimal_info.get('slope', abs(lat) - 10)

        irradiation_url = "https://re.jrc.ec.europa.eu/api/v5_2/PVcalc"
        params = {
            'lat': lat, 'lon': lon, 'outputformat': 'json',
            'peakpower': 1, 'loss': 14, 'angle': optimal_tilt, 'aspect': 180
        }
        response = requests.get(irradiation_url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        annual_production_kwh_kwp = data['outputs']['totals']['fixed']['E_y']

        estimated_kwp = annual_consumption_kwh * 0.85 / annual_production_kwh_kwp
        estimated_modules = math.ceil(estimated_kwp * 1000 / 545)
        estimated_cost = estimated_kwp * 1000 * 1.0

        logger.info(
            f"⚡️ QuickEstimate: kwp={estimated_kwp:.1f}, módulos={estimated_modules}, "
            f"coste={estimated_cost:.0f}€, producción anual={estimated_kwp * annual_production_kwh_kwp:.0f} kWh"
        )
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
        logger.error(f"Error en quick-estimate: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error en estimación: {str(e)}")

@router.post("/geocode")
async def geocode_endpoint(address: str):
    """Devuelve lat/lon para una dirección mediante Google Maps."""
    try:
        if not GOOGLE_MAPS_API_KEY:
            logger.warning("Petición de geocodificación sin API Key de Google Maps")
            return {
                "address": address,
                "error": "GOOGLE_MAPS_API_KEY no configurada en .env",
                "success": False
            }
        lat, lon = await geocode_address(address)
        logger.info(f"Geocodificación '{address}': ({lat}, {lon})")
        return {
            "address": address,
            "latitude": lat,
            "longitude": lon,
            "success": True
        }
    except HTTPException as e:
        logger.error(f"Error en geocodificación: {e.detail}")
        return {
            "address": address,
            "error": e.detail,
            "success": False
        }

@router.post("/solar-insights")
async def solar_insights_endpoint(lat: float, lon: float):
    """Obtiene datos solares de tejado via Google Solar API."""
    try:
        if not GOOGLE_MAPS_API_KEY:
            logger.warning("Petición de Solar Insights sin API Key")
            return {
                "latitude": lat,
                "longitude": lon,
                "error": "GOOGLE_MAPS_API_KEY no configurada en .env",
                "success": False
            }

        solar_data = await get_building_insights(lat, lon)
        if solar_data:
            logger.info(f"Datos Solar API obtenidos para ({lat},{lon})")
            return {
                "latitude": lat,
                "longitude": lon,
                "solar_data": solar_data.dict(),
                "attribution": "Source: Includes solar data from Google",
                "success": True
            }
        else:
            logger.info(f"Sin cobertura Solar API para ({lat},{lon})")
            return {
                "latitude": lat,
                "longitude": lon,
                "message": "No hay cobertura Solar API para esta ubicación",
                "success": False
            }
    except Exception as e:
        logger.error(f"Error en solar-insights: {str(e)}")
        return {
            "latitude": lat,
            "longitude": lon,
            "error": str(e),
            "success": False
        }
