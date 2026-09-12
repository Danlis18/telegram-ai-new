(() => {
  'use strict';
  const tg = window.Telegram?.WebApp;
  const initData = tg?.initData || '';
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const esc = (v = '') => String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let channels = null;
  let avatars = {sources:{}, targets:{}};
  let syncing = false;
  let sheetEntity = null;

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set('X-Telegram-Init-Data', initData);
    if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
    const res = await fetch(path, {...options, headers});
    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
    return data;
  }

  function initials(value, fallback = 'AP') {
    const clean = String(value || '').replace(/^@/, '').trim();
    if (!clean) return fallback;
    return clean.slice(0, 2).toUpperCase();
  }

  function applyAvatar(el, url, fallback) {
    if (!el) return;
    if (el.dataset.avatarApplied === String(url || 'fallback')) return;
    el.dataset.avatarApplied = String(url || 'fallback');
    el.classList.remove('avatar-fallback');
    if (!url) {
      el.innerHTML = esc(fallback);
      el.classList.add('avatar-fallback');
      return;
    }
    el.classList.add('avatar-loading');
    const img = document.createElement('img');
    img.alt = '';
    img.decoding = 'async';
    img.loading = 'lazy';
    img.onload = () => el.classList.remove('avatar-loading');
    img.onerror = () => {
      el.classList.remove('avatar-loading');
      el.classList.add('avatar-fallback');
      el.textContent = fallback;
    };
    el.innerHTML = '';
    el.append(img);
    img.src = url;
  }

  function buildSheet() {
    if ($('#channelActionBackdrop')) return;
    const root = document.createElement('div');
    root.id = 'channelActionBackdrop';
    root.className = 'channel-action-backdrop';
    root.innerHTML = '<section class="channel-action-sheet"><div class="channel-sheet-handle"></div><div id="channelSheetBody"></div></section>';
    document.body.append(root);
    root.addEventListener('click', e => {
      if (e.target === root) closeSheet();
    });
  }

  function closeSheet() {
    const root = $('#channelActionBackdrop');
    root?.classList.remove('open');
    sheetEntity = null;
  }

  async function confirmText(text) {
    if (tg?.showConfirm) return await new Promise(resolve => tg.showConfirm(text, resolve));
    return window.confirm(text);
  }

  function openTelegram(url) {
    if (!url) return;
    if (tg?.openTelegramLink) tg.openTelegramLink(url);
    else window.open(url, '_blank', 'noopener');
  }

  function headAvatar(entity) {
    const kindMap = entity.kind === 'source' ? avatars.sources : avatars.targets;
    const url = kindMap?.[String(entity.id)] || '';
    const fallback = initials(entity.username || entity.title || entity.channel_ref, entity.kind === 'source' ? 'TG' : 'AP');
    return `<div class="channel-avatar ${url ? 'avatar-loading' : 'avatar-fallback'}" data-sheet-avatar>${url ? `<img src="${esc(url)}" alt="">` : esc(fallback)}</div>`;
  }

  function openActions(entity) {
    buildSheet();
    sheetEntity = entity;
    const body = $('#channelSheetBody');
    const title = entity.kind === 'source' ? `@${entity.username}` : (entity.title || entity.channel_ref || 'Канал');
    const sub = entity.kind === 'source'
      ? `Новини → ${entity.target_title || 'канал не вибрано'}`
      : `${entity.channel_ref || ''}${entity.source_count != null ? ` · ${entity.source_count} джерел` : ''}`;
    const actions = [];
    if (entity.url) actions.push(`<button class="channel-sheet-btn" data-sheet-action="open"><span>Відкрити в Telegram</span><span>↗</span></button>`);
    if (entity.kind === 'source') {
      actions.push(`<button class="channel-sheet-btn primary" data-sheet-action="route"><span>Змінити канал призначення</span><span>→</span></button>`);
      actions.push(`<button class="channel-sheet-btn danger" data-sheet-action="delete-source"><span>Видалити джерело</span><span>⌫</span></button>`);
    } else {
      if (!entity.is_default) actions.push(`<button class="channel-sheet-btn primary" data-sheet-action="default"><span>Зробити основним</span><span>✓</span></button>`);
      actions.push(`<button class="channel-sheet-btn danger" data-sheet-action="delete-target"><span>Видалити канал</span><span>⌫</span></button>`);
    }
    body.innerHTML = `<div class="channel-sheet-head">${headAvatar(entity)}<div class="channel-sheet-copy"><strong>${esc(title)}</strong><small>${esc(sub)}</small></div></div><div class="channel-sheet-actions">${actions.join('')}</div>`;
    $$('[data-sheet-action]', body).forEach(btn => btn.addEventListener('click', () => handleSheetAction(btn.dataset.sheetAction)));
    requestAnimationFrame(() => $('#channelActionBackdrop')?.classList.add('open'));
    try { tg?.HapticFeedback?.impactOccurred('light'); } catch (_) {}
  }

  function renderRoutePicker(source) {
    const body = $('#channelSheetBody');
    if (!body || !channels) return;
    const options = (channels.targets || []).map(t => {
      const active = Number(source.target_id) === Number(t.id);
      return `<button class="route-option ${active ? 'active' : ''}" data-route-target="${Number(t.id)}"><span>${esc(t.title || t.channel_ref)}</span><span>${active ? '✓' : '→'}</span></button>`;
    }).join('');
    body.innerHTML = `<div class="channel-sheet-head">${headAvatar(source)}<div class="channel-sheet-copy"><strong>@${esc(source.username)}</strong><small>Обери канал для нових постів</small></div></div><div class="route-picker"><div class="route-picker-title">Публікувати в</div>${options}</div>`;
    $$('[data-route-target]', body).forEach(btn => btn.addEventListener('click', async () => {
      try {
        await api(`/api/channels/sources/${source.id}/route`, {method:'POST', body:JSON.stringify({target_id:Number(btn.dataset.routeTarget)})});
        try { tg?.HapticFeedback?.notificationOccurred('success'); } catch (_) {}
        closeSheet();
        await refreshExistingChannelScreen();
      } catch (e) { showAlert(e.message); }
    }));
  }

  function showAlert(message) {
    if (tg?.showAlert) tg.showAlert(String(message));
    else window.alert(String(message));
  }

  async function handleSheetAction(action) {
    const entity = sheetEntity;
    if (!entity) return;
    try {
      if (action === 'open') return openTelegram(entity.url);
      if (action === 'route') return renderRoutePicker(entity);
      if (action === 'default') {
        await api(`/api/channels/targets/${entity.id}/default`, {method:'POST'});
        closeSheet();
        await refreshExistingChannelScreen();
        return;
      }
      if (action === 'delete-source') {
        if (!(await confirmText(`Видалити @${entity.username} зі списку джерел?`))) return;
        await api(`/api/channels/sources/${entity.id}`, {method:'DELETE'});
        closeSheet();
        await refreshExistingChannelScreen();
        return;
      }
      if (action === 'delete-target') {
        if (!(await confirmText(`Видалити канал ${entity.title || entity.channel_ref}? Джерела будуть перепризначені на інший активний канал.`))) return;
        await api(`/api/channels/targets/${entity.id}`, {method:'DELETE'});
        closeSheet();
        await refreshExistingChannelScreen();
      }
    } catch (e) { showAlert(e.message); }
  }

  function replaceWithMore(actions, entity) {
    const old = $('[data-route-source], [data-default-target]', actions);
    if (!old || actions.querySelector('.channel-more')) return;
    const more = document.createElement('button');
    more.className = 'mini-btn channel-more';
    more.type = 'button';
    more.textContent = '⋯';
    more.setAttribute('aria-label', 'Дії з каналом');
    more.addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); openActions(entity); });
    old.replaceWith(more);
  }

  function enhanceLists() {
    if (!channels) return;
    const targetCards = $$('#targetsList .channel-card');
    targetCards.forEach((card, index) => {
      const t = channels.targets?.[index];
      if (!t) return;
      const entity = {...t, kind:'target'};
      applyAvatar($('.channel-avatar', card), avatars.targets?.[String(t.id)], initials(t.title || t.channel_ref, 'AP'));
      replaceWithMore($('.channel-actions', card), entity);
      card.dataset.proEnhanced = '1';
    });
    const sourceCards = $$('#sourcesList .source-card');
    sourceCards.forEach((card, index) => {
      const s = channels.sources?.[index];
      if (!s) return;
      const target = channels.targets?.find(t => Number(t.id) === Number(s.target_id));
      const entity = {...s, kind:'source', target_title:target?.title || target?.channel_ref || ''};
      applyAvatar($('.channel-avatar', card), avatars.sources?.[String(s.id)], initials(s.username, 'TG'));
      replaceWithMore($('.channel-actions', card), entity);
      card.dataset.proEnhanced = '1';
    });
  }

  async function syncChannels() {
    if (!initData || syncing) return;
    syncing = true;
    try {
      [channels, avatars] = await Promise.all([
        api('/api/channels'),
        api('/api/channel-avatars').catch(() => ({sources:{}, targets:{}})),
      ]);
      enhanceLists();
    } catch (_) {
      // Existing channel UI remains fully functional if enhancement sync fails.
    } finally { syncing = false; }
  }

  async function refreshExistingChannelScreen() {
    const nav = $('[data-nav="channels"]');
    nav?.click();
    await new Promise(r => setTimeout(r, 180));
    await syncChannels();
  }

  async function renderStorageStatus() {
    try {
      const s = await fetch('/healthz/storage').then(r => r.json());
      const host = $('.version-note');
      if (!host || $('#storageChip')) return;
      const chip = document.createElement('div');
      chip.id = 'storageChip';
      chip.className = `storage-chip ${s.persistent ? '' : 'bad'}`.trim();
      chip.innerHTML = `<i></i><span>${s.persistent ? 'DATA PERSISTENCE · ON' : 'DATA PERSISTENCE · NEEDS VOLUME'}</span>`;
      host.before(chip);
    } catch (_) {}
  }

  function observe() {
    const targets = $('#targetsList');
    const sources = $('#sourcesList');
    const callback = () => {
      const hasUnenhanced = $$('#targetsList .channel-card, #sourcesList .source-card').some(x => x.dataset.proEnhanced !== '1');
      if (hasUnenhanced) setTimeout(syncChannels, 40);
    };
    if (targets) new MutationObserver(callback).observe(targets, {childList:true, subtree:false});
    if (sources) new MutationObserver(callback).observe(sources, {childList:true, subtree:false});
    $('[data-nav="channels"]')?.addEventListener('click', () => setTimeout(syncChannels, 140));
  }

  async function start() {
    if (!initData) return;
    buildSheet();
    observe();
    renderStorageStatus();
    if ($('#screen-channels')?.classList.contains('active')) await syncChannels();
  }

  start();
})();
