# Flujo de Simulación Fotovoltaica

Este documento explica, paso a paso, qué hace el backend durante una simulación y qué fórmulas utiliza en cada fase.

El flujo descrito corresponde a la implementación actual de `POST /simulate` y, por extensión, de `POST /simulate-and-report`, ya que este segundo endpoint ejecuta primero la misma simulación y después envía el resultado a Google Apps Script.

## Vista general

```text
[Entrada JSON]
      |
      v
[Hay address?] -- sí --> [Geocodificación Google]
      | no                    |
      v                       v
[Usar lat/lon] --------> [Coordenadas finales]
                               |
                               v
                    [Usar Google Solar API?]
                         | sí         | no
                         v            v
              [Obtener geometría]   [Seguir sin Solar API]
                         |            |
                         +-----+------+ 
                               |
                               v
                  [Descargar TMY y ángulos PVGIS]
                               |
                               v
               [Determinar geometría final instalación]
                               |
                               v
                [Calcular irradiación óptima y real]
                               |
                               v
                  [Dimensionar módulos y potencia]
                               |
                               v
                      [Seleccionar inversor]
                               |
                               v
                        [Configurar strings]
                               |
                               v
               [Verificar compatibilidad eléctrica DC]
                               |
                               v
                  [Dimensionar secciones de cable]
                               |
                               v
                    [Calcular pérdidas DC y AC]
                               |
                               v
               [Simular producción horaria con pvlib]
                               |
                               v
             [Ajustar producción por pérdidas de cable]
                               |
                               v
                   [Generar perfil horario de consumo]
                               |
                               v
                      [Analizar autoconsumo]
                               |
                               v
                  [Calcular economía y protecciones]
                               |
                               v
                      [Construir respuesta API]
```

## 1. Entrada de datos

La simulación recibe un `SimulateRequest` con dos posibles estrategias de localización:

- `address`
- `lat` y `lon`

Regla actual:

1. Si llega `address`, se geocodifica con Google.
2. Si no llega `address`, se usan `lat` y `lon`.
3. Si no hay ninguna de las dos opciones, la API devuelve error `400`.

## 2. Geocodificación

Si se recibe `address`, el backend llama a Google Geocoding API.

Resultado de esta fase:

- `lat`
- `lon`
- `data_source = geocoded`

Si no se usa dirección, `data_source` parte como `manual`.

## 3. Decisión de uso de Google Solar API

El backend evalúa cuatro elementos:

- `roof_tilt`
- `roof_azimuth`
- `roof_area_m2`
- `use_solar_api`

Regla actual:

1. Si el usuario ya aporta `roof_tilt`, `roof_azimuth` y `roof_area_m2`, no se consulta Solar API.
2. Si falta alguno de esos valores y `use_solar_api = true`, se intenta consultar Google Solar API.
3. Si `installation_type = optimal`, no se usa Solar API para geometría de tejado.
4. Si Solar API no tiene cobertura o falla, la simulación continúa con el flujo clásico.

## 4. Selección de segmento de tejado en Solar API

Cuando hay respuesta de Google Solar API, el backend selecciona el mejor segmento de tejado usando un criterio de área y orientación.

Se calcula la desviación respecto al sur:

$$
\Delta_{az} = \min(|azimuth - 180|, 360 - |azimuth - 180|)
$$

Factor de orientación:

$$
f_{orient} = \max(0.3, 1 - \frac{\Delta_{az}}{90})
$$

Puntuación del segmento:

$$
score = area \cdot f_{orient}
$$

Se priorizan segmentos con:

- mayor área útil
- orientación más cercana al sur
- área mayor de `10 m²`

El mejor segmento puede completar estos campos si faltan en la petición:

- `roof_tilt`
- `roof_azimuth`
- `roof_area_m2`

## 5. Obtención de clima y geometría óptima con PVGIS

El backend descarga:

- un TMY horario con `ghi`, `dni`, `dhi`, `temp_air`, `wind_speed`
- los ángulos óptimos de instalación para la localización

Si PVGIS no devuelve ángulos óptimos, se usa un fallback:

$$
tilt_{opt} = \max(15, \min(45, |lat| - 10))
$$

$$
azimuth_{opt} = 180^\circ
$$

## 6. Determinación de la geometría final de instalación

La geometría final depende de `installation_type`.

### Caso `coplanar`

- Si `roof_tilt >= 10`, se usa la inclinación del tejado.
- Si `roof_tilt < 10`, el sistema considera tejado plano y fuerza inclinación óptima con estructura.

### Caso `fixed`

- Usa `roof_tilt` y `roof_azimuth` como geometría final.

### Caso por defecto u `optimal`

- Usa `tilt_opt`
- Usa azimut sur `180°`
- Marca `with_support_structure = true`

## 7. Irradiación óptima y real

Con `pvlib.irradiance.get_total_irradiance`, el backend calcula la irradiancia anual sobre plano inclinado en dos escenarios:

1. Plano óptimo
2. Plano real de la instalación

La irradiación anual se obtiene sumando la irradiancia POA horaria y convirtiendo a `kWh/m²`:

$$
H_{annual} = \frac{\sum POA_{global}}{1000}
$$

Donde:

- `H_annual_optimal` usa `tilt_opt`, `180°`
- `H_annual_real` usa `installation_tilt`, `installation_azimuth`

## 8. Pérdidas por orientación e inclinación

El backend usa una fórmula simplificada inspirada en CTE-HE5.

Primero calcula la desviación angular respecto al sur:

$$
\Delta_{az} = |azimuth - 180|
$$

Si supera `180°`, se corrige con simetría circular.

La pérdida porcentual se estima así:

Para `tilt > 15°`:

$$
Loss_{orient} = 100 \cdot (1.2 \times 10^{-4} \cdot (tilt - lat + 10)^2 + 3.5 \times 10^{-5} \cdot \Delta_{az}^2)
$$

Para `tilt ≤ 15°`:

$$
Loss_{orient} = 100 \cdot (1.2 \times 10^{-4} \cdot (tilt - lat + 10)^2)
$$

Finalmente se acota a un máximo del `50%`:

$$
Loss_{orient,capped} = \min(50, \max(0, Loss_{orient}))
$$

## 9. Estimación preliminar del rendimiento específico

Antes de simular toda la planta, el backend hace una estimación rápida del rendimiento específico para dimensionar potencia.

La fórmula actual es:

$$
Y_{spec,est} = H_{annual,real} \cdot 0.80 \cdot shading\_factor
$$

Donde:

- `H_annual_real` está en `kWh/m²`
- `0.80` es un factor global simplificado previo a la simulación detallada
- `shading_factor` lo aporta el usuario

## 10. Dimensionado del generador FV

### 10.1 Energía objetivo

$$
E_{target} = annual\_consumption \cdot \frac{coverage\_percentage}{100}
$$

### 10.2 Potencia pico requerida

$$
P_{req,kWp} = \frac{E_{target}}{Y_{spec,est}}
$$

### 10.3 Número de módulos

Si la potencia nominal del módulo es `P_mod` en `kW`:

$$
N_{mod} = \lceil \frac{P_{req,kWp}}{P_{mod}} \rceil
$$

### 10.4 Limitación por área disponible

Si hay `roof_area_m2`, el máximo de módulos se limita con un factor de aprovechamiento del `75%`:

$$
N_{max,area} = \lfloor \frac{roof\_area\_m2}{area_{mod}} \cdot 0.75 \rfloor
$$

Si `N_mod > N_max,area`, el backend recorta el campo FV al máximo instalable por superficie.

### 10.5 Potencia y área finales del campo preliminar

$$
P_{dc,pre} = N_{mod} \cdot P_{mod}
$$

$$
A_{pre} = N_{mod} \cdot area_{mod}
$$

## 11. Selección del inversor

El inversor se elige buscando un ratio DC/AC cercano a `1.2`, con restricciones técnicas.

### 11.1 Ratio DC/AC

$$
ratio = \frac{P_{dc}}{P_{ac}}
$$

### 11.2 Criterio de selección

Se minimiza:

$$
score = |ratio - 1.2|
$$

El inversor se descarta si:

- `P_dc > max_dc` del inversor
- `Imp` del módulo supera la corriente máxima por MPPT
- `Isc` del módulo supera la corriente máxima de cortocircuito por MPPT

## 12. Configuración de strings

El backend calcula primero condiciones térmicas de diseño.

### 12.1 Temperaturas de diseño

$$
T_{air,min} = \min(temp\_air) - 10
$$

$$
T_{air,max} = P99(temp\_air)
$$

$$
T_{cell,max} = T_{air,max} + (\frac{NOCT - 20}{800}) \cdot 1000
$$

### 12.2 Tensión del módulo corregida por temperatura

$$
V_{mp,hot} = V_{mp} \cdot (1 + \frac{temp\_coef\_vmp}{100} \cdot (T_{cell,max} - 25))
$$

$$
V_{oc,cold} = V_{oc} \cdot (1 + \frac{temp\_coef\_voc}{100} \cdot (T_{air,min} - 25))
$$

### 12.3 Rango admisible de módulos por string

$$
N_{series,min} = \max(2, \lfloor \frac{V_{mppt,min}}{V_{mp,hot}} \cdot 1.1 \rfloor)
$$

$$
N_{series,max} = \lfloor \frac{V_{dc,max}}{V_{oc,cold}} \rfloor
$$

Luego se prueban combinaciones de:

- módulos por string
- strings en paralelo

La configuración final debe cumplir:

- potencia DC total no superior al máximo del inversor
- tensión en MPPT válida
- tensión de circuito abierto en frío válida
- corriente por MPPT válida
- máximo número de strings por MPPT válido

La configuración elegida es la que minimiza el desperdicio de módulos respecto al objetivo preliminar.

## 13. Verificación eléctrica DC

Con la configuración final, el backend calcula:

### 13.1 Tensiones de string

$$
V_{string,vmp} = N_{series} \cdot V_{mp}
$$

$$
V_{string,voc,stc} = N_{series} \cdot V_{oc}
$$

$$
V_{string,voc,cold} = N_{series} \cdot V_{oc,cold,module}
$$

### 13.2 Corrientes del campo FV

$$
I_{array,imp} = N_{parallel} \cdot I_{mp}
$$

$$
I_{array,isc,stc} = N_{parallel} \cdot I_{sc}
$$

Corrección conservadora de cortocircuito:

$$
I_{array,isc,corr} = N_{parallel} \cdot I_{sc} \cdot (1 + \frac{temp\_coef\_isc}{100} \cdot (T_{cell,max} - 25))
$$

### 13.3 Corriente por MPPT

$$
strings_{MPPT} = \lceil \frac{N_{parallel}}{MPPT_{count}} \rceil
$$

$$
I_{MPPT,imp} = strings_{MPPT} \cdot I_{mp}
$$

$$
I_{MPPT,isc} = strings_{MPPT} \cdot I_{sc} \cdot (1 + \frac{temp\_coef\_isc}{100} \cdot (T_{cell,max} - 25))
$$

La entrada DC del inversor es válida solo si todas estas comprobaciones son verdaderas:

- tensión MPPT válida
- tensión DC máxima válida
- corriente de entrada por MPPT válida
- corriente de cortocircuito por MPPT válida
- número de strings por MPPT válido

## 14. Selección de secciones de cable

El backend analiza por separado el tramo DC y el tramo AC.

### 14.1 Criterios de selección

Cada sección candidata debe cumplir simultáneamente:

1. caída de tensión máxima permitida
2. intensidad admisible según tabla UNE

La caída de tensión se calcula con resistividad del material:

$$
\Delta V = 2 \cdot L \cdot I \cdot \frac{\rho}{S}
$$

$$
\Delta V_{pct} = \frac{\Delta V}{V_{nom}} \cdot 100
$$

Donde:

- `L` es la longitud del tramo
- `I` es la corriente del tramo
- `ρ` es la resistividad
- `S` es la sección del conductor

El criterio de proyecto actual usa:

$$
\Delta V_{pct,max} = 1.5
$$

## 15. Pérdidas de cableado

Una vez seleccionadas las secciones, el backend estima caída de tensión y pérdidas en DC y AC.

### 15.1 Corrientes usadas

DC total:

$$
I_{dc,total} = N_{parallel} \cdot I_{mp}
$$

AC monofásica:

$$
I_{ac} = \frac{P_{ac} \cdot 1000}{230}
$$

### 15.2 Caída de tensión DC

$$
\Delta V_{dc} = 2 \cdot L_{dc} \cdot I_{dc,string} \cdot R_{dc}
$$

$$
\Delta V_{dc,pct} = \frac{\Delta V_{dc}}{N_{series} \cdot V_{mp}} \cdot 100
$$

### 15.3 Caída de tensión AC

$$
\Delta V_{ac} = 2 \cdot L_{ac} \cdot I_{ac} \cdot R_{ac}
$$

$$
\Delta V_{ac,pct} = \frac{\Delta V_{ac}}{230} \cdot 100
$$

### 15.4 Pérdidas por efecto Joule

$$
P_{loss} = I^2 \cdot R
$$

En DC:

$$
P_{loss,dc,pct} = \frac{P_{loss,dc}}{P_{nom,dc}} \cdot 100
$$

En AC:

$$
P_{loss,ac,pct} = \frac{P_{loss,ac}}{P_{nom,ac}} \cdot 100
$$

## 16. Simulación de producción con pvlib

El backend usa `pvlib.ModelChain` con:

- `aoi_model = physical`
- `spectral_model = no_loss`
- `temperature_model = faiman`
- `losses_model = no_loss`

Es decir, la simulación base no mete todas las pérdidas como caja negra, sino que varias se aplican explícitamente después.

### 16.1 Pérdidas del sistema aplicadas en simulación

El proyecto define un conjunto de pérdidas en `SYSTEM_LOSSES`, pero durante esta fase aplica solo las no modeladas ya por `pvlib`.

Se excluyen de la multiplicación:

- `temperature`
- `irradiance`
- `inverter`

El factor de pérdidas aplicado es:

$$
f_{loss} = \prod (1 - loss_i) \cdot shading\_factor
$$

para todas las pérdidas del diccionario excepto las tres excluidas.

La potencia AC ajustada queda como:

$$
P_{ac,adj} = P_{ac,pvlib} \cdot f_{loss}
$$

Luego se limita a la potencia nominal AC del inversor.

## 17. Producción anual, mensual y KPIs energéticos

### 17.1 Producción anual

$$
E_{annual} = \sum P_{ac,hourly}
$$

### 17.2 Producción específica

$$
Y_{spec} = \frac{E_{annual}}{P_{dc,installed}}
$$

### 17.3 Performance Ratio

Con la irradiación sobre plano obtenida en la simulación:

$$
PR = \frac{E_{annual}}{P_{dc,installed} \cdot H_{POA}}
$$

### 17.4 Capacity Factor

$$
CF = \frac{E_{annual}}{P_{dc,installed} \cdot 8760} \cdot 100
$$

## 18. Ajuste final por pérdidas de cable

La producción obtenida de `pvlib` se reduce con las pérdidas estimadas de cableado:

$$
loss_{cable,total} = \frac{loss_{dc} + loss_{ac}}{100}
$$

$$
E_{annual,adj} = E_{annual} \cdot (1 - loss_{cable,total})
$$

$$
P_{hourly,adj} = P_{hourly} \cdot (1 - loss_{cable,total})
$$

El `PR` también se corrige con el mismo factor para mantener consistencia interna de resultados.

## 19. Perfil de consumo

El backend genera un perfil horario sintético de consumo para todo el año.

El perfil combina:

- patrón diario horario
- estacionalidad anual
- corrección por fin de semana

### 19.1 Factor estacional

$$
f_{season} = 1 + 0.3 \cdot \cos(2\pi \cdot \frac{dayOfYear - 21}{365})
$$

### 19.2 Factor fin de semana

$$
f_{weekend} =
\begin{cases}
1.1 & \text{si es sábado o domingo} \\
1.0 & \text{si no}
\end{cases}
$$

### 19.3 Normalización al consumo anual

Si `pattern_h` es el patrón horario bruto:

$$
consumption_h = pattern_h \cdot \frac{annual\_consumption}{\sum pattern_h}
$$

## 20. Análisis de autoconsumo

Para cada hora se comparan producción y consumo.

### 20.1 Autoconsumo horario

$$
self\_consumption_h = \min(production_h, consumption_h)
$$

### 20.2 Excedente a red

$$
grid\_injection_h = \max(0, production_h - consumption_h)
$$

### 20.3 Compra a red

$$
grid\_purchase_h = \max(0, consumption_h - production_h)
$$

### 20.4 Indicadores anuales

Tasa de autoconsumo:

$$
SCR = \frac{annual\_self\_consumption}{annual\_production} \cdot 100
$$

Tasa de autosuficiencia:

$$
SSR = \frac{annual\_self\_consumption}{annual\_consumption} \cdot 100
$$

## 21. Análisis económico

## 21.1 Inversión inicial

$$
CAPEX = P_{kWp} \cdot 1000 \cdot cost\_per\_wp
$$

## 21.2 OPEX anual

$$
OPEX = P_{kWp} \cdot maintenance_{annual} + P_{kWp} \cdot insurance_{annual}
$$

## 21.3 Ahorro bruto anual

$$
Savings_{gross} = E_{self} \cdot price_{electricity} + E_{surplus} \cdot price_{surplus}
$$

## 21.4 Ahorro neto anual

$$
Savings_{net} = Savings_{gross} - OPEX
$$

## 21.5 Payback simple

$$
Payback = \frac{CAPEX}{Savings_{net}}
$$

Si el ahorro neto es menor o igual que cero, el backend usa un valor de `999` años.

## 21.6 VAN a 25 años

Se construye un flujo de caja con:

- degradación anual del generador
- crecimiento del precio eléctrico
- crecimiento de compensación de excedentes
- crecimiento de OPEX
- tasa de descuento

La expresión general es:

$$
VAN = \sum_{t=0}^{25} \frac{CF_t}{(1+r)^t}
$$

Donde `CF_0 = -CAPEX`.

## 21.7 TIR

La TIR es la tasa `r` que hace:

$$
0 = \sum_{t=0}^{25} \frac{CF_t}{(1+r)^t}
$$

La implementación actual la resuelve por búsqueda binaria.

## 21.8 LCOE

Se descuentan energía y costes durante 25 años:

$$
LCOE = \frac{CAPEX + \sum_{t=1}^{25} \frac{OPEX_t}{(1+r)^t}}{\sum_{t=1}^{25} \frac{E_t}{(1+r)^t}}
$$

## 22. Impacto ambiental

El CO2 evitado se calcula con un factor fijo de red:

$$
CO2_{avoided} = E_{annual,adj} \cdot factor_{CO2}
$$

En la configuración actual:

$$
factor_{CO2} = 0.28\ kg/kWh
$$

## 23. Protecciones eléctricas

### 23.1 Fusible DC gPV

Se parte de la corriente de cortocircuito corregida por string:

$$
I_{sc,string,corr} = I_{sc} \cdot (1 + \frac{temp\_coef\_isc}{100} \cdot (T_{cell,max} - 25))
$$

Corriente mínima de protección:

$$
I_{prot,DC} = 1.25 \cdot I_{sc,string,corr}
$$

Después se selecciona el siguiente calibre comercial `gPV` disponible.

### 23.2 Magnetotérmico AC

$$
I_{prot,AC} = 1.25 \cdot I_{ac}
$$

Después se selecciona el siguiente calibre comercial disponible.

## 24. Construcción de la respuesta

La respuesta final se organiza en bloques:

- `location_info`
- `geometry_analysis`
- `technical_specs`
- `system_config`
- `electrical_analysis`
- `cable_analysis`
- `cable_section_analysis`
- `protections`
- `energy_production`
- `environmental_impact`
- `autoconsumption_analysis`
- `economic_analysis`
- `inputs`
- series horarias de producción y consumo

Si Solar API se usó con éxito, también se añade un bloque `google_solar_data` dentro de `location_info`.

## 25. Flujo específico de `simulate-and-report`

`POST /simulate-and-report` añade una última fase sobre `POST /simulate`:

```text
[SimulateRequest]
        |
        v
[simulate_pv_system]
        |
        v
[Respuesta completa de simulación]
        |
        v
[call_google_apps_script_report]
        |
        v
[URLs de folder, sheet, doc, csv y pdf]
```

Es decir:

1. ejecuta toda la simulación anterior,
2. convierte el resultado a diccionario JSON,
3. lo envía al Web App de Google Apps Script,
4. devuelve tanto la simulación como los enlaces de salida.

## 26. Lectura rápida del flujo

Si quieres resumir el comportamiento del backend en una sola secuencia, es esta:

1. localizar la instalación,
2. completar geometría con Solar API si hace falta,
3. descargar clima y orientación óptima,
4. dimensionar módulos e inversor,
5. cerrar la configuración eléctrica,
6. estimar pérdidas y producción,
7. cruzar producción con consumo,
8. valorar rentabilidad e impacto,
9. devolver simulación y, si aplica, generar informe.
