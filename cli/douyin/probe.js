(() => {
  // CSS-module semantic prefixes, never their build-dependent hash suffixes.
  const cls = prefix => `[class^="${prefix}-"], [class*=" ${prefix}-"]`;
  const one = selector => {
    const es=[...document.querySelectorAll(selector)].filter(visible);
    return es.length===1 ? es[0] : null;
  };
  const byClass = prefix => one(cls(prefix));
  const semantic = (selector, hidden=false) => {
    const es=[...document.querySelectorAll(selector)].filter(e=>hidden||visible(e));
    if(es.length!==1)return null;
    return {target:selector, stable:true, enabled:!es[0].matches(':disabled') && es[0].getAttribute('aria-disabled')!=='true'};
  };
  const button = names => {
    const es=[...document.querySelectorAll('button')].filter(e=>visible(e)&&names.includes(label(e)));
    return es.length===1 ? {target:{selector:'button',text:label(es[0]),exact:true},stable:true,
      enabled:!es[0].disabled && es[0].getAttribute('aria-disabled')!=='true'} : null;
  };
  const text=document.body?.innerText||'';
  const editor=one('[data-slate-editor="true"][contenteditable="true"]');
  const plain=s=>String(s||'').replace(/\u200b/g,'').replace(/\r\n/g,'\n').replace(/\n+$/,'');
  const accountNode=byClass('unique_id');
  const account=accountNode ? clean(accountNode.innerText).replace(/^(抖音号|Douyin ID)\s*[:：]\s*/i,'') : null;
  // Title has a 30-character counter as its suffix; no translated placeholder needed.
  const titleInputs=[...document.querySelectorAll('input[type="text"]')].filter(e=>visible(e)&&
    /\/\s*30\s*$/.test(clean(e.parentElement?.innerText)));
  const title=titleInputs.length===1?titleInputs[0]:null;
  const titleSelector='input[type="text"]';
  const titleTarget=title ? {target:{selector:titleSelector,name:title.getAttribute('placeholder'),exact:true},stable:true,enabled:!title.disabled}:null;
  const progress=byClass('upload-card')||byClass('uploading-container');
  const previewVideo=[...document.querySelectorAll('video')].find(e=>visible(e)&&e.getAttribute('src')?.startsWith('https://'));
  const alerts=[...document.querySelectorAll('[role="alert"],.semi-toast-content,.semi-form-field-error-message')]
    .filter(visible).map(e=>clean(e.innerText)).filter(Boolean);
  const dialog=[...document.querySelectorAll('[role="dialog"],.semi-modal-content')].find(visible);
  const continueDraft=semantic(cls('continue'));
  // Only read whitelisted draft fields; never expose cookies or storage tokens.
  const drafts=[];
  try {
    for(const key of Object.keys(localStorage).filter(k=>/^publish_form_cache:\d+$/.test(k))) {
      const c=JSON.parse(localStorage.getItem(key)||'{}')?.cache;
      if(c?.longFileInfo?.vid) drafts.push({uid:key.split(':')[1],vid:c.longFileInfo.vid,
        file:{name:c.longFileInfo.name,size:c.longFileInfo.size,lastModified:c.longFileInfo.lastModified},
        title:c.itemTitle||'',description:c.textResult?.text||'',visibility:String(c.visibilityType),
        uploaded:c.longStatus===2});
    }
  } catch {}
  const posts=[...document.querySelectorAll('a[href*="/video/"],a[href*="modal_id="],a[href*="/work-detail/"]')]
    .filter(visible).map(e=>({url:e.href,id:e.href.match(/(?:\/video\/|modal_id=|\/work-detail\/)(\d+)/)?.[1],
      text:clean(e.innerText||e.parentElement?.innerText)})).filter(p=>p.id);
  const permission=[...document.querySelectorAll('input[type="checkbox"]')].filter(e=>visible(e)&&e.value==='2');
  const visibilityRoot=permission.length===1?permission[0].parentElement.parentElement:null;
  const visibilityActions={};
  for(const input of visibilityRoot?.querySelectorAll('input[type="checkbox"]')||[]) {
    const parent=input.closest('label')||input.parentElement;
    if(['0','1','2'].includes(input.value))visibilityActions[input.value]={
      target:{selector:'input[type="checkbox"]',name:clean(parent.innerText),exact:true},stable:true,enabled:!input.disabled,checked:input.checked};
  }
  const scheduleInput=one('input[format="yyyy-MM-dd HH:mm"]');
  const scheduleOption=[...document.querySelectorAll('input[type="checkbox"]')].find(e=>visible(e)&&clean((e.closest('label')||e.parentElement).innerText)==='定时发布');
  const contentItems=[...document.querySelectorAll('div[class]')].filter(e=>[...e.classList].some(c=>/^video-card-[A-Za-z0-9_]+$/.test(c)) && e.querySelector('[class^="info-title-text-"]'))
    .filter(e=>!e.parentElement.closest('[class^="video-card-"]')).map(e=>({text:clean(e.querySelector('[class^="info-title-text-"]')?.innerText),body:clean(e.innerText)}));
  return {url:location.href,host:location.hostname,path:location.pathname,account,
    loggedIn:!!document.querySelector('#header-avatar'),
    loginRequired:/\/passport|\/login/.test(location.pathname)||(!document.querySelector('#header-avatar') && /扫码登录|验证码登录|请先登录|手机号登录|Sign in|Log in/i.test(text))||(!document.querySelector('#header-avatar')&&
      !!first(document,['input[type="password"]','input[autocomplete="tel"]','[class*="login-panel"]','[class*="login-container"]'])),
    challenge:!!first(document,['iframe[src*="captcha"]','[id*="captcha_container"]','[class*="captcha_verify_container"]']),
    title:title?.value, description:editor?plain(editor.innerText):null,editor:!!editor,
    uploaded:!!editor&&!!previewVideo&&!progress,
    progress:clean(progress?.innerText),alerts,dialog:dialog?clean(dialog.innerText):null,
    rejected:alerts.some(s=>/失败|错误|不支持|异常|failed|error/i.test(s)),
    draftAvailable:!!continueDraft,drafts,posts,visibilityActions,
    contentReady:/\/content\/manage/.test(location.pathname)&&!!first(document,['[class^="manage-v2-card-"]']),listEnd:/没有更多作品|暂无作品|没有相关作品|暂无数据/.test(text),
    total:Number(text.match(/作品\s*[（(](\d+)[）)]/)?.[1])||null,
    scheduleValue:scheduleInput?.value||null, scheduleChecked:!!scheduleOption?.checked, contentItems,
    actions:{schedule:{target:{selector:'input[type="checkbox"]',name:'定时发布',exact:true},enabled:true},
      immediate:{target:{selector:'input[type="checkbox"]',name:'立即发布',exact:true},enabled:true},
      scheduleInput:scheduleInput?{target:'input[format="yyyy-MM-dd HH:mm"]',enabled:true}:null,upload:semantic('input[type="file"][accept*="video"]',true),title:titleTarget,
      description:semantic('[data-slate-editor="true"][contenteditable="true"]'),
      draft:button(['暂存离开','保存草稿','Save draft']),post:button(['发布','Publish']),
      continueDraft},
    text:text.slice(-3500)};
})()
