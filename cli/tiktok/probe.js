(() => {
  // Read-only DOM adapter (TikTok Studio). Shared helpers — visible, clean,
  // label, control, first, locate — come from cli/common/assets/dom_helpers.js.
  // It keys off URLs, data-e2e attributes, the design system's semantic class
  // names (TUXModal, Switch__content, Select__item) and ARIA, never UI text, so
  // it reads the same in every UI language.
  const path=location.pathname;
  const e2e=id=>`[data-e2e="${id}"]`;
  const box=id=>document.querySelector(e2e(id));
  // A semantic selector, used only when it names exactly one visible element.
  const named=(e,sel)=>{
    if(!e)return null;
    const c=control(e);
    const hits=[...document.querySelectorAll(sel)].filter(visible);
    return hits.length===1&&hits[0]===e?{...c,target:sel,stable:true}:c;
  };

  // The signed-in account: the app context first, the page's own profile links second.
  let handle=null;
  try { handle=window.__Creator_Center_Context__?.commonAppContext?.user?.uniqueId||null; } catch (e) { handle=null; }
  const videoLinks=[...document.querySelectorAll('a[href*="/video/"],a[href*="/photo/"]')];
  if(!handle){
    const m=videoLinks.map(a=>a.getAttribute('href').match(/^\/@([^/]+)\//)).find(Boolean);
    handle=m?m[1]:null;
  }

  // A modal the page opened (confirmations, "continue editing?", errors).
  const modals=[...document.querySelectorAll('.TUXModal,[role="dialog"][aria-modal="true"]')].filter(visible).map(m=>({
    text:clean(m.innerText).slice(0,600),
    // Two button systems are in use: data-type="…" and TUXButton--… classes.
    primary:control(m.querySelector('button[data-type="primary"],button.TUXButton--primary')),
    neutral:control(m.querySelector('button[data-type="neutral"],button[data-type="secondary"],button.TUXButton--secondary')),
    buttons:[...m.querySelectorAll('button')].filter(visible).length
  }));
  // A product tour (react-joyride) covers the page with an overlay; Escape closes it.
  const tour=[...document.querySelectorAll('.react-joyride__overlay,.react-joyride__tooltip,.__floater__open')].some(visible);

  let upload=null;
  if(/^\/tiktokstudio\/upload/.test(path)){
    const file=document.querySelector('input[type="file"][accept*="video"]')||document.querySelector('input[type="file"]');
    const status=box('upload_status_container');
    const captionBox=box('caption_container');
    const editor=captionBox?.querySelector('[contenteditable="true"]');
    const popover=[...document.querySelectorAll('.mention-list-popover')].find(visible);
    const items=popover?[...popover.querySelectorAll('.hashtag-suggestion-item,[id^="mention-option"]')].filter(visible):[];
    const visibility=box('video_visibility_container');
    const visTrigger=visibility?.querySelector('button[role="combobox"]');
    const options=[...document.querySelectorAll('[role="option"][data-value]')].filter(visible);
    const advanced=box('advanced_settings_container');
    const aigc=box('aigc_container');
    const aigcInput=aigc?.querySelector('input[type="checkbox"]');
    const post=box('post_video_button');
    const draft=box('save_draft_button');
    // The upload card's state is a class on .info-status (e.g. success); .info-progress is its bar.
    const state=status?.querySelector('.info-status');
    const stateClass=state?[...state.classList].filter(c=>c!=='info-status'&&!/^jsx-/.test(c)):[];
    const bar=status?.querySelector('.info-progress,[role="progressbar"]');
    const percent=bar?(bar.getAttribute('aria-valuenow')||(bar.style.width||'').replace('%','')||null):null;
    const counter=captionBox?(clean(captionBox.innerText).match(/(\d[\d,]*)\s*\/\s*(\d[\d,]*)/)||null):null;
    upload={
      fileInput:file?{target:locate(file).selector,accept:file.accept,multiple:file.multiple}:null,
      selectStage:!!box('select_video_container')&&!status,
      status:status?{text:clean(status.innerText).slice(0,300),state:stateClass,
        uploaded:stateClass.includes('success'),failed:stateClass.some(c=>/fail|error/i.test(c)),
        percent:percent===null?null:Number(percent)}:null,
      caption:editor?{...named(editor,`${e2e('caption_container')} [contenteditable="true"]`),value:editor.innerText,
        tags:[...editor.querySelectorAll('[data-testid="mentionText"],.mention')].map(m=>m.innerText),
        length:counter?Number(counter[1].replace(/,/g,'')):null,limit:counter?Number(counter[2].replace(/,/g,'')):null}:null,
      suggestions:popover?{items:items.map(i=>clean((i.innerText||'').split('\n')[0])),
        focused:items.findIndex(i=>i.classList.contains('focused')||i.getAttribute('aria-selected')==='true')}:null,
      visibility:visTrigger?{...named(visTrigger,`${e2e('video_visibility_container')} button[role="combobox"]`),open:visTrigger.getAttribute('aria-expanded')==='true',text:clean(visTrigger.innerText),
        options:options.map(o=>({value:String(o.getAttribute('data-value')).replace(/"/g,''),selected:o.getAttribute('aria-selected')==='true',
          target:`[role="option"][data-value='${o.getAttribute('data-value')}']`,text:clean(o.innerText)}))}:null,
      advanced:advanced?{collapsed:advanced.classList.contains('collapsed'),
        toggle:named(advanced.querySelector('.more-btn'),`${e2e('advanced_settings_container')} .more-btn`)}:null,
      aigc:aigcInput?{checked:aigcInput.checked,disabled:aigcInput.disabled,
        toggle:named(aigc.querySelector('.Switch__content'),`${e2e('aigc_container')} .Switch__content`)}:null,
      post:post?{...control(post),enabled:post.getAttribute('aria-disabled')!=='true'&&post.getAttribute('data-disabled')!=='true'&&!post.disabled,
        loading:post.getAttribute('data-loading')==='true'}:null,
      draft:draft?{...control(draft),enabled:draft.getAttribute('aria-disabled')!=='true'&&draft.getAttribute('data-disabled')!=='true'&&!draft.disabled,
        loading:draft.getAttribute('data-loading')==='true'}:null
    };
  }

  // Posts as TikTok Studio's content table lists them, newest first.
  const posts=videoLinks.map(a=>{
    const m=a.getAttribute('href').match(/^\/@([^/]+)\/(video|photo)\/(\d+)/);
    if(!m)return null;
    const row=a.closest('[data-tt="components_RowLayout_FlexRow"]')||a.parentElement;
    const privacy=row?.querySelector('[data-tt="components_PrivacyCell_TUXButton"],button[role="combobox"]');
    return {id:m[3],author:m[1],kind:m[2],text:a.innerText.trim(),privacy:privacy?clean(privacy.innerText):null,
      row:clean(row?.innerText||'').slice(0,300)};
  }).filter(Boolean);
  const content=/^\/tiktokstudio\/content/.test(path)&&new URLSearchParams(location.search).get('tab')!=='draft'?{ready:posts.length>0||!!document.querySelector('[data-tt^="components_PostTable"]'),
    empty:!posts.length&&!!document.querySelector('[data-tt*="Empty" i],[class*="empty" i]')}:null;

  // Drafts (content?tab=draft) have no links or ids; each row shows its caption.
  const drafts=[...document.querySelectorAll('[data-tt="components_DraftRowLayout_FlexRow"]')]
    .map(r=>r.querySelector('[data-tt="components_DraftCells_TruncateText"]')).filter(Boolean)
    .map(t=>({text:t.innerText.trim(),row:clean(t.closest('[data-tt="components_DraftRowLayout_FlexRow"]').innerText).slice(0,300)}));
  const draftPage=/^\/tiktokstudio\/content/.test(path)&&new URLSearchParams(location.search).get('tab')==='draft';
  // The content page's header tabs are Posts and Drafts, each with its count.
  const tabs=[...document.querySelectorAll('[data-tt="Header_HeaderTabBar_Container"]')];
  const countOf=t=>{const m=t&&clean(t.innerText).match(/(\d[\d,]*)\s*$/);return m?Number(m[1].replace(/,/g,'')):null;};
  const draftCount=tabs.length===2?countOf(tabs[1]):null;
  const draftList=draftPage?{count:draftCount,ready:draftCount!==null&&(draftCount===0||drafts.length>=Math.min(draftCount,1)),
    empty:draftCount===0}:null;

  const alerts=[...document.querySelectorAll('[role="alert"],.TUXTopToast,[class*="Toast" i]')].filter(visible)
    .map(t=>clean(t.innerText)).filter(Boolean).slice(0,5);
  const body=clean(document.body?.innerText);
  return {
    url:location.href,host:location.hostname,path,ready:document.readyState,
    loggedIn:!!handle,handle,
    loginRequired:!handle&&(/^\/(login|signup)\b/.test(path)||
      !!document.querySelector('[data-e2e="top-login-button"],[data-e2e="login-button"],form[action*="login"]')),
    challenge:[...document.querySelectorAll('[id*="captcha" i],[class*="captcha" i],iframe[src*="captcha" i]')].some(visible),
    tour,modals,upload,content,posts,draftList,drafts,alerts,
    preview:body.slice(0,300)
  };
})()
