'use strict';
// Second brain screens: overview, assistant, related notes and suggested reminders.
// Loaded after app.js and reminders.js. It uses their globals (api, message, openNote, leaveNote, formatWait,
// renderMarkdownInto, currentNote) and never builds markup from untrusted text: only textContent and CSSOM.
(() => {
  const $ = id => document.getElementById(id);
  const NS = 'http://www.w3.org/2000/svg';
  const ASSISTANT_COST = 1;
  const VIEWS = ['overview', 'assistant'];
  const monthsShort = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
  const weekdaysShort = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
  const zoneNames = {'Europe/Moscow': 'Москва', 'Europe/Berlin': 'Берлин', 'Asia/Yekaterinburg': 'Екатеринбург',
    'Asia/Novosibirsk': 'Новосибирск', 'Asia/Vladivostok': 'Владивосток', UTC: 'UTC'};
  const kindLabels = {ask: 'Вопрос', recommend: 'Рекомендации', digest: 'Сводка'};
  const statusLabels = {queued: 'В очереди', running: 'Готовится', succeeded: 'Готово', failed: 'Не получилось'};
  const suggestionKinds = {idea: 'Идея', task: 'Задача', connection: 'Связь', habit: 'Привычка'};
  const periodNames = {7: 'неделю', 30: 'месяц', 90: '3 месяца', 365: 'год'};

  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null && text !== '') node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function svg(tag, attrs = {}, text = '') {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (text !== '') node.textContent = String(text);
    return node;
  }
  function plural(n, forms) {
    const mod10 = n % 10, mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return forms[0];
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return forms[1];
    return forms[2];
  }
  function dayLabel(iso, withWeekday = false) {
    const [y, m, d] = iso.split('-').map(Number);
    const base = `${d} ${monthsShort[m - 1]}`;
    return withWeekday ? `${base}, ${weekdaysShort[new Date(Date.UTC(y, m - 1, d)).getUTCDay()]}` : base;
  }
  function visible(card) { return !$(card).hidden && !$('workspace').hidden; }
  function failure(error, fallback = 'Не удалось выполнить запрос.') {
    return error && typeof error.message === 'string' && error.message ? error.message : fallback;
  }
  function openNoteSafely(id) {
    if (typeof openNote !== 'function') return;
    openNote(id, {userAction: true}).catch(error => message(failure(error)));
  }

  // ---- usage ----
  let usage = null;
  function costText() {
    let text = `Стоит ${ASSISTANT_COST} единицу ИИ`;
    if (!usage) return text;
    const left = usage.daily_units_remaining;
    const total = typeof usage.daily_unit_limit === 'number' && usage.daily_unit_limit > 0 ? usage.daily_unit_limit : null;
    if (typeof left === 'number') text += ` · осталось ${left}${total ? ` из ${total}` : ''}`;
    return text;
  }
  function exhausted() { return Boolean(usage) && usage.daily_units_remaining === 0; }
  function resetText() {
    const ms = usage && usage.limit_resets_at ? Date.parse(usage.limit_resets_at) - Date.now() : NaN;
    return Number.isNaN(ms) ? '' : ms > 0 ? ` Обновится через ${formatWait(ms)}.` : ' Скоро обновится.';
  }
  const running = {ask: false, recommend: false, digest: false};
  function syncButtons() {
    const off = exhausted();
    const text = off ? `Лимит ИИ на сегодня исчерпан.${resetText()}` : costText();
    for (const id of ['ask-cost', 'rec-cost', 'digest-cost']) $(id).textContent = text;
    $('ask-submit').disabled = running.ask || off;
    $('rec-run').disabled = running.recommend || off || !$('rec-switch').checked;
    $('digest-run').disabled = running.digest || off;
  }
  async function loadUsage() {
    try { usage = await api('/api/v1/provider/usage'); } catch (_) { /* the counter is a hint only */ }
    syncButtons();
  }

  // ---- routing ----
  let current = null;
  function hashName() {
    const name = location.hash.replace(/^#/, '');
    return VIEWS.includes(name) ? name : null;
  }
  function setHash(name) {
    try {
      if (name) { if (location.hash !== `#${name}`) location.hash = name; }
      else if (VIEWS.includes(location.hash.replace(/^#/, ''))) history.replaceState(null, '', location.pathname + location.search);
    } catch (_) { /* a sandboxed frame may refuse */ }
  }
  function syncNav() {
    for (const button of document.querySelectorAll('[data-brain-view]')) {
      button.setAttribute('aria-pressed', String(button.dataset.brainView === current));
    }
    $('workspace').classList.toggle('has-view', Boolean(current));
  }
  function hideViews() {
    current = null;
    for (const name of VIEWS) $(`${name}-card`).hidden = true;
    stopAll(); syncNav();
  }
  function show(name) {
    if (current === name) { refresh(name); return true; }
    if (typeof leaveNote === 'function' && !leaveNote('Есть несохранённые правки. Перейти в раздел?')) return false;
    $('capture-card').hidden = true;
    for (const view of VIEWS) $(`${view}-card`).hidden = view !== name;
    if (current && current !== name) stopAll();
    current = name; syncNav(); message();
    refresh(name);
    return true;
  }
  function refresh(name) {
    if (name === 'overview') loadOverview();
    else openAssistant();
  }
  function navigate(name) {
    if (show(name)) setHash(name);
    else setHash(current);
  }
  function route() {
    if ($('workspace').hidden) return;
    const name = hashName();
    if (name === current) return;
    if (name) { if (!show(name)) setHash(current); return; }
    if (current) {
      hideViews();
      if ($('note-card').hidden && $('source-card').hidden) $('capture-card').hidden = false;
    }
  }
  for (const button of document.querySelectorAll('[data-brain-view]')) button.onclick = () => navigate(button.dataset.brainView);
  window.addEventListener('hashchange', route);
  // Another view (a note, a source, the capture form) took the column: the screens step aside.
  new MutationObserver(() => {
    if (current && (!$('capture-card').hidden || !$('note-card').hidden || !$('source-card').hidden)) {
      hideViews(); setHash(null);
    }
  }).observe($('workspace'), {attributes: true, attributeFilter: ['hidden'], subtree: true});
  new MutationObserver(() => {
    if ($('workspace').hidden) { hideViews(); usage = null; settingsKnown = false; } else route();
  }).observe($('workspace'), {attributes: true, attributeFilter: ['hidden']});
  $('category-navigation').addEventListener('click', () => $('workspace').classList.add('library-open'));

  // ---- overview ----
  let days = 30, dashboard = null, overviewRun = 0, chartFrame = 0;
  for (const button of $('overview-period').querySelectorAll('button')) {
    button.onclick = () => {
      days = Number(button.dataset.days);
      for (const other of $('overview-period').querySelectorAll('button')) other.setAttribute('aria-pressed', String(other === button));
      loadOverview();
    };
  }
  function niceTop(max) {
    if (max <= 4) return {top: Math.max(max, 1), step: 1};
    const raw = max / 4, magnitude = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 5, 10].map(f => f * magnitude).find(value => value >= raw);
    return {top: Math.ceil(max / step) * step, step};
  }
  function drawChart(host, rows) {
    host.replaceChildren();
    const width = Math.max(host.clientWidth || 640, 260), height = 176;
    const left = 30, right = 8, top = 10, bottom = 24;
    const plotW = width - left - right, plotH = height - top - bottom;
    const values = rows.map(row => row.notes), peak = Math.max(0, ...values);
    const {top: ceiling, step} = niceTop(peak), slot = plotW / rows.length;
    const gap = slot >= 6 ? 2 : 1, barW = Math.max(1.5, slot - gap), total = values.reduce((a, b) => a + b, 0);
    const best = rows.reduce((a, b) => (b.notes > a.notes ? b : a), rows[0]);
    const summary = total
      ? `${total} ${plural(total, ['заметка', 'заметки', 'заметок'])} за ${rows.length} ${plural(rows.length, ['день', 'дня', 'дней'])}. Больше всего ${dayLabel(best.date)}, ${best.notes}.`
      : `За ${rows.length} ${plural(rows.length, ['день', 'дня', 'дней'])} заметок не было.`;
    const root = svg('svg', {viewBox: `0 0 ${width} ${height}`, width, height, role: 'img', class: 'br-chart',
      'aria-label': `Заметки по дням. ${summary}`});
    root.append(svg('title', {}, 'Заметки по дням'));
    const y = value => top + plotH - (value / ceiling) * plotH;
    for (let tick = 0; tick <= ceiling; tick += step) {
      root.append(svg('line', {x1: left, x2: width - right, y1: y(tick), y2: y(tick), class: tick ? 'br-grid' : 'br-axis'}));
      root.append(svg('text', {x: left - 6, y: y(tick) + 4, 'text-anchor': 'end', class: 'br-tick'}, tick));
    }
    const labelEvery = rows.length <= 7 ? 1 : rows.length <= 31 ? 7 : 15;
    rows.forEach((row, index) => {
      const x = left + index * slot + (slot - barW) / 2;
      const column = svg('g', {class: 'br-col', 'data-index': index});
      const label = `${dayLabel(row.date, true)}: ${row.notes} ${plural(row.notes, ['заметка', 'заметки', 'заметок'])}`;
      column.append(svg('title', {}, label));
      column.append(svg('rect', {x: left + index * slot, y: top, width: slot, height: plotH, fill: 'transparent', class: 'br-hit'}));
      const h = row.notes ? Math.max(2, (row.notes / ceiling) * plotH) : 2;
      const r = Math.min(4, barW / 2, h);
      const base = top + plotH, x2 = x + barW;
      const d = `M${x},${base} V${base - h + r} Q${x},${base - h} ${x + r},${base - h} H${x2 - r} Q${x2},${base - h} ${x2},${base - h + r} V${base} Z`;
      column.append(svg('path', {d, class: row.notes ? 'br-bar' : 'br-bar is-zero'}));
      root.append(column);
      if ((rows.length - 1 - index) % labelEvery === 0) {
        const edge = index === rows.length - 1 && labelEvery > 1;
        root.append(svg('text', {x: edge ? width - right : x + barW / 2, y: height - 6, class: 'br-tick',
          'text-anchor': edge ? 'end' : 'middle'}, labelEvery === 1 ? dayLabel(row.date, true) : dayLabel(row.date)));
      }
    });
    host.append(root);
    return {total, best, rows};
  }
  function chartBlock(rows) {
    const figure = el('figure', '', 'br-figure');
    const caption = el('figcaption', '', 'br-fig-head');
    caption.append(el('h2', 'Заметки по дням'));
    const readout = el('p', '', 'br-readout'); readout.setAttribute('aria-live', 'polite');
    caption.append(readout);
    const host = el('div', '', 'br-chart-host');
    figure.append(caption, host);
    const idle = () => {
      const {total, best} = rows.stats;
      readout.textContent = total ? `Всего ${total}. Больше всего ${dayLabel(best.date)}, ${best.notes}.` : 'За период заметок нет.';
    };
    const paint = () => { rows.stats = drawChart(host, rows.data); idle(); };
    const point = event => {
      const column = event.target.closest?.('.br-col');
      if (!column) return;
      const row = rows.data[Number(column.dataset.index)];
      readout.textContent = `${dayLabel(row.date, true)}: ${row.notes} ${plural(row.notes, ['заметка', 'заметки', 'заметок'])}`;
    };
    host.addEventListener('mouseover', point);
    host.addEventListener('click', point);
    host.addEventListener('mouseleave', idle);
    figure.paint = paint;
    const table = document.createElement('details'); table.className = 'br-table';
    table.append(el('summary', 'Показать таблицей'));
    const grid = document.createElement('table');
    const head = grid.createTHead().insertRow();
    head.append(el('th', 'День'), el('th', 'Заметок'));
    const bodyRows = grid.createTBody();
    for (const row of rows.data) { const line = bodyRows.insertRow(); line.append(el('td', dayLabel(row.date, true)), el('td', row.notes)); }
    const scroll = el('div', '', 'br-table-scroll'); scroll.append(grid); table.append(scroll);
    figure.append(table);
    return figure;
  }
  function kpi(value, label, sub) {
    const cell = el('div', '', 'br-kpi');
    cell.append(el('p', value, 'br-kpi-value'), el('p', label, 'br-kpi-label'));
    if (sub) cell.append(el('p', sub, 'br-kpi-sub'));
    return cell;
  }
  function meter(done, total, label) {
    const track = el('div', '', 'br-track');
    track.setAttribute('role', 'progressbar'); track.setAttribute('aria-label', label);
    track.setAttribute('aria-valuemin', '0'); track.setAttribute('aria-valuemax', String(total)); track.setAttribute('aria-valuenow', String(done));
    const fill = el('div', '', 'br-fill'); fill.style.width = `${total ? Math.round((done / total) * 100) : 0}%`;
    track.append(fill);
    return track;
  }
  function splitBlock(title, pairs) {
    const group = el('div', '', 'br-split');
    group.append(el('h3', title));
    const sum = pairs.reduce((a, [, n]) => a + n, 0);
    for (const [name, count] of pairs) {
      const row = el('div', '', 'br-split-row');
      const track = el('div', '', 'br-track is-thin'); track.setAttribute('aria-hidden', 'true');
      const fill = el('div', '', 'br-fill'); fill.style.width = `${sum ? Math.round((count / sum) * 100) : 0}%`;
      track.append(fill);
      row.append(el('span', name, 'br-split-name'), track, el('span', count, 'br-split-count'));
      group.append(row);
    }
    return group;
  }
  function projectsBlock(categories) {
    const section = el('section', '', 'br-projects');
    section.append(el('h2', 'Проекты'));
    if (!categories.length) {
      section.append(el('p', 'Категорий пока нет. Создайте их слева, и здесь появится прогресс по задачам.', 'muted'));
      return section;
    }
    const list = el('ul', '', 'br-project-list'), LIMIT = 8;
    categories.forEach((category, index) => {
      const item = el('li', '', 'br-project');
      if (index >= LIMIT) item.hidden = true;
      const head = el('div', '', 'br-project-head');
      head.append(el('span', category.name, 'br-project-name'),
        el('span', `${category.notes} ${plural(category.notes, ['заметка', 'заметки', 'заметок'])}`, 'br-project-notes'));
      item.append(head);
      if (category.tasks_total) {
        const line = el('div', '', 'br-project-tasks');
        const text = `Задач выполнено ${category.tasks_done} из ${category.tasks_total}`;
        line.append(meter(category.tasks_done, category.tasks_total, text), el('span', `${category.tasks_done} из ${category.tasks_total}`, 'br-project-count'));
        item.append(line);
      } else item.append(el('p', 'Задач нет', 'br-project-none'));
      list.append(item);
    });
    section.append(list);
    if (categories.length > LIMIT) {
      const more = el('button', `Показать все (${categories.length})`, 'quiet br-mini');
      more.type = 'button';
      more.onclick = () => { for (const item of list.children) item.hidden = false; more.remove(); };
      section.append(more);
    }
    return section;
  }
  function reminderLine(info) {
    const line = el('p', '', 'br-next');
    if (!info.next_at) {
      line.textContent = 'Ближайших напоминаний нет.';
      return line;
    }
    let when;
    try {
      when = new Intl.DateTimeFormat('ru-RU', {timeZone: info.next_timezone || undefined, weekday: 'short', day: 'numeric',
        month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}).format(new Date(info.next_at));
    } catch (_) { when = info.next_at; }
    const zone = info.next_timezone ? ` (${zoneNames[info.next_timezone] || info.next_timezone})` : '';
    line.append('Ближайшее напоминание: ', el('strong', `${when}${zone}`));
    if (info.upcoming_7d) line.append(el('span', ` · на неделю вперёд ${info.upcoming_7d}`, 'muted'));
    return line;
  }
  function renderOverview(data) {
    const body = $('overview-body'); body.replaceChildren();
    const blank = !data.totals.notes && !data.captures.total;
    if (blank) {
      const empty = el('div', '', 'br-empty');
      empty.append(el('h2', 'Здесь появится ваша статистика'),
        el('p', 'Запишите первую мысль, и обзор покажет, как растёт ваша база заметок: записи по дням, задачи и проекты.'));
      const start = el('button', 'Новая запись'); start.type = 'button'; start.id = 'overview-start';
      start.onclick = () => $('new-note').click();
      empty.append(start);
      body.append(empty);
      return;
    }
    const strip = el('div', '', 'br-kpis');
    const rate = data.tasks.completion_rate;
    strip.append(
      kpi(String(data.totals.notes_in_period), `${plural(data.totals.notes_in_period, ['заметка', 'заметки', 'заметок'])} за период`, `всего ${data.totals.notes}`),
      kpi(String(data.tasks.completed_in_period), `${plural(data.tasks.completed_in_period, ['задача выполнена', 'задачи выполнены', 'задач выполнено'])}`,
        rate === null ? 'задач пока нет' : `${Math.round(rate * 100)}% всех задач`),
      kpi(String(data.streak_days), `${plural(data.streak_days, ['день', 'дня', 'дней'])} подряд`, 'с записью каждый день'),
      kpi(data.ai.units_remaining === null ? 'Без лимита' : String(data.ai.units_remaining),
        data.ai.units_remaining === null ? 'ИИ сегодня' : 'ИИ сегодня, осталось',
        data.ai.units_remaining === null ? '' : `из ${data.ai.units_limit}`),
    );
    body.append(strip, reminderLine(data.reminders));
    const rows = {data: data.notes_per_day, stats: null};
    const figure = chartBlock(rows);
    body.append(figure);
    figure.paint();
    dashboard = {figure};
    const lower = el('div', '', 'br-lower');
    const source = el('section', '', 'br-source');
    source.append(el('h2', 'Откуда'));
    source.append(splitBlock('Где записано', [['Веб', data.captures.by_channel.web || 0], ['Telegram', data.captures.by_channel.telegram || 0]]));
    source.append(splitBlock('Как записано', [['Текст', data.captures.by_input_kind.text || 0], ['Голос', data.captures.by_input_kind.audio || 0]]));
    lower.append(projectsBlock(data.categories), source);
    body.append(lower);
  }
  async function loadOverview() {
    const run = ++overviewRun;
    $('overview-status').textContent = 'Загружаем…';
    try {
      const data = await api(`/api/v1/dashboard?days=${days}`);
      if (run !== overviewRun || current !== 'overview') return;
      $('overview-status').textContent = '';
      renderOverview(data);
    } catch (error) {
      if (run !== overviewRun || current !== 'overview') return;
      $('overview-status').textContent = `Не удалось загрузить обзор. ${failure(error, '')}`.trim();
    }
  }
  window.addEventListener('resize', () => {
    if (!dashboard || !visible('overview-card') || chartFrame) return;
    chartFrame = requestAnimationFrame(() => { chartFrame = 0; if (dashboard) dashboard.figure.paint(); });
  });

  // ---- assistant ----
  const state = {ask: 0, recommend: 0, digest: 0};
  const hiddenSuggestions = new Set();
  const pending = {ask: null, recommend: null, digest: null};
  const slots = {
    ask: {status: 'ask-status', result: 'ask-result'},
    recommend: {status: 'rec-status', result: 'rec-result'},
    digest: {status: 'digest-status', result: 'digest-result'},
  };
  let settingsKnown = false;
  function pollMs() { return typeof window.assistantPollMs === 'number' ? window.assistantPollMs : 2000; }
  function maxWaitMs() { return typeof window.assistantMaxWaitMs === 'number' ? window.assistantMaxWaitMs : 180000; }
  function stopAll() { for (const kind of Object.keys(state)) { state[kind]++; running[kind] = false; } if (!$('workspace').hidden) syncButtons(); }
  function say(kind, text) { $(slots[kind].status).textContent = text; }
  function noteButton(id, title, className) {
    const button = el('button', title, className); button.type = 'button';
    button.onclick = () => openNoteSafely(id);
    return button;
  }
  function citeCard(cite) {
    const card = el('button', '', 'br-cite'); card.type = 'button';
    const gone = !cite.note_title;
    card.append(el('span', gone ? 'Из удалённой заметки' : `Из заметки «${cite.note_title}»`, 'br-cite-from'),
      el('span', `«${cite.quote}»`, 'br-cite-quote'));
    if (gone) card.disabled = true; else card.onclick = () => openNoteSafely(cite.note_id);
    return card;
  }
  function citations(list, title) {
    const wrap = el('div', '', 'br-cites');
    if (title) wrap.append(el('h3', title));
    for (const cite of list) wrap.append(citeCard(cite));
    return wrap;
  }
  function renderAsk(row, host) {
    host.replaceChildren();
    if (row.question) host.append(el('p', row.question, 'br-asked'));
    const answer = el('div', '', 'br-answer');
    renderMarkdownInto(answer, row.result.answer_markdown);
    host.append(answer);
    if (row.result.citations.length) host.append(citations(row.result.citations, 'Откуда это взято'));
    const used = (row.input_note_ids || []).length;
    if (used) host.append(el('p', `Ответ собран по ${used} ${plural(used, ['заметке', 'заметкам', 'заметкам'])}.`, 'muted'));
  }
  function startNote(suggestion) {
    const text = suggestion.title ? `${suggestion.title}\n${suggestion.text}` : suggestion.text;
    $('new-note').click();
    if ($('capture-card').hidden) return;
    const field = $('thought');
    if (field.value.trim() && field.value !== text && !confirm('Заменить ваш текст предложением помощника?')) return;
    field.value = text; field.focus?.();
  }
  function renderRecommend(row, host) {
    host.replaceChildren();
    const list = row.result.suggestions;
    if (!list.length) { host.append(el('p', 'Пока нечего предложить. Добавьте больше заметок и задач, и помощник найдёт, что сделать дальше.', 'muted')); return; }
    list.forEach((suggestion, index) => {
      const mark = `${row.id}:${index}`;
      if (hiddenSuggestions.has(mark)) return;
      const card = el('article', '', `br-suggestion kind-${suggestion.kind}`);
      card.dataset.kind = suggestion.kind;
      card.append(el('span', suggestionKinds[suggestion.kind] || suggestion.kind, 'br-kind'));
      card.append(el('h3', suggestion.title), el('p', suggestion.text));
      if (suggestion.quote) card.append(el('p', `«${suggestion.quote}»`, 'br-quote'));
      if ((suggestion.notes || []).length) {
        const chips = el('div', '', 'br-chips');
        for (const note of suggestion.notes) {
          if (note.title) chips.append(noteButton(note.id, note.title, 'br-chip'));
        }
        if (chips.children.length) card.append(chips);
      }
      const actions = el('div', '', 'br-card-actions');
      const create = el('button', 'Создать заметку', 'secondary br-mini'); create.type = 'button';
      create.onclick = () => startNote(suggestion);
      const hide = el('button', 'Скрыть', 'quiet br-mini'); hide.type = 'button';
      hide.onclick = () => { hiddenSuggestions.add(mark); card.remove(); if (!host.querySelector('.br-suggestion')) host.append(el('p', 'Все предложения скрыты.', 'muted')); };
      actions.append(create, hide); card.append(actions); host.append(card);
    });
    if (!host.children.length) host.append(el('p', 'Все предложения скрыты.', 'muted'));
  }
  function renderDigest(row, host) {
    host.replaceChildren();
    const result = row.result;
    const summary = el('div', '', 'br-answer');
    renderMarkdownInto(summary, result.summary_markdown);
    host.append(summary);
    if (result.highlights.length) host.append(citations(result.highlights, 'Главное'));
    if (result.open_tasks.length) {
      const wrap = el('div', '', 'br-tasks');
      wrap.append(el('h3', 'Открытые задачи'));
      const list = el('ul');
      for (const task of result.open_tasks) {
        const item = el('li'), button = noteButton(task.note_id, task.text, 'br-task');
        item.append(button);
        if (task.note_title) item.append(el('span', `из «${task.note_title}»`, 'muted'));
        list.append(item);
      }
      wrap.append(list); host.append(wrap);
    }
    if (result.themes.length) {
      const wrap = el('div', '', 'br-themes');
      wrap.append(el('h3', 'Темы'));
      const chips = el('div', '', 'br-chips');
      for (const theme of result.themes) chips.append(el('span', theme, 'br-chip is-static'));
      wrap.append(chips); host.append(wrap);
    }
  }
  const renderers = {ask: renderAsk, recommend: renderRecommend, digest: renderDigest};
  function renderRow(row) {
    const kind = row.kind;
    if (!renderers[kind]) return;
    say(kind, '');
    renderers[kind](row, $(slots[kind].result));
  }
  async function follow(kind, id) {
    const run = ++state[kind], started = Date.now();
    running[kind] = true; syncButtons();
    say(kind, kind === 'ask' ? 'Ищем ответ в заметках…' : kind === 'recommend' ? 'Собираем рекомендации…' : 'Собираем сводку…');
    let errors = 0;
    const alive = () => run === state[kind] && visible('assistant-card');
    try {
      while (alive()) {
        let row = null;
        try { row = await api(`/api/v1/assistant/requests/${encodeURIComponent(id)}`); errors = 0; }
        catch (error) {
          if (!alive()) return;
          if (error.status && error.status < 500 && error.status !== 429) { say(kind, failure(error)); return; }
          if (++errors >= 3) { say(kind, 'Не удалось получить ответ. Он сохранится в недавних запросах.'); return; }
        }
        if (!alive()) return;
        if (row && row.status === 'succeeded') { $(slots[kind].result).replaceChildren(); renderRow(row); break; }
        if (row && row.status === 'failed') {
          say(kind, row.error_message || 'Не получилось собрать ответ. Попробуйте ещё раз позже.');
          break;
        }
        if (Date.now() - started >= maxWaitMs()) { say(kind, 'Ответ задерживается, проверьте позже. Запрос останется в недавних.'); break; }
        await new Promise(resolve => setTimeout(resolve, pollMs()));
      }
    } finally {
      if (run === state[kind]) { running[kind] = false; syncButtons(); }
      if (visible('assistant-card')) { loadUsage(); loadHistory(); }
    }
  }
  async function submitRequest(kind, path, body, key) {
    if (running[kind]) return;
    running[kind] = true; syncButtons();
    $(slots[kind].result).replaceChildren(); say(kind, 'Отправляем…');
    let accepted = null;
    const run = ++state[kind];
    try {
      accepted = await api(path, {method: 'POST', headers: {'Idempotency-Key': key}, ...(body ? {body: JSON.stringify(body)} : {})});
      pending[kind] = null;
    } catch (error) {
      if (run !== state[kind]) return;
      running[kind] = false;
      if (error.status) {
        pending[kind] = null;
        const resets = error.data && typeof error.data.limit_resets_at === 'string' ? error.data.limit_resets_at : null;
        if (error.status === 429 && resets) {
          const ms = Date.parse(resets) - Date.now();
          say(kind, `${error.message}${Number.isNaN(ms) ? '' : ms > 0 ? ` Обновится через ${formatWait(ms)}.` : ' Скоро обновится.'}`);
          loadUsage();
        } else {
          say(kind, error.message);
          if (error.status === 409 && kind === 'recommend') loadSettings(true);
        }
      } else say(kind, 'Нет связи с сервером. Нажмите ещё раз, повтор не спишет вторую единицу.');
      syncButtons();
      return;
    }
    if (run !== state[kind]) { running[kind] = false; syncButtons(); return; }
    running[kind] = false;
    follow(kind, accepted.id);
  }
  // The same key is reused only after a lost response, so a retry cannot spend a second unit.
  function keyFor(kind, signature) {
    if (!pending[kind] || pending[kind].signature !== signature) pending[kind] = {signature, key: window.berestaId()};
    return pending[kind].key;
  }
  $('ask-form').onsubmit = event => {
    event.preventDefault();
    const question = $('ask-question').value.trim();
    if (question.length < 3) return say('ask', 'Напишите вопрос подлиннее, хотя бы три знака.');
    const period = Number($('ask-period').value);
    submitRequest('ask', '/api/v1/assistant/ask', {question, days: period}, keyFor('ask', `${question}|${period}`));
  };
  $('rec-run').onclick = () => submitRequest('recommend', '/api/v1/assistant/recommendations', null, keyFor('recommend', 'recommend'));
  $('digest-run').onclick = () => submitRequest('digest', '/api/v1/assistant/digest', {days: 7}, keyFor('digest', 'digest7'));
  $('ask-question').addEventListener('keydown', event => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $('ask-form').requestSubmit?.($('ask-submit')); }
  });

  function drawSettings(enabled) {
    $('rec-switch').checked = enabled;
    $('rec-disabled').hidden = enabled; $('rec-on').hidden = !enabled;
    syncButtons();
  }
  async function loadSettings(force = false) {
    if (settingsKnown && !force) return;
    try {
      const data = await api('/api/v1/assistant/settings');
      settingsKnown = true; drawSettings(Boolean(data.recommendations_enabled));
    } catch (error) { say('recommend', `Не удалось узнать настройку. ${failure(error, '')}`.trim()); }
  }
  $('rec-switch').onchange = async () => {
    const wanted = $('rec-switch').checked;
    $('rec-switch').disabled = true;
    try {
      const data = await api('/api/v1/assistant/settings', {method: 'PATCH', body: JSON.stringify({recommendations_enabled: wanted})});
      settingsKnown = true; drawSettings(Boolean(data.recommendations_enabled));
      if (!data.recommendations_enabled) { state.recommend++; running.recommend = false; say('recommend', ''); $('rec-result').replaceChildren(); }
    } catch (error) {
      $('rec-switch').checked = !wanted;
      say('recommend', `Не удалось сохранить настройку. ${failure(error, '')}`.trim());
    } finally { $('rec-switch').disabled = false; syncButtons(); }
  };

  async function reopen(id) {
    try {
      const row = await api(`/api/v1/assistant/requests/${encodeURIComponent(id)}`);
      if (!visible('assistant-card') || !renderers[row.kind]) return;
      const slot = slots[row.kind];
      state[row.kind]++; running[row.kind] = false;
      $(slot.result).replaceChildren();
      if (row.status === 'succeeded' && row.result) renderRow(row);
      else if (row.status === 'failed') say(row.kind, row.error_message || 'Не получилось собрать ответ.');
      else follow(row.kind, row.id);
      syncButtons();
      $(slot.result).scrollIntoView?.({block: 'nearest'});
    } catch (error) { message(failure(error)); }
  }
  function historyTitle(row) {
    if (row.kind === 'ask') return row.question || 'Вопрос';
    if (row.kind === 'digest') return `Сводка за ${row.days || 7} ${plural(row.days || 7, ['день', 'дня', 'дней'])}`;
    return 'Рекомендации';
  }
  let historyRun = 0;
  async function loadHistory() {
    const run = ++historyRun, host = $('assistant-history');
    try {
      const rows = await api('/api/v1/assistant/requests?limit=10');
      if (run !== historyRun || !visible('assistant-card')) return;
      host.replaceChildren();
      if (!rows.length) { host.append(el('p', 'Запросов пока не было.', 'muted')); return; }
      for (const row of rows) {
        const button = el('button', '', 'br-history-row'); button.type = 'button'; button.dataset.requestId = row.id;
        const when = typeof row.created_at === 'number'
          ? new Date(row.created_at * 1000).toLocaleString('ru-RU', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}) : '';
        button.append(el('span', kindLabels[row.kind] || row.kind, 'br-history-kind'), el('span', historyTitle(row), 'br-history-text'),
          el('span', when, 'br-history-when'), el('span', statusLabels[row.status] || row.status, `br-history-status is-${row.status}`));
        button.onclick = () => reopen(row.id);
        host.append(button);
      }
    } catch (error) {
      if (run === historyRun) { host.replaceChildren(el('p', `Не удалось загрузить историю. ${failure(error, '')}`.trim(), 'muted')); }
    }
  }
  function openAssistant() {
    syncButtons(); loadUsage(); loadSettings(); loadHistory();
  }

  // ---- note screen: related notes and suggested reminders ----
  let relatedKey = '', relatedRun = 0, remindFromLink = location.hash === '#remind', noteShown = false;
  function drawRelated(items) {
    const list = $('related-list'); list.replaceChildren();
    for (const hit of items) {
      const row = el('li', '', 'br-rel-row');
      const button = el('button', '', 'br-rel'); button.type = 'button';
      button.append(el('span', hit.title || 'Без названия', 'br-rel-title'));
      const chips = el('span', '', 'br-rel-chips');
      for (const term of (hit.shared_terms || []).slice(0, 5)) chips.append(el('span', term, 'br-chip is-tiny'));
      button.append(chips);
      button.onclick = () => openNoteSafely(hit.note_id);
      row.append(button); list.append(row);
    }
    $('view-related').hidden = !items.length;
  }
  async function loadRelated(note) {
    const key = `${note.id}:${note.updated_at}`;
    if (key === relatedKey) return;
    relatedKey = key; $('view-related').hidden = true;
    const run = ++relatedRun;
    try {
      const data = await api(`/api/v1/notes/${encodeURIComponent(note.id)}/related?limit=5`);
      if (run !== relatedRun || typeof currentNote === 'undefined' || currentNote?.id !== note.id) return;
      drawRelated(data.related || []);
    } catch (_) { if (run === relatedRun) { relatedKey = ''; drawRelated([]); } }
  }
  const bell = 'M6 9a6 6 0 0 1 12 0c0 6 2 7 2 7H4s2-1 2-7ZM10 20a2 2 0 0 0 4 0';
  let proposalForm = false;
  function openProposal(item, focusTime) {
    proposalForm = true;
    const proposal = item.proposed_reminder;
    window.BerestaReminders?.startForTask(item, {local_time: proposal.local_time, timezone: proposal.timezone, focusTime});
  }
  function drawProposals(items) {
    for (const old of document.querySelectorAll('#items .br-propose')) old.remove();
    for (const item of items) {
      if (!item.proposed_reminder || item.kind !== 'task' || item.status !== 'open') continue;
      const row = [...document.querySelectorAll('#items .item')].find(node => node.dataset.itemId === item.id);
      const main = row?.querySelector('.item-main');
      if (!main) continue;
      const box = el('div', '', 'br-propose');
      const mark = svg('svg', {viewBox: '0 0 24 24', width: 14, height: 14, 'aria-hidden': 'true', focusable: 'false'});
      mark.append(svg('path', {d: bell, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.8, 'stroke-linecap': 'round', 'stroke-linejoin': 'round'}));
      const label = item.proposed_reminder.label || 'в предложенное время';
      const set = el('button', 'Поставить', 'quiet br-mini'); set.type = 'button'; set.dataset.propose = 'set';
      set.setAttribute('aria-label', `Поставить напоминание ${label}`);
      set.onclick = () => openProposal(item, false);
      const edit = el('button', 'Изменить время', 'quiet br-mini'); edit.type = 'button'; edit.dataset.propose = 'edit';
      edit.onclick = () => openProposal(item, true);
      box.append(mark, el('span', `Напомнить ${label}?`, 'br-propose-text'), set, edit);
      main.append(box);
    }
  }
  document.addEventListener('beresta:note-rendered', () => {
    if (typeof currentNote === 'undefined' || !currentNote) return;
    noteShown = true; proposalForm = false;
    drawProposals(currentNote.items || []);
    loadRelated(currentNote);
  });
  // The reminder list changes outside the note. When the form closes, ask which suggestions still stand.
  new MutationObserver(async () => {
    if ($('reminder-form').hidden && proposalForm && typeof currentNote !== 'undefined' && currentNote) {
      proposalForm = false;
      const id = currentNote.id;
      try {
        const fresh = await api(`/api/v1/notes/${encodeURIComponent(id)}`);
        if (currentNote?.id === id) drawProposals(fresh.items || []);
      } catch (_) { /* the suggestions stay as they were */ }
    }
  }).observe($('reminder-form'), {attributes: true, attributeFilter: ['hidden']});
  // A Telegram link ends with #remind: after the note opens, show the first suggestion and its form.
  document.addEventListener('beresta:note-idle', () => {
    if (!remindFromLink || !noteShown) return;
    remindFromLink = false;
    try { history.replaceState(null, '', location.pathname + location.search); } catch (_) { /* ignore */ }
    Promise.resolve().then(() => {
      const first = document.querySelector('#items .br-propose');
      if (!first) return;
      first.scrollIntoView?.({block: 'center'});
      first.querySelector('[data-propose="set"]').click();
    });
  });
  window.berestaBrain = {navigate, hashName};
  if (!$('workspace').hidden) route();
})();
