(() => {
  const dialogs=[...document.querySelectorAll('dialog,[role="dialog"]')].filter(visible);
  const dialog=dialogs.at(-1), scope=dialog||document;
  const buttons=[...scope.querySelectorAll('button,[role="button"]')].filter(visible);
  const button=names=>{const found=buttons.filter(e=>names.includes(label(e)));return found.length===1?control(found[0]):null};
  const editor=scope.querySelector('[componentkey="ShareBox_textEditor"],[contenteditable="true"][role="textbox"]');
  const own=[...document.querySelectorAll('a[href*="/in/"]')].find(e=>/^\/in\/[^/]+\/?$/.test(new URL(e.href).pathname));
  const profile=own?new URL(own.href):null;
  const namedProfile=profile?[...document.querySelectorAll('a[href]')].find(e=>e.href===profile.href&&clean(e.innerText)):null;
  const accountName=namedProfile?.innerText.split('\n').map(s=>s.trim()).find(Boolean)||null;
  const file=[...document.querySelectorAll('input[type="file"]')].find(e=>e.accept.includes('image'));
  const imageNodes=[...scope.querySelectorAll('img')].filter(e=>!e.closest('button')&&e.naturalWidth>100&&!/profile-displayphoto|profile-framedphoto/.test(e.currentSrc));
  const images=[...new Set(imageNodes.map(e=>e.currentSrc))];
  const alerts=[...document.querySelectorAll('[role="alert"]')].filter(visible).map(e=>clean(e.innerText));
  const posts=[];
  for(const link of document.querySelectorAll('a[href*="/feed/update/urn:li:activity:"],a[href*="/posts/"]')) {
    const row=link.closest('[data-urn],article,[role="listitem"]');
    if(!row)continue;
    const body=row.querySelector('[data-testid="expandable-text-box"],.feed-shared-update-v2__description,.update-components-text');
    if(!body)continue;
    const url=new URL(link.href);url.search='';url.hash='';
    if(!posts.some(p=>p.url===url.href))posts.push({url:url.href,text:body.innerText.trim(),images:row.querySelectorAll('.update-components-image img,[data-testid="image-viewer"] img').length});
  }
  const actor=buttons.find(e=>e.querySelector('img')&&clean(e.innerText));
  return {url:location.href,host:location.hostname,path:location.pathname,
    account:profile?.pathname.split('/')[2],profileUrl:profile?profile.origin+profile.pathname:null,
    actor:actor?clean(actor.innerText):null,accountName,
    loginRequired:/\/login|\/uas\/login|\/signup/.test(location.pathname)||!!document.querySelector('input[name="session_password"]'),
    challenge:/\/checkpoint|\/challenge/.test(location.pathname)||!!document.querySelector('iframe[src*="captcha"]'),
    editor:!!editor,text:editor?.innerText.replace(/\n+$/,''),mediaCount:images.length,
    uploading:!!scope.querySelector('[role="progressbar"]'),alerts,
    rejected:alerts.some(t=>/upload failed|couldn.t upload|上传失败/i.test(t)),
    listReady:/\/recent-activity\//.test(location.pathname)&&!!document.querySelector('main,[role="main"]'),posts,
    draftPrompt:!!button(['Save draft','Save as draft','保存为草稿']),
    actions:{text:editor?{target:'[componentkey="ShareBox_textEditor"]',enabled:true,stable:true}:null,file:file?{target:'input[type="file"][accept*="image"]',enabled:true,stable:true}:null,
      start:button(['Start a post','发动态']),media:button(['Media','媒体文件','Add media']),
      next:button(['Next','下一步']),post:button(['Post','动态','发布']),
      close:button(['关闭'])||button(['Dismiss'])||button(['Close']),saveDraft:button(['Save draft','Save as draft','保存为草稿'])}}
})()
