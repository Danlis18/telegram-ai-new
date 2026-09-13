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

(() => {
  if (!document.querySelector('link[data-external-ai-style]')) {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = './external-control.css?v=1';
    link.dataset.externalAiStyle = '1';
    document.head.appendChild(link);
  }
  if (!document.querySelector('script[data-external-ai-script]')) {
    const script = document.createElement('script');
    script.src = './external-control.js?v=1';
    script.defer = true;
    script.dataset.externalAiScript = '1';
    document.head.appendChild(script);
  }
})();