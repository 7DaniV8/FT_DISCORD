/* ────────────────────────────────────────────────────────────────────
   tabs/dashboard_estrategia.js — Dashboard "estilo Historial de Picks"
   para 🎓 Partidos Maestros, 🚨 Cuota Mal Puesta y 📈 UTR VALUE
   (08/10/2026, Rubén).

   Mismas tarjetas que Picks Pre-partido / en Vivo: reusa pkTarjeta,
   pkNum, pkPct, pkSigno, pkColor y pkFecha de picks.js (que se carga
   antes). Los KPIs los calcula el SERVIDOR con las mismas fórmulas que
   Picks (production/engines/dashboard_estrategia.py): acá no se hace
   ninguna cuenta de plata.

   Cada pestaña deja un <div data-dashboard="clave"></div> y llama a
   deCargar('clave') desde su función de carga.
   ──────────────────────────────────────────────────────────────────── */

// 08/10/2026 (Rubén): Cuota Mal Puesta y UTR Value cuentan SOLO lo que llegó a Discord
// (vista fija, sin selector); los usuarios ven ganadas/perdidas y el historial, los
// admin ven todas las tarjetas y pueden reiniciar el historial (reinicio: true).
const DE_CONF = {
  maestros: { endpoint: '/research/api/dashboard/partidos-maestros', fecha: 'Partido',
              vacio: 'Todavía no calificó ningún partido con esta vista.', kpis: 'maestros', reinicio: true },
  cmp:      { endpoint: '/research/api/dashboard/cuota-mal-puesta', fecha: 'Detectada',
              vacio: 'Todavía no hay señales enviadas a Discord.', fijo: 'discord', basico: true, reinicio: true },
  utrv:     { endpoint: '/research/api/dashboard/utr-value', fecha: 'Visto',
              vacio: 'Todavía no hay partidos enviados a Discord.', fijo: 'discord', basico: true, reinicio: true },
  // 08/10/2026 (Rubén): Under en Vivo y UTR Market Anomaly con dashboard propio.
  under:    { endpoint: '/research/api/dashboard/under-vivo', fecha: 'Alerta',
              vacio: 'Todavía no hay Under que hayan cumplido la estrategia con esta vista.' },
  utra:     { endpoint: '/research/api/dashboard/utr-anomalia', fecha: 'Visto',
              vacio: 'Todavía no hay anomalías enviadas a Discord.', fijo: 'discord', basico: true, reinicio: true },
  // 📡 UTR EXTERNO (09/10/2026): las alertas del canal TIRS que sonaron en Discord, en nuestro formato.
  utrx:     { endpoint: '/research/api/dashboard/utr-externo', fecha: 'Recibida',
              vacio: 'Todavía no llegó ninguna alerta del canal a Discord.', basico: true, reinicio: true },
  // 📰 FT NEWS (08/10/2026 noche): solo las 🔥 dichas en Discord; lo calcula el servicio FT_NEWS.
  // 🩹 Recordador IA (11/10/2026 v43d): predicciones dichas en Discord con favorito ≥ 60 %.
  recordador: { endpoint: '/research/api/dashboard/recordador', fecha: 'Avisada',
              vacio: 'Todavía no hay avisos dichos en Discord con un favorito de 60 % o más.', fijo: 'discord', basico: true },
  news:     { endpoint: '/research/api/dashboard/ft-news', fecha: 'Dicha',
              vacio: 'Todavía no hay alertas dichas en Discord.', fijo: 'discord', basico: true },
  // 🏆 Implicación de Torneo (13/10/2026 v45): solo los PICKS del motor, nunca los partidos investigados.
  // Vistas: ✅ validados / 🧪 experimentales / todos. Dashboard completo para todos (como Picks).
  implicacion: { endpoint: '/research/api/dashboard/implicacion', fecha: 'Detectado',
              vacio: 'Todavía no hay picks con esta vista. El motor los guarda desde el día en que se activa.' },
  // 📉 Movimiento Rentable (v54f, 14/10/2026): lo que salió a Telegram, a la cuota de ENTRADA (la actual).
  mov:      { endpoint: '/research/api/dashboard/movimiento-rentable', fecha: 'Enviado',
              vacio: 'Todavía no hay movimientos enviados a Telegram con esta vista.' },
  // 🔥 UNDER UTR (v54i, 15/10/2026): todas las activaciones (histórico completo) o solo las que salieron a Discord.
  // 🎲 FT PROBABILIDAD (30/10/2026): candidatos (prob > 60 % y cuota > 1.70) que se enviaron a Discord.
  ftprob:   { endpoint: '/research/api/dashboard/ft-probabilidad', fecha: 'Enviado',
              vacio: 'Todavía no hay candidatos enviados a Discord.', fijo: 'discord', basico: true },
  underutr: { endpoint: '/research/api/dashboard/under-utr', fecha: 'Activada',
              vacio: 'Todavía no se activó ningún UNDER UTR con esta vista.' },
};
// 🧠 v54 (13/10/2026, Rubén: «agregar la columna de investigación y su funcionamiento a Partidos Maestros, Cuota
// Mal Puesta y UTR Value»): la misma investigación del Recordador para la selección de cada estrategia. UTR Market
// Anomaly vive en la misma pestaña que UTR VALUE y la lleva también.
const DE_INV = ['maestros', 'cmp', 'utrv', 'utra', 'mov'];   // v54f: + 📉 Movimiento Rentable
function _deCols(clave) { return 8 + (DE_INV.includes(clave) ? 1 : 0); }
function deEsAdmin() {
  const P = window.FTR_PERMISOS || {};
  return P.rol === 'admin' || (P.permisos || []).indexOf('administrar') >= 0;
}
const DE_ESTADO = {};
const DE_PAGINA = {};
const DE_POR_PAGINA = 10;

function _deEl(clave, suf) { return document.getElementById(`de-${clave}-${suf}`); }

function _deMontar(clave) {
  const caja = document.querySelector(`[data-dashboard="${clave}"]`);
  if (!caja || caja.dataset.montado) return caja;
  caja.dataset.montado = '1';
  const conf = DE_CONF[clave], admin = deEsAdmin();
  caja.innerHTML = `
    <h3 class="pk-h"><span class="pk-h-i">📊</span>Dashboard${conf.fijo ? ' <span class="muted" style="font-weight:normal;font-size:0.8rem">· solo lo enviado a Discord</span>' : ''}</h3>
    <div class="ctrl-row">
      <select id="de-${clave}-vista" onchange="DE_PAGINA['${clave}']=1; deCargar('${clave}')"${conf.fijo ? ' style="display:none"' : ''}></select>
      <select id="de-${clave}-genero" onchange="DE_PAGINA['${clave}']=1; deCargar('${clave}')">
        <option value="">Hombres y mujeres</option><option value="M">Hombres</option><option value="F">Mujeres</option>
      </select>
      <span class="muted" id="de-${clave}-estado"></span>
      ${''/* 10/10/2026 (Rubén): sin "Reiniciar historial": lo enviado a Discord nunca se borra; se pagina de a 10 */}
    </div>
    <div id="de-${clave}-aviso" class="warn" style="display:none;margin:4px 0 8px;font-size:0.82rem"></div>
    <div id="de-${clave}-kpis" class="pk-grid"></div>
    <p class="muted" style="font-size:0.74rem;margin:2px 0 8px">${DE_CONF[clave].kpis === 'maestros'
      ? 'Sobre los partidos decididos; % de victoria en dos sets sobre las ganadas con marcador legible (un retiro no cuenta como 2-0); % de remontada: de los que perdieron el primer set, cuántos terminaron ganando (los retiros no cuentan).'
      : 'Apuesta fija de 1 unidad a la cuota con la que la estrategia recomendó el partido. Con apuesta fija, <strong>ROI y Yield son la misma cuenta</strong> (beneficio ÷ total arriesgado), igual que en Picks. Las pendientes no cuentan.'}</p>
    <h3 class="pk-h"><span class="pk-h-i">🕐</span>Historial</h3>
    <div class="table-scroll"><table>
      <thead><tr><th>${DE_CONF[clave].fecha}</th><th>Partido</th><th>Selección</th>${DE_INV.includes(clave) ? '<th title="Lo que investigó FullTennis de los dos jugadores antes del partido">🧠 Investigación</th>' : ''}<th>Torneo</th>
        <th>Cuota</th><th>Resultado</th><th>Marcador</th><th>Unidades</th></tr></thead>
      <tbody id="de-${clave}-tbody"><tr><td colspan="${_deCols(clave)}" class="muted">Cargando...</td></tr></tbody>
    </table></div>
    <div class="ctrl-row" style="justify-content:center;gap:14px">
      <button class="go" id="de-${clave}-prev" onclick="deCambiarPagina('${clave}', -1)">← Anterior</button>
      <span class="muted" id="de-${clave}-pag"></span>
      <button class="go" id="de-${clave}-next" onclick="deCambiarPagina('${clave}', 1)">Siguiente →</button>
    </div>`;
  return caja;
}

// 10/10/2026 (Rubén): "colócame una opción de eliminar filas". Oculta la fila del
// dashboard (y de los KPI); nada se borra de la base y se puede restaurar.
async function deOcultar(clave, id, boton) {
  const fila = boton && boton.closest('tr');
  const partido = fila ? (fila.children[1].textContent || '').trim().slice(0, 60) : `#${id}`;
  if (!confirm(`¿Quitar "${partido}" del dashboard? No se borra de la base; se puede restaurar desde Admin.`)) return;
  const res = await fetch('/admin/estrategias/ocultar', {method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({estrategia: clave, id})});
  const d = await res.json().catch(() => ({}));
  if (!res.ok) { alert('No se pudo: ' + (d.detail || res.status)); return; }
  deCargar(clave);
}

// ✏️ Cerrar a mano (13/10/2026, Rubén: "cerrar apuestas, como en historial de picks"). Usa el
// MISMO modal que Historial de Picks (quién ganó + marcador por sets o escrito). Solo admin.
const DE_CERRABLES = ['maestros', 'cmp', 'utrv', 'utra', 'utrx', 'under', 'recordador', 'implicacion', 'mov', 'underutr'];

function deCerrar(clave, boton) {
  if (typeof abrirModalCerrarAlerta !== 'function' || !document.getElementById('ha-edit-overlay')) {
    alert('Para cerrar a mano hace falta tener acceso a Historial de Picks (ahí vive la ventana de cierre).');
    return;
  }
  const d = boton.dataset;
  // la selección es el lado A (en Under la selección es la línea, no un jugador: no se precarga)
  const ganador = (clave === 'under' || clave === 'underutr') ? '' : d.res === 'ganado' ? d.a : d.res === 'perdido' ? d.b : '';
  abrirModalCerrarAlerta(Number(d.id), d.a, d.b, 'estrategia:' + clave, ganador, d.marcador || '');
}

async function deReiniciar(clave) {
  const nombre = {maestros: 'Partidos Maestros', cmp: 'Cuota Mal Puesta', utrv: 'UTR Value', utra: 'UTR Market Anomaly', utrx: 'UTR Externo'}[clave] || clave;
  if (!confirm(`¿Borrar TODO el historial de ${nombre}? La pestaña arranca de cero (queda un respaldo en la base).`)) return;
  if (prompt('Escribe REINICIAR para confirmar') !== 'REINICIAR') return;
  const res = await fetch('/admin/estrategias/reiniciar', {method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({estrategia: clave, confirmar: 'REINICIAR'})});
  const d = await res.json().catch(() => ({}));
  if (!res.ok) { alert('No se pudo: ' + (d.detail || res.status)); return; }
  alert('Historial reiniciado: ' + JSON.stringify(d.borradas));
  DE_PAGINA[clave] = 1; deCargar(clave);
}

function _deKpis(clave, k) {
  if (DE_CONF[clave].basico && !deEsAdmin()) {
    // 08/10/2026 (Rubén): los usuarios solo ven ganadas y perdidas (y el historial).
    _deEl(clave, 'kpis').innerHTML = [
      pkTarjeta('Ganadas', pkNum(k.ganadas, 0), 'color:var(--green)'),
      pkTarjeta('Perdidas', pkNum(k.perdidas, 0), 'color:var(--red)'),
      pkTarjeta('% de acierto', pkPct(k.hit_rate), '', k.liquidadas ? `sobre ${k.ganadas + k.perdidas} decididas` : ''),
    ].join('');
    return;
  }
  if (DE_CONF[clave].kpis === 'maestros') {
    // 08/10/2026 (Rubén): Partidos Maestros muestra SOLO % de acierto y % de victoria en dos sets.
    _deEl(clave, 'kpis').innerHTML = [
      pkTarjeta('% de acierto', pkPct(k.hit_rate), '', k.liquidadas ? `${k.ganadas} de ${k.ganadas + k.perdidas} decididas` : ''),
      pkTarjeta('% de victoria en dos sets', pkPct(k.pct_dos_sets), '',
                k.dos_sets_base ? `${k.dos_sets} de ${k.dos_sets_base} ganadas con marcador` : 'sin marcadores todavía'),
      // 13/10/2026 (Rubén): los que empezaron perdiendo (perdieron el 1.er set) y remontaron.
      pkTarjeta('% de remontada', pkPct(k.pct_remontada), '',
                k.empezaron_perdiendo ? `${k.remontaron} de ${k.empezaron_perdiendo} que perdieron el 1.er set` : 'ninguno perdió el 1.er set todavía'),
    ].join('');
    return;
  }
  _deEl(clave, 'kpis').innerHTML = [
    pkTarjeta('Total de apuestas', pkNum(k.total, 0), '', k.pendientes ? `${k.pendientes} sin resolver` : ''),
    pkTarjeta('Ganadas', pkNum(k.ganadas, 0), 'color:var(--green)'),
    pkTarjeta('Perdidas', pkNum(k.perdidas, 0), 'color:var(--red)'),
    ...(k.push ? [pkTarjeta('Push', pkNum(k.push, 0), '', 'apuesta devuelta')] : []),
    pkTarjeta('% de acierto', pkPct(k.hit_rate), '', k.liquidadas ? `sobre ${k.ganadas + k.perdidas} decididas` : ''),
    pkTarjeta('Unidades', pkSigno(k.unidades), pkColor(k.unidades)),
    pkTarjeta('ROI', pkPct(k.roi), pkColor(k.roi)),
    pkTarjeta('Yield', pkPct(k['yield']), pkColor(k['yield'])),
    pkTarjeta('Cuota promedio', pkNum(k.cuota_promedio)),
    pkTarjeta('Cuota promedio ganadas', pkNum(k.cuota_promedio_ganadas)),
    pkTarjeta('Mayor racha ganadora', pkNum(k.racha_victorias, 0), 'color:var(--green)'),
    pkTarjeta('Mayor racha perdedora', pkNum(k.racha_derrotas, 0), 'color:var(--red)'),
    pkTarjeta('Ganancia', pkSigno(k.ganancia_dinero), pkColor(k.ganancia_dinero),
              `unidad = ${pkNum(k.valor_unidad)} · arriesgado ${pkNum(k.arriesgado_dinero)}`),
  ].join('');
}

const _DE_RES = {
  seguimiento: ['Seguimiento', 'color:var(--suave)', ''],     // 📰/🔎 de FT NEWS: se muestran, no cuentan como apuesta
  ganado: ['Ganado', 'color:var(--green)', 'background:rgba(46,160,67,.10)'],
  perdido: ['Perdido', 'color:var(--red)', 'background:rgba(220,60,60,.10)'],
  push: ['Push', 'color:var(--amarillo,#e8b339)', ''],
  pendiente: ['Pendiente', 'color:var(--suave)', ''],
};

function deCambiarPagina(clave, delta) {
  const d = DE_ESTADO[clave];
  const total = d ? d.historial.length : 0;
  const max = Math.max(1, Math.ceil(total / DE_POR_PAGINA));
  DE_PAGINA[clave] = Math.min(max, Math.max(1, (DE_PAGINA[clave] || 1) + delta));
  _deHistorial(clave);
}

// 17/10/2026 (Daniel): clic en un jugador → su perfil UTR (como en 🎾 PRÓXIMOS PARTIDOS · UTR). El backend manda
// url_a / url_b solo si hay UTR ID (identidad usada en la señal o confirmada en el padrón); sin URL, texto normal.
// 17/10/2026 (Daniel: «el URL se ve demasiado feo, no se distingue»): el enlace al perfil UTR ya no usa el azul/morado
// del navegador sobre fondo oscuro; texto claro, subrayado punteado en el color de la página y ↗ chico. Al pasar, acento.
(function _estiloLinkUtr() {
  if (document.getElementById('utr-link-css')) return;
  const st = document.createElement('style');
  st.id = 'utr-link-css';
  st.textContent = `a.utr-link{color:#f2f2f2;font-weight:600;text-decoration:none;border-bottom:1px dotted var(--accent,#c6f432);transition:color .15s}
    a.utr-link:visited{color:#f2f2f2}
    a.utr-link:hover,a.utr-link:focus{color:var(--accent,#c6f432);border-bottom-style:solid}
    a.utr-link .utr-ext{font-size:0.7em;opacity:.7;margin-left:2px;vertical-align:super}`;
  document.head.appendChild(st);
})();
function _deJugadorUtr(p, nombre) {
  if (!nombre) return '';
  const url = nombre === p.lado_a ? p.url_a : nombre === p.lado_b ? p.url_b : null;
  return url ? `<a class="utr-link" href="${escaparHtml(url)}" target="_blank" rel="noopener" title="Abrir el perfil UTR">${escaparHtml(nombre)}<span class="utr-ext">↗</span></a>`
             : escaparHtml(nombre);
}
function _dePartidoUtr(p) {
  if ((p.url_a || p.url_b) && p.lado_a && p.lado_b && p.partido === `${p.lado_a} vs ${p.lado_b}`) {
    return `${_deJugadorUtr(p, p.lado_a)} vs ${_deJugadorUtr(p, p.lado_b)}`;
  }
  return escaparHtml(p.partido || '');
}
function _deHistorial(clave) {
  const d = DE_ESTADO[clave];
  const tbody = _deEl(clave, 'tbody');
  const h = (d && d.historial) || [];
  const max = Math.max(1, Math.ceil(h.length / DE_POR_PAGINA));
  const pag = Math.min(DE_PAGINA[clave] || 1, max);
  DE_PAGINA[clave] = pag;
  _deEl(clave, 'pag').textContent = h.length ? `Página ${pag} de ${max}` : '';
  _deEl(clave, 'prev').disabled = pag <= 1;
  _deEl(clave, 'next').disabled = pag >= max;
  if (!h.length) {
    tbody.innerHTML = `<tr><td colspan="${_deCols(clave)}" class="muted">${DE_CONF[clave].vacio}</td></tr>`;
    return;
  }
  tbody.innerHTML = h.slice((pag - 1) * DE_POR_PAGINA, pag * DE_POR_PAGINA).map(p => {
    const [txt, colTxt, colFila] = _DE_RES[p.resultado] || ['—', '', ''];
    const admin = deEsAdmin();
    return `<tr style="${colFila}">
      <td class="muted">${pkFecha(p.fecha)}${admin ? ` <button class="go" style="padding:0 5px;font-size:0.7rem;margin-left:4px" title="Quitar esta fila del dashboard (no se borra de la base; se puede restaurar)" onclick="deOcultar('${clave}', ${Number(p.id)}, this)">🗑</button>` : ''}</td>
      <td>${clave === 'cmp' ? `<a href="#" onclick="cmpAbrirModal(${Number(p.id)}); return false;" style="color:inherit;text-decoration:underline dotted" title="Ver todo lo analizado">${escaparHtml(p.partido || '')}</a>` : clave === 'recordador' && p.recordatorio_id ? `<a href="#" onclick="recInvestigacion(${Number(p.recordatorio_id)}); return false;" style="color:inherit;text-decoration:underline dotted" title="Ver la investigación">${escaparHtml(p.partido || '')}</a>` : _dePartidoUtr(p)}${p.detalle ? `<div class="muted" style="font-size:0.72rem">${escaparHtml(p.detalle)}</div>` : ''}</td>
      <td><b>${_deJugadorUtr(p, p.seleccion) || '—'}</b> ${p.etiqueta ? escaparHtml(p.etiqueta) : ''}</td>${DE_INV.includes(clave) ? `<td>${_deInvCelda(clave, p)}</td>` : ''}
      <td class="muted">${escaparHtml(p.torneo || '—')}</td>
      <td>${pkNum(p.cuota)}</td>
      <td style="${colTxt}">${txt}${admin && DE_CERRABLES.includes(clave) && p.lado_a && p.lado_b ? `<div><button class="go" style="padding:1px 7px;font-size:0.7rem;margin-top:3px" data-id="${Number(p.id)}" data-a="${escaparHtml(p.lado_a)}" data-b="${escaparHtml(p.lado_b)}" data-res="${escaparHtml(p.resultado || '')}" data-marcador="${escaparHtml(p.marcador_crudo !== undefined ? (p.marcador_crudo || '') : (p.marcador || ''))}" onclick="deCerrar('${clave}', this)" title="Cargar o corregir el resultado a mano">${p.resultado === 'pendiente' ? '✏️ Cerrar' : '✏️ Editar'}</button></div>` : ''}</td>
      <td class="muted">${escaparHtml(p.marcador || '—')}${p.remonto === true ? ' <span style="color:var(--green)" title="Perdió el primer set y ganó">↩ remontó</span>' : ''}</td>
      <td style="${pkColor(p.unidades)}">${p.unidades === null || p.unidades === undefined ? '—' : pkSigno(p.unidades)}</td>
    </tr>`;
  }).join('');
}

async function deCargar(clave) {
  if (!DE_CONF[clave] || !_deMontar(clave)) return;
  const est = _deEl(clave, 'estado');
  est.textContent = 'Cargando...';
  try {
    const selVista = _deEl(clave, 'vista');
    const res = await fetch(DE_CONF[clave].endpoint + qs({vista: DE_CONF[clave].fijo || selVista.value || '',
                                                         genero: _deEl(clave, 'genero').value}),
                            {credentials: 'same-origin'});
    if (!res.ok) throw new Error(await mensajeError(res));
    const d = await res.json();
    if (!selVista.options.length) {
      selVista.innerHTML = Object.entries(d.vistas || {}).map(([v, t]) =>
        `<option value="${escaparHtml(v)}"${v === d.vista ? ' selected' : ''}>${escaparHtml(t)}</option>`).join('');
    }
    DE_ESTADO[clave] = d;
    // v45: aviso del servidor (p. ej. 🧪 experimentales sin respaldo estadístico: no son picks confirmados)
    const aviso = _deEl(clave, 'aviso');
    if (aviso) { aviso.textContent = d.aviso || ''; aviso.style.display = d.aviso ? '' : 'none'; }
    _deKpis(clave, d.kpis);
    _deHistorial(clave);
    // 10/10/2026: FT NEWS lista todo lo dicho (📰 🔎 🔥); las apuestas (KPI) son solo las 🔥.
    est.textContent = (d.kpis.alertas_dichas !== undefined
      ? `${d.kpis.alertas_dichas} alerta(s) dicha(s) en Discord · ${d.kpis.total} apuesta(s) 🔥 · ${d.kpis.seguimiento || 0} en seguimiento (📰/🔎)`
      : `${d.kpis.total} pick(s)`) + (d.truncado ? ' · la tabla muestra los últimos; los KPIs cuentan todos' : '');
  } catch (e) {
    est.innerHTML = `<span class="warn">${escaparHtml(e.message || e)}</span>`;
  }
}


// ═══════════════════════════════════════════════════════════════════════
//  🔎 MODAL DE UNA SEÑAL DE CUOTA MAL PUESTA (09/10/2026, Rubén: "que cuando
//  uno le dé clic al partido salga un modal con todo lo relacionado: por qué
//  carga y demás; y el UTR de los jugadores si lo tienen, solo informativo").
//  Lee /research/api/cuota-mal-puesta/{id}: la foto guardada al detectar, los
//  JSON de carga / MTO / L10 abiertos, la revalidación (FOTO 2) y el UTR del
//  padrón. No calcula nada: muestra lo que el motor guardó.
// ═══════════════════════════════════════════════════════════════════════
function _cmpPct(p, dec = 0) { return (p === null || p === undefined) ? '—' : (Number(p) * 100).toFixed(dec) + '%'; }
function _cmpPp(v, dec = 1) { return (v === null || v === undefined) ? '—' : (Number(v) >= 0 ? '+' : '') + Number(v).toFixed(dec) + ' pp'; }
function _cmpNum(v, dec = 2) { return (v === null || v === undefined || v === '') ? '—' : Number(v).toFixed(dec); }
function _cmpFecha(v) { try { return v ? pkFecha(v) : '—'; } catch (e) { return v || '—'; } }
function _cmpFila(k, v, nota) {
  return `<tr><td class="muted" style="white-space:nowrap;padding:2px 10px 2px 0">${k}</td><td>${v}</td>${nota !== undefined ? `<td class="muted" style="font-size:0.72rem">${nota}</td>` : ''}</tr>`;
}
function _cmpSeccion(titulo, cuerpo, abierta = true) {
  return `<details ${abierta ? 'open' : ''} style="margin:8px 0;border:1px solid rgba(186,255,0,.14);border-radius:8px;padding:6px 10px">
    <summary style="cursor:pointer;font-weight:700">${titulo}</summary><div style="margin-top:6px">${cuerpo}</div></details>`;
}
function _cmpSigno(v) { return v > 0 ? '✅' : v < 0 ? '❌' : '➖'; }

function cmpCerrarModal() { const m = document.getElementById('cmp-modal'); if (m) m.remove(); }

async function cmpAbrirModal(id) {
  cmpCerrarModal();
  const caja = document.createElement('div');
  caja.id = 'cmp-modal'; caja.className = 'modal-overlay';
  caja.onclick = (e) => { if (e.target === caja) cmpCerrarModal(); };
  caja.innerHTML = `<div class="modal-caja" style="width:min(860px,96vw);max-height:90vh">
    <div class="modal-titulo">🔥 Cuota Mal Puesta · señal #${id} <button class="modal-cerrar" onclick="cmpCerrarModal()">✕</button></div>
    <div id="cmp-modal-cuerpo" class="muted">Cargando...</div></div>`;
  document.body.appendChild(caja);
  const esc = (e) => { if (e.key === 'Escape') { cmpCerrarModal(); document.removeEventListener('keydown', esc); } };
  document.addEventListener('keydown', esc);
  try {
    const res = await fetch(`/research/api/cuota-mal-puesta/${id}`, {credentials: 'same-origin'});
    if (!res.ok) throw new Error(await mensajeError(res));
    document.getElementById('cmp-modal-cuerpo').innerHTML = _cmpModalHtml(await res.json());
  } catch (e) {
    document.getElementById('cmp-modal-cuerpo').innerHTML = `<span class="warn">${escaparHtml(e.message || e)}</span>`;
  }
}

// 10/10/2026 (Rubén): la sección de carga explicada en un párrafo, no solo en números.
// V3 (misma noche): PUERTA 1 = carga ABSOLUTA de cada uno (NORMAL / CONSIDERABLE /
// ALTA / EXTREMA / INACTIVIDAD / DATOS_INSUFICIENTES); PUERTA 2 = comparación.
// El texto principal lo arma el servidor (d.factores, el mismo que Discord);
// este párrafo agrega el "cómo se midió" con los umbrales vigentes.
const _CMP_CLASE_TXT = {CARGA_NORMAL: 'carga normal', CARGA_CONSIDERABLE: 'carga considerable', CARGA_ALTA: 'carga alta',
  CARGA_EXTREMA: 'carga extrema', INACTIVIDAD: 'inactividad (sin competir varios días)', DATOS_INSUFICIENTES: 'datos insuficientes'};
function _cmpCargaParrafo(s, cj, cfg, A, B) {
  const ca = cj.jugador || {}, cb = cj.rival || {}, ka = ca.componentes || {}, kb = cb.componentes || {};
  const n1 = (v, d = 0) => (v === null || v === undefined) ? '—' : Number(v).toFixed(d);
  const clA = cj.clase_jugador || s.carga_clase_jugador, clB = cj.clase_rival || s.carga_clase_rival;
  const quien = (nombre, c, k, cl) => {
    if ((c.motivo && !c.suficiente) || cl === 'DATOS_INSUFICIENTES') return `<b>${nombre}</b>: sin historial suficiente para medir su carga${c.motivo ? ` (${escaparHtml(c.motivo)})` : ''}.`;
    const partes = [];
    partes.push(`${n1(k.partidos_7d)} partido(s), ${n1(k.sets_7d)} sets y ${n1(k.games_7d)} games en los últimos 7 días` + (k.partidos_3d >= 2 ? ` (${n1(k.partidos_3d)} en 3 días)` : '') + (k.tres_sets_7d ? `, ${k.tres_sets_7d} a tres sets` : ''));
    if (k.doble_jornada) partes.push('doble jornada');
    if (k.descanso_dias !== null && k.descanso_dias !== undefined) partes.push(`${n1(k.descanso_dias, 1)} día(s) desde su último partido` + (k.cambio_torneo ? ' (cambia de torneo)' : ''));
    if (k.habitual_14d) partes.push(`ritmo habitual ${n1(k.habitual_14d, 1)} partidos cada 14 días`);
    const ac = c.acumulacion ? (c.acumulacion.alcanza ? `acumulación real: ${escaparHtml(c.acumulacion.resumen || '')}` : 'sin acumulación real') : '';
    return `<b>${nombre}</b>: índice <b>${n1(c.carga, 1)}</b> → <b>${_CMP_CLASE_TXT[cl] || cl || 'sin clase'}</b> (${partes.join('; ')}${ac ? '; ' + ac : ''}).`;
  };
  const puerta = cj.puerta || s.carga_puerta;
  const umbr = `índice: NORMAL &lt; ${n1(cfg.CMP_CARGA_CONSIDERABLE ?? 45)} ≤ CONSIDERABLE &lt; ${n1(cfg.CMP_CARGA_ALTA ?? 65)} ≤ ALTA &lt; ${n1(cfg.CMP_CARGA_EXTREMA ?? 80)} ≤ EXTREMA · y además acumulación real: ≥ ${n1(cfg.CMP_CARGA_ACUM_PARTIDOS_7D ?? 4)} partidos en 7 días, o ≥ ${n1(cfg.CMP_CARGA_ACUM_PARTIDOS_3D ?? 3)} en 3 días, o ≥ ${n1(cfg.CMP_CARGA_ACUM_GAMES_7D ?? 110)} games en 7 días, o doble jornada, o ≤ ${n1(cfg.CMP_CARGA_ACUM_DESCANSO_MAX ?? 1, 1)} día de descanso con ≥ 3 partidos · inactividad desde ${n1(cfg.CMP_INACTIVIDAD_DIAS ?? 7)} días sin jugar · banda muerta ${n1(cfg.CMP_CARGA_BANDA_MUERTA ?? 10)} · tope ±${n1(cfg.CMP_CARGA_MAX_PP ?? 4)} pp`;
  let cierre = puerta ? `<b>Puerta:</b> ${escaparHtml(puerta)}.` : (s.carga_suficiente
    ? `Diferencia ${n1(Math.abs(Number(s.diferencia_carga || 0)), 1)} puntos → ajuste <b>${_cmpPp(s.ajuste_carga_pp)}</b> (evaluación anterior a V3: sin puerta de carga absoluta).`
    : 'A alguno de los dos le falta historial: la carga <b>no ajustó</b> la probabilidad.');
  return `Cómo se midió: cada uno recibe un índice de 0 a 100 con lo que jugó en los últimos 7–14 días (partidos, sets, games, doble jornada, descanso y comparación con su ritmo habitual). Para ser carga <b>considerable</b> hace falta el índice <b>y además acumulación real</b> (concentración de partidos o games, poco descanso o doble jornada): una semana normal nunca es fatiga, aunque el índice suba por el ritmo relativo. Solo si alguno llega con carga considerable o más y el otro, <b>con ritmo</b>, claramente menos cargado, se ajusta la probabilidad. ${quien(A, ca, ka, clA)} ${quien(B, cb, kb, clB)} ${cierre} <span class="muted" style="font-size:0.78rem">Umbrales vigentes (provisorios hasta fijarlos con la distribución): ${umbr}.</span>`;
}

// Bloques 🧠 / 🏃 / 🩹 en palabras (d.factores): lo que lee un usuario en 5 segundos.
function _cmpFactoresHtml(factores) {
  if (!factores || !factores.length) return '';
  return `<div style="display:grid;gap:8px;margin:6px 0 10px">${factores.map(f => `<div style="padding:8px 10px;border-radius:8px;background:${f.mostrar ? 'rgba(155,225,93,0.08)' : 'rgba(255,255,255,0.03)'};border:1px solid ${f.mostrar ? 'rgba(155,225,93,0.35)' : 'rgba(255,255,255,0.08)'}">
    <div style="font-weight:600">${escaparHtml(f.titulo || '')}</div>${(f.lineas || []).map(l => `<div style="font-size:0.86rem;line-height:1.4">${escaparHtml(l)}</div>`).join('')}${f.mostrar ? '' : '<div class="muted" style="font-size:0.74rem">Factor neutro: no se muestra en Discord.</div>'}</div>`).join('')}</div>`;
}

function _cmpModalHtml(d) {
  const s = d.senal || {}, x = d.detalle || {}, u = d.utr || {}, l10 = d.l10 || {A: [], B: []};
  const A = escaparHtml(s.player_a || '?'), B = escaparHtml(s.player_b || '?');
  const nivel = s.discord_nivel === 'PREMIUM' ? '💎 Premium' : s.discord_nivel === 'CMP' ? '🔥 Cuota mal puesta' : (s.grupo || '—');
  const res = s.result === 'W' ? '<span style="color:#9be15d">GANADA</span>' : s.result === 'L' ? '<span style="color:#ff7b7b">PERDIDA</span>' : 'Pendiente';
  // ── Cabecera ──
  let h = `<div style="font-size:1.05rem"><b>⭐ ${A}</b> vs ${B} <span class="muted">· ${escaparHtml(s.torneo || '')}${s.superficie ? ' · ' + escaparHtml(s.superficie) : ''}${s.genero ? ' · ' + escaparHtml(s.genero) : ''}</span></div>
    <div class="muted" style="font-size:0.8rem;margin:2px 0 8px">Pick <b>${A} @ ${_cmpNum(s.odds_a)}</b> · ${nivel} · detectada ${_cmpFecha(s.timestamp_signal)} · partido ${_cmpFecha(s.fecha_partido)} · ${res}${s.resultado_score ? ' ' + escaparHtml(s.resultado_score) : ''}${s.profit_loss !== null && s.profit_loss !== undefined ? ' · ' + pkSigno(s.profit_loss) + ' u' : ''}</div>`;
  // ── La puerta ──
  const fav = [['OPOSICIÓN', s.ajuste_oposicion_pp], ['CARGA', s.ajuste_carga_pp], ['MTO', s.ajuste_mto_pp]].filter(([, v]) => v > 0).map(([n]) => n);
  h += _cmpSeccion('🚪 Por qué pasó la puerta de Discord', `<table>
    ${_cmpFila('FT FINAL', `<b>${_cmpPct(s.ft_prob_ajustada)}</b>`, `base ${_cmpPct(s.ft_prob_base)} ${[['oposición', s.ajuste_oposicion_pp], ['carga', s.ajuste_carga_pp], ['MTO', s.ajuste_mto_pp]].filter(([, v]) => v).map(([n, v]) => `${n} ${_cmpPp(v, 0)}`).join(', ')}`)}
    ${_cmpFila('Mercado', `${_cmpPct(s.market_prob)} <span class="muted">(1/${_cmpNum(s.odds_a)})</span>`, s.market_prob_sin_margen ? `sin margen ${_cmpPct(s.market_prob_sin_margen)} · overround ${_cmpNum(s.overround, 3)}` : '')}
    ${_cmpFila('Ventaja (EDGE)', `<b>${_cmpPp(s.mispricing_pp)}</b>`, `cuota justa ${_cmpNum(s.ft_fair_odds)}`)}
    ${_cmpFila('Factor que activó', fav.length ? `✅ ${fav.join(' + ')}` : '❌ ninguno', `factores a favor: ${s.factores_positivos ?? '—'} (mínimo ${s.factores_positivos_min_usado ?? 1})`)}
    ${_cmpFila('Elo y FTR', s.elo_ftr_alineados ? '✅ coinciden en el favorito' : '❌ NO coinciden', `Elo → ${escaparHtml(s.favorito_elo || '—')} · FTR → ${escaparHtml(s.favorito_ftr || '—')}`)}
    ${_cmpFila('Oposición (L10)', s.l10_suficiente ? '✅ datos suficientes' : '⚠️ insuficiente', `${s.l10_partidos_player ?? '—'} / ${s.l10_partidos_opponent ?? '—'} partidos`)}
    ${_cmpFila('Cuotas coherentes', s.odds_ok ? '✅' : '❌', '')}
    ${s.motivo_no_discord ? _cmpFila('No enviada', `<span class="warn">${escaparHtml(s.motivo_no_discord)}</span>`, '') : ''}
    ${s.discord_revocado_en ? _cmpFila('Retirada de Discord', _cmpFecha(s.discord_revocado_en), '') : ''}
  </table>`);
  // ── Revalidación (FOTO 2) ──
  if (s.revalidado_publicacion) {
    const rv = x.revalidacion_json || {};
    h += _cmpSeccion(`📸 Revalidación antes de publicar (${_cmpFecha(s.timestamp_revalidacion)}) · ${s.aprobado_publicacion ? '✅ aprobada' : '❌ descartada'}`, `<table>
      ${_cmpFila('Cuota entonces → ahora', `${_cmpNum(s.odds_a)} → <b>${_cmpNum(s.cuota_publicacion)}</b>`, '')}
      ${_cmpFila('FT FINAL', `${_cmpPct(s.ft_prob_ajustada)} → <b>${_cmpPct(s.ft_final_publicacion)}</b>`, `base ${_cmpPct(s.ft_base_publicacion)} · Elo ${_cmpPct(s.elo_publicacion)} · FTR ${_cmpPct(s.ftr_publicacion)}`)}
      ${_cmpFila('Factores', `oposición ${_cmpPp(s.oposicion_publicacion, 0)} · carga ${_cmpPp(s.fatiga_publicacion, 0)} · MTO ${_cmpPp(s.mto_publicacion, 0)}`, `a favor: ${s.factores_positivos_publicacion ?? '—'}`)}
      ${_cmpFila('Ventaja', `${_cmpPp(s.mispricing_pp)} → <b>${_cmpPp(s.edge_publicacion)}</b>`, `cuota justa ${_cmpNum(s.cuota_justa_publicacion)}`)}
      ${s.motivos_no_publicacion ? _cmpFila('Motivo', `<span class="warn">${escaparHtml(s.motivos_no_publicacion)}</span>`, (x.motivos_codigos_publicacion || []).join(', ')) : ''}
      ${_cmpFila('Reglas', escaparHtml(s.version_reglas_publicacion || '—'), '')}
    </table>`);
  }
  // ── Base Elo + FTR ──
  h += _cmpSeccion('📊 Base: Elo + FTR (sin mirar la cuota)', `<table>
    ${_cmpFila('Prob. Elo', _cmpPct(s.elo_prob), `diferencia de Elo ${_cmpNum(s.elo_diff, 0)}`)}
    ${_cmpFila('Prob. FTR', _cmpPct(s.ftr_prob), `diferencia de FTR ${_cmpNum(s.ftr_diff, 0)}`)}
    ${_cmpFila('Base', `<b>${_cmpPct(s.ft_prob_base)}</b>`, s.penalizacion_desacuerdo_pp ? `penalización por desacuerdo ${_cmpPp(-Math.abs(s.penalizacion_desacuerdo_pp), 1)}` : 'sin penalización')}
    ${_cmpFila('Ventaja base', _cmpPp(s.mispricing_base_pp), 'antes de los tres factores')}
  </table>`, false);
  // ── Oposición ──
  const l10tabla = (lado, nombre) => {
    const filas = l10[lado] || [];
    if (!filas.length) return `<div class="muted">Sin detalle L10 para ${nombre}.</div>`;
    return `<div style="font-weight:600;margin:4px 0">${nombre}</div><div class="table-scroll"><table style="font-size:0.78rem">
      <thead><tr><th>Fecha</th><th>Rival</th><th>Rival</th><th>Esperado</th><th>Res.</th><th>Torneo</th></tr></thead>
      <tbody>${filas.map(f => `<tr><td class="muted">${_cmpFecha(f.fecha)}</td><td>${escaparHtml(f.rival || '')}</td>
        <td class="muted">${escaparHtml(f.tipo_rival || '')}${f.rival_percentil !== null && f.rival_percentil !== undefined ? ' · p' + Math.round(f.rival_percentil) : ''}</td>
        <td>${_cmpPct(f.prob_esperada)}</td><td style="color:${f.resultado === 'W' ? '#9be15d' : '#ff7b7b'}">${escaparHtml(f.resultado || '')}</td>
        <td class="muted">${escaparHtml(f.torneo || '')}${f.superficie ? ' · ' + escaparHtml(f.superficie) : ''}</td></tr>`).join('')}</tbody></table></div>`;
  };
  h += _cmpSeccion('🗣️ Factores en palabras (lo mismo que dice Discord)', _cmpFactoresHtml(d.factores), true);
  h += _cmpSeccion(`${_cmpSigno(s.ajuste_oposicion_pp)} Oposición (L10) · ajuste ${_cmpPp(s.ajuste_oposicion_pp)}`, `<table>
    ${_cmpFila('Score ' + A, _cmpNum(s.oposicion_jugador ?? s.l10_score_player), `${s.l10_partidos_player ?? '—'} partidos`)}
    ${_cmpFila('Score ' + B, _cmpNum(s.oposicion_rival ?? s.l10_score_opponent), `${s.l10_partidos_opponent ?? '—'} partidos`)}
    ${_cmpFila('Diferencia', _cmpNum(s.diferencia_oposicion ?? s.l10_diff), s.l10_contradice ? '⚠️ contradice' : s.l10_confirma ? '✅ confirma' : '')}
  </table>${l10tabla('A', A)}${l10tabla('B', B)}`, false);
  // ── Carga ──
  const cj = x.carga_json || {};
  const comp = (lado) => {
    const c = (cj[lado] || {}), k = c.componentes || {};
    if (c.motivo && !c.suficiente) return `<div class="muted">${escaparHtml(c.motivo)}</div>`;
    return `<table>
      ${_cmpFila('Carga 0–100', `<b>${_cmpNum(c.carga, 1)}</b>`, '')}
      ${_cmpFila('Partidos 3 / 7 / 14 días', `${k.partidos_3d ?? '—'} / ${k.partidos_7d ?? '—'} / ${k.partidos_14d ?? '—'}`, `habitual cada 14 días: ${_cmpNum(k.habitual_14d, 1)}`)}
      ${_cmpFila('Sets / games (7 días)', `${k.sets_7d ?? '—'} / ${k.games_7d ?? '—'}`, `${k.tres_sets_7d ?? 0} partido(s) a 3 sets`)}
      ${_cmpFila('Doble jornada (3 días)', k.doble_jornada ? '⚠️ sí' : 'no', '')}
      ${_cmpFila('Descanso', k.descanso_dias !== null && k.descanso_dias !== undefined ? `${_cmpNum(k.descanso_dias, 1)} días` : '—', k.cambio_torneo ? 'cambió de torneo' : '')}
      ${c.puntos ? _cmpFila('Puntos de carga', `<span style="font-size:0.78rem">${Object.entries(c.puntos).filter(([, b]) => Number(b) > 0).map(([a, b]) => `${({partidos_7d: '7 días', partidos_3d: '3 días', sets_7d: 'sets', tres_sets_7d: '3 sets', doble_jornada: 'doble jornada', descanso: 'descanso', relativo: 'vs habitual'})[a] || a} ${_cmpNum(b, 1)}`).join(' · ') || '0'}</span>`, '') : ''}
    </table>`;
  };
  h += _cmpSeccion(`${_cmpSigno(s.ajuste_carga_pp)} 🏃 Carga y ritmo · ajuste ${_cmpPp(s.ajuste_carga_pp)}`, `<div style="font-size:0.86rem;line-height:1.45;margin-bottom:6px">${_cmpCargaParrafo(s, cj, x.config_json || {}, A, B)}</div>
    <div class="muted" style="font-size:0.78rem">Índice ${A}: <b>${_cmpNum(s.carga_jugador, 1)}</b> (${escaparHtml(cj.clase_jugador || s.carga_clase_jugador || '—')}) · ${B}: <b>${_cmpNum(s.carga_rival, 1)}</b> (${escaparHtml(cj.clase_rival || s.carga_clase_rival || '—')}) · diferencia ${_cmpNum(s.diferencia_carga, 1)}${s.carga_suficiente ? '' : ' · ⚠️ cobertura insuficiente'}</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:6px"><div><div style="font-weight:600">${A}</div>${comp('jugador')}</div><div><div style="font-weight:600">${B}</div>${comp('rival')}</div></div>`, false);
  // ── MTO ──
  const mj = x.mto_json || {};
  const mto = (lado, nombre, flag, ts, edad) => flag || ts
    ? `${nombre}: ${flag ? '🩹 MTO confirmado' : '🩹 MTO dudoso'} ${_cmpFecha(ts)}${edad !== null && edad !== undefined ? ` (hace ${_cmpNum(edad, 1)} h)` : ''}${mj[lado] && mj[lado].torneo ? ' · ' + escaparHtml(mj[lado].torneo) : ''}`
    : `${nombre}: sin MTO en las últimas ${mj.horas || 24} h`;
  h += _cmpSeccion(`${_cmpSigno(s.ajuste_mto_pp)} MTO (< ${mj.horas || 24} h) · ajuste ${_cmpPp(s.ajuste_mto_pp)}`,
    `<div>${mto('jugador', A, s.mto_jugador_24h, s.mto_jugador_timestamp, s.mto_jugador_edad_h)}</div><div>${mto('rival', B, s.mto_rival_24h, s.mto_rival_timestamp, s.mto_rival_edad_h)}</div>
     <div class="muted" style="font-size:0.75rem">Cruce: ${escaparHtml(s.mto_confianza_cruce || '—')} · evaluado ${_cmpFecha(mj.evaluado_en || s.timestamp_evaluacion)}</div>`, false);
  // ── UTR (solo informativo) ──
  const utr = (p, nombre) => !p ? `${nombre}: <span class="muted">sin dato en el padrón</span>`
    : `${nombre}: <b>${escaparHtml(p.display || '—')}</b>${p.utr_dura || p.utr_tierra || p.utr_hierba ? ` <span class="muted">(dura ${_cmpNum(p.utr_dura)} · tierra ${_cmpNum(p.utr_tierra)} · hierba ${_cmpNum(p.utr_hierba)})</span>` : ''} <span class="muted" style="font-size:0.75rem">· ${escaparHtml(p.identidad || '')} ${escaparHtml(p.confianza || '')}${p.fecha_captura ? ' · ' + escaparHtml(String(p.fecha_captura).slice(0, 10)) : ''}${p.fuente ? ' · ' + escaparHtml(p.fuente) : ''}</span>`;
  h += _cmpSeccion(`🎯 UTR de los jugadores <span class="muted" style="font-weight:normal;font-size:0.75rem">(informativo: no interviene en Cuota Mal Puesta)</span>`,
    `<div>${utr(u.A, A)}</div><div>${utr(u.B, B)}</div>${u.delta !== undefined ? `<div class="muted">ΔUTR ${(u.delta >= 0 ? '+' : '') + _cmpNum(u.delta)} a favor de ${u.delta >= 0 ? A : B}</div>` : ''}`);
  // ── Cuotas ──
  h += _cmpSeccion('💰 Cuotas', `<table>
    ${_cmpFila('Apertura', `${_cmpNum(s.odds_apertura_a)} / ${_cmpNum(s.odds_apertura_b)}`, `${A} / ${B}`)}
    ${_cmpFila('Al detectar', `<b>${_cmpNum(s.odds_a)}</b> / ${_cmpNum(s.odds_b)}`, '')}
    ${s.cuota_publicacion ? _cmpFila('Al publicar', `${_cmpNum(s.cuota_publicacion)}`, _cmpFecha(s.timestamp_publicacion)) : ''}
    ${s.cuota_cierre_a ? _cmpFila('Cierre', `${_cmpNum(s.cuota_cierre_a)} / ${_cmpNum(s.cuota_cierre_b)}`, s.clv_pct !== null && s.clv_pct !== undefined ? `CLV ${(Number(s.clv_pct) * 100).toFixed(1)}%` : '') : ''}
  </table>`, false);
  // ── Mensaje ──
  const msg = s.texto_discord_publicacion || s.texto_discord;
  if (msg) h += _cmpSeccion('💬 Mensaje de Discord', `<pre style="white-space:pre-wrap;font-family:inherit;font-size:0.82rem;margin:0">${escaparHtml(msg)}</pre>`, false);
  h += `<div class="muted" style="font-size:0.72rem;margin-top:6px">Motor ${escaparHtml(s.version_motor || '')} · evaluada ${_cmpFecha(s.timestamp_evaluacion || s.timestamp_signal)} · señal #${s.id}</div>`;
  return h;
}


// ═══════════════════════════════════════════════════════════════════════
//  🧠 INVESTIGACIÓN (v54, 13/10/2026) — columna y modal de Partidos Maestros, Cuota Mal Puesta, UTR VALUE y
//  UTR Market Anomaly. Lee /research/api/estrategia/{clave}/{id}/investigacion (production/engines/
//  estrategia_investigacion.py): la misma investigación del Recordador para la selección. Si la fila todavía no
//  tiene, el servidor la hace al abrir (sin IA) y la guarda.
// ═══════════════════════════════════════════════════════════════════════
const _EI_PCT = p => (p === null || p === undefined) ? '—' : Math.round(p * 100) + '%';
const _EI_PCT1 = p => (p === null || p === undefined) ? '—' : (p * 100).toFixed(1) + '%';
const _EI_N = (v, d = 2) => (v === null || v === undefined || v === '') ? '—' : Number(v).toFixed(d);
const _EI_MOM = {PREVIA: '', EN_JUEGO: ' <span class="muted" title="Hecha con el partido ya empezado">· en juego</span>',
                 POSTERIOR: ' <span class="muted" title="Hecha después del partido con la foto del motor; no cuenta para medir">· posterior</span>'};

function _deInvCelda(clave, p) {
  if (!p.investigable) return '<span class="muted" title="Las alertas del canal externo no se investigan">—</span>';
  // v54l (17/10/2026, Rubén): en el bot UTR la columna es solo el acceso al CONTEXTO DEL PARTIDO (formato corto).
  // Probabilidades en estudio, fuentes y cálculos quedan en «Detalles técnicos (admin)» dentro del contexto.
  if (clave === 'utrv' || clave === 'utra') return `<button class="go" style="padding:1px 7px;font-size:0.72rem" onclick="deInvestigacion('${clave}', ${Number(p.id)})" title="Edad, rendimiento por superficie, último partido en cada superficie, H2H y lo realmente importante">🔎 Contexto</button>`;
  const i = p.inv;
  if (!i) return `<button class="go" style="padding:1px 7px;font-size:0.72rem" onclick="deInvestigacion('${clave}', ${Number(p.id)})" title="Todavía no tiene: se investiga al abrir">🧠 Investigar</button>`;
  // v54c: en UTR Market Anomaly no hay «acompaña / no acompaña» (solo referencia)
  const apoyo = clave === 'utra' ? '' : i.apoya === 1 ? '<span style="color:var(--green)" title="La probabilidad FullTennis supera a la del mercado">✅ acompaña</span>'
    : i.apoya === 0 ? '<span style="color:var(--amarillo,#e8b339)" title="La probabilidad FullTennis queda por debajo de la del mercado">⚠️ no acompaña</span>' : '';
  // v54c: el contexto de ¿POR QUÉ LA CUOTA? (referencia); versiones viejas: su reserva
  const cx = i.contexto;
  const res = cx ? `<div style="font-size:0.75rem" title="${escaparHtml(((i.reservas || []).join(' ')) || cx.texto || '')}">🔎 ${escaparHtml(cx.icono || '⚪')} ${escaparHtml((cx.areas || [])[0] || cx.texto || '')}</div>`
    : (i.reservas || []).length ? `<div style="font-size:0.75rem;color:var(--amarillo,#e8b339)" title="${escaparHtml(i.reservas.join(' '))}">⚠️ revisar: ${escaparHtml(i.reservas[0].split('.')[0])}</div>` : '';
  const corta = (i.conclusion || '').replace(/^(⚠️|🟡) Contexto:[^;]*;[^.]*\.\s*/, '').replace(/^⚠️[^.]*\.\s*/, '').split(/(?<=\.)\s/)[0] || '';
  // v54g: en UTR VALUE / Market Anomaly la probabilidad es solo por UTR (tabla de UTR VALUE · en estudio)
  const prob = i.recalculando ? '<span class="muted" title="Estaba hecha con Elo y FTR: se rehace solo con UTR">⏳ recalculando solo con UTR</span>'
    : i.p == null ? (i.solo_utr ? '<span class="muted" title="Sin estimación por UTR (falta UTR, mismo UTR o ΔUTR debajo del primer escalón de la tabla)">sin % por UTR</span>'
                                : '<span class="muted" title="No hay Elo ni FTR de los dos: el UTR y la cuota quedan como datos">sin probabilidad FullTennis</span>')
    : `<b>${_EI_PCT(i.p)}</b> vs ${_EI_PCT(i.p_rival)}${i.solo_utr ? ' <span class="muted" title="Probabilidad solo por UTR (tabla de UTR VALUE · en estudio)">UTR</span>' : ''}`;
  return `<div style="min-width:170px">${prob}${_EI_MOM[i.momento] || ''}${res}${apoyo ? `<div style="font-size:0.75rem">${apoyo}</div>` : ''}
    <div class="muted" style="font-size:0.72rem;max-width:240px">${escaparHtml(corta.slice(0, 140))}</div>
    <button class="go" style="padding:1px 7px;font-size:0.72rem;margin-top:3px" onclick="deInvestigacion('${clave}', ${Number(p.id)})" title="Ver la investigación completa">🧠 Ver</button></div>`;
}

// ═══ v54l · 🔎 FT INTELLIGENCE · CONTEXTO DEL PARTIDO (UTR VALUE y UTR MARKET ANOMALY) ═══
const _CTX_SUP = [['clay', 'Tierra', '#b5451b'], ['hard', 'Dura', '#1f4e8c'], ['grass', 'Hierba', '#2e7d32']];
function _ctxSupChip(nombre, color) { return `<span style="display:inline-block;min-width:74px;text-align:center;padding:3px 8px;border-radius:6px;background:${color};color:#fff;font-weight:600;font-size:0.82rem">${nombre}</span>`; }
function _ctxUtrHtml(c) {
  const [a, b] = c.jugadores || [];
  if (!a || !b) return '<div class="muted">Sin datos de los jugadores.</div>';
  const tarjeta = (j, sel) => `<div style="flex:1;min-width:230px;padding:10px 14px;border-radius:10px;border:1px solid ${sel ? 'var(--accent)' : 'var(--line,#333)'}">
      <div style="font-size:1.15rem;font-weight:700;color:var(--accent)">${escaparHtml(j.bandera || '')} ${escaparHtml(j.nombre || '')}</div>
      <div style="margin-top:2px">${j.edad ? `${j.edad} años` : '<span class="muted">edad —</span>'} &nbsp;|&nbsp; UTR: <b>${j.utr != null ? Number(j.utr).toFixed(2) : '—'}</b></div></div>`;
  const caja = (titulo, cuerpo) => `<div style="margin-top:10px;border:1px solid var(--line,#333);border-radius:10px;padding:8px 12px"><div style="font-weight:700;margin-bottom:6px">${titulo}</div>${cuerpo}</div>`;
  // 17/10/2026: debajo del %, el promedio UTR de los rivales de sus ÚLTIMOS 10 partidos en esa superficie; clic → modal
  window._ctxRivales = {};
  const riv = (j, k) => { const r = ((j.sup || {})[k] || {}).rivales || {};
    if (!r.de) return '';
    const key = `${j.player_id || j.nombre}|${k}`;
    window._ctxRivales[key] = {jugador: j.nombre, sup: (_CTX_SUP.find(x => x[0] === k) || [])[1] || k, r};
    const txt = r.utr_prom != null ? `Prom. riv. últ. ${r.de}: UTR <b style="color:var(--text,#ddd)">${Number(r.utr_prom).toFixed(2)}</b>` : 'Prom. riv.: sin UTR';
    return `<div style="font-size:0.76rem;margin-top:2px"><a href="#" class="utr-link" style="font-weight:normal;color:var(--suave,#bbb)" onclick="_ctxRivalesModal('${escaparHtml(key).replace(/'/g, '&#39;')}'); return false;"
      title="Promedio del UTR actual (padrón) de los rivales de sus últimos ${r.de} partidos en esta superficie (${r.n} con UTR). Clic para ver la lista.">${txt}</a></div>`; };
  const celdaSup = (j, k) => `<td style="text-align:center"><b>${escaparHtml(((j.sup || {})[k] || {}).txt || '—')}</b>${riv(j, k)}</td>`;
  const filaSup = ([k, n, col]) => `<tr><td>${_ctxSupChip(n, col)}</td>${celdaSup(a, k)}${celdaSup(b, k)}</tr>`;
  const ult = (j, k) => { const u = ((j.sup || {})[k] || {}).ultimo; return u ? escaparHtml(u.txt) : '<span class="muted">Sin partidos registrados.</span>'; };
  const filaUlt = ([k, n, col]) => `<tr><td>${_ctxSupChip(n, col)}</td><td>${ult(a, k)}</td><td>${ult(b, k)}</td></tr>`;
  const th = `<tr><th></th><th>${escaparHtml(a.nombre)}</th><th>${escaparHtml(b.nombre)}</th></tr>`;
  const h = c.h2h || {};
  const h2h = `<div style="font-size:1.05rem"><b>H2H:</b> ${escaparHtml(a.nombre)} <b style="color:var(--accent)">${h.w ?? 0} – ${h.l ?? 0}</b> ${escaparHtml(b.nombre)}</div>`
    + ((h.ultimos || []).length ? `<ul style="margin:4px 0 0;padding-left:18px">${h.ultimos.map(x => `<li>${escaparHtml(x.fecha)} · ${escaparHtml(x.superficie)} · ${escaparHtml(x.resultado)}</li>`).join('')}</ul>`
                                : '<div class="muted">Sin enfrentamientos registrados.</div>');
  const dest = (c.destacados || []).length ? `<ul style="margin:0;padding-left:4px;list-style:none">${c.destacados.map(d => `<li style="margin:3px 0">${escaparHtml(d.icono)} ${escaparHtml(d.texto)}</li>`).join('')}</ul>`
    : '<div>🟢 Sin alertas relevantes adicionales.</div>';
  return `<div style="display:flex;gap:12px;flex-wrap:wrap">${tarjeta(a, true)}${tarjeta(b, false)}</div>
    ${caja(`🎾 RENDIMIENTO POR SUPERFICIE <span class="muted" style="font-weight:normal;font-size:0.8rem">% de partidos ganados · Prom. riv. = UTR medio de los rivales de los últimos 10 (clic para verlos) · Registro FullTennis (desde 2022, no la carrera completa)</span>`, `<table class="tabla" style="width:100%">${th}${_CTX_SUP.map(filaSup).join('')}</table>`)}
    ${caja('📅 ÚLTIMO REGISTRADO EN FULLTENNIS (por superficie)', `<table class="tabla" style="width:100%">${th}${_CTX_SUP.map(filaUlt).join('')}</table>`)}
    ${caja('⚔️ ENFRENTAMIENTOS ENTRE ELLOS <span class="muted" style="font-weight:normal;font-size:0.8rem">Registro FullTennis · solo con los dos identificados por ID</span>', h2h)}
    ${caja('⭐ DATOS RELEVANTES', dest)}
    <div class="muted" style="font-size:0.74rem;margin-top:8px">${escaparHtml(c.aviso || 'Solo referencia.')}${c.superficie_hoy ? ` · se juega en ${escaparHtml(c.superficie_hoy.toLowerCase())}` : ''}</div>
    ${c.es_admin ? _ctxAuditoriaHtml(c) : ''}`;
}

// 17/10/2026 (Daniel): los rivales de los últimos 10 partidos en una superficie, con su UTR actual
function _ctxRivalesModal(key) {
  const d = (window._ctxRivales || {})[key];
  if (!d) return;
  const viejo = document.getElementById('ctx-riv-modal'); if (viejo) viejo.remove();
  const r = d.r;
  const filas = (r.partidos || []).map(p => `<tr><td>${escaparHtml(p.fecha)}</td><td>${escaparHtml(p.rival || '—')}</td>
      <td style="text-align:center"><b>${p.utr != null ? Number(p.utr).toFixed(2) : '<span class="muted">—</span>'}</b></td>
      <td style="color:${p.wl === 'W' ? 'var(--green)' : 'var(--red)'}">${p.wl === 'W' ? 'Ganó' : 'Perdió'} ${escaparHtml(p.score || '')}</td>
      <td class="muted" style="font-size:0.78rem">${escaparHtml(p.torneo || '')}</td></tr>`).join('');
  const caja = document.createElement('div');
  caja.id = 'ctx-riv-modal'; caja.className = 'modal-overlay'; caja.style.zIndex = 10001;
  caja.onclick = (e) => { if (e.target === caja) caja.remove(); };
  caja.innerHTML = `<div class="modal-caja" style="width:min(720px,94vw);max-height:85vh;overflow:auto">
    <div class="modal-titulo" style="color:var(--accent)">🎾 ${escaparHtml(d.jugador)} · rivales en ${escaparHtml(d.sup.toLowerCase())} (últimos ${r.de})
      <button class="modal-cerrar" onclick="document.getElementById('ctx-riv-modal').remove()">✕</button></div>
    <div style="margin:6px 0 10px">Promedio UTR de los rivales: <b style="color:var(--accent)">${r.utr_prom != null ? Number(r.utr_prom).toFixed(2) : '—'}</b>
      <span class="muted">(${r.n} de ${r.de} con UTR)</span></div>
    <table class="tabla" style="width:100%"><tr><th>Fecha</th><th>Rival</th><th>UTR</th><th>Resultado</th><th>Torneo</th></tr>${filas}</table>
    <div class="muted" style="font-size:0.74rem;margin-top:8px">UTR ACTUAL del padrón (solo perfiles usables), no el que tenía el rival el día del partido. Registro FullTennis.</div></div>`;
  document.body.appendChild(caja);
}

// 17/10/2026 (Rubén): confianza por dato — VERIFICADO / PARCIAL / NO_VERIFICABLE, con fuente, cobertura y evidencia. Solo admin.
const _CTX_CONF_COL = {VERIFICADO: 'var(--green)', PARCIAL: 'var(--amarillo,#e8b339)', NO_VERIFICABLE: 'var(--red)'};
// 17/10/2026: el valor de cada dato en palabras (antes salía el JSON crudo: {"ultima_actividad":…,"dias":0,…})
function _ctxValorTxt(v) {
  if (v == null) return '—';
  if (typeof v !== 'object') return String(v);
  if (Array.isArray(v)) return v.map(_ctxValorTxt).join(' · ') || '—';
  if ('ultima_actividad' in v || 'inactivo' in v) {
    const p = [];
    if (v.ultima_actividad) p.push(`última actividad ${String(v.ultima_actividad).slice(0, 10)}`);
    if (v.dias != null) p.push(`hace ${v.dias} día${v.dias === 1 ? '' : 's'}`);
    if (v.inactivo != null) p.push(v.inactivo ? 'inactiva/o' : 'activa/o');
    return p.join(' · ') || '—';
  }
  if ('w' in v && 'l' in v) {
    const per = v.periodo && typeof v.periodo === 'object' ? `${String(v.periodo.desde || '').slice(0, 7)} → ${String(v.periodo.hasta || '').slice(0, 7)}` : (v.periodo || '');
    return `${v.w}-${v.l}${v.n != null ? ` (${v.n} partidos)` : ''}${per ? ' · ' + per : ''}`;
  }
  if ('fecha' in v && ('rival' in v || 'wl' in v)) {
    return [String(v.fecha || '').slice(0, 10), v.rival ? 'vs ' + v.rival : '', v.wl === 'W' ? 'ganó' : v.wl === 'L' ? 'perdió' : '', v.score || '', v.torneo || ''].filter(Boolean).join(' · ');
  }
  return Object.entries(v).filter(([, x]) => x != null && typeof x !== 'object').map(([k, x]) => `${k.replace(/_/g, ' ')}: ${x}`).join(' · ') || '—';
}

function _ctxAuditoriaHtml(c) {
  const js = c.jugadores || [];
  if (!js.length || !js[0].datos) return '';
  const fila = (campo, quien, d) => !d ? '' : `<tr><td>${escaparHtml(campo)}</td><td>${escaparHtml(quien)}</td>
    <td style="max-width:220px;overflow-wrap:anywhere">${escaparHtml(_ctxValorTxt(d.valor))}</td>
    <td><b style="color:${_CTX_CONF_COL[d.confianza] || 'inherit'}">${escaparHtml(d.confianza || '—')}</b></td>
    <td>${escaparHtml(d.fuente || '—')}</td><td>${escaparHtml(d.fecha_actualizacion ? String(d.fecha_actualizacion).slice(0, 16) : '—')}</td>
    <td class="muted" style="font-size:0.78rem">${escaparHtml(d.evidencia || '')}${d.cobertura ? `<br>${escaparHtml(d.cobertura)}` : ''}</td></tr>`;
  let filas = '';
  for (const j of js) {
    const d = j.datos || {};
    filas += fila('Edad', j.nombre, d.edad) + fila('País', j.nombre, d.pais) + fila('UTR', j.nombre, d.utr) + fila('Inactividad', j.nombre, d.inactividad);
    for (const [k, n] of _CTX_SUP) filas += fila(`Superficie ${n}`, j.nombre, (d.superficie || {})[k]) + fila(`Rivales ${n} (UTR medio)`, j.nombre, (d.rivales_por_superficie || {})[k]) + fila(`Último ${n}`, j.nombre, (d.ultimo_por_superficie || {})[k]);
  }
  filas += fila('H2H', '', (c.h2h || {}).dato);
  const lista = (xs, f) => (xs || []).length ? `<ul style="margin:4px 0;padding-left:18px">${xs.map(f).join('')}</ul>` : '<div class="muted">Ninguno.</div>';
  return `<details style="margin:10px 0;border:1px solid var(--line,#333);border-radius:8px;padding:6px 10px">
    <summary style="cursor:pointer;font-weight:600">🧾 Confianza por dato (admin) · VERIFICADO / PARCIAL / NO_VERIFICABLE</summary>
    <div style="overflow:auto"><table class="tabla" style="width:100%;font-size:0.82rem"><tr><th>Dato</th><th>Jugador</th><th>Valor</th><th>Confianza</th><th>Fuente</th><th>Actualizado</th><th>Evidencia · cobertura</th></tr>${filas}</table></div>
    <div style="margin-top:8px;font-weight:600">🟡 Hallazgos PARCIALES (no salen en el aviso)</div>
    ${lista(c.destacados_parciales, x => `<li>${escaparHtml(x.icono)} ${escaparHtml(x.texto)} <span class="muted">· ${escaparHtml(x.fuente || '')}${x.evidencia ? ' · ' + escaparHtml(x.evidencia) : ''}</span></li>`)}
    <div style="margin-top:8px;font-weight:600">🔴 NO_VERIFICABLES (no generan conclusiones)</div>
    ${lista(c.no_verificables, x => `<li>${escaparHtml(x.texto)} <span class="muted">· ${escaparHtml(x.evidencia || '')}</span></li>`)}
    ${(c.h2h_parciales || []).length ? `<div style="margin-top:8px;font-weight:600">⚔️ Enfrentamientos solo por nombre (no cuentan en el H2H)</div>${lista(c.h2h_parciales, x => `<li>${escaparHtml(x.fecha)} · ${escaparHtml(x.superficie)} · ${escaparHtml(x.resultado)}</li>`)}` : ''}
  </details>`;
}

function deInvestigacionCerrar() { const m = document.getElementById('ei-modal'); if (m) m.remove(); }
const _eiSec = (titulo, cuerpo, abierta = true) => `<details ${abierta ? 'open' : ''} style="margin:8px 0;border:1px solid var(--line,#333);border-radius:8px;padding:6px 10px"><summary style="cursor:pointer;font-weight:600">${titulo}</summary><div style="margin-top:6px">${cuerpo}</div></details>`;
const _eiWL = ult => (ult || []).map(x => `<span style="display:inline-block;width:16px;height:16px;line-height:16px;border-radius:50%;text-align:center;font-size:0.65rem;font-weight:700;margin-right:2px;background:${x === 'W' ? 'var(--green)' : 'var(--red)'};color:#000">${x}</span>`).join('');
const _eiCargaColor = c => c === 'CARGA_EXTREMA' || c === 'CARGA_ALTA' ? 'var(--red)' : c === 'CARGA_CONSIDERABLE' ? 'var(--amarillo,#e8b339)' : c === 'INACTIVIDAD' ? 'var(--suave)' : 'var(--green)';
const _eiFecha = v => { try { return v ? pkFecha(v) : '—'; } catch (e) { return v || '—'; } };

function _eiTarjeta(l, seleccion) {
  if (!l) return '';
  const probCol = (l.prob ?? 0) >= 0.5 ? 'var(--green)' : 'var(--red)';
  // v54g: UTR VALUE / Market Anomaly → solo UTR (sin Elo ni FTR)
  return `<div class="compare-card" style="min-width:260px${seleccion ? ';border:1px solid var(--accent)' : ''}">
    <h3 style="margin-bottom:6px">${escaparHtml(l.nombre || '')}${seleccion ? ' <span class="muted" style="font-size:0.75rem;font-weight:normal">selección</span>' : ''}</h3>
    <div style="font-size:0.85rem"><b>UTR:</b> ${_EI_N(l.utr)}${l.solo_utr ? '' : ` · <b>FTR:</b> ${_EI_N(l.ftr)} · <b>Elo:</b> ${_EI_N(l.elo, 0)}`}</div>
    <div class="prob" style="color:${probCol};margin-top:6px">${_EI_PCT1(l.prob)}</div>
    <div class="muted">${l.solo_utr ? 'Probabilidad por UTR <span title="Tabla ΔUTR → % de UTR VALUE (Admin). Es la de partida: en estudio, no validada.">(en estudio)</span>' : 'Probabilidad FullTennis'}</div>
    ${l.solo_utr ? '' : `<div style="font-size:0.85rem;margin-top:6px"><b>${_EI_PCT1(l.prob_elo)}</b> <span class="muted">Elo</span> · <b>${_EI_PCT1(l.prob_ftr)}</b> <span class="muted">FTR</span></div>`}
    <div style="margin-top:8px">${_eiWL(l.ultimos)}${l.ultimos_txt ? ` <span class="muted">Últimos ${(l.ultimos || []).length}: ${l.ultimos_txt}</span>` : '<span class="muted">sin últimos partidos</span>'}</div>
    ${l.oposicion ? `<div class="muted" style="font-size:0.78rem">${escaparHtml(l.oposicion)}</div>` : ''}
    <div style="font-size:0.85rem;margin-top:6px"><b>Carga:</b> <span style="color:${_eiCargaColor(l.carga_clase)}">${escaparHtml(l.carga || 'sin datos')}</span></div>
    <div style="font-size:0.85rem"><b>Tiempos médicos (90 d):</b> ${l.mto_n}${l.mto_hace != null ? ` <span class="muted">(hace ${Math.round(l.mto_hace)} d)</span>` : ''} · <b>Retiros (180 d):</b> ${l.retiros_n}${l.retiros_hace != null ? ` <span class="muted">(hace ${Math.round(l.retiros_hace)} d)</span>` : ''}</div>
    ${l.ultimo_partido ? `<div style="font-size:0.85rem"><b>Último partido:</b> ${escaparHtml(l.ultimo_partido)}</div>` : ''}
    ${_eiRodaje(l.rodaje)}
  </div>`;
}

// v54b: rodaje en la superficie del partido (match_log por superficie)
function _eiRodaje(r) {
  if (!r) return '';
  const col = r.sin_rodaje ? 'var(--amarillo,#e8b339)' : 'inherit';
  const txt = r.ultimo ? `último hace ${escaparHtml(r.hace || '—')} (${escaparHtml(r.ultimo)}) · ${r.partidos_12m} en 12 meses · ${r.partidos_24m} en 24 meses${r.partidos_24m ? ` (${r.ganados_24m} ganados)` : ''}`
    : `ningún partido en nuestros registros${r.registros_desde ? ` (desde ${escaparHtml(r.registros_desde)})` : ''}`;
  return `<div style="font-size:0.85rem;color:${col}"><b>En ${escaparHtml(r.superficie)}:</b> ${r.sin_rodaje ? '⚠️ ' : ''}${txt}</div>`;
}

// v54c: el contexto de ¿POR QUÉ LA CUOTA? (siete áreas, hechos comprobados, lo que no sabemos). Solo referencia.
function _eiContexto(c) {
  const cl = c.clase || {};
  const areas = (c.areas || []).map(a => `<li>${escaparHtml(a.icono || '')} <b>${escaparHtml(a.nombre || '')}:</b> ${escaparHtml(a.estado || '')} ${escaparHtml(a.texto || '')}</li>`).join('');
  const hechos = (c.hechos || []).map(x => `<li>${x.peso === 'FUERTE' ? '⚠️' : '🟡'} ${escaparHtml(x.texto || '')}${x.externa ? ' <span class="muted">(búsqueda externa)</span>' : ''}</li>`).join('');
  const nos = (c.no_sabemos || []).map(x => `<li>${escaparHtml(x)}</li>`).join('');
  return `<div style="margin-top:10px;padding:8px 12px;border-radius:8px;border:1px solid var(--line,#444);background:var(--panel)">
    <div><b>🔎 ¿POR QUÉ LA CUOTA?</b> · ${escaparHtml(cl.icono || '⚪')} ${escaparHtml(cl.texto || '')} <span class="muted" style="font-size:0.75rem">(${escaparHtml(c.origen || 'datos internos')}${c.externa_estado ? ' · externa ' + escaparHtml(c.externa_estado) : ''})</span></div>
    <ul style="margin:4px 0 0;padding-left:18px">${areas}</ul>
    ${hechos ? `<div style="margin-top:6px"><b>Hechos comprobados</b> <span class="muted">(podrían ser relevantes para interpretar la cuota)</span><ul style="margin:2px 0 0;padding-left:18px">${hechos}</ul></div>` : ''}
    ${nos ? `<details style="margin-top:6px"><summary class="muted" style="cursor:pointer">❔ Lo que no sabemos (${(c.no_sabemos || []).length})</summary><ul style="margin:2px 0 0;padding-left:18px">${nos}</ul></details>` : ''}
    ${c.error ? `<div class="warn" style="font-size:0.8rem">${escaparHtml(c.error)}</div>` : ''}
    <div class="muted" style="font-size:0.72rem;margin-top:4px">${escaparHtml(c.aviso || '')}</div></div>`;
}

function _eiVersion(v, abierta) {
  const f = v.ficha;
  let h = `<div style="font-size:1.05rem">🎾 <b>${escaparHtml(v.jugador || '')}</b> vs ${escaparHtml(v.rival || '')} <span class="muted">· ${escaparHtml(v.torneo || '')} · ${v.inicio_programado ? _eiFecha(v.inicio_programado) : 'hora por confirmar'}</span></div>
    <div class="muted" style="font-size:0.8rem;margin:2px 0 8px">Versión ${v.version}${v.vigente ? ' · vigente' : ''} · investigada ${_eiFecha(v.calculado_en)} · ${escaparHtml(v.momento_txt || '')}${v.cuota_jugador ? ` · cuota de la selección ${_EI_N(v.cuota_jugador)}${v.cuota_rival ? ' / rival ' + _EI_N(v.cuota_rival) : ''}` : ''}</div>`;
  if (f) {
    h += `<div class="compare-box" style="gap:16px">${_eiTarjeta(f.jugador, true)}${_eiTarjeta(f.rival, false)}</div>`;
    if (f.delta_utr != null) {
      // v54g: con ΔUTR 0.00 decía «X es superior por UTR»: ahora dice que tienen el mismo UTR
      const igual = Math.abs(f.delta_utr) < 0.005;
      const sup = f.delta_utr >= 0 ? f.jugador.nombre : f.rival.nombre;
      h += `<div style="margin-top:10px;padding:8px 12px;border-radius:8px;background:var(--panel)"><b>ΔUTR</b> (${escaparHtml(f.jugador.nombre)} − ${escaparHtml(f.rival.nombre)}): <b style="color:${igual ? 'inherit' : f.delta_utr >= 0 ? 'var(--green)' : 'var(--red)'}">${f.delta_utr > 0 ? '+' : ''}${_EI_N(f.delta_utr)}</b> · ${igual ? 'mismo UTR' : escaparHtml(sup) + ' es superior por UTR'}</div>`;
    }
  }
  const pm = v.p_mercado_jugador;
  if (v.contexto) h += _eiContexto(v.contexto);
  else if ((v.reservas || []).length) h += `<div style="margin-top:10px;padding:8px 12px;border-radius:8px;border:1px solid var(--amarillo,#e8b339);background:rgba(232,179,57,.08)">
    <b>⚠️ Lo que la probabilidad no tiene en cuenta</b> <span class="muted">(versión anterior)</span><ul style="margin:4px 0 0;padding-left:18px">${v.reservas.map(x => `<li>${escaparHtml(x)}</li>`).join('')}</ul></div>`;
  const sinNivel = v.p_base_jugador == null;
  const soloUtr = String(v.metodo_base || '').startsWith('Probabilidad UTR');
  if (soloUtr) h += `<div style="margin-top:10px;padding:8px 12px;border-radius:8px;background:var(--panel)">
    <div><b>📈 PROBABILIDAD POR UTR</b> <span class="muted">· solo UTR, sin Elo ni FTR (tabla de UTR VALUE · en estudio)</span></div>
    ${sinNivel ? `<div class="muted" style="font-size:0.85rem">${escaparHtml(v.metodo_base || '')}</div>`
      : `<div>${escaparHtml(v.jugador || '')} <b>${_EI_PCT(v.p_base_jugador)}</b> · ${escaparHtml(v.rival || '')} <b>${_EI_PCT(1 - v.p_base_jugador)}</b>${pm != null ? ` <span class="muted">· la cuota dice ${_EI_PCT(pm)}</span>` : ''}</div>`}
    ${v.superficie_es ? `<div class="muted" style="font-size:0.8rem">Se juega en ${escaparHtml(v.superficie_es)}</div>` : ''}
    ${v.frase_apoyo ? `<div style="margin-top:4px">ℹ️ ${escaparHtml(v.frase_apoyo)}</div>` : ''}
  </div>`;
  else h += `<div style="margin-top:10px;padding:8px 12px;border-radius:8px;background:var(--panel)">
    ${sinNivel ? `<div><b>📊 SIN PROBABILIDAD FULLTENNIS</b> <span class="muted">· no hay Elo ni FTR de los dos (el ΔUTR es solo un dato)</span>${pm != null ? `<div style="font-size:0.85rem">💰 La cuota dice (aparte): ${escaparHtml(v.jugador || '')} ${_EI_PCT(pm)}</div>` : ''}</div>`
      : `<div><b>📊 PROBABILIDAD FULLTENNIS</b> · ${escaparHtml(v.jugador || '')} <b>${_EI_PCT(v.p_base_jugador)}</b> · ${escaparHtml(v.rival || '')} <b>${_EI_PCT(1 - v.p_base_jugador)}</b>${pm != null ? ` <span class="muted">· la cuota dice ${_EI_PCT(pm)}</span>` : ''}</div>
    <div class="muted" style="font-size:0.8rem">Calculada con: ${escaparHtml(v.metodo_base || '—')}</div>`}
    ${v.superficie_es ? `<div class="muted" style="font-size:0.8rem">Se juega en ${escaparHtml(v.superficie_es)}</div>` : ''}
    ${v.frase_apoyo ? `<div style="margin-top:4px;color:${v.apoya === 1 ? 'var(--green)' : v.apoya === 0 ? 'var(--amarillo,#e8b339)' : 'inherit'}">${v.apoya === 1 ? '✅' : v.apoya === 0 ? '⚠️' : 'ℹ️'} ${escaparHtml(v.frase_apoyo)}</div>` : ''}
  </div>`;
  h += `<div style="margin-top:10px;padding:8px 12px;border-radius:8px;background:var(--panel)"><b>🧠 Conclusión:</b> ${escaparHtml(v.conclusion || '')}
    <div class="muted" style="font-size:0.75rem;margin-top:4px">${v.conclusion_fuente === 'ia' ? 'Redactada por la IA con los números de arriba (no puede cambiarlos).' : 'Armada con los datos, sin IA.'}</div></div>`;
  // v54d: arriba ya están las fichas, la probabilidad y el contexto; acá solo lo demás (ajustes, FT NEWS)
  if ((v.lectura || []).length) h += _eiSec(v.ficha ? '📋 Lo demás que se miró' : '📋 Todo lo que se miró', `<ul style="margin:0;padding-left:18px">${(v.lectura || []).map(x => `<li>${escaparHtml(x)}</li>`).join('')}</ul>`, abierta);
  if (v.datos) h += _eiSec('🔧 Detalles técnicos (admin)', `<pre style="white-space:pre-wrap;font-size:0.75rem">${escaparHtml(JSON.stringify(v.datos, null, 1))}</pre>`, false);
  return h;
}

// v54l: en el bot UTR el modal pide primero el CONTEXTO corto (rápido, /contexto); la investigación completa solo se
// carga si un admin abre «Detalles técnicos» (así un cálculo pesado nunca deja el contexto sin mostrar).
async function deContextoUtr(clave, id) {
  deInvestigacionCerrar();
  const caja = document.createElement('div');
  caja.id = 'ei-modal'; caja.className = 'modal-overlay';
  caja.onclick = (e) => { if (e.target === caja) deInvestigacionCerrar(); };
  caja.innerHTML = `<div class="modal-caja" style="width:min(1000px,96vw);max-height:90vh;overflow:auto">
    <div class="modal-titulo" style="color:var(--accent)">🔎 FT INTELLIGENCE · CONTEXTO DEL PARTIDO <span class="muted" style="font-size:0.8rem;font-weight:normal">solo referencia</span> <button class="modal-cerrar" onclick="deInvestigacionCerrar()">✕</button></div>
    <div id="ei-modal-cuerpo" class="muted">Cargando el contexto…</div></div>`;
  document.body.appendChild(caja);
  const esc = (e) => { if (e.key === 'Escape') { deInvestigacionCerrar(); document.removeEventListener('keydown', esc); } };
  document.addEventListener('keydown', esc);
  const cuerpo = document.getElementById('ei-modal-cuerpo');
  let c = null;
  for (let intento = 0; intento < 2 && !c; intento++) {
    try {
      const res = await fetch(`/research/api/estrategia/${encodeURIComponent(clave)}/${Number(id)}/contexto`, {credentials: 'same-origin'});
      if (!res.ok) throw new Error(await mensajeError(res));
      c = await res.json();
    } catch (e) {
      if (intento === 1) { cuerpo.innerHTML = `<span class="warn">No se pudo cargar el contexto: ${escaparHtml(e.message || e)}</span>`; return; }
      await new Promise(r => setTimeout(r, 2500));
    }
  }
  cuerpo.classList.remove('muted');
  cuerpo.innerHTML = _ctxUtrHtml(c) + (c.es_admin ? `<details id="ei-tecnico" style="margin:10px 0;border:1px solid var(--line,#333);border-radius:8px;padding:6px 10px">
      <summary style="cursor:pointer;font-weight:600">🔧 Detalles técnicos (admin) · investigación completa: probabilidad en estudio, fuentes, carga, oposición, salud, noticias</summary>
      <div id="ei-tecnico-cuerpo" class="muted" style="margin-top:6px">Se carga al abrir.</div></details>` : '');
  const det = document.getElementById('ei-tecnico');
  if (det) det.addEventListener('toggle', async () => {
    if (!det.open || det.dataset.cargado) return;
    det.dataset.cargado = '1';
    const tc = document.getElementById('ei-tecnico-cuerpo');
    tc.textContent = 'Investigando… (si es la primera vez, tarda unos segundos)';
    try {
      const res = await fetch(`/research/api/estrategia/${encodeURIComponent(clave)}/${Number(id)}/investigacion`, {credentials: 'same-origin'});
      if (!res.ok) throw new Error(await mensajeError(res));
      const d = await res.json();
      const vs = d.versiones || [];
      tc.classList.remove('muted');
      tc.innerHTML = !vs.length ? '<div class="muted">Sin investigación para esta fila.</div>'
        : _eiVersion(vs[0], true) + (vs.length > 1 ? _eiSec(`🕘 Versiones anteriores (${vs.length - 1})`,
            vs.slice(1).map(v => `<div style="margin:6px 0;padding:6px;border-left:3px solid #666">${_eiVersion(v, false)}</div>`).join(''), false) : '');
    } catch (e) { tc.innerHTML = `<span class="warn">${escaparHtml(e.message || e)}</span>`; det.dataset.cargado = ''; }
  });
}

// ═══ 🚨 CUOTA MAL PUESTA · ficha (17/10/2026, Daniel/Rubén: diseño «Tupitsyna vs Lim») ═══
// Los 4 números son los CONGELADOS de la señal (los mismos de Discord y del histórico): FT final, mercado SIN MARGEN,
// ventaja y cuota. Hora en Colombia con la UTC al lado. Oposición por UTR. Salud: solo lo detectado.
const _FC = {
  card: 'border:1px solid var(--line,#333);border-radius:12px;padding:10px 14px;background:rgba(255,255,255,0.02)',
  ttl: 'font-weight:800;letter-spacing:.02em;font-size:0.86rem;text-transform:uppercase;margin-bottom:6px',
  verde: '#7CFC00', rojo: '#ff6b6b', amar: '#e8d23a'};
const _fcN = (v, d = 2) => (v === null || v === undefined) ? '—' : Number(v).toFixed(d);
const _fcPct = (v, d = 1) => (v === null || v === undefined) ? '—' : (Number(v) * 100).toFixed(d) + '%';
function _fcBarra(pct, color) {
  const w = Math.max(0, Math.min(100, Number(pct) || 0));
  return `<div style="height:8px;border-radius:5px;background:rgba(255,255,255,0.08);overflow:hidden;margin:4px 0"><div style="width:${w}%;height:100%;background:${color}"></div></div>`;
}
function _fcWL(lista) {
  return (lista || []).map(x => `<span style="display:inline-block;width:22px;height:22px;line-height:22px;border-radius:50%;text-align:center;font-size:0.72rem;font-weight:800;margin:0 2px;background:${x === 'W' ? '#2fbf55' : '#e04848'};color:#fff">${x}</span>`).join('');
}
function _cmpFichaHtml(f) {
  const [a, b] = f.jugadores || [];
  const n = f.numeros || {}, c = f.contexto || {}, o = f.oposicion || {}, cg = f.carga || {}, sl = f.salud || {};
  const [ca, cb] = c.jugadores || [{}, {}];
  const titulo = (f.nivel === 'PREMIUM' || f.nivel === 'MUY') ? 'CUOTA MUY MAL PUESTA' : 'CUOTA MAL PUESTA';
  const ini = f.inicio || {}, det = f.detectada || {};
  const cab = `<div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:flex-start">
    <div><div style="font-size:1.6rem;font-weight:900;color:${_FC.verde}">🚨 ${titulo}</div>
      <div class="muted" style="font-size:0.86rem">${escaparHtml(f.torneo || '')}${ini.col ? ` · <b style="color:var(--text,#eee)">${escaparHtml(ini.col)} (Col)</b> · ${escaparHtml(ini.utc)}` : ''}
        ${det.hace ? ` · Detectada ${escaparHtml(det.hace)}` : ''} · ${escaparHtml(f.version_motor || '')}</div></div>
    <span style="border:1px solid ${_FC.amar};color:${_FC.amar};border-radius:8px;padding:4px 10px;font-size:0.8rem">ⓘ Solo referencia</span></div>`;
  const tarjeta = (j) => `<div style="flex:1;min-width:240px;${_FC.card};border-color:${_FC.verde}">
      <div style="display:flex;gap:10px;align-items:center"><span style="font-size:2rem">${escaparHtml(j.bandera || '🏳️')}</span>
      <div><div style="font-size:1.25rem;font-weight:800">${escaparHtml(j.nombre || '')}</div>
      <div class="muted" style="font-size:0.84rem">${j.edad ? j.edad + ' años' : 'edad —'} &nbsp;|&nbsp; UTR: <b>${_fcN(j.utr)}</b> &nbsp;|&nbsp; FTR: <b>${_fcN(j.ftr)}</b> &nbsp;|&nbsp; Elo: <b>${_fcN(j.elo, 0)}</b></div></div></div></div>`;
  const kpi = (t, v, sub, col) => `<div style="flex:1;min-width:150px;${_FC.card};text-align:center${col ? `;border-color:${col}` : ''}">
      <div style="${_FC.ttl}">${t}</div><div style="font-size:1.9rem;font-weight:900;color:${col || 'var(--text,#eee)'}">${v}</div>
      ${sub ? `<div class="muted" style="font-size:0.74rem">${sub}</div>` : ''}</div>`;
  const edge = n.edge_pp;
  const kpis = `<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px">
    ${kpi('Probabilidad FullTennis', _fcPct(n.prob_ft), n.prob_ft_base != null ? `base Elo + FTR ${_fcPct(n.prob_ft_base, 0)}` : '', _FC.verde)}
    ${kpi('Probabilidad del mercado', _fcPct(n.mercado_sin_margen), '(sin margen)' + (n.mercado_bruto != null ? ` · 1/cuota ${_fcPct(n.mercado_bruto)}` : ''))}
    ${kpi('Ventaja FullTennis', edge != null ? (edge >= 0 ? '+' : '') + Number(edge).toFixed(1) + ' pp' : '—', 'FT − mercado sin margen', edge != null && edge >= 0 ? _FC.verde : _FC.rojo)}
    ${kpi('Cuota', _fcN(n.odds_a), `cuota de la selección · rival ${_fcN(n.odds_b)}`)}</div>
    <div class="muted" style="font-size:0.72rem;margin-top:4px">Números congelados al detectar la señal${n.congelado_en ? ' (' + escaparHtml(String(n.congelado_en).slice(0, 16).replace('T', ' ')) + ' UTC)' : ''}: son los mismos de Discord y del histórico.${n.reconstruido ? ' Señal anterior a V3: mercado sin margen calculado con sus cuotas guardadas.' : ''}</div>`;
  const opoLado = (j, x, col) => `<div style="flex:1;min-width:120px"><div class="muted" style="font-size:0.8rem">${escaparHtml(j.nombre || '')}</div>
      <div style="font-size:1.35rem;font-weight:800;color:${col}">${x.pct != null ? x.pct + '%' : '—'}</div>
      ${x.rivales_ftr_prom != null || x.rivales_utr_prom == null && x.rivales_utr_actual != null
        ? `<div class="muted" style="font-size:0.76rem">Rivales FTR prom.: <b>${_fcN(x.rivales_ftr_prom)}</b></div>
           <div class="muted" style="font-size:0.7rem" title="UTR actual del padrón de esos mismos rivales. No es el que usó la señal (V2 midió por FTR/Elo).">UTR actual de esos rivales: ${_fcN(x.rivales_utr_actual)}${x.rivales_utr_actual_n ? ` (${x.rivales_utr_actual_n} con UTR · referencia)` : ''}</div>`
        : `<div class="muted" style="font-size:0.76rem">Rivales UTR prom.: <b>${_fcN(x.rivales_utr_prom)}</b></div>`}${_fcBarra(x.pct, col)}</div>`;
  const oposicion = `<div style="flex:1;min-width:260px;${_FC.card}"><div style="${_FC.ttl}">📊 Oposición <span class="muted" style="text-transform:none;font-weight:normal">(últimos ${o.n_a ?? '—'} / ${o.n_b ?? '—'} · ${o.fuente === 'utr' ? 'por UTR' : 'por FTR/Elo'})</span></div>
      <div style="display:flex;gap:12px">${opoLado(a, o.a || {}, _FC.verde)}${opoLado(b, o.b || {}, _FC.rojo)}</div>
      <div class="muted" style="font-size:0.76rem">ⓘ ${escaparHtml(o.texto || '')}${o.ajuste_pp ? ` · ajuste ${o.ajuste_pp > 0 ? '+' : ''}${Number(o.ajuste_pp).toFixed(1)} pp` : ''}</div></div>`;
  const cargaLado = (j, x) => `<div style="flex:1;min-width:120px"><div class="muted" style="font-size:0.8rem">${escaparHtml(j.nombre || '')}</div>
      <div style="font-size:1.25rem;font-weight:800">${x.games_7d ?? '—'} games</div><div class="muted" style="font-size:0.76rem">${x.partidos_7d ?? '—'} partido(s)</div>
      ${_fcBarra(x.indice, _FC.amar)}<div style="font-size:0.74rem;text-align:center">${escaparHtml((_CMP_CLASE_TXT[x.clase] || x.clase || 'sin datos'))}</div></div>`;
  const carga = `<div style="flex:1;min-width:260px;${_FC.card}"><div style="${_FC.ttl}">🏃 Fatiga / carga <span class="muted" style="text-transform:none;font-weight:normal">(últimos 7 días)</span></div>
      <div style="display:flex;gap:12px">${cargaLado(a, cg.a || {})}${cargaLado(b, cg.b || {})}</div>
      <div class="muted" style="font-size:0.76rem">ⓘ ${cg.ajuste_pp ? `ajuste por carga ${cg.ajuste_pp > 0 ? '+' : ''}${Number(cg.ajuste_pp).toFixed(1)} pp` : 'sin ajuste por carga'}</div></div>`;
  const saludLado = (j, x) => { x = x || {};
    const ev = (x.mto_n || 0) + (x.retiros_n || 0);
    return `<div style="flex:1;min-width:120px"><div class="muted" style="font-size:0.8rem">${escaparHtml(j.nombre || '')}</div>
      ${ev ? `<div style="font-weight:800;color:${_FC.rojo}">🩹 ${x.mto_n ? `${x.mto_n} MTO${x.mto_hace != null ? ` (hace ${Math.round(x.mto_hace)} d)` : ''}` : ''}${x.retiros_n ? ` · ${x.retiros_n} retiro(s)` : ''}</div>`
          : `<div style="font-weight:700">Ninguno detectado</div>`}</div>`; };
  const salud = `<div style="flex:1;min-width:260px;${_FC.card}"><div style="${_FC.ttl}">➕ Salud / MTO <span class="muted" style="text-transform:none;font-weight:normal">(últimos 6 meses)</span></div>
      <div style="display:flex;gap:12px">${saludLado(a, sl.a)}${saludLado(b, sl.b)}</div>
      <div class="muted" style="font-size:0.72rem">${escaparHtml(sl.nota || '')}</div></div>`;
  const supFila = ([k, nm, col]) => `<tr><td>${_ctxSupChip(nm, col)}</td><td style="text-align:center"><b>${escaparHtml(((ca.sup || {})[k] || {}).txt || '—')}</b></td><td style="text-align:center"><b>${escaparHtml(((cb.sup || {})[k] || {}).txt || '—')}</b></td></tr>`;
  const sup = `<div style="flex:1.3;min-width:280px;${_FC.card}"><div style="${_FC.ttl}">🎾 Rendimiento por superficie <span class="muted" style="text-transform:none;font-weight:normal">Registro FullTennis (desde 2022)</span></div>
      <table class="tabla" style="width:100%"><tr><th>Superficie</th><th>${escaparHtml(a.nombre)}</th><th>${escaparHtml(b.nombre)}</th></tr>${_CTX_SUP.map(supFila).join('')}</table></div>`;
  const u5 = (j, x) => { x = x || {};
    return `<div style="flex:1;min-width:120px;text-align:center"><div class="muted" style="font-size:0.8rem">${escaparHtml(j.nombre || '')}</div>
      <div style="margin:4px 0">${_fcWL(x.resultados)}</div><div style="font-size:1.2rem;font-weight:800">${x.w ?? 0} - ${x.l ?? 0}</div>
      <div class="muted" style="font-size:0.74rem">Rendimiento: ${x.pct != null ? x.pct + '%' : '—'}<br>UTR rivales prom.: ${_fcN(x.rivales_utr_prom)}</div></div>`; };
  const ult5 = `<div style="flex:1;min-width:250px;${_FC.card}"><div style="${_FC.ttl}">📅 Últimos 5 partidos</div>
      <div style="display:flex;gap:10px">${u5(a, ca.ultimos5)}${u5(b, cb.ultimos5)}</div></div>`;
  const h = c.h2h || {};
  const h2h = `<div style="flex:0.8;min-width:200px;${_FC.card}"><div style="${_FC.ttl}">⚔️ Enfrentamientos entre ellos</div>
      <div style="font-size:1.3rem;font-weight:800;text-align:center;margin-top:8px">H2H: <span style="color:${_FC.verde}">${h.w ?? 0} - ${h.l ?? 0}</span></div>
      ${(h.ultimos || []).length ? `<ul style="margin:6px 0 0;padding-left:16px;font-size:0.8rem">${h.ultimos.map(x => `<li>${escaparHtml(x.fecha)} · ${escaparHtml(x.superficie)} · ${escaparHtml(x.resultado)}</li>`).join('')}</ul>`
        : '<div class="muted" style="text-align:center;font-size:0.82rem">Sin enfrentamientos registrados.</div>'}</div>`;
  const ultSup = (j, k) => { const u = ((j.sup || {})[k] || {}).ultimo; return u ? escaparHtml(u.txt) : '<span class="muted">Sin partidos registrados.</span>'; };
  const ultimo = `<div style="${_FC.card};margin-top:10px"><div style="${_FC.ttl}">🗓️ Último registrado en FullTennis (por superficie)</div>
      <table class="tabla" style="width:100%"><tr><th></th><th>${escaparHtml(a.bandera || '')} ${escaparHtml(a.nombre)}</th><th>${escaparHtml(b.bandera || '')} ${escaparHtml(b.nombre)}</th></tr>
      ${_CTX_SUP.map(([k, nm, col]) => `<tr><td>${_ctxSupChip(nm, col)}</td><td>${ultSup(ca, k)}</td><td>${ultSup(cb, k)}</td></tr>`).join('')}</table></div>`;
  const dest = (c.destacados || []).length ? `<ul style="margin:0;padding-left:4px;list-style:none">${c.destacados.map(d => `<li style="margin:3px 0">${escaparHtml(d.icono)} ${escaparHtml(d.texto)}</li>`).join('')}</ul>`
    : '<div>🟢 Sin alertas relevantes adicionales.</div>';
  const datos = `<div style="${_FC.card};margin-top:10px"><div style="${_FC.ttl}">⭐ Datos relevantes</div>${dest}</div>`;
  return `${cab}<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:10px">${tarjeta(a)}<div style="font-weight:900;font-size:1.3rem">VS</div>${tarjeta(b)}</div>
    ${kpis}
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px">${oposicion}${carga}${salud}</div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px">${sup}${ult5}${h2h}</div>
    ${ultimo}${datos}
    <div class="muted" style="font-size:0.74rem;margin-top:8px;text-align:center">${escaparHtml(f.aviso || '')}</div>
    ${f.es_admin && c.jugadores ? _ctxAuditoriaHtml(c) : ''}`;
}
async function deFichaCmp(id) {
  deInvestigacionCerrar();
  const caja = document.createElement('div');
  caja.id = 'ei-modal'; caja.className = 'modal-overlay';
  caja.onclick = (e) => { if (e.target === caja) deInvestigacionCerrar(); };
  caja.innerHTML = `<div class="modal-caja" style="width:min(1100px,97vw);max-height:92vh;overflow:auto">
    <div class="modal-titulo" style="justify-content:flex-end"><button class="modal-cerrar" onclick="deInvestigacionCerrar()">✕</button></div>
    <div id="ei-modal-cuerpo" class="muted">Cargando la señal…</div></div>`;
  document.body.appendChild(caja);
  const esc = (e) => { if (e.key === 'Escape') { deInvestigacionCerrar(); document.removeEventListener('keydown', esc); } };
  document.addEventListener('keydown', esc);
  const cuerpo = document.getElementById('ei-modal-cuerpo');
  try {
    const res = await fetch(`/research/api/estrategia/cmp/${Number(id)}/ficha`, {credentials: 'same-origin'});
    if (!res.ok) throw new Error(await mensajeError(res));
    const f = await res.json();
    cuerpo.classList.remove('muted');
    cuerpo.innerHTML = _cmpFichaHtml(f) + (f.es_admin ? `<details id="ei-tecnico" style="margin:10px 0;border:1px solid var(--line,#333);border-radius:8px;padding:6px 10px">
      <summary style="cursor:pointer;font-weight:600">🔧 Detalles técnicos (admin) · investigación completa</summary>
      <div id="ei-tecnico-cuerpo" class="muted" style="margin-top:6px">Se carga al abrir.</div></details>` : '');
    const dt = document.getElementById('ei-tecnico');
    if (dt) dt.addEventListener('toggle', async () => {
      if (!dt.open || dt.dataset.cargado) return;
      dt.dataset.cargado = '1';
      const tc = document.getElementById('ei-tecnico-cuerpo');
      tc.textContent = 'Investigando… (si es la primera vez, tarda unos segundos)';
      try {
        const r2 = await fetch(`/research/api/estrategia/cmp/${Number(id)}/investigacion`, {credentials: 'same-origin'});
        if (!r2.ok) throw new Error(await mensajeError(r2));
        const d = await r2.json();
        const vs = d.versiones || [];
        tc.classList.remove('muted');
        tc.innerHTML = !vs.length ? '<div class="muted">Sin investigación para esta señal.</div>' : _eiVersion(vs[0], true);
      } catch (e) { tc.innerHTML = `<span class="warn">${escaparHtml(e.message || e)}</span>`; dt.dataset.cargado = ''; }
    });
  } catch (e) {
    cuerpo.innerHTML = `<span class="warn">No se pudo cargar la señal: ${escaparHtml(e.message || e)}</span>`;
  }
}

async function deInvestigacion(clave, id) {
  if (clave === 'utrv' || clave === 'utra') return deContextoUtr(clave, id);
  if (clave === 'cmp') return deFichaCmp(id);   // 17/10/2026: ficha nueva de Cuota Mal Puesta
  deInvestigacionCerrar();
  const caja = document.createElement('div');
  caja.id = 'ei-modal'; caja.className = 'modal-overlay';
  caja.onclick = (e) => { if (e.target === caja) deInvestigacionCerrar(); };
  caja.innerHTML = `<div class="modal-caja" style="width:min(1000px,96vw);max-height:90vh;overflow:auto">
    <div class="modal-titulo" style="color:var(--accent)">🧠 Investigación <span id="ei-modal-titulo"></span> <button class="modal-cerrar" onclick="deInvestigacionCerrar()">✕</button></div>
    <div id="ei-modal-cuerpo" class="muted">Investigando… (si es la primera vez, tarda unos segundos)</div></div>`;
  document.body.appendChild(caja);
  const esc = (e) => { if (e.key === 'Escape') { deInvestigacionCerrar(); document.removeEventListener('keydown', esc); } };
  document.addEventListener('keydown', esc);
  try {
    const res = await fetch(`/research/api/estrategia/${encodeURIComponent(clave)}/${Number(id)}/investigacion`, {credentials: 'same-origin'});
    if (!res.ok) throw new Error(await mensajeError(res));
    const d = await res.json();
    const vs = d.versiones || [];
    const v0 = vs[0];
    document.getElementById('ei-modal-titulo').textContent = `· ${d.estrategia_txt || ''}${v0 ? ' · ' + v0.jugador + ' vs ' + v0.rival : ''}`;
    let h = '';
    let tecnico = '';
    if (!vs.length) tecnico = '<div class="muted">No se pudo investigar esta fila (faltan los nombres de los jugadores).</div>';
    else {
      tecnico += _eiVersion(vs[0], true);
      if (vs.length > 1) tecnico += _eiSec(`🕘 Versiones anteriores (${vs.length - 1}): la investigación cambió antes del partido (cuota o datos)`,
        vs.slice(1).map(v => `<div style="margin:6px 0;padding:6px;border-left:3px solid #666">${_eiVersion(v, false)}</div>`).join(''), false);
    }
    h += tecnico;
    h += '<p class="muted" style="font-size:0.75rem;margin-top:10px">Investigación con los datos de FullTennis; no es una recomendación de apuesta. No cambia la estrategia ni sus resultados.</p>';
    document.getElementById('ei-modal-cuerpo').innerHTML = h;
    // la fila ya tiene investigación: refrescar la columna sin recargar todo
    const est = DE_ESTADO[clave];
    if (est && v0) {
      const fila = (est.historial || []).find(x => Number(x.id) === Number(id));
      if (fila && !fila.inv) {
        fila.inv = {p: v0.p_base_jugador, p_rival: v0.p_base_jugador == null ? null : 1 - v0.p_base_jugador, version: v0.version,
                    momento: v0.momento, apoya: v0.apoya, conclusion: v0.conclusion, reservas: v0.reservas || [],
                    solo_utr: String(v0.metodo_base || '').startsWith('Probabilidad UTR'), recalculando: false,
                    contexto: v0.contexto ? {icono: (v0.contexto.clase || {}).icono, texto: (v0.contexto.clase || {}).texto,
                                             areas: (v0.contexto.areas || []).filter(a => a.estado === '⚠️' || a.estado === '🟡').map(a => `${a.icono} ${a.texto}`)} : null};
        _deHistorial(clave);
      }
    }
  } catch (e) { document.getElementById('ei-modal-cuerpo').innerHTML = `<span class="warn">${escaparHtml(e.message || e)}</span>`; }
}
