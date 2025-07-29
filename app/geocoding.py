import aiohttp
from fastapi import HTTPException
import logging
from app.constants import GOOGLE_MAPS_API_KEY, GEOCODING_API_URL

logger = logging.getLogger(__name__)

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
