/* Дашборд аналитики рекламы WB.
   Графики рисуются инлайновым SVG — без внешних библиотек, чтобы дашборд
   работал офлайн и не тянул ничего из интернета. */

'use strict';

const state = {
  days: 7,
  from: null,
  to: null,
  filter: 'all',
  report: null,
  open: new Set(),
};

/* ── форматирование ───────────────────────────────────────────────────── */

const nf = new Intl.NumberFormat('ru-RU');

const money = (v, digits = 0) =>
  new Intl.NumberFormat('ru-RU', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  }).format(v || 0) + ' ₽';

const num = (v) => nf.format(Math.round(v || 0));
const pct = (v, d = 1) => (v || 0).toFixed(d).replace('.', ',') + '%';

/* ДРР без выручки не считается — показываем прочерк, а не ноль */
const drrText = (m) => (m.revenue > 0 ? pct(m.drr) : '—');

const signed = (v) => {
  if (v === null || v === undefined || !isFinite(v)) return '—';
  return (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(0) + '%';
};

const dayLabel = (iso) => {
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' });
};

const el = (tag, className, html) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (html !== undefined) node.innerHTML = html;
  return node;
};

const cssVar = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/* ── графики ──────────────────────────────────────────────────────────── */

const SVG_NS = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs = {}) => {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
};

/**
 * Линейный график с общей осью Y.
 * Все серии обязаны быть в одних единицах — двух шкал на одном полотне не бывает:
 * это самый частый способ соврать графиком.
 *
 * @param {HTMLElement} host   контейнер
 * @param {object} spec
 *   dates       — подписи оси X (ISO)
 *   series      — [{ name, values, color, fill }]
 *   format      — функция форматирования значения
 *   height      — высота полотна
 *   target      — { value, label } горизонтальная линия-ориентир
 */
function lineChart(host, spec) {
  host.innerHTML = '';
  const width = host.clientWidth || 520;
  const height = spec.height || 210;
  const pad = { top: 12, right: 16, bottom: 26, left: 54 };
  const plotW = Math.max(10, width - pad.left - pad.right);
  const plotH = Math.max(10, height - pad.top - pad.bottom);
  const n = spec.dates.length;

  const all = spec.series.flatMap((s) => s.values);
  if (spec.target) all.push(spec.target.value);
  let max = Math.max(...all, 0);
  if (max <= 0) max = 1;
  max *= 1.1;

  const x = (i) => pad.left + (n <= 1 ? plotW / 2 : (i * plotW) / (n - 1));
  const y = (v) => pad.top + plotH - (Math.max(0, v) / max) * plotH;

  const svg = svgEl('svg', {
    viewBox: `0 0 ${width} ${height}`, height,
    role: 'img', 'aria-label': spec.ariaLabel || 'График динамики',
  });

  /* сетка и подписи оси Y — намеренно неброские */
  const ticks = 4;
  for (let t = 0; t <= ticks; t++) {
    const value = (max / ticks) * t;
    const yy = y(value);
    svg.appendChild(svgEl('line', {
      x1: pad.left, x2: width - pad.right, y1: yy, y2: yy,
      stroke: cssVar('--grid'), 'stroke-width': 1,
    }));
    const label = svgEl('text', {
      x: pad.left - 8, y: yy + 4, 'text-anchor': 'end',
      fill: cssVar('--text-muted'), 'font-size': 11,
    });
    label.textContent = spec.axisFormat ? spec.axisFormat(value) : num(value);
    svg.appendChild(label);
  }

  /* подписи оси X — не чаще, чем помещается */
  const step = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(plotW / 62))));
  spec.dates.forEach((d, i) => {
    if (i % step !== 0 && i !== n - 1) return;
    const label = svgEl('text', {
      x: x(i), y: height - 8, 'text-anchor': 'middle',
      fill: cssVar('--text-muted'), 'font-size': 11,
    });
    label.textContent = dayLabel(d);
    svg.appendChild(label);
  });

  /* линия-ориентир (цель) — сплошная и приглушённая, с прямой подписью */
  if (spec.target) {
    const ty = y(spec.target.value);
    svg.appendChild(svgEl('line', {
      x1: pad.left, x2: width - pad.right, y1: ty, y2: ty,
      stroke: cssVar('--border-strong'), 'stroke-width': 1.5,
    }));
    const label = svgEl('text', {
      x: width - pad.right, y: ty - 6, 'text-anchor': 'end',
      fill: cssVar('--text-secondary'), 'font-size': 11, 'font-weight': 600,
    });
    label.textContent = spec.target.label;
    svg.appendChild(label);
  }

  /* серии */
  spec.series.forEach((s) => {
    const points = s.values.map((v, i) => [x(i), y(v)]);
    const path = points.map((p, i) => `${i ? 'L' : 'M'}${p[0]},${p[1]}`).join(' ');

    if (s.fill) {
      const area = `${path} L${x(n - 1)},${y(0)} L${x(0)},${y(0)} Z`;
      svg.appendChild(svgEl('path', { d: area, fill: s.color, opacity: 0.09 }));
    }
    svg.appendChild(svgEl('path', {
      d: path, fill: 'none', stroke: s.color, 'stroke-width': 2,
      'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    }));
  });

  /* слой наведения: вертикаль + точки + подсказка.
     Ловим движение по всему полотну, а не по самим точкам — попасть проще. */
  const cursor = svgEl('line', {
    y1: pad.top, y2: pad.top + plotH, stroke: cssVar('--border-strong'),
    'stroke-width': 1, opacity: 0,
  });
  svg.appendChild(cursor);

  const markers = spec.series.map((s) => {
    const dot = svgEl('circle', {
      r: 4.5, fill: s.color, stroke: cssVar('--surface-1'),
      'stroke-width': 2, opacity: 0,
    });
    svg.appendChild(dot);
    return dot;
  });

  const tip = el('div', 'tooltip');
  host.appendChild(tip);

  const overlay = svgEl('rect', {
    x: pad.left, y: pad.top, width: plotW, height: plotH,
    fill: 'transparent', style: 'cursor:crosshair',
  });
  svg.appendChild(overlay);

  const show = (event) => {
    const box = svg.getBoundingClientRect();
    const scale = width / box.width;
    const px = (event.clientX - box.left) * scale;
    const i = n <= 1 ? 0 : Math.round(((px - pad.left) / plotW) * (n - 1));
    const idx = Math.max(0, Math.min(n - 1, i));

    cursor.setAttribute('x1', x(idx));
    cursor.setAttribute('x2', x(idx));
    cursor.setAttribute('opacity', 1);

    markers.forEach((dot, k) => {
      dot.setAttribute('cx', x(idx));
      dot.setAttribute('cy', y(spec.series[k].values[idx]));
      dot.setAttribute('opacity', 1);
    });

    tip.innerHTML =
      `<div class="t-date">${dayLabel(spec.dates[idx])}</div>` +
      spec.series.map((s) =>
        `<div class="t-row"><span><span class="swatch dot" style="background:${s.color}"></span> ${s.name}</span>` +
        `<b>${spec.format(s.values[idx])}</b></div>`).join('');
    tip.style.opacity = 1;

    const left = (x(idx) / scale) + 14;
    const flip = left + tip.offsetWidth > box.width;
    tip.style.left = (flip ? (x(idx) / scale) - tip.offsetWidth - 14 : left) + 'px';
    tip.style.top = '8px';
  };

  const hide = () => {
    tip.style.opacity = 0;
    cursor.setAttribute('opacity', 0);
    markers.forEach((d) => d.setAttribute('opacity', 0));
  };

  svg.addEventListener('mousemove', show);
  svg.addEventListener('mouseleave', hide);
  svg.addEventListener('touchmove', (e) => { show(e.touches[0]); }, { passive: true });

  host.appendChild(svg);
}

/** Спарклайн: одна серия, без осей — только форма тренда. */
function sparkline(values, color) {
  const w = 92, h = 30, pad = 3;
  const max = Math.max(...values, 1);
  const n = values.length;
  const x = (i) => pad + (n <= 1 ? (w - pad * 2) / 2 : (i * (w - pad * 2)) / (n - 1));
  const y = (v) => h - pad - (Math.max(0, v) / max) * (h - pad * 2);
  const d = values.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" aria-hidden="true">
    <path d="${d} L${x(n - 1)},${h - pad} L${x(0)},${h - pad} Z" fill="${color}" opacity=".10"/>
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.6"
          stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

/* ── загрузка данных ──────────────────────────────────────────────────── */

function query() {
  const p = new URLSearchParams();
  if (state.from && state.to) { p.set('from', state.from); p.set('to', state.to); }
  else p.set('days', state.days);
  return p.toString();
}

async function load() {
  try {
    const res = await fetch('/api/report?' + query());
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    state.report = data;
    document.getElementById('error').hidden = true;
    render();
  } catch (err) {
    const box = document.getElementById('error');
    box.hidden = false;
    box.textContent = 'Не удалось загрузить данные: ' + err.message;
  }
}

/* ── отрисовка ────────────────────────────────────────────────────────── */

function render() {
  const r = state.report;
  if (!r) return;

  const p = r.period;
  document.getElementById('subtitle').textContent =
    `${dayLabel(p.from)} — ${dayLabel(p.to)} · ${p.days} дн. · сравнение с ` +
    `${dayLabel(p.prev_from)} — ${dayLabel(p.prev_to)}`;
  document.getElementById('date-from').value = p.from;
  document.getElementById('date-to').value = p.to;
  document.getElementById('target-drr').value = r.thresholds.target_drr;
  document.getElementById('export').href = '/api/export.csv?' + query();

  renderTodo(r);
  renderKpis(r);
  renderCharts(r);
  renderFilters(r);
  renderCampaigns(r);
  renderFoot(r);
}

function renderTodo(r) {
  const card = document.getElementById('todo-card');
  const list = document.getElementById('todo');
  const items = r.summary.top_actions || [];
  card.hidden = items.length === 0;
  list.innerHTML = '';
  items.forEach((a, i) => {
    const li = el('li', a.severity);
    li.appendChild(el('span', 'n', String(i + 1)));
    const body = el('div');
    body.appendChild(el('div', 'who', `${a.campaign} — ${a.title}`));
    body.appendChild(el('div', 'what', a.action));
    li.appendChild(body);
    list.appendChild(li);
  });
}

function renderKpis(r) {
  const t = r.totals, c = r.compare, s = r.summary;
  const target = r.thresholds.target_drr;

  /* Для расхода рост — это не «хорошо» и не «плохо» само по себе,
     поэтому цветом отмечаем только те показатели, где направление однозначно. */
  const tiles = [
    { label: 'Расход', value: money(t.spend), delta: c.spend.delta_pct, good: null },
    { label: 'Выручка с рекламы', value: money(t.revenue), delta: c.revenue.delta_pct, good: 'up' },
    {
      label: 'ДРР', value: t.revenue > 0 ? pct(t.drr) : '—',
      delta: c.drr.delta_pct, good: 'down',
      note: `цель ${pct(target, 0)}`, flagged: t.revenue > 0 && t.drr > target,
    },
    { label: 'Заказы', value: num(t.orders), delta: c.orders.delta_pct, good: 'up' },
    { label: 'Цена заказа', value: t.orders > 0 ? money(t.cpo) : '—', delta: c.cpo.delta_pct, good: 'down' },
    { label: 'ROAS', value: t.spend > 0 ? t.roas.toFixed(1) : '—', delta: c.roas.delta_pct, good: 'up' },
  ];

  const host = document.getElementById('kpis');
  host.innerHTML = '';
  tiles.forEach((tile) => {
    const node = el('div', 'kpi' + (tile.flagged ? ' flagged' : ''));
    node.appendChild(el('div', 'label', tile.label));
    node.appendChild(el('div', 'value', tile.value));

    let cls = '', arrow = '';
    if (tile.delta !== null && tile.delta !== undefined && isFinite(tile.delta)) {
      arrow = tile.delta >= 0 ? '▲' : '▼';
      if (tile.good === 'up') cls = tile.delta >= 0 ? 'up' : 'down';
      if (tile.good === 'down') cls = tile.delta <= 0 ? 'up' : 'down';
    }
    node.appendChild(el('div', 'delta',
      `<span class="arrow ${cls}">${arrow}</span><span class="${cls}">${signed(tile.delta)}</span>` +
      `<span class="target">${tile.note ? '· ' + tile.note : 'к прошлому периоду'}</span>`));
    host.appendChild(node);
  });

  /* Деньги мимо цели — отдельная плитка-предупреждение */
  if (r.summary.money_at_risk > 0) {
    const node = el('div', 'kpi flagged');
    node.appendChild(el('div', 'label', 'Уходит мимо цели'));
    node.appendChild(el('div', 'value', money(s.money_at_risk)));
    node.appendChild(el('div', 'delta',
      `<span class="target">расход сверх цели по проблемным кампаниям</span>`));
    host.appendChild(node);
  }
}

function renderCharts(r) {
  const dates = r.series.map((p) => p.date);
  const c1 = cssVar('--series-1'), c2 = cssVar('--series-2');

  document.getElementById('legend-money').innerHTML =
    `<span><i class="swatch" style="background:${c1}"></i> Выручка с рекламы</span>` +
    `<span><i class="swatch" style="background:${c2}"></i> Расход</span>`;

  lineChart(document.getElementById('chart-money'), {
    dates,
    series: [
      { name: 'Выручка', values: r.series.map((p) => p.revenue), color: c1, fill: true },
      { name: 'Расход', values: r.series.map((p) => p.spend), color: c2, fill: true },
    ],
    format: (v) => money(v),
    axisFormat: (v) => (v >= 1000 ? Math.round(v / 1000) + 'к' : num(v)),
    ariaLabel: 'Расход и выручка с рекламы по дням, рубли',
  });

  document.getElementById('legend-drr').innerHTML =
    `<span><i class="swatch" style="background:${c1}"></i> ДРР по дням</span>` +
    `<span><i class="swatch" style="background:${cssVar('--border-strong')}"></i> Цель</span>`;

  lineChart(document.getElementById('chart-drr'), {
    dates,
    series: [{
      name: 'ДРР', color: c1, fill: true,
      values: r.series.map((p) => (p.revenue > 0 ? p.drr : 0)),
    }],
    format: (v) => pct(v),
    axisFormat: (v) => v.toFixed(0) + '%',
    target: { value: r.thresholds.target_drr, label: `цель ${pct(r.thresholds.target_drr, 0)}` },
    ariaLabel: 'Доля рекламных расходов по дням, проценты',
  });
}

const FILTERS = [
  { key: 'all', label: 'Все' },
  { key: 'critical', label: '🔴 Требуют действий' },
  { key: 'warning', label: '🟡 Под наблюдением' },
  { key: 'opportunity', label: '🚀 Масштабировать' },
  { key: 'ok', label: '🟢 Норма' },
];

function renderFilters(r) {
  const counts = {
    all: r.campaigns.length,
    critical: r.summary.critical,
    warning: r.summary.warning,
    opportunity: r.summary.opportunity,
    ok: r.summary.healthy,
  };
  const host = document.getElementById('filters');
  host.innerHTML = '';
  FILTERS.forEach((f) => {
    const b = el('button', 'btn', `${f.label} · ${counts[f.key] ?? 0}`);
    b.setAttribute('aria-pressed', String(state.filter === f.key));
    if (state.filter === f.key) b.style.borderColor = cssVar('--text-primary');
    b.onclick = () => { state.filter = f.key; renderFilters(r); renderCampaigns(r); };
    host.appendChild(b);
  });
}

function renderCampaigns(r) {
  const host = document.getElementById('campaigns');
  host.innerHTML = '';
  const list = r.campaigns.filter((c) => state.filter === 'all' || c.verdict === state.filter);

  if (!list.length) {
    host.appendChild(el('div', 'empty', 'В этой группе кампаний нет.'));
    return;
  }
  list.forEach((c) => host.appendChild(campaignCard(c, r)));
}

function campaignCard(c, r) {
  const m = c.metrics;
  const target = r.thresholds.target_drr;
  const card = el('div', 'camp ' + c.verdict + (state.open.has(c.advert_id) ? ' open' : ''));

  const head = el('div', 'camp-head');
  head.setAttribute('role', 'button');
  head.setAttribute('tabindex', '0');

  const titleBox = el('div');
  const title = el('div', 'camp-title');
  title.innerHTML =
    `<span class="name">${c.verdict_icon} ${escapeHtml(c.name)}</span>` +
    `<span class="badge ${c.verdict}">${c.verdict_label}</span>` +
    `<span class="badge">${escapeHtml(c.type_name)}</span>` +
    `<span class="badge">${escapeHtml(c.status_name)}</span>`;
  titleBox.appendChild(title);

  /* Что именно не так — видно, не раскрывая карточку */
  if (c.findings.length) {
    const names = c.findings.slice(0, 3).map((f) => escapeHtml(f.title));
    const rest = c.findings.length - names.length;
    titleBox.appendChild(el('div', 'camp-issues',
      names.join(' · ') + (rest > 0 ? ` · ещё ${rest}` : '')));
  }
  head.appendChild(titleBox);

  const drrBad = m.revenue > 0 && m.drr > target;
  const drrGood = m.revenue > 0 && m.drr > 0 && m.drr <= target;
  const metrics = el('div', 'camp-metrics');
  metrics.innerHTML =
    sparkline(c.series.map((p) => p.spend), cssVar('--series-2')) +
    metric('Расход', money(m.spend)) +
    metric('Выручка', money(m.revenue)) +
    metric('ДРР', drrText(m), drrBad ? 'bad' : (drrGood ? 'good' : '')) +
    metric('Заказы', num(m.orders)) +
    `<span class="chev">${state.open.has(c.advert_id) ? '▲' : '▼'}</span>`;
  head.appendChild(metrics);

  const body = el('div', 'camp-body');
  const toggle = () => {
    if (state.open.has(c.advert_id)) state.open.delete(c.advert_id);
    else { state.open.add(c.advert_id); }
    renderCampaigns(r);
  };
  head.onclick = toggle;
  head.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } };

  card.appendChild(head);
  card.appendChild(body);

  if (state.open.has(c.advert_id)) fillBody(body, c, r);
  return card;
}

function metric(key, value, cls = '') {
  return `<div class="m"><div class="k">${key}</div><div class="v ${cls}">${value}</div></div>`;
}

function fillBody(body, c, r) {
  /* находки с рекомендациями */
  if (c.findings.length) {
    c.findings.forEach((f) => {
      const node = el('div', 'finding');
      node.appendChild(el('div', 'f-head',
        `<span class="dot ${f.severity}"></span>${escapeHtml(f.title)}`));
      node.appendChild(el('div', 'f-why', escapeHtml(f.why)));
      if (f.actions.length) {
        const ul = el('ul');
        f.actions.forEach((a) => ul.appendChild(el('li', '', escapeHtml(a))));
        node.appendChild(ul);
      }
      body.appendChild(node);
    });
  } else {
    body.appendChild(el('div', 'finding', 'Проблем не найдено — кампания работает в пределах ваших порогов.'));
  }

  /* графики кампании */
  body.appendChild(el('div', 'subhead', 'Динамика кампании'));
  const charts = el('div', 'mini-charts');
  body.appendChild(charts);

  const dates = c.series.map((p) => p.date);
  const c1 = cssVar('--series-1'), c2 = cssVar('--series-2');

  const money_ = el('div');
  money_.appendChild(el('div', 'legend', ''));
  money_.querySelector('.legend').innerHTML =
    `<span><i class="swatch" style="background:${c1}"></i> Выручка</span>` +
    `<span><i class="swatch" style="background:${c2}"></i> Расход</span>`;
  const moneyHost = el('div', 'chart-host');
  money_.appendChild(moneyHost);
  charts.appendChild(money_);

  const drrBox = el('div');
  drrBox.appendChild(el('div', 'legend',
    `<span><i class="swatch" style="background:${c1}"></i> ДРР</span>` +
    `<span><i class="swatch" style="background:${cssVar('--border-strong')}"></i> Цель</span>`));
  const drrHost = el('div', 'chart-host');
  drrBox.appendChild(drrHost);
  charts.appendChild(drrBox);

  const cpcBox = el('div');
  cpcBox.appendChild(el('div', 'legend',
    `<span><i class="swatch" style="background:${c2}"></i> Цена клика</span>`));
  const cpcHost = el('div', 'chart-host');
  cpcBox.appendChild(cpcHost);
  charts.appendChild(cpcBox);

  /* размеры контейнеров известны только после вставки в документ */
  requestAnimationFrame(() => {
    lineChart(moneyHost, {
      dates, height: 170,
      series: [
        { name: 'Выручка', values: c.series.map((p) => p.revenue), color: c1, fill: true },
        { name: 'Расход', values: c.series.map((p) => p.spend), color: c2, fill: true },
      ],
      format: (v) => money(v),
      axisFormat: (v) => (v >= 1000 ? Math.round(v / 1000) + 'к' : num(v)),
      ariaLabel: 'Расход и выручка кампании по дням',
    });
    lineChart(drrHost, {
      dates, height: 170,
      series: [{
        name: 'ДРР', color: c1, fill: true,
        values: c.series.map((p) => (p.revenue > 0 ? p.drr : 0)),
      }],
      format: (v) => pct(v),
      axisFormat: (v) => v.toFixed(0) + '%',
      target: { value: r.thresholds.target_drr, label: 'цель' },
      ariaLabel: 'ДРР кампании по дням',
    });
    lineChart(cpcHost, {
      dates, height: 170,
      series: [{ name: 'Цена клика', values: c.series.map((p) => p.cpc), color: c2, fill: true }],
      format: (v) => money(v, 2),
      axisFormat: (v) => v.toFixed(0) + '₽',
      ariaLabel: 'Цена клика по дням',
    });
  });

  /* артикулы внутри кампании */
  if (c.nm_items && c.nm_items.length) {
    body.appendChild(el('div', 'subhead', 'Артикулы внутри кампании'));
    body.appendChild(el('p', 'hint',
      'Цветная полоса слева: красная — тянет вниз, жёлтая — выше цели, зелёная — лучше цели.'));
    const table = el('table', 'data');
    table.innerHTML =
      '<thead><tr><th>Артикул</th><th>Показы</th><th>Клики</th><th>CTR</th>' +
      '<th>Цена клика</th><th>В корзину</th><th>Заказы</th><th>Расход</th>' +
      '<th>Выручка</th><th>ДРР</th></tr></thead>';
    const tbody = el('tbody');
    c.nm_items.forEach((it) => {
      const tr = el('tr', it.flag);
      tr.innerHTML =
        `<td>${escapeHtml(it.name)}<br><span style="color:var(--text-muted);font-size:11px">${it.nm_id}</span></td>` +
        `<td>${num(it.views)}</td><td>${num(it.clicks)}</td><td>${pct(it.ctr, 2)}</td>` +
        `<td>${money(it.cpc, 2)}</td><td>${num(it.atbs)}</td><td>${num(it.orders)}</td>` +
        `<td>${money(it.spend)}</td><td>${money(it.revenue)}</td><td>${drrText(it)}</td>`;
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    body.appendChild(table);
  }
}

function renderFoot(r) {
  const meta = r.meta;
  const parts = [];
  if (meta.data_from) parts.push(`данные в базе: ${dayLabel(meta.data_from)} — ${dayLabel(meta.data_to)}`);
  if (meta.last_collect) {
    const lc = meta.last_collect;
    parts.push(`последний сбор: ${(lc.finished_at || lc.started_at || '').replace('T', ' ').slice(0, 16)}` +
               (lc.source === 'demo' ? ' (демо-данные)' : ''));
  }
  if (meta.balance && meta.balance.net) parts.push(`баланс кабинета: ${money(meta.balance.net)}`);
  document.getElementById('foot').textContent = parts.join(' · ');
}

/* ── утилиты ──────────────────────────────────────────────────────────── */

function escapeHtml(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}


/* ── события ──────────────────────────────────────────────────────────── */

document.getElementById('period-seg').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-days]');
  if (!btn) return;
  state.days = Number(btn.dataset.days);
  state.from = state.to = null;
  document.querySelectorAll('#period-seg button').forEach((b) =>
    b.setAttribute('aria-pressed', String(b === btn)));
  load();
});

['date-from', 'date-to'].forEach((id) => {
  document.getElementById(id).addEventListener('change', () => {
    const from = document.getElementById('date-from').value;
    const to = document.getElementById('date-to').value;
    if (from && to) {
      state.from = from; state.to = to;
      document.querySelectorAll('#period-seg button').forEach((b) =>
        b.setAttribute('aria-pressed', 'false'));
      load();
    }
  });
});

let targetTimer = null;
document.getElementById('target-drr').addEventListener('input', (e) => {
  const value = Number(e.target.value);
  if (!value || value <= 0) return;
  clearTimeout(targetTimer);
  targetTimer = setTimeout(async () => {
    await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_drr: value }),
    });
    load();
  }, 500);
});

document.getElementById('theme').addEventListener('click', () => {
  const root = document.documentElement;
  const dark = root.getAttribute('data-theme') === 'dark' ||
    (!root.getAttribute('data-theme') &&
      window.matchMedia('(prefers-color-scheme: dark)').matches);
  root.setAttribute('data-theme', dark ? 'light' : 'dark');
  try { localStorage.setItem('wbads-theme', dark ? 'light' : 'dark'); } catch (_) {}
  render();
});

try {
  const saved = localStorage.getItem('wbads-theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);
} catch (_) {}

/* Ссылка вида /?open=12345 сразу раскрывает нужную кампанию —
   удобно кинуть коллеге ссылку на конкретную проблему. */
const openParam = new URLSearchParams(location.search).get('open');
if (openParam) openParam.split(',').forEach((id) => state.open.add(Number(id)));

let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (state.report) render(); }, 150);
});

load();
