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
