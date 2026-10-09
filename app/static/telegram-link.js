'use strict';
// Telegram linking in one step. Loaded after app.js and uses its api(), message() and element().
// The code issued here lives in memory only. A reload forgets it, and the bot's deep link
// (#telegram-confirm) or the "Проверить запрос" button still finish the job.
(() => {
  const BOT_USERNAME = 'beresta_ru_bot'; // The only place in this file that names the bot.
  const POLL_MS = 3000, CODE_MS = 10 * 60 * 1000;
  const el = id => document.getElementById(id);
  let issued = null, timer = null, busy = false, box = null, statusLine = null, openLink = null;

  const botUrl = code => `https://t.me/${BOT_USERNAME}?start=${encodeURIComponent(code)}`;
  const say = text => { if (statusLine) statusLine.textContent = text; };

  function ensureBox() {
    if (box) return;
    box = document.createElement('div'); box.id = 'telegram-open-bot'; box.hidden = true;
    openLink = document.createElement('a'); openLink.className = 'button-link'; openLink.textContent = 'Открыть бота';
    openLink.target = '_blank'; openLink.rel = 'noopener';
    const hint = element('p', 'Если бот не открылся, отправьте ему это сообщение.', 'muted');
    statusLine = element('p', '', 'muted'); statusLine.id = 'telegram-status'; statusLine.setAttribute('aria-live', 'polite');
    box.append(openLink, hint, statusLine);
    el('telegram-code').before(box);
  }

  function showCode(code) {
    ensureBox();
    openLink.href = botUrl(code); openLink.hidden = false; box.hidden = false;
    el('telegram-code').textContent = `/start ${code}`;
    say('Нажмите «Открыть бота» и «Запустить» в Telegram. Подключение подтвердится само.');
  }

  function clearCode() {
    el('telegram-code').textContent = '';
    if (box) box.hidden = true;
  }

  function stopPolling() { clearTimeout(timer); timer = null; }

  function renderLinks(links) {
    const holder = el('telegram-links'); holder.replaceChildren();
    if (links.identities.length) {
      holder.append(element('p', 'Telegram подключён', 'telegram-linked'));
      holder.append(element('p', `Записывать и получать напоминания можно через @${BOT_USERNAME}.`, 'muted'));
    }
    for (const link of links.pending) {
      const button = element('button', 'Подтвердить подключение Telegram', 'secondary');
      button.onclick = async () => {
        button.disabled = true;
        try { await confirmAndFinish(link.link_request_id); }
        catch (e) { message(e.message); button.disabled = false; }
      };
      holder.append(button);
    }
    if (!links.pending.length && !links.identities.length) holder.append(element('p', 'Telegram ещё не подключён.', 'muted'));
  }

  async function loadLinks() {
    const links = await api('/api/v1/telegram/links');
    renderLinks(links);
    return links;
  }

  async function confirmAndFinish(id) {
    await api(`/api/v1/telegram/links/${encodeURIComponent(id)}/confirm`, {method: 'POST'});
    issued = null; stopPolling(); clearCode();
    await loadLinks();
    message('Telegram подключён.');
  }

  function expire() {
    issued = null; stopPolling(); clearCode();
    ensureBox(); box.hidden = false; openLink.hidden = true;
    say('Код истёк. Нажмите «Подключить Telegram» ещё раз.');
  }

  function schedule() {
    stopPolling();
    if (issued && !document.hidden) timer = setTimeout(tick, POLL_MS);
  }

  async function tick() {
    timer = null;
    const mine = issued;
    if (!mine || el('workspace').hidden) { issued = null; return; }
    if (Date.now() >= mine.expires) return expire();
    try {
      const links = await api('/api/v1/telegram/links');
      if (issued !== mine) return;
      const pending = links.pending.find(item => item.link_request_id === mine.id);
      if (pending) {
        mine.seen = true;
        if (!busy) {
          busy = true;
          try { await confirmAndFinish(pending.link_request_id); }
          finally { busy = false; }
        }
        if (issued === mine) schedule();
        return;
      }
      if (mine.seen && links.identities.length) {
        // Another tab or button confirmed it already.
        issued = null; stopPolling(); clearCode(); renderLinks(links); message('Telegram подключён.');
        return;
      }
    } catch (e) {
      if (e.status === 401) { issued = null; return; }
      if (e.status === 409 || e.status === 404) { say(e.message); issued = null; clearCode(); return; }
    }
    if (issued === mine) schedule();
  }

  el('link-code').textContent = 'Подключить Telegram';
  const intro = el('telegram-settings').querySelector('p');
  if (intro) intro.textContent = 'Нажмите «Подключить Telegram», затем «Открыть бота» и «Запустить» в Telegram. Подключение подтвердится само.';

  el('link-code').onclick = async () => {
    try {
      const link = await api('/api/v1/telegram/link-code', {method: 'POST'});
      issued = {id: link.link_request_id, expires: Date.now() + CODE_MS, seen: false};
      showCode(link.code);
      await loadLinks();
      schedule();
    } catch (e) { message(e.message); }
  };

  el('refresh-links').onclick = async () => {
    try {
      const links = await loadLinks();
      const mine = issued && links.pending.find(item => item.link_request_id === issued.id);
      if (mine && !busy) { busy = true; try { await confirmAndFinish(mine.link_request_id); } finally { busy = false; } }
    } catch (e) { message(e.message); }
  };

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopPolling();
    else if (issued) { stopPolling(); tick(); }
  });

  function openDialog() {
    const dialog = el('telegram-dialog');
    if (dialog.open) return;
    el('open-telegram').click();
    if (!dialog.open && dialog.showModal) dialog.showModal();
  }

  function clearHash() {
    try { history.replaceState(null, '', location.pathname + location.search); } catch { location.hash = ''; }
  }

  async function handleHash() {
    const hash = location.hash;
    if (hash !== '#telegram' && hash !== '#telegram-confirm') return;
    if (el('workspace').hidden) return; // Wait for login: the observer below calls this again.
    clearHash(); openDialog();
    try {
      const links = await loadLinks();
      if (hash === '#telegram') return;
      if (links.pending.length === 1) {
        busy = true;
        try { await confirmAndFinish(links.pending[0].link_request_id); } finally { busy = false; }
      } else if (links.pending.length > 1) {
        message('Есть несколько запросов. Выберите свой и подтвердите.');
      } else if (links.identities.length) {
        message('Telegram уже подключён.');
      } else {
        ensureBox(); box.hidden = false; openLink.hidden = true;
        say('Запрос не найден или истёк. Нажмите «Подключить Telegram» и откройте бота ещё раз.');
        message('Запрос на подключение не найден или истёк.');
      }
    } catch (e) { message(e.message); }
  }

  new MutationObserver(() => {
    if (el('workspace').hidden) { issued = null; stopPolling(); } else handleHash();
  }).observe(el('workspace'), {attributes: true, attributeFilter: ['hidden']});
  window.addEventListener('hashchange', handleHash);
  handleHash();
})();
