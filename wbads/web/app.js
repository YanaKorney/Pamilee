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
  renderFunnel(r);
  renderConvCharts(r);
  renderCharts(r);
  renderCampHeader();
  renderFilters(r);
  renderCampaigns(r);
  renderFoot(r);
}

/* ── воронка и конверсии ──────────────────────────────────────────────── */

/* Порог для каждого шага воронки берётся из настроек: ниже него шаг
   считается проблемным, заметно выше — сильным. */
function convClass(value, floor) {
  if (!floor || !isFinite(value)) return '';
  if (value < floor) return 'bad';
  if (value >= floor * 2) return 'good';
  return '';
}

function floors(r) {
  return {
    ctr: r.thresholds.ctr_floor,
    cr_cart: r.thresholds.cr_cart_floor,
    cr_order: r.thresholds.cr_order_floor,
  };
}

const FUNNEL_STEPS = [
  { key: 'ctr', label: 'CTR', hint: 'показ → клик' },
  { key: 'cr_cart', label: 'В корзину', hint: 'клик → корзина' },
  { key: 'cr_order', label: 'В заказ', hint: 'корзина → заказ' },
];

function renderFunnel(r) {
  const t = r.totals, c = r.compare, f = floors(r);
  const stages = [
    { key: 'views', label: 'Показы' },
    { key: 'clicks', label: 'Клики' },
    { key: 'atbs', label: 'В корзине' },
    { key: 'orders', label: 'Заказы' },
  ];

  const host = document.getElementById('funnel');
  host.innerHTML = '';

  stages.forEach((stage, i) => {
    const box = el('div', 'stage');
    const d = c[stage.key].delta_pct;
    box.innerHTML =
      `<div class="k">${stage.label}</div>` +
      `<div class="v">${num(t[stage.key])}</div>` +
      `<div class="d">${signed(d)} к прошлому периоду</div>`;
    host.appendChild(box);

    const step = FUNNEL_STEPS[i];
    if (!step) return;
    const value = t[step.key];
    const cls = convClass(value, f[step.key]);
    const conv = el('div', 'step');
    conv.innerHTML =
      `<div class="arrow">→</div>` +
      `<div class="k">${step.hint}</div>` +
      `<div class="v ${cls}">${pct(value, 2)}</div>` +
      `<div class="d">было ${pct(c[step.key].previous, 2)} · ${signed(c[step.key].delta_pct)}</div>`;
    host.appendChild(conv);
  });

  const note = document.getElementById('funnel-note');
  const parts = [
    `Сквозная конверсия клик → заказ: <b>${pct(t.cr_click_order, 2)}</b>`,
    `Показ → заказ: <b>${pct(safeDiv(t.orders, t.views) * 100, 3)}</b>`,
    `Средний чек: <b>${money(t.aov)}</b>`,
  ];

  /* Средняя по кабинету прячет провалы: воронка целиком может быть в норме,
     пока отдельные кампании валятся. Поэтому считаем не только общий
     показатель, но и сколько кампаний не дотягивает на каждом шаге. */
  const active = r.campaigns.filter((c) => c.metrics.clicks >= 50);
  const weak = FUNNEL_STEPS
    .map((step) => ({
      step,
      count: active.filter((c) => c.metrics[step.key] < f[step.key]).length,
    }))
    .filter((x) => x.count > 0)
    .sort((a, b) => b.count - a.count);

  if (weak.length) {
    parts.push(weak.map((w) =>
      `<b class="warn">${w.count}</b> ${plural(w.count, 'кампания', 'кампании', 'кампаний')} ` +
      `ниже порога на шаге «${w.step.hint}»`).join(' · '));
  } else if (active.length) {
    parts.push('Ни одна кампания не проваливает пороги воронки');
  }
  note.innerHTML = parts.map((x) => `<span>${x}</span>`).join('');
}

const safeDiv = (a, b) => (b ? a / b : 0);

function renderConvCharts(r) {
  const host = document.getElementById('conv-charts');
  host.innerHTML = '';
  const dates = r.series.map((p) => p.date);
  const f = floors(r);
  const color = cssVar('--series-1');

  FUNNEL_STEPS.forEach((step) => {
    const box = el('div');
    box.appendChild(el('div', 'title', `${step.label} · ${pct(r.totals[step.key], 2)}`));
    box.appendChild(el('div', 'sub', step.hint));
    const chartHost = el('div', 'chart-host');
    box.appendChild(chartHost);
    host.appendChild(box);

    const values = r.series.map((p) => p[step.key]);
    const digits = Math.max(...values, f[step.key]) < 10 ? 1 : 0;

    requestAnimationFrame(() => {
      lineChart(chartHost, {
        dates, height: 165,
        series: [{ name: step.label, values, color, fill: true }],
        format: (v) => pct(v, 2),
        axisFormat: (v) => v.toFixed(digits).replace('.', ',') + '%',
        target: { value: f[step.key], label: `порог ${pct(f[step.key], 1)}` },
        ariaLabel: `${step.label} по дням, ${step.hint}`,
      });
    });
  });
}

/* ── шапка таблицы кампаний ───────────────────────────────────────────── */

const CAMP_COLUMNS = ['Кампания', 'Расход по дням', 'Показы', 'CTR', 'Клики',
                      '→ корзина', '→ заказ', 'Заказы', 'Расход', 'Выручка', 'ДРР', ''];

function renderCampHeader() {
  const host = document.getElementById('camp-header');
  host.innerHTML = CAMP_COLUMNS.map((label) => `<div>${label}</div>`).join('');
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
    /* Статус показываем, только когда он необычный: «идут показы» стоит
       почти у всех и лишь засоряет строку. */
    (c.status === 9 ? '' : `<span class="badge">${escapeHtml(c.status_name)}</span>`);
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
  const f = floors(r);
  const delta = (key) => (c.compare ? c.compare[key].delta_pct : null);

  head.insertAdjacentHTML('beforeend',
    sparkline(c.series.map((p) => p.spend), cssVar('--series-2')) +
    cell(num(m.views), '', delta('views')) +
    cell(pct(m.ctr, 2), convClass(m.ctr, f.ctr), delta('ctr')) +
    cell(num(m.clicks), '', delta('clicks')) +
    cell(pct(m.cr_cart, 1), convClass(m.cr_cart, f.cr_cart), delta('cr_cart')) +
    cell(pct(m.cr_order, 1), convClass(m.cr_order, f.cr_order), delta('cr_order')) +
    cell(num(m.orders), '', delta('orders')) +
    cell(money(m.spend)) +
    cell(money(m.revenue)) +
    cell(drrText(m), drrBad ? 'bad' : (drrGood ? 'good' : ''),
         m.revenue > 0 ? delta('drr') : null) +
    `<span class="chev">${state.open.has(c.advert_id) ? '▲' : '▼'}</span>`);

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

/* Ячейка таблицы: значение и, если есть с чем сравнить, изменение к прошлому периоду. */
function cell(value, cls = '', deltaPct = null) {
  const change = (deltaPct === null || deltaPct === undefined || !isFinite(deltaPct))
    ? '' : `<span class="was">${signed(deltaPct)}</span>`;
  return `<div class="cell ${cls}">${value}${change}</div>`;
}

function fillBody(body, c, r) {
  const m = c.metrics;

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

  /* воронка кампании: где именно теряются люди */
  body.appendChild(el('div', 'subhead', 'Воронка кампании'));
  const f = floors(r);
  const cm = c.compare || {};
  const funnel = el('div', 'funnel');
  const stages = [['views', 'Показы'], ['clicks', 'Клики'],
                  ['atbs', 'В корзине'], ['orders', 'Заказы']];
  stages.forEach(([key, label], i) => {
    const box = el('div', 'stage');
    box.innerHTML = `<div class="k">${label}</div><div class="v">${num(m[key])}</div>` +
      (cm[key] ? `<div class="d">${signed(cm[key].delta_pct)}</div>` : '');
    funnel.appendChild(box);
    const step = FUNNEL_STEPS[i];
    if (!step) return;
    const conv = el('div', 'step');
    conv.innerHTML =
      `<div class="arrow">→</div><div class="k">${step.hint}</div>` +
      `<div class="v ${convClass(m[step.key], f[step.key])}">${pct(m[step.key], 2)}</div>` +
      (cm[step.key]
        ? `<div class="d">было ${pct(cm[step.key].previous, 2)}</div>`
        : `<div class="d">порог ${pct(f[step.key], 1)}</div>`);
    funnel.appendChild(conv);
  });
  body.appendChild(funnel);

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

  /* каждая конверсия — своё полотно: масштабы у них несопоставимы */
  const convHosts = FUNNEL_STEPS.map((step) => {
    const box = el('div');
    box.appendChild(el('div', 'legend',
      `<span><i class="swatch" style="background:${c1}"></i> ${step.label}</span>` +
      `<span><i class="swatch" style="background:${cssVar('--border-strong')}"></i> Порог</span>`));
    const host = el('div', 'chart-host');
    box.appendChild(host);
    charts.appendChild(box);
    return { step, host };
  });

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
    convHosts.forEach(({ step, host }) => {
      lineChart(host, {
        dates, height: 170,
        series: [{ name: step.label, values: c.series.map((p) => p[step.key]),
                   color: c1, fill: true }],
        format: (v) => pct(v, 2),
        axisFormat: (v) => v.toFixed(
          Math.max(...c.series.map((q) => q[step.key]), f[step.key]) < 10 ? 1 : 0
        ).replace('.', ',') + '%',
        target: { value: f[step.key], label: 'порог' },
        ariaLabel: `${step.label} кампании по дням`,
      });
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
      '<th>В корзине</th><th>→ корзина</th><th>Заказы</th><th>→ заказ</th>' +
      '<th>Цена клика</th><th>Расход</th><th>Выручка</th><th>ДРР</th></tr></thead>';
    const tbody = el('tbody');
    c.nm_items.forEach((it) => {
      const tr = el('tr', it.flag);
      tr.innerHTML =
        `<td>${escapeHtml(it.name)}<br><span style="color:var(--text-muted);font-size:11px">${it.nm_id}</span></td>` +
        `<td>${num(it.views)}</td><td>${num(it.clicks)}</td>` +
        `<td class="${convClass(it.ctr, f.ctr)}">${pct(it.ctr, 2)}</td>` +
        `<td>${num(it.atbs)}</td>` +
        `<td class="${convClass(it.cr_cart, f.cr_cart)}">${pct(it.cr_cart, 1)}</td>` +
        `<td>${num(it.orders)}</td>` +
        `<td class="${convClass(it.cr_order, f.cr_order)}">${pct(it.cr_order, 1)}</td>` +
        `<td>${money(it.cpc, 2)}</td><td>${money(it.spend)}</td>` +
        `<td>${money(it.revenue)}</td><td>${drrText(it)}</td>`;
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

/* Русское склонение: 1 кампания, 2 кампании, 5 кампаний. */
function plural(n, one, few, many) {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

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
