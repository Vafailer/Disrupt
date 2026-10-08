'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const nf = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 8});
  const moscowDay = date => new Intl.DateTimeFormat('sv-SE', {timeZone: 'Europe/Moscow'}).format(date);
  const shiftDay = (day, delta) => new Date(Date.parse(day + 'T12:00:00Z') + delta * 86400000).toISOString().slice(0, 10);
  const timestamp = value => {
    if (typeof value !== 'string' || !Number.isFinite(Date.parse(value))) throw new Error('schema');
    return new Intl.DateTimeFormat('ru-RU', {timeZone: 'Europe/Moscow', dateStyle: 'short', timeStyle: 'short'}).format(new Date(value)) + ' МСК';
  };
  const count = value => {
    if (!Number.isSafeInteger(value) || value < 0) throw new Error('schema');
    return nf.format(value);
  };
  const number = value => {
    if (value === null) return 'Нет данных';
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) throw new Error('schema');
    return nf.format(value);
  };
  const text = value => {
    if (value === null) return 'Нет данных';
    if (typeof value !== 'string') throw new Error('schema');
    return value;
  };
  const money = value => {
    if (value === null) return 'Нет данных';
    if (typeof value !== 'string' || !/^\d+(\.\d+)?$/.test(value) || !Number.isFinite(Number(value))) throw new Error('schema');
    return nf.format(Number(value)) + ' ₽';
  };
  const percent = value => {
    count(value.numerator); count(value.denominator);
    if (value.numerator > value.denominator || (value.denominator === 0) !== (value.value === null)) throw new Error('schema');
    if (value.value !== null && (value.value > 100 || Math.abs(value.value - 100 * value.numerator / value.denominator) > 0.000001)) throw new Error('schema');
    return value.value === null ? 'Нет данных' : number(value.value) + '%';
  };
  const node = (tag, content, className) => {
    const el = document.createElement(tag);
    if (content !== undefined) el.textContent = content;
    if (className) el.className = className;
    return el;
  };
  function cards(id, items) {
    const nodes = items.map(([label, value, hint]) => {
      const el = node('article', undefined, 'metric');
      el.append(node('h3', label), node('p', value));
      if (hint) el.append(node('small', hint));
      return el;
    });
    $(id).replaceChildren(...nodes);
  }
  function table(id, headings, rows) {
    if (!rows.length) { $(id).replaceChildren(node('p', 'За этот период данных нет.')); return; }
    const el = node('table'), head = node('thead'), tr = node('tr'), body = node('tbody');
    for (const label of headings) { const th = node('th', label); th.scope = 'col'; tr.append(th); }
    head.append(tr);
    for (const row of rows) { const r = node('tr'); for (const cell of row) r.append(node('td', cell)); body.append(r); }
    el.append(head, body); $(id).replaceChildren(el);
  }
  const ratio = p => `${count(p.numerator)} из ${count(p.denominator)}`;
  let generation = 0, controller = null, applied = null, next = null, offsets = [], offset = 0;
  let exporting = false;
  function clear() {
    applied = null; next = null;
    $('dashboard').hidden = true;
    for (const id of ['cards','daily','funnel','retention','costs','quality','usage']) $(id).replaceChildren();
    $('export').disabled = true; $('next').disabled = true; $('previous').disabled = true;
  }
  function status(message, error = false) { $('status').textContent = message; $('status').className = error ? 'error' : ''; }
  function filters() {
    const from = $('from').value, to = $('to').value, channel = $('channel').value, source = $('source').value.trim() || 'all';
    const validDay = day => /^\d{4}-\d{2}-\d{2}$/.test(day) && Number.isFinite(Date.parse(day)) && new Date(day).toISOString().slice(0,10) === day;
    if (!validDay(from) || !validDay(to) || from > to || !['all','web','telegram'].includes(channel) || source.length > 100) throw new Error('filters');
    return {from, to, channel, source};
  }
  function render(data, slice, currentOffset) {
    if (![data.daily, data.funnel, data.usage].every(Array.isArray) || data.usage.length > 50) throw new Error('schema');
    const c = data.cards, q = data.quality, costs = q.costs, r = data.retention;
    if (costs.currency !== 'RUB') throw new Error('schema');
    const stamp = timestamp(data.generated_at), lastFinished = shiftDay(moscowDay(new Date(data.generated_at)), -1);
    const dauDay = slice.to < lastFinished ? slice.to : lastFinished;
    const dauLabel = slice.from > lastFinished ? 'DAU текущего дня · предварительно' : 'DAU за ' + dauDay;
    cards('cards', [
      [dauLabel, count(c.dau)], ['Активные за период', count(c.unique_users)], ['Регистрации', count(c.registrations)],
      ['Новые пользователи', count(c.new_users)], ['Вернувшиеся пользователи', count(c.returning_users)],
      ['Активация с ИИ', percent(c.ai_activation), ratio(c.ai_activation)],
      ['Активация без ИИ', percent(c.manual_activation), ratio(c.manual_activation)],
      ['Завершили целевой сценарий', count(c.completed_scenario)], ['Возвраты после активации', count(c.returns)],
    ]);
    $('activation-pending').textContent = `Ожидают завершения окна активации: ${count(c.activation_pending)}.`;
    table('daily', ['Дата','Регистрации','DAU','Новые','Вернувшиеся','Сессии','LLM','STT','LLM / DAU','Вызовы без полного usage'], data.daily.map(d => [text(d.date), count(d.registrations), count(d.dau), count(d.new_users), count(d.returning_users), count(d.session_count), money(d.llm_cost), money(d.stt_cost), money(d.llm_cost_per_dau), count(d.unknown_usage_calls)]));
    const labels = {registered:'Зарегистрировались',capture_saved:'Сохранили запись',note_opened:'Открыли результат',structure_checked:'Проверили структуру',returned:'Вернулись'};
    table('funnel', ['Шаг','Пользователи','Конверсия'], data.funnel.map(f => [labels[f.step] || text(f.step), count(f.users), percent(f.conversion) + ' · ' + ratio(f.conversion)]));
    cards('retention', [['D1',percent(r.d1),ratio(r.d1)],['D7',percent(r.d7),ratio(r.d7)]]);
    $('retention-pending').textContent = `Окно ещё не завершилось: D1 — ${count(r.pending_d1)}, D7 — ${count(r.pending_d7)}.`;
    cards('costs', [
      ['LLM · полная стоимость',money(costs.llm_cost)], ['STT · полная стоимость',money(costs.stt_cost)],
      ['LLM · известная часть',money(costs.known_llm_cost)], ['STT · известная часть',money(costs.known_stt_cost)],
      ['Вызовы без полного usage',count(costs.unknown_usage_calls)], ['Вызовы LLM',count(costs.llm_calls)], ['Вызовы STT',count(costs.stt_calls)],
      ['Входные токены',number(costs.input_tokens)], ['Выходные токены',number(costs.output_tokens)], ['Кэшированные токены',number(costs.cache_tokens)],
      ['STT · минуты', costs.stt_minutes === null ? 'Нет данных' : text(costs.stt_minutes)], ['Пользователи в расчёте',count(costs.user_count)],
      ['Сессии',count(costs.session_count)], ['Сумма дневных DAU',count(costs.dau_sum)],
      ['Расход на пользователя',money(costs.cost_per_user)], ['Расход на сессию',money(costs.cost_per_session)], ['LLM cost/DAU',money(costs.llm_cost_per_dau)],
    ]);
    cards('quality', [
      ['Успешные обработки ИИ',count(q.ai_succeeded)], ['Ошибки ИИ',count(q.ai_failed)], ['Успешность ИИ',percent(q.ai_success_rate),ratio(q.ai_success_rate)],
      ['Исправленные заметки',count(q.edited_notes)], ['Обработка p95 · мс',number(q.processing_p95_ms)],
      ['Напоминания отправлены',count(q.reminder_sent)], ['Доставка заблокирована',count(q.reminder_blocked)],
      ['Ошибки доставки',count(q.reminder_failed)], ['Неизвестный исход доставки',count(q.reminder_unknown)], ['Напоминания открыты',count(q.reminder_opened)],
    ]);
    table('usage', ['Время МСК','Пользователь','Сессия','Операция','Канал','Тип','Модель','Статус','Входные токены','Выходные','Кэш','STT · с','Стоимость','Оценка стоимости','Тариф','Задержка · мс'], data.usage.map(u => [timestamp(u.occurred_at),text(u.user_pseudonym),text(u.session_pseudonym),text(u.operation_pseudonym),text(u.channel),text(u.kind),text(u.model),text(u.status),number(u.input_tokens),number(u.output_tokens),number(u.cache_tokens),number(u.stt_seconds),money(u.cost),money(u.estimated_cost),text(u.tariff_version),number(u.latency_ms)]));
    $('snapshot').textContent = `${slice.from} — ${slice.to} · ${slice.channel === 'all' ? 'Все каналы' : slice.channel} · Источник ${slice.source} · Сформировано ${stamp}`;
    $('page').textContent = data.usage.length ? `Операции ${currentOffset + 1}–${currentOffset + data.usage.length}` : 'Нет операций';
  }
  const errorMessage = error => ({401:'Сессия завершилась. Войди в приложение заново.',403:'Доступ только для администратора.',404:'API статистики ещё не подключён. Данные не загружены.',503:'Статистика временно недоступна. Попробуй позже.',filters:'Проверь даты: начало периода не должно быть позже окончания.'})[error.status || error.message] || 'Не удалось загрузить данные. Повтори запрос.';
  async function load(targetOffset = 0, history = [], slice) {
    const focusAfter = ['next','previous'].includes(document.activeElement.id);
    const serial = ++generation;
    if (controller) controller.abort();
    controller = new AbortController();
    const active = controller, timer = setTimeout(() => active.abort(), 15000);
    clear(); $('login').hidden = true; $('apply').disabled = true; $('dashboard').setAttribute('aria-busy','true'); status('Загружаем статистику…');
    try {
      slice = slice || filters();
      const params = new URLSearchParams({...slice, usage_limit:'50',usage_offset:String(targetOffset)});
      const response = await fetch('/api/admin/summary?' + params, {credentials:'same-origin',cache:'no-store',redirect:'error',signal:active.signal,headers:{Accept:'application/json'}});
      if (!response.ok) throw {status:response.status};
      const data = await response.json();
      if (serial !== generation) return;
      const cursor = response.headers.get('X-Next-Usage-Offset');
      if (cursor !== null && (!/^\d+$/.test(cursor) || !Number.isSafeInteger(Number(cursor)) || Number(cursor) <= targetOffset)) throw new Error('schema');
      render(data, slice, targetOffset);
      applied = {...slice}; offset = targetOffset; offsets = history; next = cursor === null ? null : Number(cursor);
      $('dashboard').hidden = false; $('export').disabled = false; $('next').disabled = next === null; $('previous').disabled = !offsets.length;
      status('Данные загружены.');
      if (focusAfter) $('usage').focus({preventScroll:true});
    } catch (error) {
      if (serial !== generation) return;
      clear(); status(errorMessage(error), true); $('login').hidden = error.status !== 401;
    } finally {
      clearTimeout(timer);
      if (serial === generation) { $('apply').disabled = false; $('dashboard').setAttribute('aria-busy','false'); }
    }
  }
  $('filters').addEventListener('submit', event => {event.preventDefault(); load();});
  $('filters').addEventListener('input', () => {
    generation++; if (controller) controller.abort(); clear(); $('apply').disabled = false;
    $('dashboard').setAttribute('aria-busy','false');
    status('Фильтры изменены. Нажми «Показать».');
  });
  $('next').addEventListener('click', () => {if (next !== null && applied) load(next, [...offsets,offset], applied);});
  $('previous').addEventListener('click', () => {if (offsets.length && applied) load(offsets.at(-1), offsets.slice(0,-1), applied);});
  $('export').addEventListener('click', async () => {
    if (!applied || exporting) return;
    exporting = true; $('export').disabled = true;
    const serial = generation, slice = {...applied}, abort = new AbortController(), timer = setTimeout(() => abort.abort(), 15000);
    try {
      const response = await fetch('/api/admin/export?' + new URLSearchParams(slice), {credentials:'same-origin',cache:'no-store',redirect:'error',signal:abort.signal,headers:{Accept:'text/csv'}});
      if (!response.ok) throw {status:response.status};
      if (!(response.headers.get('Content-Type') || '').toLowerCase().startsWith('text/csv')) throw new Error('schema');
      const blob = await response.blob();
      if (serial !== generation) return;
      const url = URL.createObjectURL(blob), link = node('a');
      link.href = url; link.download = `beresta-${slice.from}-${slice.to}.csv`; document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000); status('CSV скачан для выбранного периода.');
    } catch (error) {
      if (serial !== generation) return;
      if ([401,403].includes(error.status)) clear();
      status(errorMessage(error), true); $('login').hidden = error.status !== 401;
    } finally { clearTimeout(timer); exporting = false; $('export').disabled = !applied; }
  });
  $('to').value = shiftDay(moscowDay(new Date()), -1);
  $('from').value = shiftDay($('to').value, -6);
  load();
})();
