import aiohttp
from fastapi import HTTPException
from typing import Optional
import logging
from app.models import SolarApiData
from app.constants import GOOGLE_MAPS_API_KEY, SOLAR_API_BASE_URL

import logging
logger = logging.getLogger(__name__)


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
        azimuth_degrees = segment.get('azimuthDegrees', 180)

        segment_data = {
            'segment_index': segment.get('segmentIndex', 0),
            'tilt_degrees': segment.get('pitchDegrees', 30),
            'azimuth_degrees': azimuth_degrees,
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
