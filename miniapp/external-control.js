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