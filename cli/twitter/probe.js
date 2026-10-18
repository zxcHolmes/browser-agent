(() => {
  // Read-only DOM adapter (x.com). Shared helpers — visible, clean, label,
  // control, first, locate — come from cli/common/assets/dom_helpers.js.
  // Everything here keys off URLs, data-testid and ARIA roles, never UI text,
  // so it works the same in every UI language.
  const path=location.pathname;
  const tid=id=>`[data-testid="${id}"]`;
  const profileLink=first(document,[tid('AppTabBar_Profile_Link')]);
  const handle=(profileLink?.getAttribute('href')||'').split('/').filter(Boolean)[0]||null;

  // Text as written: emoji are <img alt="…">, external links are t.co anchors
  // showing a shortened URL — keep the former, drop the latter.
  const textOf=root=>{
    let out='';
    const walk=n=>{
      if(n.nodeType===3){out+=n.nodeValue;return;}
      if(n.nodeType!==1)return;
      if(n.tagName==='IMG'){out+=n.getAttribute('alt')||'';return;}
      if(n.tagName==='A'&&/^https?:\/\/t\.co\//.test(n.href)){out+=' ';return;}
      if(n.tagName==='BR'){out+='\n';return;}
      for(const c of n.childNodes)walk(c);
    };
    if(root)walk(root);
    return out;
  };

  const dialogs=[...document.querySelectorAll('[role="dialog"]')].filter(visible);
  const composer=dialogs.find(d=>d.querySelector(tid('tweetTextarea_0')))||null;
  const scopes=composer?['[role="dialog"]']:[];
  let compose=null;
  if(composer){
    const box=composer.querySelector(tid('tweetTextarea_0'));
    const file=composer.querySelector(`input[type="file"]${tid('fileInput')}`)||composer.querySelector('input[type="file"]');
    const att=composer.querySelector(tid('attachments'));
    const videos=att?att.querySelectorAll('video').length:0;
    const images=att?[...att.querySelectorAll('img')].filter(i=>!i.closest('video')&&visible(i)).length:0;
    const bars=att?[...att.querySelectorAll('[role="progressbar"]')]:[];
    const uploading=bars.some(b=>{const v=Number(b.getAttribute('aria-valuenow'));return visible(b)&&!(v>=100);});
    compose={
      text:box?{...control(box,scopes),value:box.innerText}:null,
      // An open hashtag/mention suggestion list covers the Post button.
      autocomplete:!!openPopup(box),
      fileInput:file?{target:locate(file,scopes).selector,accept:file.accept,multiple:file.multiple}:null,
      media:{images,videos,total:images+videos,uploading,progress:bars.map(b=>b.getAttribute('aria-valuenow'))},
      post:control(composer.querySelector(tid('tweetButton')),scopes),
      alerts:[...composer.querySelectorAll('[role="alert"]')].filter(visible).map(a=>clean(a.innerText)).filter(Boolean)
    };
  }

  const toasts=[...document.querySelectorAll(`${tid('toast')},[role="alert"]`)].filter(visible)
    .filter(t=>!composer||!composer.contains(t))
    .map(t=>{const a=t.querySelector('a[href*="/status/"]');const m=a?.getAttribute('href')?.match(/\/([^/]+)\/status\/(\d+)/);
      return {text:clean(t.innerText).slice(0,500),author:m?.[1]||null,statusId:m?.[2]||null};});

  const sheet=first(document,[tid('confirmationSheetDialog')]);
  const column=first(document,[tid('primaryColumn')])||document;
  const posts=[...column.querySelectorAll(`article${tid('tweet')}`)].map(a=>{
    const time=a.querySelector('time');
    const link=time?.closest('a[href*="/status/"]');
    const m=link?.getAttribute('href')?.match(/^\/([^/]+)\/status\/(\d+)/);
    if(!m)return null;
    const body=a.querySelector(tid('tweetText'));
    return {id:m[2],author:m[1],datetime:time.getAttribute('datetime'),
      text:textOf(body).replace(/[ \t]+\n/g,'\n').trim(),
      truncated:!!a.querySelector(tid('tweet-text-show-more-link')),
      socialContext:!!a.querySelector(tid('socialContext')),
      images:a.querySelectorAll(tid('tweetPhoto')).length,
      video:!!a.querySelector(`${tid('videoPlayer')},${tid('videoComponent')}`)};
  }).filter(Boolean);

  const body=clean(document.body?.innerText);
  return {
    url:location.href,host:location.hostname,path,ready:document.readyState,
    loggedIn:!!handle,handle,
    loginRequired:!handle&&(/^\/(i\/flow\/(login|signup)|login|signup)\b/.test(path)||
      !!document.querySelector(`${tid('loginButton')},${tid('signupButton')},input[autocomplete="username"]`)),
    accountLocked:/^\/account\/access\b/.test(path),
    challenge:!!document.querySelector('iframe[src*="arkoselabs"],iframe[src*="recaptcha"],iframe[src*="captcha"]'),
    composer:compose,
    toasts,
    sheet:sheet?{text:clean(sheet.innerText).slice(0,500),confirm:control(sheet.querySelector(tid('confirmationSheetConfirm'))),
      cancel:control(sheet.querySelector(tid('confirmationSheetCancel')))}:null,
    timeline:{ready:!!column.querySelector(`article${tid('tweet')},${tid('emptyState')}`),empty:!!column.querySelector(tid('emptyState'))},
    posts,
    preview:body.slice(0,300)
  };
})()
