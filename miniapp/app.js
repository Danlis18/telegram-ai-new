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
    const online = b.system.reader_online && b.system.premium_publisher_online;
    $('#systemPill').classList.toggle('online', online);
    $('#systemPill span').textContent = online ? 'ONLINE' : 'CHECK';
    setStateBadge($('#readerState'), b.system.reader_online, b.system.reader_online ? 'ONLINE' : 'OFFLINE');
    setStateBadge($('#publisherState'), b.system.premium_publisher_online, b.system.premium_publisher_online ? 'PREMIUM' : 'OFFLINE');
    $('#publisherDesc').textContent = b.system.publisher_username ? `@${b.system.publisher_username}` : 'Публікація з Premium emoji';
    $('#publishModeState').textContent = (b.modes.publish || 'manual').toUpperCase();
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