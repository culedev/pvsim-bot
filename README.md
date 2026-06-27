# Sistema de Dimensionado Fotovoltaico

API en FastAPI para simular instalaciones fotovoltaicas, estimar producción y generar documentación automática mediante Google Apps Script.

## Documentación técnica

- Flujo detallado de simulación y fórmulas: `docs/flujo-simulacion-fv.md`

## Funcionalidades

- Simulación técnica y económica de sistemas FV.
- Uso opcional de Google Solar API para obtener geometría de tejado.
- Geocodificación de direcciones con Google Geocoding API.
- Generación automática de informe con `POST /simulate-and-report`.
- Endpoints auxiliares para módulos, inversores, geocodificación y solar insights.

## Requisitos

- Python 3.11 o superior.
- `pip`.
- Opcional: Docker y Docker Compose.
- Una `API_KEY` interna para proteger el backend.
- Una `GOOGLE_MAPS_API_KEY` si quieres usar Google Solar API y/o geocodificación.
- Una `APPS_SCRIPT_WEBAPP_URL` si quieres usar `simulate-and-report`.

## Estructura básica

- `app/main.py`: arranque de FastAPI y middleware de autenticación.
- `app/endpoints.py`: endpoints principales.
- `app/solar_api.py`: integración con Google Solar API.
- `app/geocoding.py`: integración con Google Geocoding API.
- `run.py`: arranque simple de la app.

## Instalación local

1. Crear y activar un entorno virtual.

En Windows `cmd`:

```bat
python -m venv .venv
.venv\Scripts\activate.bat
```

En PowerShell:

```powershell
python -m venv .venv
& ".\.venv\Scripts\Activate.ps1"
```

2. Instalar dependencias.

```bash
pip install -r requirements.txt
```

3. Crear un archivo `.env` en la raíz del proyecto.

Ejemplo:

```env
API_KEY=tu_clave_interna_del_backend
GOOGLE_MAPS_API_KEY=tu_api_key_de_google
APPS_SCRIPT_WEBAPP_URL=https://script.google.com/macros/s/XXXXXXXXXXXX/exec
TZ=Europe/Madrid
```

## Variables de entorno

- `API_KEY`: obligatoria para acceder a casi todos los endpoints.
- `GOOGLE_MAPS_API_KEY`: necesaria para:
  - `POST /geocode`
  - `POST /solar-insights`
  - simulaciones con `use_solar_api=true`
- `APPS_SCRIPT_WEBAPP_URL`: necesaria para `POST /simulate-and-report`.
- `TZ`: zona horaria del contenedor si usas Docker.

## Arranque local

Con `uvicorn`:

```bash
uvicorn app.main:app --reload
```

O con el script del proyecto:

```bash
python run.py
```

La API quedará disponible en:

```text
http://127.0.0.1:8000
```

## Autenticación

La API usa una cabecera obligatoria llamada `x-api-key`.

Las únicas rutas públicas son:

- `GET /`
- `GET /health`

Todas las demás requieren:

```http
x-api-key: TU_API_KEY
```

Si no la envías, el backend responderá con `401 Unauthorized`.

## Documentación interactiva

Si levantas la API en local, puedes usar Swagger en:

```text
http://127.0.0.1:8000/docs
```

Recuerda añadir la cabecera `x-api-key` en tus pruebas.

## Uso rápido

### 1. Comprobar estado

```bash
curl http://127.0.0.1:8000/health
```

### 2. Simulación estándar

Endpoint:

```text
POST /simulate
```

Ejemplo de petición:

```bash
curl -X POST "http://127.0.0.1:8000/simulate" ^
  -H "Content-Type: application/json" ^
  -H "x-api-key: TU_API_KEY" ^
  -d "{\"lat\":37.8882,\"lon\":-4.7794,\"annual_consumption_kwh\":7200,\"roof_area_m2\":75,\"roof_tilt\":20,\"roof_azimuth\":220,\"installation_type\":\"coplanar\",\"coverage_percentage\":90,\"shading_factor\":0.94,\"electricity_price\":0.13,\"surplus_price\":0.06}"
```

Body JSON equivalente:

```json
{
  "lat": 37.8882,
  "lon": -4.7794,
  "annual_consumption_kwh": 7200,
  "roof_area_m2": 75,
  "roof_tilt": 20,
  "roof_azimuth": 220,
  "installation_type": "coplanar",
  "coverage_percentage": 90,
  "shading_factor": 0.94,
  "electricity_price": 0.13,
  "surplus_price": 0.06
}
```

### 3. Simulación con generación de informe

Endpoint:

```text
POST /simulate-and-report
```

Este endpoint:

1. Ejecuta la misma simulación que `POST /simulate`.
2. Envía el resultado a Google Apps Script.
3. Devuelve enlaces al informe generado.

Ejemplo de petición:

```bash
curl -X POST "http://127.0.0.1:8000/simulate-and-report" ^
  -H "Content-Type: application/json" ^
  -H "x-api-key: TU_API_KEY" ^
  -d "{\"lat\":37.8882,\"lon\":-4.7794,\"annual_consumption_kwh\":7200,\"roof_area_m2\":75,\"roof_tilt\":20,\"roof_azimuth\":220,\"installation_type\":\"coplanar\",\"coverage_percentage\":90,\"shading_factor\":0.94,\"electricity_price\":0.13,\"surplus_price\":0.06}"
```

Body JSON:

```json
{
  "lat": 37.8882,
  "lon": -4.7794,
  "annual_consumption_kwh": 7200,
  "roof_area_m2": 75,
  "roof_tilt": 20,
  "roof_azimuth": 220,
  "installation_type": "coplanar",
  "coverage_percentage": 90,
  "shading_factor": 0.94,
  "electricity_price": 0.13,
  "surplus_price": 0.06
}
```

Respuesta esperada, resumida:

```json
{
  "success": true,
  "simulation": {
    "location_info": {},
    "geometry_analysis": {},
    "technical_specs": {},
    "system_config": {},
    "electrical_analysis": {},
    "energy_production": {},
    "economic_analysis": {}
  },
  "report": {
    "folder_url": "https://...",
    "sheet_url": "https://...",
    "doc_url": "https://...",
    "csv_url": "https://...",
    "pdf_url": "https://..."
  }
}
```

## Uso de Google Solar API

El campo `use_solar_api` existe en el modelo de entrada y por defecto vale `true`.

Comportamiento actual:

- Si ya envías `roof_area_m2`, `roof_tilt` y `roof_azimuth`, la simulación usa esos datos manuales y no necesita Solar API.
- Si faltan esos datos y `use_solar_api=true`, el backend intentará completar la geometría con Google Solar API.
- Si no hay cobertura o falla la consulta, la simulación sigue con el flujo tradicional.

Ejemplo forzando uso de Solar API cuando no se informa la geometría:

```json
{
  "lat": 37.8882,
  "lon": -4.7794,
  "annual_consumption_kwh": 7200,
  "installation_type": "coplanar",
  "coverage_percentage": 90,
  "shading_factor": 0.94,
  "electricity_price": 0.13,
  "surplus_price": 0.06,
  "use_solar_api": true
}
```

Ejemplo desactivando Solar API explícitamente:

```json
{
  "lat": 37.8882,
  "lon": -4.7794,
  "annual_consumption_kwh": 7200,
  "roof_area_m2": 75,
  "roof_tilt": 20,
  "roof_azimuth": 220,
  "installation_type": "coplanar",
  "coverage_percentage": 90,
  "shading_factor": 0.94,
  "electricity_price": 0.13,
  "surplus_price": 0.06,
  "use_solar_api": false
}
```

## Usar dirección en vez de coordenadas

También puedes enviar `address` en lugar de `lat` y `lon`.

Ejemplo:

```json
{
  "address": "Calle Claudio Marcelo 10, Cordoba, Espana",
  "annual_consumption_kwh": 7200,
  "installation_type": "coplanar",
  "use_solar_api": true
}
```

Esto requiere `GOOGLE_MAPS_API_KEY` con acceso a `Geocoding API`.

## Endpoints disponibles

- `GET /`: mensaje de bienvenida y versión.
- `GET /health`: estado del servicio.
- `POST /simulate`: simulación completa.
- `POST /simulate-and-report`: simulación más generación de informe.
- `GET /modules`: catálogo de módulos FV disponibles.
- `GET /inverters`: catálogo de inversores disponibles.
- `POST /quick-estimate`: estimación rápida.
- `POST /geocode`: geocodificación de dirección.
- `POST /solar-insights`: consulta directa a Google Solar API.

## Ejemplos auxiliares

### Geocodificar una dirección

```bash
curl -X POST "http://127.0.0.1:8000/geocode?address=Calle%20Claudio%20Marcelo%2010%2C%20Cordoba" ^
  -H "x-api-key: TU_API_KEY"
```

### Consultar Solar API directamente

```bash
curl -X POST "http://127.0.0.1:8000/solar-insights?lat=37.8882&lon=-4.7794" ^
  -H "x-api-key: TU_API_KEY"
```

## Docker

El proyecto incluye `Dockerfile` y `docker-compose.yml`.

Levantar con Docker Compose:

```bash
docker compose up --build
```

Variables esperadas por `docker-compose.yml`:

```env
API_KEY=tu_clave_interna_del_backend
GOOGLE_MAPS_API_KEY=tu_api_key_de_google
TZ=Europe/Madrid
```

La API quedará expuesta en:

```text
http://127.0.0.1:8000
```

## Problemas frecuentes

### `401 Unauthorized`

Falta la cabecera:

```http
x-api-key: TU_API_KEY
```

### Error con Google Solar API o Geocoding

Revisa:

- que `GOOGLE_MAPS_API_KEY` exista en `.env`
- que la clave pertenezca al proyecto correcto de Google Cloud
- que `Solar API` y `Geocoding API` estén habilitadas
- que la clave no tenga restricciones incompatibles

### Error en `simulate-and-report`

Revisa que `APPS_SCRIPT_WEBAPP_URL` esté configurada y que el Web App de Google Apps Script esté desplegado y accesible.
