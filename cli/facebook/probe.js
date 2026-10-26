(() => {
  const dialogs=[...document.querySelectorAll('[role="dialog"],dialog')].filter(visible),dialog=dialogs.at(-1),scope=dialog||document;
  const buttons=[...scope.querySelectorAll('button,[role="button"]')].filter(visible);
  const button=names=>{const found=buttons.filter(e=>names.includes(label(e)));return found.length===1?control(found[0]):null};
  const editor=scope.querySelector('[data-lexical-editor="true"][contenteditable="true"]');
  const placeholder=editor?.getAttribute('aria-placeholder')||editor?.getAttribute('aria-label')||'';
  const timeline=[...document.querySelectorAll('a[aria-label][href]')].find(e=>/的时间线$|'s (?:timeline|profile)$/i.test(e.getAttribute('aria-label')));
  const english=placeholder.match(/^What's on your mind,\s*(.+)\?$/i);
  const account=english?english[1]:placeholder.replace(/，分享你的新鲜事吧！$|, what's on your mind\??$/i,'');
  const own=timeline?.getAttribute('aria-label').replace(/的时间线$|'s (?:timeline|profile)$/i,'');
  const file=scope.querySelector('input[type="file"][accept*="image"]');
  const remove=buttons.filter(e=>/^(移除照片|Remove photo)$/i.test(label(e)));
  const alerts=[...document.querySelectorAll('[role="alert"]')].filter(visible).map(e=>clean(e.innerText));
  const ownUrl=timeline?.href||null;
  const posts=[];
  for(const row of document.querySelectorAll('[role="article"],article,[role="row"]')) {
    const link=[...row.querySelectorAll('a[href]')].find(e=>/\/posts\/|\/permalink\/|story_fbid=|\/videos\//.test(e.href));
    const body=row.querySelector('[data-ad-preview="message"],[data-ad-comet-preview="message"]');
    if(!link||!body)continue;
    const url=new URL(link.href);url.hash='';
    for(const k of [...url.searchParams.keys()])if(!['story_fbid','id'].includes(k))url.searchParams.delete(k);
    posts.push({url:url.href,text:body.innerText.trim(),images:row.querySelectorAll('a[href*="/photo"] img').length});
  }
  const draftRows=[...document.querySelectorAll('[role="row"]')].filter(e=>e.querySelector('[role="gridcell"],td'));
  return {url:location.href,host:location.hostname,path:location.pathname,
    account:(account&&(english||account!==placeholder)?account:own)||null,profileUrl:ownUrl,
    loginRequired:/\/login|\/recover|\/reg\//.test(location.pathname)||!!document.querySelector('input[type="password"]'),
    challenge:/\/checkpoint|\/two_step_verification/.test(location.pathname),
    editor:!!editor,text:editor?.innerText.replace(/\n+$/,''),mediaCount:remove.length,
    uploading:!!scope.querySelector('[role="progressbar"]'),alerts,
    rejected:alerts.some(t=>/upload failed|couldn.t upload|上传失败/i.test(t)),
    listReady:!dialog&&!!document.querySelector('[role="main"],main'),posts,
    draftRows:draftRows.map(e=>({text:clean(e.innerText),target:control([...e.querySelectorAll('[role="button"]')].find(b=>/草稿帖编辑链接|Edit draft/i.test(label(b)))||e)})),
    actions:{text:editor?{target:'[role="dialog"] [data-lexical-editor="true"][contenteditable="true"][role="textbox"]',enabled:true,stable:true}:null,file:file?{target:'[role="dialog"] input[type="file"][accept*="image"]',enabled:true,stable:true}:null,post:button(['Post','Publish','发布','发帖']),
      start:control([...document.querySelectorAll('[role="region"] [role="button"]')].find(e=>/分享你的新鲜事|what.s on your mind/i.test(e.innerText))),
      saveDraft:button(['存为草稿','Save as draft','Save draft']),
      drafts:control([...document.querySelectorAll('a[href]')].find(e=>/filter=DRAFT/.test(e.href)))}}
})()
