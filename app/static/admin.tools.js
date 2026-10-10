'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const moscowDay = date => new Intl.DateTimeFormat('sv-SE', {timeZone: 'Europe/Moscow'}).format(date);
  const shiftDay = (day, delta) => new Date(Date.parse(day + 'T12:00:00Z') + delta * 86400000).toISOString().slice(0, 10);
  const auth = () => window.adminSession || {csrf: '', expired() {}};
  const node = (tag, content) => { const el = document.createElement(tag); el.textContent = content; return el; };
  const MAX_DAYS = 92;
  const actions = {
    login_success: 'Вход', login_failed: 'Неудачный вход', account_locked: 'Вход заблокирован', logout: 'Выход',
    export_csv: 'Выгрузка CSV', export_zip: 'Выгрузка архива метрик', feedback_status: 'Статус обращения',
    user_marked_test: 'Аккаунт помечен тестовым', user_unmarked_test: 'Пометка тестового снята',
    admin_created: 'Администратор создан', admin_confirmed: 'Код подтверждён', admin_confirm_failed: 'Код не подошёл',
    admin_disabled: 'Администратор отключён', admin_enabled: 'Администратор включён', admin_2fa_reset: 'Код сброшен',
    admin_list: 'Просмотр списка администраторов',
  };

  // Metrics archive for the organizers.
  let archiving = false, archiveSerial = 0;
  const archiveStatus = (message, error = false) => { $('archive-status').textContent = message; $('archive-status').className = error ? 'error' : ''; };
  function archivePeriod() {
    const from = $('archive-from').value, to = $('archive-to').value;
    const valid = day => /^\d{4}-\d{2}-\d{2}$/.test(day) && Number.isFinite(Date.parse(day)) && new Date(day).toISOString().slice(0, 10) === day;
    const local = message => Object.assign(new Error(message), {local: true});
    if (!valid(from) || !valid(to) || from > to) throw local('Проверьте даты: начало периода не должно быть позже окончания.');
    if ((Date.parse(to) - Date.parse(from)) / 86400000 + 1 > MAX_DAYS) throw local(`Период не может быть длиннее ${MAX_DAYS} дней.`);
    return {from, to};
  }
  $('archive-to').value = shiftDay(moscowDay(new Date()), -1);
  $('archive-from').value = shiftDay($('archive-to').value, -6);
  $('archive-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (archiving) return;
    const serial = ++archiveSerial, abort = new AbortController(), timer = setTimeout(() => abort.abort(), 60000);
    archiving = true; $('archive-export').disabled = true;
    try {
      const period = archivePeriod();
      archiveStatus('Собираем архив…');
      const response = await fetch('/admin-api/v1/export.zip?' + new URLSearchParams(period), {
        credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: abort.signal, headers: {Accept: 'application/zip'},
      });
      if (!response.ok) {
        let detail = '';
        try { const data = await response.json(); if (typeof data.detail === 'string') detail = data.detail; } catch (error) { /* no body */ }
        throw Object.assign(new Error(detail), {status: response.status});
      }
      if (!(response.headers.get('Content-Type') || '').toLowerCase().startsWith('application/zip')) throw new Error('schema');
      const blob = await response.blob();
      if (serial !== archiveSerial) return;
      const url = URL.createObjectURL(blob), link = document.createElement('a');
      link.href = url; link.download = `beresta-metrics-${period.from}-${period.to}.zip`; document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      archiveStatus('Архив скачан.');
    } catch (error) {
      if (serial !== archiveSerial) return;
      if (error.status === 401) { archiveStatus(''); auth().expired(); return; }
      let message = 'Не удалось собрать архив. Повторите запрос.';
      if (error.local || ([413, 422].includes(error.status) && error.message)) message = error.message;
      else if (error.status === 404) message = 'Архив недоступен.';
      archiveStatus(message, true);
    } finally { clearTimeout(timer); archiving = false; $('archive-export').disabled = false; }
  });

  // Audit log.
  let auditSerial = 0;
  const auditStatus = message => { $('audit-status').textContent = message; };
  const stamp = seconds => new Intl.DateTimeFormat('ru-RU', {timeZone: 'Europe/Moscow', dateStyle: 'short', timeStyle: 'medium'}).format(new Date(seconds * 1000)) + ' МСК';
  const details = value => {
    if (!value || typeof value !== 'object') return '';
    return Object.entries(value).map(([name, item]) => `${name}: ${item}`).join(', ');
  };
  async function loadAudit() {
    const serial = ++auditSerial;
    $('audit-list').replaceChildren(); auditStatus('Загружаем журнал…');
    try {
      const response = await fetch('/admin-api/v1/audit?limit=100', {credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: {Accept: 'application/json'}});
      if (!response.ok) throw Object.assign(new Error('status'), {status: response.status});
      const rows = await response.json();
      if (serial !== auditSerial) return;
      if (!Array.isArray(rows)) throw new Error('schema');
      if (!rows.length) { auditStatus('Журнал пока пуст.'); return; }
      const table = document.createElement('table'), head = document.createElement('thead'), tr = document.createElement('tr'), body = document.createElement('tbody');
      for (const label of ['Время', 'Кто', 'Действие', 'Объект', 'IP', 'Подробности']) { const th = node('th', label); th.scope = 'col'; tr.append(th); }
      head.append(tr);
      for (const row of rows) {
        const line = document.createElement('tr');
        for (const cell of [stamp(row.created_at), row.admin || 'сервер', actions[row.action] || row.action, row.target || '', row.ip || '', details(row.details)]) line.append(node('td', String(cell)));
        body.append(line);
      }
      table.append(head, body); $('audit-list').append(table);
      auditStatus(`Показано записей: ${rows.length}`);
    } catch (error) {
      if (serial !== auditSerial) return;
      $('audit-list').replaceChildren();
      if (error.status === 401) { auditStatus(''); auth().expired(); return; }
      auditStatus('Не удалось загрузить журнал.');
    }
  }
  $('audit-refresh').addEventListener('click', loadAudit);
  document.addEventListener('admin:view', event => { if (event.detail === 'audit') loadAudit(); });
  if (window.adminSession) {
    window.adminSession.onLogout(() => { auditSerial++; archiveSerial++; $('audit-list').replaceChildren(); auditStatus(''); archiveStatus(''); });
  }
})();
