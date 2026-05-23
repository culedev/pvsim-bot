/************ CONFIG ************/
const MONTHS_ORDER = ['Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'];


/************ MENÚ ************/
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('TFG FV')
    .addItem('Pegar JSON y rellenar', 'promptAndPopulate')
    .addItem('Rellenar desde Raw!A1', 'populateFromRaw')
    .addSeparator()
    .addItem('Nueva prueba → Copiar template y rellenar', 'createRunFromPrompt')
    .addToUi();
}

/************ CARPETA ************/
function createRunFolder_(data) {
  const now = new Date();
  const y = now.getFullYear();
  const pad = n => String(n).padStart(2,'0');
  const stamp = `${y}${pad(now.getMonth()+1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;

  const addrRaw = (data?.location_info?.address ||
                   `${data?.location_info?.latitude || data?.lat || ''},${data?.location_info?.longitude || data?.lon || ''}` ||
                   'sin_ubicacion').toString();

  // Nota: algunos motores de Apps Script no soportan \p{L}; usamos ascii-safe.
  const addr = addrRaw.replace(/[^A-Za-z0-9\s\-_.,]/g,'').replace(/\s+/g,' ').trim().slice(0,80) || 'ubicacion';

  const name = `FV_${addr}_${stamp}`;
  const parent = PARENT_FOLDER_ID ? DriveApp.getFolderById(PARENT_FOLDER_ID) : DriveApp.getRootFolder();
  return parent.createFolder(name);
}

/************ ENTRADAS ************/
function promptAndPopulate() {
  const ui = SpreadsheetApp.getUi();
  const res = ui.prompt('Pega aquí el JSON de /simulate', ui.ButtonSet.OK_CANCEL);
  if (res.getSelectedButton() !== ui.Button.OK) return;
  const jsonText = res.getResponseText();
  populateIn_(SpreadsheetApp.getActive(), JSON.parse(jsonText));
  ui.alert('✅ Hoja rellenada con éxito.');
}

function populateFromRaw() {
  const ss = SpreadsheetApp.getActive();
  const sh = ss.getSheetByName('Raw') || ss.insertSheet('Raw');
  const jsonText = String(sh.getRange('A1').getValue() || '').trim();
  if (!jsonText) {
    SpreadsheetApp.getUi().alert('No se encontró JSON en Raw!A1');
    return;
  }
  populateIn_(ss, JSON.parse(jsonText));
  SpreadsheetApp.getUi().alert('✅ Hoja rellenada con éxito desde Raw!A1.');
}

function createRunFromPrompt() {
  const ui = SpreadsheetApp.getUi();
  const res = ui.prompt('Pega el JSON para la nueva prueba', ui.ButtonSet.OK_CANCEL);
  if (res.getSelectedButton() !== ui.Button.OK) return;

  const data = JSON.parse(res.getResponseText());
  const folder = createRunFolder_(data);
  const newId = createRunSpreadsheet(JSON.stringify(data), folder);
  const url = 'https://docs.google.com/spreadsheets/d/' + newId + '/edit';
  ui.alert('✅ Creada nueva prueba en carpeta:\n' + folder.getUrl() + '\n\n' + url);
}

/************ NÚCLEO ************/
function populateIn_(ss, data) {
  writeSummary_(ss, data);
  writeMonthly_(ss, data);
  writeLosses_(ss, data);
  writeInputs_(ss, data);
  ensureCharts_(ss);
}

function createRunSpreadsheet(jsonText, folder) {
  const now = new Date();
  const name = `FV_${now.toISOString().slice(0,19).replace(/[:T]/g,'-')}`;
  const src = SpreadsheetApp.getActive();
  const newFile = DriveApp.getFileById(src.getId()).makeCopy(name, folder || DriveApp.getRootFolder());
  const ss = SpreadsheetApp.openById(newFile.getId());
  populateIn_(ss, JSON.parse(jsonText));
  return newFile.getId();
}

/************ ESCRITURAS ************/
function writeSummary_(ss, data) {
  const sh = ss.getSheetByName('Summary') || ss.insertSheet('Summary');
  if (sh.getLastRow() > 0 && sh.getLastColumn() > 0) sh.getDataRange().clearContent();

  sh.getRange('A1').setValue('RESUMEN');

  const rows = [
    ['Potencia Instalada (kWp)', safe(data?.system_config?.total_power_kwp)],
    ['Energía anual (kWh)', safe(data?.energy_production?.annual_production_kwh)],
    ['PR', safe(data?.energy_production?.performance_ratio)],
    ['Capacity Factor (%)', safe(data?.energy_production?.capacity_factor_percent)],
    ['Payback (años)', safe(data?.economic_analysis?.payback_years)],
    ['VAN (€)', safe(data?.economic_analysis?.npv_25_years_eur)],
    ['TIR (%)', safe(data?.economic_analysis?.irr_percent)],
    ['LCOE (€/kWh)', safe(data?.economic_analysis?.lcoe_eur_kwh)],
  ];
  sh.getRange(2,1,rows.length,2).setValues(rows);
}

function writeMonthly_(ss, data) {
  const sh = ss.getSheetByName('Monthly') || ss.insertSheet('Monthly');
  if (sh.getLastRow() > 0 && sh.getLastColumn() > 0) sh.getDataRange().clearContent();

  sh.getRange(1,1,1,8).setValues([[
    'Mes','Producción (kWh)','Consumo (kWh)','Autoconsumo (kWh)',
    'Inyección a red (kWh)','Compra a red (kWh)',
    'Tasa de autoconsumo (%)','Tasa de autosuficiencia (%)'
  ]]);

  const monthly = data?.autoconsumption_analysis?.monthly_analysis || [];
  const prod = data?.energy_production?.monthly_production || [];

  const map = {};
  monthly.forEach((m, i) => {
    map[String(m.month_name)] = [
      String(m.month_name),
      Number(prod[i] ?? 0),
      Number(m.consumption_kwh ?? 0),
      Number(m.self_consumption_kwh ?? 0),
      Number(m.grid_injection_kwh ?? 0),
      Number(m.grid_consumption_kwh ?? 0),
      Number(m.self_consumption_rate_percent ?? 0),
      Number(m.self_sufficiency_rate_percent ?? 0)
    ];
  });

  const rows = MONTHS_ORDER.map(mn => map[mn] || [mn,0,0,0,0,0,0,0]);
  sh.getRange(2,1,rows.length,rows[0].length).setValues(rows);
}

function writeLosses_(ss, data) {
  const sh = ss.getSheetByName('Losses') || ss.insertSheet('Losses');
  if (sh.getLastRow() > 0 && sh.getLastColumn() > 0) sh.getDataRange().clearContent();

  sh.getRange(1,1,1,3).setValues([['Pérdida','Porcentaje (%)','Energía perdida (kWh)']]);

  const sysLosses = {
    'temperature':0.10,'irradiance':0.03,'spectral':0.015,'soiling':0.02,'shading':0.03,
    'mismatch':0.02,'ohmic_dc':0.015,'ohmic_ac':0.01,'inverter':0.02,'availability':0.01
  };

  const dcPct = Number(data?.electrical_analysis?.dc_cable_losses_percent ?? 0) / 100.0;
  const acPct = Number(data?.electrical_analysis?.ac_cable_losses_percent ?? 0) / 100.0;

  const annual = Number(data?.energy_production?.annual_production_kwh ?? 0);
  const denom = 1 - (dcPct + acPct);
  const baseKWh = denom > 0 ? (annual / denom) : annual;

  const rows = Object.entries(sysLosses).map(([k,p]) => [k, p*100, baseKWh*p]);
  rows.push(['cable_dc', dcPct*100, baseKWh*dcPct]);
  rows.push(['cable_ac', acPct*100, baseKWh*acPct]);

  sh.getRange(2,1,rows.length,3).setValues(rows);
}

function writeInputs_(ss, data) {
  const sh = ss.getSheetByName('Inputs') || ss.insertSheet('Inputs');
  if (sh.getLastRow() > 0 && sh.getLastColumn() > 0) sh.getDataRange().clearContent();

  const kv = [
    ['latitude', safe(data?.location_info?.latitude)],
    ['longitude', safe(data?.location_info?.longitude)],
    ['timezone', safe(data?.location_info?.timezone)],
    ['calculation_date', safe(data?.location_info?.calculation_date)],

    ['optimal_tilt', safe(data?.geometry_analysis?.optimal_tilt)],
    ['optimal_azimuth', safe(data?.geometry_analysis?.optimal_azimuth)],
    ['installation_tilt', safe(data?.geometry_analysis?.installation_tilt)],
    ['installation_azimuth', safe(data?.geometry_analysis?.installation_azimuth)],
    ['with_support_structure', safe(data?.geometry_analysis?.with_support_structure)],

    ['module_model', safe(data?.technical_specs?.module_model)],
    ['module_power_wp', safe(data?.technical_specs?.module_power_wp)],
    ['inverter_model', safe(data?.technical_specs?.inverter_model)],
    ['inverter_power_kw', safe(data?.technical_specs?.inverter_power_kw)],
    ['dc_ac_ratio', safe(data?.technical_specs?.dc_ac_ratio)],

    ['modules_total', safe(data?.system_config?.total_modules)],
    ['modules_per_string', safe(data?.system_config?.modules_per_string)],
    ['strings_parallel', safe(data?.system_config?.strings_parallel)],
    ['array_configuration', safe(data?.system_config?.array_configuration)],

    ['mppt_compatibility', safe(data?.electrical_analysis?.mppt_compatibility)],
    ['dc_cable_losses_percent', safe(data?.electrical_analysis?.dc_cable_losses_percent)],
    ['ac_cable_losses_percent', safe(data?.electrical_analysis?.ac_cable_losses_percent)],

    ['recommended_fuse_dc_a', safe(data?.protections?.recommended_fuse_dc_a)],
    ['recommended_breaker_ac_a', safe(data?.protections?.recommended_breaker_ac_a)],

    ['co2_avoided_kg_per_year', safe(data?.environmental_impact?.co2_avoided_kg_per_year)],
    ['factor_kg_per_kwh', safe(data?.environmental_impact?.factor_kg_per_kwh)],
  ];

  sh.getRange(1,1,kv.length,2).setValues(kv);
}

/************ GRÁFICAS MENSUALES ************/
function ensureCharts_(ss) {
  const sh = ss.getSheetByName('Monthly');
  const charts = sh.getCharts();
  if (charts.length >= 3) return;

  // 1) Producción vs Consumo (Column)
  let c1 = sh.newChart().asColumnChart()
    .addRange(sh.getRange('A1:C13'))
    .setPosition(1,10,0,0)
    .setOption('title','Producción vs Consumo mensual (kWh/mes)')
    .build();

  // 2) Autoconsumo + Inyección (Stacked)
  let c2 = sh.newChart().asColumnChart()
    .addRange(sh.getRange('A1:A13'))
    .addRange(sh.getRange('D1:E13'))
    .setOption('isStacked', true)
    .setPosition(16,10,0,0)
    .setOption('title','Balance de autoconsumo e inyección a red (kWh/mes)')
    .build();

  // 3) Compra a Red (Line)
  let c3 = sh.newChart().asLineChart()
    .addRange(sh.getRange('A1:A13'))
    .addRange(sh.getRange('F1:F13'))
    .setPosition(31,10,0,0)
    .setOption('title','Compra de energía a red (kWh/mes)')
    .build();

  sh.insertChart(c1); sh.insertChart(c2); sh.insertChart(c3);
}


/************ UTILES ************/
function safe(v) { return (v === null || v === undefined) ? '' : v; }

/************ ENDPOINT (webapp) ************/
function doPost(e) {
  try {
    const data = JSON.parse(e.postData.contents);

    // 0) Carpeta
    const folder = createRunFolder_(data);

    // 1) Hoja en carpeta
    const spreadsheetId = createRunSpreadsheet(JSON.stringify(data), folder);

    // 2) CSV en carpeta
    const csvInfo = exportHourlyCSV_(data, folder);

    // 3) Documento en carpeta (crea e inserta gráficos)
    const docId = createDocFromTemplate(spreadsheetId, data, folder);

    // 4) Añadir al final el enlace del CSV
    const doc = DocumentApp.openById(docId);
    const body = doc.getBody();
    body.appendParagraph('').setSpacingAfter(0);
    body.appendParagraph('Enlace al CSV horario').setHeading(DocumentApp.ParagraphHeading.HEADING3);
    body.appendParagraph(csvInfo.name).setLinkUrl(csvInfo.url);
    doc.saveAndClose();

    // Exportar el informe a PDF en la misma carpeta
    const pdfInfo = exportDocToPdf_(docId, folder);

    return ContentService.createTextOutput(JSON.stringify({
      success: true,
      folder_url: folder.getUrl(),
      sheet_url: `https://docs.google.com/spreadsheets/d/${spreadsheetId}/edit`,
      doc_url: `https://docs.google.com/document/d/${docId}/edit`,
      csv_url: csvInfo.url,
      pdf_url: pdfInfo.url
    })).setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({
      success: false,
      error: err.message
    })).setMimeType(ContentService.MimeType.JSON);
  }
}

/************ DOC DESDE TEMPLATE ************/
function createDocFromTemplate(spreadsheetId, data, folder) {
  const destFolder = folder || DriveApp.getRootFolder();
  const copy = DriveApp.getFileById(TEMPLATE_DOC_ID)
    .makeCopy(`Informe FV - ${new Date().toISOString().slice(0,10)}`, destFolder);
  const doc = DocumentApp.openById(copy.getId());
  const body = doc.getBody();

  const map = buildPlaceholderMap_(data);
  replaceAllPlaceholders_(body, map);

  const ss = SpreadsheetApp.openById(spreadsheetId);
  writeHourlyAndDerived_(ss, data);
  SpreadsheetApp.flush();
  ensureHourlyCharts_(ss);
  SpreadsheetApp.flush();
  insertAnnexCharts_(ss, body);

  body.appendParagraph('').setSpacingAfter(0);
  body.appendParagraph('Carpeta del proyecto').setHeading(DocumentApp.ParagraphHeading.HEADING3);
  body.appendParagraph(destFolder.getName()).setLinkUrl(destFolder.getUrl());

  // 👇 Limpia la portada duplicada si existe
  stripDuplicateCover_(doc);

  doc.saveAndClose();
  return doc.getId();
}

/************ PLACEHOLDERS ************/
function replaceAllPlaceholders_(body, map) {
  Object.keys(map).forEach(k => {
    const pattern = k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    body.replaceText(pattern, String(map[k] ?? ''));
  });
}

function buildPlaceholderMap_(data) {
  const yesNo = v => (v === true || v === 'true') ? 'Sí' : (v === false || v === 'false') ? 'No' : String(v ?? '');
  const fmt = (n, d=2) => (typeof n === 'number') ? n.toFixed(d) : (n ?? '');
  const get = (o, path, def='') => { try { return path.split('.').reduce((a, c) => (a && a[c] !== undefined ? a[c] : undefined), o) ?? def; } catch { return def; } };

  const direccion = get(data, 'location_info.address', '');
  const fuente = 'PVGIS TMY' + (get(data, 'location_info.data_source','') ? ` + ${get(data,'location_info.data_source','')}` : '');
  const usoSolarAPI = /solar/i.test(get(data,'location_info.data_source','')) ? 'Sí' : 'No';

  const cumpleCaidaDC = yesNo(get(data, 'cable_section_analysis.cable_dc.meets_voltage_drop', ''));
  const cumpleCaidaAC = yesNo(get(data, 'cable_section_analysis.cable_ac.meets_voltage_drop', ''));
  const cumpleUneDC   = yesNo(get(data, 'cable_section_analysis.cable_dc.meets_ampacity', ''));
  const cumpleUneAC   = yesNo(get(data, 'cable_section_analysis.cable_ac.meets_ampacity', ''));
  const cumpleCTE     = (cumpleCaidaDC === 'Sí' && cumpleCaidaAC === 'Sí' && cumpleUneDC === 'Sí' && cumpleUneAC === 'Sí') ? 'Sí' : 'Revisar';

  return {
    '{{direccion}}': direccion,
    '{{fecha}}': new Date().toLocaleDateString('es-ES'),

    '{{potencia_kwp}}': fmt(get(data,'system_config.total_power_kwp', '')),
    '{{produccion_anual_kwh}}': fmt(get(data,'energy_production.annual_production_kwh',''),0),
    '{{pr}}': fmt(get(data,'energy_production.performance_ratio',''),3),
    '{{payback}}': fmt(get(data,'economic_analysis.payback_years',''),2),
    '{{van}}': fmt(get(data,'economic_analysis.npv_25_years_eur',''),0),
    '{{tir}}': fmt(get(data,'economic_analysis.irr_percent',''),2),
    '{{lcoe}}': fmt(get(data,'economic_analysis.lcoe_eur_kwh',''),3),

    '{{lat}}': fmt(get(data,'location_info.latitude',''),6),
    '{{lon}}': fmt(get(data,'location_info.longitude',''),6),
    '{{fuente_datos}}': fuente,
    '{{uso_solar_api}}': usoSolarAPI,
    '{{consumo_anual}}': fmt(get(data,'autoconsumption_analysis.annual_consumption_kwh',''),0),
    '{{roof_area}}': fmt(get(data,'system_config.total_area_m2','') || get(data,'inputs.roof_area_m2',''),1),

    '{{tilt_optimo}}': fmt(get(data,'geometry_analysis.optimal_tilt',''),1),
    '{{azimut_optimo}}': fmt(get(data,'geometry_analysis.optimal_azimuth',''),1),
    '{{tilt_real}}': fmt(get(data,'geometry_analysis.installation_tilt',''),1),
    '{{azimut_real}}': fmt(get(data,'geometry_analysis.installation_azimuth',''),1),
    '{{irradiacion_optima}}': fmt(get(data,'geometry_analysis.annual_irradiation_optimal',''),0),
    '{{irradiacion_real}}': fmt(get(data,'geometry_analysis.annual_irradiation_real',''),0),
    '{{perdidas_orientacion}}': fmt(get(data,'geometry_analysis.orientation_losses_percent',''),2),
    '{{perdidas_sombreado}}': fmt(get(data,'geometry_analysis.shading_losses_percent',''),2),

    '{{modelo_modulo}}': get(data,'technical_specs.module_model',''),
    '{{potencia_modulo}}': fmt(get(data,'technical_specs.module_power_wp',''),0),
    '{{eficiencia_modulo}}': fmt(get(data,'technical_specs.module_efficiency_percent',''),1),
    '{{num_modulos}}': fmt(get(data,'system_config.total_modules',''),0),
    '{{area_total}}': fmt(get(data,'system_config.total_area_m2',''),1),
    '{{modelo_inversor}}': get(data,'technical_specs.inverter_model',''),
    '{{potencia_inversor}}': fmt(get(data,'technical_specs.inverter_power_kw',''),2),
    '{{eficiencia_inversor}}': fmt(get(data,'technical_specs.inverter_efficiency_percent',''),1),
    '{{dc_ac_ratio}}': fmt(get(data,'technical_specs.dc_ac_ratio',''),2),
    '{{compatibilidad_mppt}}': yesNo(get(data,'electrical_analysis.mppt_compatibility','')),
    '{{strings_config}}': get(data,'system_config.array_configuration',''),
    '{{clipping_info}}': get(data,'analysis.clipping','N/A'),

    '{{caida_tension_dc}}': fmt(get(data,'cable_analysis.voltage_drop_dc_percent','') || get(data,'electrical_analysis.dc_cable_losses_percent',''),2),
    '{{seccion_dc}}': fmt(get(data,'cable_section_analysis.cable_dc.chosen_section_mm2',''),1),
    '{{cumple_une_dc}}': cumpleUneDC,
    '{{caida_tension_ac}}': fmt(get(data,'cable_analysis.voltage_drop_ac_percent','') || get(data,'electrical_analysis.ac_cable_losses_percent',''),2),
    '{{seccion_ac}}': fmt(get(data,'cable_section_analysis.cable_ac.chosen_section_mm2',''),1),
    '{{cumple_une_ac}}': cumpleUneAC,
    '{{fusible_dc}}': fmt(get(data,'protections.recommended_fuse_dc_a',''),1),
    '{{breaker_ac}}': fmt(get(data,'protections.recommended_breaker_ac_a',''),1),
    '{{cumple_caida_dc}}': cumpleCaidaDC,
    '{{cumple_caida_ac}}': cumpleCaidaAC,

    '{{rendimiento_especifico}}': fmt(get(data,'energy_production.specific_yield_kwh_kwp',''),0),
    '{{cf}}': fmt(get(data,'energy_production.capacity_factor_percent',''),1),

    '{{autoconsumo_anual}}': fmt(get(data,'autoconsumption_analysis.annual_self_consumption_kwh',''),0),
    '{{tasa_autoconsumo}}': fmt(get(data,'autoconsumption_analysis.self_consumption_rate_percent',''),1),
    '{{tasa_autosuficiencia}}': fmt(get(data,'autoconsumption_analysis.self_sufficiency_rate_percent',''),1),
    '{{inyeccion_anual}}': fmt(get(data,'autoconsumption_analysis.annual_grid_injection_kwh',''),0),
    '{{compra_red_anual}}': fmt(get(data,'autoconsumption_analysis.annual_grid_purchase_kwh',''),0),

    '{{coste_total}}': fmt(get(data,'economic_analysis.system_cost_eur',''),0),
    '{{ahorro_anual}}': fmt(get(data,'economic_analysis.annual_savings_eur',''),0),
    '{{precio_kwh}}': fmt(get(data,'inputs.electricity_price','') || get(data,'economic_analysis.electricity_price',''),3),
    '{{precio_excedente}}': fmt(get(data,'inputs.surplus_price','') || get(data,'economic_analysis.surplus_price',''),3),

    '{{cumple_cte}}': cumpleCTE,
  };
}

/************ CSV ************/
function exportHourlyCSV_(data, folder) {
  const prod = data?.hourly_production_ac_kwh || [];
  const cons = data?.hourly_consumption_kwh || [];
  const timestamps = [];

  const baseDate = new Date(`${new Date().getFullYear()}-01-01T00:00:00Z`);
  for (let i = 0; i < prod.length; i++) {
    const ts = new Date(baseDate.getTime() + i * 3600000);
    timestamps.push(ts.toISOString());
  }

  const rows = [['timestamp','production_kWh','consumption_kWh']];
  for (let i = 0; i < prod.length; i++) {
    rows.push([timestamps[i], prod[i] ?? '', cons[i] ?? '']);
  }

  const csvContent = rows.map(r => r.join(',')).join('\n');
  const blob = Utilities.newBlob(csvContent, 'text/csv', `datos_horarios_${new Date().toISOString().slice(0,10)}.csv`);

  const parent = folder || DriveApp.getRootFolder();
  const file = parent.createFile(blob);
  return { url: file.getUrl(), fileId: file.getId(), name: file.getName() };
}

/************ HORARIO: VOLCADO + DERIVADOS (sin QUERY) ************/
function writeHourlyAndDerived_(ss, data) {
  const tz = data?.location_info?.timezone || 'Europe/Madrid';

  const prod = data?.hourly_production_ac_kwh || [];
  const cons = data?.hourly_consumption_kwh || [];
  const N = Math.max(prod.length, cons.length);
  if (!N) return;

  // HOURLY
  const sh = ss.getSheetByName('Hourly') || ss.insertSheet('Hourly');
  sh.clear();
  sh.appendRow(['timestamp','production_kWh','consumption_kWh','month_local','hour_local']);

  const baseDate = new Date(`${new Date().getFullYear()}-01-01T00:00:00Z`);
  const rows = [];
  for (let i=0;i<N;i++){
    const ts = new Date(baseDate.getTime() + i*3600000);
    const monthLocal = parseInt(Utilities.formatDate(ts, tz, 'M'));
    const hourLocal  = parseInt(Utilities.formatDate(ts, tz, 'H'));
    rows.push([ts.toISOString(), Number(prod[i]||0), Number(cons[i]||0), monthLocal, hourLocal]);
  }
  sh.getRange(2,1,rows.length,5).setValues(rows);

  // DERIVADOS con cabeceras más claras
  const dsh = ss.getSheetByName('Hourly_Derived') || ss.insertSheet('Hourly_Derived');
  dsh.clear();
  dsh.appendRow([
    'Hora',
    'Producción Enero','Consumo Enero',
    'Producción Julio','Consumo Julio',
    'Producción Media Anual','Consumo Medio Anual'
  ]);

  const agg = (targetMonth) => {
    const n = Array(24).fill(0), sp = Array(24).fill(0), sc = Array(24).fill(0);
    rows.forEach(r=>{
      const m = r[3], h = r[4]; const p=r[1], c=r[2];
      if (targetMonth === null || m === targetMonth) { n[h]++; sp[h]+=p; sc[h]+=c; }
    });
    return {avgP: sp.map((v,i)=> n[i]? v/n[i]:0), avgC: sc.map((v,i)=> n[i]? v/n[i]:0)};
  };
  const jan = agg(1), jul = agg(7), all = agg(null);
  for (let h=0;h<24;h++){
    dsh.appendRow([h, jan.avgP[h], jan.avgC[h], jul.avgP[h], jul.avgC[h], all.avgP[h], all.avgC[h]]);
  }

  // ORDENADO
  const srt = ss.getSheetByName('Hourly_Sorted') || ss.insertSheet('Hourly_Sorted');
  srt.clear();
  srt.appendRow(['Rank','Producción ordenada','Consumo ordenado']);
  const pSorted = rows.map(r=>r[1]).sort((a,b)=>b-a);
  const cSorted = rows.map(r=>r[2]).sort((a,b)=>b-a);
  const L = Math.max(pSorted.length, cSorted.length);
  const sRows = [];
  for (let i=0;i<L;i++) sRows.push([i+1, pSorted[i]||0, cSorted[i]||0]);
  srt.getRange(2,1,sRows.length,3).setValues(sRows);

  SpreadsheetApp.flush();
}

/************ HORARIO: CREAR GRÁFICOS ************/
function ensureHourlyCharts_(ss) {
  const dsh = ss.getSheetByName('Hourly_Derived');
  const srt = ss.getSheetByName('Hourly_Sorted');
  if (!dsh || !srt) return;

  dsh.getCharts().forEach(c=>dsh.removeChart(c));
  srt.getCharts().forEach(c=>srt.removeChart(c));

  // A) Curvas diarias típicas: Enero vs Julio
  const c1 = dsh.newChart().asLineChart()
    .addRange(dsh.getRange(1,1,25,5)) // Hora, ProdEne, ConsEne, ProdJul, ConsJul
    .setOption('title','Curvas diarias típicas: Enero vs Julio (kWh/h)')
    .setOption('legend', { position: 'bottom' })
    .setOption('series', {
      0: { labelInLegend: 'Producción Enero' },
      1: { labelInLegend: 'Consumo Enero' },
      2: { labelInLegend: 'Producción Julio' },
      3: { labelInLegend: 'Consumo Julio' }
    })
    .setPosition(1,8,0,0)
    .build();
  dsh.insertChart(c1);

  // B) Perfil horario promedio anual
  const c2 = dsh.newChart().asLineChart()
    .addRange(dsh.getRange(1,1,25,1)) // Hora
    .addRange(dsh.getRange(1,6,25,2)) // Producción media anual, Consumo medio anual
    .setOption('title','Perfil horario promedio anual (kWh/h)')
    .setOption('legend', { position: 'bottom' })
    .setOption('series', {
      0: { labelInLegend: 'Producción media anual' },
      1: { labelInLegend: 'Consumo medio anual' }
    })
    .setPosition(20,8,0,0)
    .build();
  dsh.insertChart(c2);

  // C) Curva de duración de carga (LDC)
  const last = srt.getLastRow();
  const c3 = srt.newChart().asLineChart()
    .addRange(srt.getRange(1,1,last,1)) // Rank
    .addRange(srt.getRange(1,2,last,2)) // Producción ordenada, Consumo ordenado
    .setOption('title','Curva de duración de carga (LDC) – Producción y Consumo')
    .setOption('legend', { position: 'bottom' })
    .setOption('series', {
      0: { labelInLegend: 'Producción ordenada' },
      1: { labelInLegend: 'Consumo ordenado' }
    })
    .setPosition(1,6,0,0)
    .build();
  srt.insertChart(c3);
}


/** Exporta un Google Doc a PDF exactamente igual que si usas "Archivo > Descargar > PDF". */
function exportDocToPdf_(docId, folder) {
  const parent = folder || DriveApp.getRootFolder();
  const file = DriveApp.getFileById(docId);
  const namePdf = file.getName().replace(/\.docx?$/i,'') + '.pdf';

  // Usa la URL oficial de exportación de Google Docs
  const url = `https://docs.google.com/document/d/${docId}/export?format=pdf`;

  const resp = UrlFetchApp.fetch(url, {
    headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
    muteHttpExceptions: true
  });

  const blob = resp.getBlob().setName(namePdf);
  const pdfFile = parent.createFile(blob);

  return { url: pdfFile.getUrl(), fileId: pdfFile.getId(), name: pdfFile.getName() };
}

/** Elimina la portada textual "Informe FV" que pueda venir en la plantilla. */
function stripDuplicateCover_(doc) {
  const body = doc.getBody();
  if (body.getNumChildren() === 0) return;

  const first = body.getChild(0);
  if (first && first.getType() === DocumentApp.ElementType.PARAGRAPH) {
    const txt = first.asParagraph().getText().trim();
    if (/^informe\s*fv$/i.test(txt)) {
      first.removeFromParent();
      // Si justo después hay un salto de página, elimínalo también
      const next = body.getChild(0);
      if (next && next.getType && next.getType() === DocumentApp.ElementType.PAGE_BREAK) {
        next.removeFromParent();
      }
    }
  }
}

/************ INSERTAR EN ANEXO (con títulos y pies profesionales) ************/
function insertAnnexCharts_(ss, body) {
  const order = ['Monthly','Hourly_Derived','Hourly_Sorted'];
  const chartsBySheet = {};

  // Recoge gráficos por hoja preservando orden
  order.forEach(name => {
    const sh = ss.getSheetByName(name);
    if (!sh) return;
    chartsBySheet[name] = sh.getCharts() || [];
  });

  // Si no hay gráficos, informa
  const totalCharts = order.reduce((acc,n)=>acc + ((chartsBySheet[n]||[]).length), 0);
  if (!totalCharts) {
    body.appendParagraph('⚠️ No se encontraron gráficas para anexar.');
    return;
  }

  body.appendParagraph('').setSpacingAfter(0);
  body.appendParagraph('Anexo de gráficas').setHeading(DocumentApp.ParagraphHeading.HEADING2);

  // Helper: meta por hoja/índice
  const metaFor = (sheet, idx) => {
    if (sheet === 'Monthly') {
      if (idx === 0) {
        return {
          title: 'Producción vs Consumo mensual (kWh/mes)',
          caption: 'Comparativa mensual entre la energía fotovoltaica generada y el consumo eléctrico.'
        };
      }
      if (idx === 1) {
        return {
          title: 'Balance de autoconsumo e inyección a red (kWh/mes)',
          caption: 'Desglose mensual entre energía autoconsumida y excedentes vertidos a la red.'
        };
      }
      return {
        title: 'Compra de energía a red (kWh/mes)',
        caption: 'Demanda cubierta mediante compras a la red eléctrica en cada mes.'
      };
    }

    if (sheet === 'Hourly_Derived') {
      if (idx === 0) {
        return {
          title: 'Curvas diarias típicas: Enero vs Julio (kWh/h)',
          caption: 'Comparación de perfiles horarios de producción y consumo en invierno (enero) y verano (julio).'
        };
      }
      return {
        title: 'Perfil horario promedio anual (kWh/h)',
        caption: 'Producción fotovoltaica y consumo promedio por hora a lo largo del año.'
      };
    }

    // Hourly_Sorted
    return {
      title: 'Curva de duración de carga (LDC) – Producción y Consumo',
      caption: 'Distribución ordenada de valores horarios para analizar la persistencia de niveles de producción y demanda.'
    };
  };

  // Inserta en el orden definido con numeración continua
  let fig = 1;
  order.forEach(sheetName => {
    const list = chartsBySheet[sheetName] || [];
    list.forEach((ch, idx) => {
      const meta = metaFor(sheetName, idx);
      const img = ch.getAs('image/png');

      const titleP = body.appendParagraph(`Figura ${fig}. ${meta.title}`);
      titleP.setBold(true);

      const inline = body.appendParagraph('').appendInlineImage(img);
      inline.setWidth(500);

      const capP = body.appendParagraph(meta.caption);
      capP.setItalic(true);

      body.appendParagraph(''); // separación
      fig++;
    });
  });
}

