/* ==================== CORE ==================== */
(() => {
  'use strict';
  const tg = window.Telegram?.WebApp;
  const state = { boot: null, screen: 'home', postTab: 'ready', selectedPost: null, channels: null, busy: false, formHandler: null };
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const esc = (v = '') => String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const haptic = (type = 'light') => { try { tg?.HapticFeedback?.impactOccurred(type); } catch (_) {} };
  const notify = (type = 'success') => { try { tg?.HapticFeedback?.notificationOccurred(type); } catch (_) {} };
  const initData = tg?.initData || '';

  if (tg) {
    tg.ready();
    tg.expand();
    try { tg.setHeaderColor('#030914'); tg.setBackgroundColor('#030914'); } catch (_) {}
    try { tg.disableVerticalSwipes(); } catch (_) {}
  }

  function toast(message, error = false) {
    const el = $('#toast');
    el.textContent = message;
    el.classList.toggle('error', error);
    el.classList.remove('hidden');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.add('hidden'), 2800);
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set('X-Telegram-Init-Data', initData);
    if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
    const response = await fetch(path, { ...options, headers });
    let data = null;
    try { data = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(data?.detail || `HTTP ${response.status}`);
    return data;
  }

  function telegramHTML(raw, editor = false) {
    const tpl = document.createElement('template');
    tpl.innerHTML = raw || '';
    const out = document.createElement('div');
    const walk = (node, target) => {
      if (node.nodeType === Node.TEXT_NODE) { target.append(document.createTextNode(node.textContent || '')); return; }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const tag = node.tagName.toLowerCase();
      if (tag === 'tg-emoji') {
        const id = node.getAttribute('emoji-id') || '';
        const span = document.createElement('span');
        span.className = 'premium-token';
        span.dataset.emojiId = /^\d+$/.test(id) ? id : '';
        span.textContent = node.textContent || '✨';
        if (editor) span.contentEditable = 'false';
        target.append(span);
        return;
      }
      const allowed = { b:'b', strong:'b', i:'i', em:'i', blockquote:'blockquote', br:'br' };
      if (allowed[tag]) {
        const el = document.createElement(allowed[tag]);
        target.append(el);
        [...node.childNodes].forEach(child => walk(child, el));
        return;
      }
      [...node.childNodes].forEach(child => walk(child, target));
    };
    [...tpl.content.childNodes].forEach(node => walk(node, out));
    return out.innerHTML;
  }

  function serializeEditor(root) {
    const walk = node => {
      if (node.nodeType === Node.TEXT_NODE) return esc(node.textContent || '');
      if (node.nodeType !== Node.ELEMENT_NODE) return '';
      if (node.classList?.contains('premium-token') && /^\d+$/.test(node.dataset.emojiId || '')) {
        return `<tg-emoji emoji-id="${node.dataset.emojiId}">${esc(node.textContent || '✨')}</tg-emoji>`;
      }
      const tag = node.tagName.toLowerCase();
      const inner = [...node.childNodes].map(walk).join('');
      if (tag === 'b' || tag === 'strong') return `<b>${inner}</b>`;
      if (tag === 'i' || tag === 'em') return `<i>${inner}</i>`;
      if (tag === 'blockquote') return `<blockquote>${inner}</blockquote>`;
      if (tag === 'br') return '\n';
      if (tag === 'div' || tag === 'p') return `${inner}\n`;
      return inner;
    };
    return [...root.childNodes].map(walk).join('').replace(/\n{3,}/g, '\n\n').trim();
  }

  function formatDate(value) {
    if (!value) return '';
    let normalized = String(value).replace(' ', 'T');
    if (!/[zZ]|[+-]\d\d:\d\d$/.test(normalized)) normalized += 'Z';
    const d = new Date(normalized);
    if (Number.isNaN(d.getTime())) return value;
    return new Intl.DateTimeFormat('uk-UA', {day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}).format(d);
  }

  function go(screen, tab = null) {
    state.screen = screen;
    $$('.screen').forEach(x => x.classList.toggle('active', x.dataset.screen === screen));
    $$('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.nav === screen));
    if (screen === 'posts') { if (tab) state.postTab = tab; setPostTab(state.postTab); loadPosts(); }
    if (screen === 'channels') loadChannels();
    if (screen === 'settings') renderSettings();
    window.scrollTo({top:0, behavior:'smooth'});
    haptic('light');
  }

  function renderBoot() {
    const b = state.boot;
    if (!b) return;
    const first = b.user?.first_name || 'редакторе';
    $('#helloTitle').textContent = `Привіт, ${first}`;
    $('#helloText').textContent = b.modes.paused ? 'Парсинг призупинено. Поточні пости залишаються доступними.' : 'Система працює. Усе важливе під контролем.';
    $('#mReady').textContent = b.stats.ready || 0;
    $('#mScheduled').textContent = b.stats.scheduled || 0;
    $('#mPublished').textContent = b.stats.published || 0;
    $('#mSources').textContent = b.workspace.sources || 0;
    $('#todayLabel').textContent = new Intl.DateTimeFormat('uk-UA',{weekday:'short',day:'2-digit',month:'short'}).format(new Date());
    const queue = Number(b.stats.ready || 0);
    const badge = $('#queueBadge');
    badge.textContent = queue > 99 ? '99+' : queue;
    badge.classList.toggle('hidden', !queue);
    const webState = b.system.services?.web_discovery?.status || 'unknown';
    const storageOk = !!b.system.storage_persistent;
    const runtimeHealthy = b.system.runtime_healthy !== false;
    const online = b.system.reader_online && b.system.premium_publisher_online && runtimeHealthy;
    $('#systemPill').classList.toggle('online', online);
    $('#systemPill span').textContent = online ? 'ONLINE' : 'CHECK';
    setStateBadge($('#readerState'), b.system.reader_online, b.system.reader_online ? 'ONLINE' : 'OFFLINE');
    setStateBadge($('#publisherState'), b.system.premium_publisher_online, b.system.premium_publisher_online ? 'PREMIUM' : 'OFFLINE');
    $('#publisherDesc').textContent = b.system.publisher_username ? `@${b.system.publisher_username}` : 'Публікація з Premium emoji';
    $('#publishModeState').textContent = (b.modes.publish || 'manual').toUpperCase();

    const webRunning = ['running','online'].includes(webState);
    setStateBadge($('#webWorkerState'), webRunning, webRunning ? 'ACTIVE' : webState.toUpperCase());
    setStateBadge($('#storageState'), storageOk, storageOk ? 'DURABLE' : 'CHECK');
    $('#storageDesc').textContent = storageOk
      ? (b.system.storage_mode || 'Railway Volume')
      : 'Потрібен persistent volume';

    const uptime = Number(b.system.uptime_seconds || 0);
    const days = Math.floor(uptime / 86400);
    const hours = Math.floor((uptime % 86400) / 3600);
    const mins = Math.floor((uptime % 3600) / 60);
    $('#uptimeState').textContent = days ? `${days}д ${hours}г` : hours ? `${hours}г ${mins}хв` : `${mins}хв`;
  }

  function setStateBadge(el, ok, text) {
    el.textContent = text;
    el.classList.toggle('ok', !!ok);
    el.classList.toggle('bad', !ok);
  }

  async function bootstrap(showLoader = false) {
    if (!initData) throw new Error('Mini App потрібно відкривати кнопкою всередині Telegram');
    if (showLoader) $('#loader').classList.remove('done');
    state.boot = await api('/api/bootstrap');
    renderBoot();
    renderSettings();
    renderPalette();
    if (showLoader) setTimeout(() => $('#loader').classList.add('done'), 220);
  }

  function setPostTab(tab) {
    state.postTab = tab;
    $$('#postTabs button').forEach(x => x.classList.toggle('active', x.dataset.postTab === tab));
  }

  function postCard(p) {
    const media = p.media?.[0];
    const mediaHTML = media ? `<div class="post-media">${media.type === 'video' ? `<video src="${media.url}" muted playsinline preload="metadata"></video>` : `<img src="${media.url}" loading="lazy" alt="">`} ${p.media.length > 1 ? `<span class="media-count">${p.media.length} медіа</span>` : ''}</div>` : '';
    const time = p.status === 'scheduled' ? `На ${formatDate(p.scheduled_at)}` : formatDate(p.created_at);
    return `<article class="post-card" data-post-id="${p.id}">${mediaHTML}<div class="post-body"><div class="post-meta"><span class="source-chip">@${esc(p.source)}</span><span class="score-chip">AI ${p.score}%</span></div><div class="post-text">${esc(p.text_plain)}</div><div class="post-foot"><span>${esc(time)}</span><span class="premium-mark">${p.premium_emoji_count ? `✦ ${p.premium_emoji_count} Premium` : p.target ? `→ ${esc(p.target.title)}` : ''}</span></div></div></article>`;
  }

  async function loadPosts() {
    const list = $('#postsList');
    list.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
    try {
      const data = await api(`/api/posts?tab=${encodeURIComponent(state.postTab)}&limit=40`);
      if (!data.items.length) { list.innerHTML = `<div class="empty-state"><strong>Тут чисто</strong><span>${state.postTab === 'ready' ? 'Нові готові пости з’являться автоматично.' : 'Поки немає записів у цьому розділі.'}</span></div>`; return; }
      list.innerHTML = data.items.map(postCard).join('');
      $$('.post-card', list).forEach(card => card.addEventListener('click', () => openPost(Number(card.dataset.postId))));
    } catch (e) { list.innerHTML = `<div class="empty-state"><strong>Не вдалося завантажити</strong><span>${esc(e.message)}</span></div>`; }
  }

  async function openPost(id) {
    haptic('medium');
    try {
      const p = await api(`/api/posts/${id}`);
      state.selectedPost = p;
      renderPostSheet(p);
      $('#sheetBackdrop').classList.remove('hidden');
      $('#postSheet').classList.remove('hidden');
    } catch (e) { toast(e.message, true); }
  }

  function renderPostSheet(p) {
    const media = p.media?.[0];
    const mediaHTML = media ? `<div class="sheet-media">${media.type === 'video' ? `<video src="${media.url}" controls playsinline preload="metadata"></video>` : `<img src="${media.url}" alt="">`}${p.media.length > 1 ? `<span class="media-count">1 / ${p.media.length}</span>` : ''}</div>` : '';
    const canAct = p.status === 'ready' || p.status === 'scheduled';
    $('#postSheetContent').innerHTML = `${mediaHTML}<div class="sheet-meta"><div><span class="source-chip">@${esc(p.source)}</span><div class="muted" style="margin-top:5px">${esc(p.target?.title || 'Канал не вибрано')}</div></div><span class="score-chip">AI ${p.score}%${p.premium_emoji_count ? ` · ✦ ${p.premium_emoji_count}` : ''}</span></div><div class="sheet-text">${telegramHTML(p.text_html)}</div>${canAct ? `<div class="sheet-actions"><button class="btn primary" data-action="publish">✓ Опублікувати</button><button class="btn" data-action="edit">✎ Текст</button><button class="btn" data-action="regen">↻ Інший варіант</button><button class="btn" data-action="schedule">◷ Запланувати</button><button class="btn danger" data-action="reject">× Відхилити</button><button class="btn ghost" data-action="close">Закрити</button></div>` : `<div class="sheet-actions"><button class="btn ghost" data-action="close">Закрити</button></div>`}`;
    $$('[data-action]', $('#postSheetContent')).forEach(btn => btn.addEventListener('click', () => postAction(btn.dataset.action)));
  }

  function closeSheet() { $('#sheetBackdrop').classList.add('hidden'); $('#postSheet').classList.add('hidden'); state.selectedPost = null; }

  async function confirmAction(text) {
    if (tg?.showConfirm) return await new Promise(resolve => tg.showConfirm(text, resolve));
    return window.confirm(text);
  }

  async function postAction(action) {
    const p = state.selectedPost;
    if (!p || state.busy) return;
    if (action === 'close') return closeSheet();
    if (action === 'edit') return openEditor(p);
    if (action === 'schedule') return openScheduleForm(p);
    if (action === 'reject') return openRejectForm(p);
    state.busy = true;
    try {
      if (action === 'publish') {
        if (!(await confirmAction('Опублікувати цей пост зараз?'))) return;
        await api(`/api/posts/${p.id}/publish`, {method:'POST'});
        notify('success'); toast('Опубліковано ✓'); closeSheet();
      } else if (action === 'regen') {
        toast('AI готує інший варіант…');
        const updated = await api(`/api/posts/${p.id}/regenerate`, {method:'POST'});
        state.selectedPost = updated; renderPostSheet(updated); notify('success'); toast('Новий варіант готовий');
      }
      await bootstrap(); await loadPosts();
    } catch (e) { notify('error'); toast(e.message, true); }
    finally { state.busy = false; }
  }

  function openEditor(post) {
    $('#richEditor').innerHTML = telegramHTML(post.text_html, true);
    renderEditorPalette();
    $('#editorModal').classList.remove('hidden');
  }

  function closeEditor() { $('#editorModal').classList.add('hidden'); }

  function insertPremium(item) {
    const editor = $('#richEditor'); editor.focus();
    const span = document.createElement('span'); span.className = 'premium-token'; span.dataset.emojiId = item.id; span.contentEditable = 'false'; span.textContent = item.fallback || '✨';
    const sel = window.getSelection();
    if (sel && sel.rangeCount) { const r = sel.getRangeAt(0); r.deleteContents(); r.insertNode(span); r.setStartAfter(span); r.collapse(true); sel.removeAllRanges(); sel.addRange(r); }
    else editor.append(span);
    haptic('light');
  }

  function renderPalette() {
    const items = state.boot?.premium_palette || [];
    const target = $('#premiumPalette');
    target.innerHTML = items.length ? items.slice(0,10).map(x => `<button class="emoji-chip">${esc(x.fallback)}</button>`).join('') : '<span class="muted">Палітра наповниться з реальних Premium emoji у постах.</span>';
  }

  function renderEditorPalette() {
    const items = state.boot?.premium_palette || [];
    const target = $('#editorPalette');
    target.innerHTML = items.length ? items.map((x,i) => `<button class="emoji-chip" data-emoji-index="${i}">${esc(x.fallback)}</button>`).join('') : '<span class="muted">Premium emoji ще не збережені в палітрі.</span>';
    $$('[data-emoji-index]', target).forEach(btn => btn.addEventListener('click', () => insertPremium(items[Number(btn.dataset.emojiIndex)])));
  }

  async function saveEditor() {
    const p = state.selectedPost; if (!p || state.busy) return;
    const html = serializeEditor($('#richEditor'));
    if (html.replace(/<[^>]+>/g,'').trim().length < 12) return toast('Текст занадто короткий', true);
    state.busy = true; $('#saveEditorButton').textContent = 'Зберігаю…';
    try {
      const updated = await api(`/api/posts/${p.id}/edit-text`, {method:'POST', body:JSON.stringify({html})});
      state.selectedPost = updated; renderPostSheet(updated); closeEditor(); notify('success'); toast('Текст збережено');
      await bootstrap(); await loadPosts();
    } catch (e) { notify('error'); toast(e.message, true); }
    finally { state.busy = false; $('#saveEditorButton').textContent = 'Зберегти'; }
  }

  function showForm({eyebrow='NEW', title, fields, submit}) {
    $('#formEyebrow').textContent = eyebrow; $('#formTitle').textContent = title; $('#formFields').innerHTML = fields; state.formHandler = submit; $('#formModal').classList.remove('hidden');
  }
  function closeForm() { $('#formModal').classList.add('hidden'); state.formHandler = null; }

  function openScheduleForm(p) {
    const now = new Date(Date.now()+60*60*1000); const pad = n => String(n).padStart(2,'0'); const value = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
    showForm({eyebrow:'SCHEDULE', title:'Запланувати пост', fields:`<div class="form-group"><label>Дата і час</label><input id="scheduleInput" class="field" type="datetime-local" value="${value}"></div>`, submit:async()=>{await api(`/api/posts/${p.id}/schedule`,{method:'POST',body:JSON.stringify({local_datetime:$('#scheduleInput').value})}); closeForm(); closeSheet(); notify('success'); toast('Пост заплановано'); await bootstrap(); setPostTab('scheduled'); await loadPosts();}});
  }

  function openRejectForm(p) {
    showForm({eyebrow:'EDITORIAL SIGNAL', title:'Відхилити новину', fields:`<div class="form-group"><label>Причина</label><textarea id="rejectInput" class="field" rows="4" placeholder="Наприклад: занадто дрібна новина, вже неактуально…"></textarea></div>`, submit:async()=>{const reason=$('#rejectInput').value.trim()||'Не підходить'; await api(`/api/posts/${p.id}/reject`,{method:'POST',body:JSON.stringify({reason})}); closeForm(); closeSheet(); notify('success'); toast('Відхилено. Шукаємо заміну'); await bootstrap(); await loadPosts();}});
  }

  async function loadChannels() {
    $('#targetsList').innerHTML = '<div class="skeleton"></div>'; $('#sourcesList').innerHTML = '<div class="skeleton"></div>';
    try { state.channels = await api('/api/channels'); renderChannels(); } catch(e) { toast(e.message,true); }
  }

  function renderChannels() {
    const c = state.channels; if (!c) return;
    $('#targetsCount').textContent = `${c.targets.length} підключено`;
    $('#targetsList').innerHTML = c.targets.length ? c.targets.map(t => `<div class="channel-card panel"><div class="channel-avatar">AP</div><div class="channel-main"><strong>${esc(t.title||t.channel_ref)}</strong><small>${esc(t.channel_ref)} · ${t.source_count} джерел</small></div><div class="channel-actions">${t.url?`<button class="mini-btn" data-open="${esc(t.url)}">↗</button>`:''}<button class="mini-btn ${t.is_default?'active':''}" data-default-target="${t.id}">${t.is_default?'✓':'○'}</button></div></div>`).join('') : '<div class="empty-state"><strong>Немає каналів</strong><span>Додай канал публікації.</span></div>';
    $('#sourcesList').innerHTML = c.sources.length ? c.sources.map(s => { const target=c.targets.find(t=>Number(t.id)===Number(s.target_id)); return `<div class="source-card panel" data-source-id="${s.id}"><div class="channel-avatar">⌁</div><div class="channel-main"><strong>@${esc(s.username)}</strong><small>→ ${esc(target?.title||'Без каналу')}</small></div><div class="channel-actions"><button class="mini-btn" data-open="${esc(s.url)}">↗</button><button class="mini-btn" data-route-source="${s.id}">⌘</button></div></div>`; }).join('') : '<div class="empty-state"><strong>Немає джерел</strong><span>Додай Telegram-канал, з якого потрібно парсити.</span></div>';
    $$('[data-open]').forEach(btn=>btn.addEventListener('click',e=>{e.stopPropagation();tg?.openTelegramLink?tg.openTelegramLink(btn.dataset.open):window.open(btn.dataset.open,'_blank')}));
    $$('[data-default-target]').forEach(btn=>btn.addEventListener('click',async()=>{try{await api(`/api/channels/targets/${btn.dataset.defaultTarget}/default`,{method:'POST'});notify('success');await loadChannels();}catch(e){toast(e.message,true)}}));
    $$('[data-route-source]').forEach(btn=>btn.addEventListener('click',()=>openRouteForm(Number(btn.dataset.routeSource))));
  }

  function openRouteForm(sourceId) {
    const s=state.channels.sources.find(x=>Number(x.id)===sourceId); if(!s)return;
    const options=state.channels.targets.map(t=>`<option value="${t.id}" ${Number(t.id)===Number(s.target_id)?'selected':''}>${esc(t.title||t.channel_ref)}</option>`).join('');
    showForm({eyebrow:'ROUTING',title:`@${s.username}`,fields:`<div class="form-group"><label>Публікувати в канал</label><select id="routeTarget" class="field">${options}</select></div>`,submit:async()=>{await api(`/api/channels/sources/${s.id}/route`,{method:'POST',body:JSON.stringify({target_id:Number($('#routeTarget').value)})});closeForm();notify('success');toast('Маршрут оновлено');await loadChannels();}})
  }

  function openAddTarget() {
    showForm({eyebrow:'PUBLICATION',title:'Новий канал',fields:`<div class="form-group"><label>@username або посилання</label><input id="targetRef" class="field" placeholder="@sports_news_ua"></div><div class="form-group"><label>Назва</label><input id="targetTitle" class="field" placeholder="SPORTS NEWS"></div>`,submit:async()=>{await api('/api/channels/targets',{method:'POST',body:JSON.stringify({channel_ref:$('#targetRef').value.trim(),title:$('#targetTitle').value.trim()})});closeForm();notify('success');toast('Канал додано');await loadChannels();}})
  }

  function openAddSource() {
    const targets=state.channels?.targets||[]; if(!targets.length)return toast('Спочатку додай канал публікації',true);
    const options=targets.map(t=>`<option value="${t.id}">${esc(t.title||t.channel_ref)}</option>`).join('');
    showForm({eyebrow:'SOURCE',title:'Нове джерело',fields:`<div class="form-group"><label>@username або t.me/...</label><input id="sourceName" class="field" placeholder="@bfootballua"></div><div class="form-group"><label>Канал призначення</label><select id="sourceTarget" class="field">${options}</select></div>`,submit:async()=>{await api('/api/channels/sources',{method:'POST',body:JSON.stringify({username:$('#sourceName').value.trim(),target_id:Number($('#sourceTarget').value)})});closeForm();notify('success');toast('Джерело додано');await loadChannels();}})
  }

  function renderSettings() {
    const b=state.boot;if(!b)return;
    $('#publishToggle').classList.toggle('on',b.modes.publish==='auto');
    $('#photoToggle').classList.toggle('on',b.modes.photo==='auto');
    $('#pauseToggle').classList.toggle('on',!!b.modes.paused);
    $('#premiumStatusText').textContent=b.system.premium_publisher_online?`ONLINE · @${b.system.publisher_username||'publisher'} · Premium`:'Publisher offline';
    const target=$('#quotaList');
    target.innerHTML=Object.entries(b.quotas||{}).map(([key,q])=>`<div class="quota-card panel" data-quota="${key}"><div class="quota-top"><div class="quota-title"><strong>${esc(q.label)}</strong><small>Використано ${q.count} з ${q.quota}</small></div><div class="stepper"><button data-q-delta="-1">−</button><span data-q-value>${q.quota}</span><button data-q-delta="1">+</button></div></div><div class="delay-row"><span>Мін. інтервал між постами</span><label><input data-delay type="number" min="0" max="360" step="15" value="${q.delay_minutes}"> хв</label></div></div>`).join('');
    $$('[data-q-delta]',target).forEach(btn=>btn.addEventListener('click',()=>changeQuota(btn.closest('[data-quota]'),Number(btn.dataset.qDelta))));
    $$('[data-delay]',target).forEach(input=>input.addEventListener('change',()=>saveQuota(input.closest('[data-quota]'))));
  }

  async function changeQuota(card,delta){const span=$('[data-q-value]',card);span.textContent=String(Math.max(0,Math.min(20,Number(span.textContent)+delta)));haptic('light');await saveQuota(card)}
  async function saveQuota(card){const key=card.dataset.quota;const quota=Number($('[data-q-value]',card).textContent);const delay=Math.max(0,Math.min(360,Number($('[data-delay]',card).value)||0));try{const r=await api(`/api/quotas/${key}`,{method:'PUT',body:JSON.stringify({quota,delay_minutes:delay})});state.boot.quotas[key].quota=r.quota;state.boot.quotas[key].delay_minutes=r.delay_minutes;toast('Ліміт збережено');}catch(e){toast(e.message,true)}}

  async function toggleMode(mode){const b=state.boot;if(!b)return;let body={};if(mode==='publish')body.publish_mode=b.modes.publish==='auto'?'manual':'auto';if(mode==='photo')body.photo_edit_mode=b.modes.photo==='auto'?'manual':'auto';if(mode==='paused')body.processing_paused=!b.modes.paused;try{await api('/api/modes',{method:'PATCH',body:JSON.stringify(body)});await bootstrap();notify('success');}catch(e){toast(e.message,true)}}

  async function toggleAnalytics(){const card=$('#analyticsCard');if(!card.classList.contains('hidden')){card.classList.add('hidden');return;}card.classList.remove('hidden');card.innerHTML='<div class="skeleton"></div>';try{const a=await api('/api/analytics');const max=Math.max(1,...a.days.map(x=>Number(x.total)||0));const bars=a.days.map(x=>`<div class="bar-item"><div class="bar" style="height:${Math.max(5,Math.round((Number(x.total)||0)/max*92))}px"></div><span class="bar-label">${esc((x.day||'').slice(5))}</span></div>`).join('');const sources=a.sources.map(x=>`<div class="source-stat"><span>@${esc(x.source)}</span><strong>${x.count}</strong></div>`).join('');card.innerHTML=`<strong style="font-size:12px">Потік за 7 днів</strong><div class="bar-chart">${bars||'<span class="muted">Поки немає даних</span>'}</div><div style="margin-top:16px"><span class="eyebrow">ТОП ДЖЕРЕЛ</span>${sources||'<span class="muted">Немає даних</span>'}</div>`;}catch(e){card.innerHTML=`<span class="muted">${esc(e.message)}</span>`}}

  function bind() {
    $$('.nav-item').forEach(b=>b.addEventListener('click',()=>go(b.dataset.nav)));
    $$('[data-go]').forEach(b=>b.addEventListener('click',()=>go(b.dataset.go,b.dataset.tab||null)));
    $$('[data-refresh]').forEach(b=>b.addEventListener('click',async()=>{try{await bootstrap();if(state.screen==='posts')await loadPosts();if(state.screen==='channels')await loadChannels();notify('success');toast('Оновлено');}catch(e){toast(e.message,true)}}));
    $$('#postTabs button').forEach(b=>b.addEventListener('click',()=>{setPostTab(b.dataset.postTab);loadPosts();haptic('light')}));
    $('#sheetBackdrop').addEventListener('click',closeSheet);
    $$('[data-close-modal]').forEach(b=>b.addEventListener('click',closeEditor));
    $$('[data-close-form]').forEach(b=>b.addEventListener('click',closeForm));
    $('#saveEditorButton').addEventListener('click',saveEditor);
    $$('.editor-toolbar [data-format]').forEach(btn=>btn.addEventListener('click',()=>{const cmd=btn.dataset.format;if(cmd==='blockquote')document.execCommand('formatBlock',false,'blockquote');else document.execCommand(cmd,false,null);$('#richEditor').focus()}));
    $('#formSubmit').addEventListener('click',async()=>{if(!state.formHandler||state.busy)return;state.busy=true;$('#formSubmit').textContent='Зберігаю…';try{await state.formHandler()}catch(e){notify('error');toast(e.message,true)}finally{state.busy=false;$('#formSubmit').textContent='Зберегти'}});
    $('#addChannelButton').addEventListener('click',openAddTarget); $('#addSourceButton').addEventListener('click',openAddSource);
    $$('[data-mode]').forEach(b=>b.addEventListener('click',()=>toggleMode(b.dataset.mode)));
    $('#analyticsButton').addEventListener('click',toggleAnalytics);
    $('#systemPill').addEventListener('click',async()=>{try{await bootstrap();toast('Статус оновлено')}catch(e){toast(e.message,true)}});
  }

  async function init() {
    bind();
    try { await bootstrap(true); }
    catch (e) { $('#loader').innerHTML=`<div class="loader-logo"></div><strong style="font-size:14px">Mini App недоступний</strong><span style="max-width:280px;text-align:center;line-height:1.5">${esc(e.message)}</span>`; return; }
  }
  init();
})();

/* ==================== PREMIUM UI ==================== */
(() => {
  'use strict';
  const tg = window.Telegram?.WebApp;
  const initData = tg?.initData || '';
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const esc = (v = '') => String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let refreshing = false;
  let lastLiveIds = '';

  const haptic = (kind = 'light') => { try { tg?.HapticFeedback?.impactOccurred(kind); } catch (_) {} };
  const notify = (kind = 'success') => { try { tg?.HapticFeedback?.notificationOccurred(kind); } catch (_) {} };

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

  function flash(message, error = false) {
    const el = $('#toast');
    if (!el) return;
    el.textContent = message;
    el.classList.toggle('error', error);
    el.classList.remove('hidden');
    clearTimeout(flash.timer);
    flash.timer = setTimeout(() => el.classList.add('hidden'), 2400);
  }

  async function confirmPublish() {
    if (tg?.showConfirm) return await new Promise(resolve => tg.showConfirm('Опублікувати цей пост зараз?', resolve));
    return window.confirm('Опублікувати цей пост зараз?');
  }

  function mediaHTML(p) {
    const m = p.media?.[0];
    if (!m) return '<div class="live-thumb placeholder"></div>';
    const media = m.type === 'video'
      ? `<video src="${esc(m.url)}" muted playsinline preload="metadata"></video>`
      : `<img src="${esc(m.url)}" loading="lazy" alt="">`;
    const multi = (p.media?.length || 0) > 1 ? `<span class="live-multi">${p.media.length}</span>` : '';
    return `<div class="live-thumb">${media}${multi}</div>`;
  }

  function liveCard(p) {
    const premium = p.premium_emoji_count ? ` · ✦ ${p.premium_emoji_count}` : '';
    const target = p.target?.title || p.target?.channel_ref || 'Канал не вибрано';
    return `<article class="live-card" data-live-id="${p.id}">
      ${mediaHTML(p)}
      <div class="live-content">
        <div class="live-meta"><span class="live-source">@${esc(p.source)}</span><span class="live-score">AI ${Number(p.score || 0)}%${premium}</span></div>
        <div class="live-text">${esc(p.text_plain || 'Готовий пост')}</div>
        <div class="live-route">→ ${esc(target)}</div>
        <div class="live-actions"><button class="live-publish" data-live-publish="${p.id}">Опублікувати</button><button class="live-detail" data-live-detail="${p.id}" aria-label="Деталі">•••</button></div>
      </div>
    </article>`;
  }

  function updateQueueNumbers(delta = null) {
    const metric = $('#mReady');
    const badge = $('#queueBadge');
    if (!metric || !badge || delta === null) return;
    const next = Math.max(0, Number(metric.textContent || 0) + delta);
    metric.textContent = String(next);
    badge.textContent = next > 99 ? '99+' : String(next);
    badge.classList.toggle('hidden', !next);
  }

  function bindLiveCards() {
    $$('[data-live-publish]').forEach(btn => btn.addEventListener('click', async e => {
      e.stopPropagation();
      const id = Number(btn.dataset.livePublish);
      if (!id || btn.classList.contains('loading')) return;
      if (!(await confirmPublish())) return;
      btn.classList.add('loading');
      btn.textContent = 'Публікую…';
      haptic('medium');
      try {
        await api(`/api/posts/${id}/publish`, {method:'POST'});
        notify('success');
        flash('Опубліковано ✓');
        const card = btn.closest('.live-card');
        card?.classList.add('removing');
        updateQueueNumbers(-1);
        setTimeout(loadLiveQueue, 360);
      } catch (err) {
        notify('error');
        flash(err.message, true);
        btn.classList.remove('loading');
        btn.textContent = 'Опублікувати';
      }
    }));
    $$('[data-live-detail]').forEach(btn => btn.addEventListener('click', e => {
      e.stopPropagation();
      openInQueue(Number(btn.dataset.liveDetail));
    }));
    $$('.live-card').forEach(card => card.addEventListener('click', e => {
      if (e.target.closest('button')) return;
      openInQueue(Number(card.dataset.liveId));
    }));
  }

  async function loadLiveQueue(force = false) {
    const root = $('#liveQueue');
    if (!root || !initData || refreshing) return;
    refreshing = true;
    const refreshBtn = $('#liveQueueRefresh');
    if (refreshBtn) refreshBtn.style.transform = 'rotate(25deg)';
    try {
      const data = await api('/api/posts?tab=ready&limit=8');
      const items = data.items || [];
      const ids = items.map(x => x.id).join(',');
      const metricTotal = Math.max(items.length, Number($('#mReady')?.textContent || 0));
      const label = $('#liveQueueCount');
      if (label) label.textContent = metricTotal ? `${metricTotal} готов${metricTotal === 1 ? 'ий' : 'і'} · live` : 'Черга чиста · live';
      if (!items.length) {
        root.innerHTML = '<div class="live-empty"><strong>Черга чиста</strong><span>Нові готові новини з’являться тут автоматично. Нічого зайвого.</span></div>';
      } else if (force || ids !== lastLiveIds || !root.querySelector('.live-card')) {
        root.innerHTML = items.slice(0, 4).map(liveCard).join('');
        bindLiveCards();
      }
      lastLiveIds = ids;
    } catch (err) {
      root.innerHTML = `<div class="live-empty"><strong>Не вдалося синхронізувати</strong><span>${esc(err.message)}</span></div>`;
    } finally {
      refreshing = false;
      if (refreshBtn) refreshBtn.style.transform = '';
    }
  }

  function openInQueue(id) {
    haptic('light');
    const nav = $('[data-nav="posts"]');
    nav?.click();
    let tries = 0;
    const timer = setInterval(() => {
      tries += 1;
      const card = $(`.post-card[data-post-id="${id}"]`);
      if (card) { clearInterval(timer); card.click(); }
      else if (tries > 18) clearInterval(timer);
    }, 100);
  }

  function enhanceQueueCards() {
    const list = $('#postsList');
    if (!list) return;
    const ready = $('#postTabs [data-post-tab="ready"]')?.classList.contains('active');
    $$('.post-card', list).forEach(card => {
      const existing = $('.queue-inline-publish', card);
      if (!ready) { existing?.remove(); card.classList.remove('has-inline-publish'); return; }
      if (existing) return;
      const id = Number(card.dataset.postId);
      if (!id) return;
      const btn = document.createElement('button');
      btn.className = 'queue-inline-publish';
      btn.type = 'button';
      btn.textContent = '✓ Опублікувати';
      btn.addEventListener('click', async e => {
        e.preventDefault(); e.stopPropagation();
        if (btn.classList.contains('loading')) return;
        if (!(await confirmPublish())) return;
        btn.classList.add('loading'); btn.textContent = 'Публікую…'; haptic('medium');
        try {
          await api(`/api/posts/${id}/publish`, {method:'POST'});
          notify('success'); flash('Опубліковано ✓'); updateQueueNumbers(-1);
          card.classList.add('removing');
          setTimeout(() => { card.remove(); loadLiveQueue(true); }, 280);
        } catch (err) {
          notify('error'); flash(err.message, true); btn.classList.remove('loading'); btn.textContent = '✓ Опублікувати';
        }
      });
      card.append(btn);
      card.classList.add('has-inline-publish');
    });
  }

  function installObservers() {
    const list = $('#postsList');
    if (list) new MutationObserver(() => requestAnimationFrame(enhanceQueueCards)).observe(list, {childList:true, subtree:true});
    const tabs = $('#postTabs');
    if (tabs) tabs.addEventListener('click', () => setTimeout(enhanceQueueCards, 120));

    if ('IntersectionObserver' in window) {
      const io = new IntersectionObserver(entries => entries.forEach(entry => {
        if (entry.isIntersecting) { entry.target.classList.add('lux-reveal'); io.unobserve(entry.target); }
      }), {threshold:.08});
      $$('.panel').forEach(el => io.observe(el));
    }

    const toast = $('#toast');
    if (toast) new MutationObserver(() => {
      if ((toast.textContent || '').includes('Опубліковано')) setTimeout(() => loadLiveQueue(true), 300);
    }).observe(toast, {childList:true, characterData:true, subtree:true});
  }

  function bind() {
    $('#liveQueueRefresh')?.addEventListener('click', () => { haptic('light'); loadLiveQueue(true); });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) loadLiveQueue(true); });
    window.addEventListener('focus', () => loadLiveQueue(false));
  }

  async function start() {
    if (!initData) return;
    bind();
    installObservers();
    await new Promise(resolve => setTimeout(resolve, 260));
    loadLiveQueue(true);
    enhanceQueueCards();
    setInterval(() => {
      if (!document.hidden && $('#screen-home')?.classList.contains('active')) loadLiveQueue(false);
    }, 20000);
  }

  start();
})();

/* ==================== MEDIA CAROUSEL ==================== */
(() => {
  'use strict';

  const tg = window.Telegram?.WebApp;
  const initData = tg?.initData || '';
  const AUTOPLAY_MS = 5000;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const esc = (value = '') => String(value).replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[char]));

  let pendingPostId = null;
  let controller = null;
  let renderToken = 0;

  const haptic = (kind = 'light') => {
    try { tg?.HapticFeedback?.impactOccurred(kind); } catch (_) {}
  };

  async function api(path) {
    const response = await fetch(path, {
      headers: {'X-Telegram-Init-Data': initData},
      cache: 'no-store',
    });
    let data = null;
    try { data = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(data?.detail || `HTTP ${response.status}`);
    return data;
  }

  function rememberPostFromEvent(event) {
    const postCard = event.target.closest?.('.post-card[data-post-id]');
    if (postCard) {
      pendingPostId = Number(postCard.dataset.postId) || null;
      return;
    }
    const live = event.target.closest?.('[data-live-id], [data-live-detail]');
    if (live) {
      pendingPostId = Number(live.dataset.liveId || live.dataset.liveDetail) || null;
    }
  }

  function mediaNode(item, index) {
    const type = String(item.type || 'photo').toLowerCase();
    const url = esc(item.url || '');
    if (type === 'video') {
      return `<div class="lux-carousel-slide" data-slide="${index}" aria-hidden="${index ? 'true' : 'false'}">
        <video src="${url}" muted playsinline preload="metadata" controls></video>
      </div>`;
    }
    return `<div class="lux-carousel-slide" data-slide="${index}" aria-hidden="${index ? 'true' : 'false'}">
      <img src="${url}" alt="Медіа ${index + 1}" draggable="false" decoding="async">
    </div>`;
  }

  function destroyCarousel() {
    if (controller) {
      controller.destroy();
      controller = null;
    }
  }

  function createCarousel(host, post) {
    destroyCarousel();

    const items = Array.isArray(post.media) ? post.media.filter(item => item?.url) : [];
    if (!items.length) return;

    const multi = items.length > 1;
    host.className = 'sheet-media lux-media-carousel';
    host.dataset.carouselReady = '1';
    host.innerHTML = `
      <div class="lux-carousel-stage">
        ${items.map(mediaNode).join('')}
        <div class="lux-carousel-shade" aria-hidden="true"></div>
        ${multi ? `<div class="lux-carousel-counter"><span data-current>1</span><i>/</i><span>${items.length}</span></div>` : ''}
        ${multi ? `<div class="lux-carousel-progress" aria-label="Автоперегортання кожні 5 секунд"><span class="lux-progress-track"><i data-progress-fill></i></span><small>5s</small></div>` : ''}
        ${multi ? '<div class="lux-swipe-hint" aria-hidden="true">‹ swipe ›</div>' : ''}
      </div>`;

    const slides = $$('.lux-carousel-slide', host);
    const currentEl = $('[data-current]', host);
    const fill = $('[data-progress-fill]', host);
    let index = 0;
    let timer = null;
    let destroyed = false;
    let touchStartX = null;
    let touchStartY = null;

    const stopTimer = () => {
      if (timer) clearTimeout(timer);
      timer = null;
    };

    const restartProgress = () => {
      if (!fill || !multi) return;
      fill.style.animation = 'none';
      void fill.offsetHeight;
      fill.style.animation = `luxMediaClock ${AUTOPLAY_MS}ms linear forwards`;
    };

    const schedule = () => {
      stopTimer();
      if (!multi || destroyed || document.hidden || $('#postSheet')?.classList.contains('hidden')) return;
      restartProgress();
      timer = setTimeout(() => show(index + 1, false), AUTOPLAY_MS);
    };

    const show = (nextIndex, manual = true) => {
      if (destroyed || !slides.length) return;
      index = (nextIndex + slides.length) % slides.length;
      slides.forEach((slide, slideIndex) => {
        const active = slideIndex === index;
        slide.classList.toggle('active', active);
        slide.setAttribute('aria-hidden', active ? 'false' : 'true');
        const video = $('video', slide);
        if (video && !active) {
          try { video.pause(); } catch (_) {}
        }
      });
      if (currentEl) currentEl.textContent = String(index + 1);
      if (manual) haptic('light');
      schedule();
    };

    const onTouchStart = event => {
      const touch = event.touches?.[0];
      if (!touch) return;
      touchStartX = touch.clientX;
      touchStartY = touch.clientY;
    };

    const onTouchEnd = event => {
      if (touchStartX === null || touchStartY === null) return;
      const touch = event.changedTouches?.[0];
      if (!touch) return;
      const dx = touch.clientX - touchStartX;
      const dy = touch.clientY - touchStartY;
      touchStartX = null;
      touchStartY = null;
      if (Math.abs(dx) < 42 || Math.abs(dx) < Math.abs(dy) * 1.2) return;
      show(index + (dx < 0 ? 1 : -1), true);
    };

    const stage = $('.lux-carousel-stage', host);
    stage?.addEventListener('touchstart', onTouchStart, {passive: true});
    stage?.addEventListener('touchend', onTouchEnd, {passive: true});

    const onWheel = event => {
      if (!multi || Math.abs(event.deltaX) < 16 || Math.abs(event.deltaX) < Math.abs(event.deltaY)) return;
      event.preventDefault();
      show(index + (event.deltaX > 0 ? 1 : -1), true);
    };
    stage?.addEventListener('wheel', onWheel, {passive: false});

    const onVisibility = () => {
      if (document.hidden) stopTimer();
      else schedule();
    };
    document.addEventListener('visibilitychange', onVisibility);

    const firstImage = $('img', slides[0]);
    if (firstImage) {
      const markReady = () => host.classList.add('media-ready');
      if (firstImage.complete) markReady();
      else firstImage.addEventListener('load', markReady, {once: true});
    } else {
      host.classList.add('media-ready');
    }

    slides[0]?.classList.add('active');
    schedule();

    controller = {
      destroy() {
        destroyed = true;
        stopTimer();
        document.removeEventListener('visibilitychange', onVisibility);
        stage?.removeEventListener('touchstart', onTouchStart);
        stage?.removeEventListener('touchend', onTouchEnd);
        stage?.removeEventListener('wheel', onWheel);
      }
    };
  }

  async function upgradeSheet() {
    const sheet = $('#postSheet');
    const host = $('#postSheetContent .sheet-media');
    if (!sheet || sheet.classList.contains('hidden') || !host || host.dataset.carouselReady === '1') return;
    if (!pendingPostId || !initData) {
      host.classList.add('lux-media-safe');
      return;
    }

    const token = ++renderToken;
    try {
      const post = await api(`/api/posts/${pendingPostId}`);
      if (token !== renderToken || sheet.classList.contains('hidden')) return;
      createCarousel(host, post);
    } catch (_) {
      host.classList.add('lux-media-safe');
    }
  }

  function install() {
    document.addEventListener('click', rememberPostFromEvent, true);

    const sheetContent = $('#postSheetContent');
    if (sheetContent) {
      new MutationObserver(() => requestAnimationFrame(upgradeSheet)).observe(sheetContent, {
        childList: true,
        subtree: true,
      });
    }

    const sheet = $('#postSheet');
    if (sheet) {
      new MutationObserver(() => {
        if (sheet.classList.contains('hidden')) {
          renderToken += 1;
          destroyCarousel();
          pendingPostId = null;
        } else {
          requestAnimationFrame(upgradeSheet);
        }
      }).observe(sheet, {attributes: true, attributeFilter: ['class']});
    }
  }

  install();
})();

/* ==================== WORKSPACE ==================== */
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

/* ==================== WEB & AI CONTROL ==================== */
(() => {
  'use strict';
  const tg = window.Telegram?.WebApp;
  const initData = tg?.initData || '';
  if (!initData) return;
  const $ = (s, root=document) => root.querySelector(s);
  const haptic = (kind='light') => { try { tg?.HapticFeedback?.impactOccurred(kind); } catch (_) {} };
  const notify = (kind='success') => { try { tg?.HapticFeedback?.notificationOccurred(kind); } catch (_) {} };

  async function api(path, options={}) {
    const headers = new Headers(options.headers || {});
    headers.set('X-Telegram-Init-Data', initData);
    if (options.body) headers.set('Content-Type','application/json');
    const res = await fetch(path,{...options,headers,cache:'no-store'});
    let data=null; try{data=await res.json()}catch(_){ }
    if(!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
    return data;
  }

  function toast(text,error=false){
    const el=$('#toast'); if(!el)return;
    el.textContent=text; el.classList.toggle('error',error); el.classList.remove('hidden');
    clearTimeout(toast.t); toast.t=setTimeout(()=>el.classList.add('hidden'),2600);
  }

  function panelHTML(s){
    const keyLabel=s.openai?.custom?'Власний API key':'Спільний Railway key';
    const masked=s.openai?.masked || 'Railway / shared';
    return `<div class="external-ai-panel" id="externalAiPanel">
      <div class="x-head"><div><strong>Web Intelligence</strong><small>Зовнішні джерела, матчі та персональний AI</small></div><span class="x-live">AI ROUTING</span></div>
      <div class="x-setting"><div class="copy"><strong>Зовнішні web-новини</strong><small>Порівняння незалежних спортивних джерел і WebRank</small></div><button class="x-toggle ${s.web_enabled?'on':''}" data-x-toggle="web"><i></i></button></div>
      <div class="x-setting"><div class="copy"><strong>Щоденні топ-матчі</strong><small>До 5 найсильніших матчів дня з київським часом</small></div><button class="x-toggle ${s.daily_fixtures_enabled?'on':''}" data-x-toggle="fixtures"><i></i></button></div>
      <div class="x-grid">
        <label class="x-mini"><span>Мін. WebRank</span><input id="xWebRank" type="number" min="40" max="95" step="3" value="${Number(s.web_min_score||62)}"></label>
        <label class="x-mini"><span>Web-постів / день</span><input id="xWebDaily" type="number" min="1" max="20" step="1" value="${Number(s.web_max_per_day||8)}"></label>
      </div>
      <div class="x-key-box">
        <div class="x-key-status"><b>${keyLabel}</b><code>${masked}</code></div>
        <div class="x-key-row"><input id="xOpenAiKey" type="password" autocomplete="off" placeholder="sk-..."><button id="xSaveKey" type="button">Зберегти</button></div>
        ${s.openai?.custom?'<button id="xRemoveKey" class="x-remove-key" type="button">Видалити власний key і повернути shared</button>':''}
        <div class="x-note">Ключ не повертається в Mini App після збереження. У базі він зберігається зашифровано окремо для твого workspace.</div>
      </div>
    </div>`;
  }

  async function load(){
    const screen=$('#screen-settings'); if(!screen)return;
    let host=$('#externalAiPanel');
    try{
      const s=await api('/api/external-control');
      if(host) host.outerHTML=panelHTML(s);
      else {
        const automation=screen.querySelector('.settings-group.panel');
        automation?.insertAdjacentHTML('afterend',panelHTML(s));
      }
      bind();
    }catch(e){toast(e.message,true)}
  }

  async function patch(body){
    try{await api('/api/external-control',{method:'PATCH',body:JSON.stringify(body)});notify('success');haptic('light');await load();toast('Налаштування збережено');}
    catch(e){notify('error');toast(e.message,true)}
  }

  function bind(){
    $('#externalAiPanel [data-x-toggle="web"]')?.addEventListener('click',e=>patch({web_enabled:!e.currentTarget.classList.contains('on')}));
    $('#externalAiPanel [data-x-toggle="fixtures"]')?.addEventListener('click',e=>patch({daily_fixtures_enabled:!e.currentTarget.classList.contains('on')}));
    $('#xWebRank')?.addEventListener('change',e=>patch({web_min_score:Math.max(40,Math.min(95,Number(e.target.value)||62))}));
    $('#xWebDaily')?.addEventListener('change',e=>patch({web_max_per_day:Math.max(1,Math.min(20,Number(e.target.value)||8))}));
    $('#xSaveKey')?.addEventListener('click',async()=>{
      const input=$('#xOpenAiKey'); const key=(input?.value||'').trim();
      if(key.length<20)return toast('Встав повний OpenAI API key',true);
      try{input.disabled=true;await api('/api/openai-key',{method:'PUT',body:JSON.stringify({api_key:key})});input.value='';notify('success');await load();toast('Власний OpenAI key підключено');}
      catch(e){notify('error');toast(e.message,true)}finally{if(input)input.disabled=false}
    });
    $('#xRemoveKey')?.addEventListener('click',async()=>{
      try{await api('/api/openai-key',{method:'DELETE'});notify('success');await load();toast('Повернуто shared API key');}
      catch(e){notify('error');toast(e.message,true)}
    });
  }

  function install(){
    const screen=$('#screen-settings'); if(!screen)return;
    new MutationObserver(()=>{if(screen.classList.contains('active')&&!$('#externalAiPanel'))load()}).observe(screen,{attributes:true,attributeFilter:['class']});
    document.addEventListener('click',e=>{if(e.target.closest?.('[data-nav="settings"]'))setTimeout(load,120)},true);
    if(screen.classList.contains('active'))load();
  }

  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',install);else install();
})();
