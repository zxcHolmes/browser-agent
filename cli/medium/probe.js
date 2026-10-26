(() => {
  const editor=document.querySelector('.postArticle-content[g_editable="true"]');
  const textOf=e=>{
    if(!e)return '';
    const copy=e.cloneNode(true);copy.querySelectorAll('.defaultValue,.u-ignoreBlock').forEach(n=>n.remove());
    return (copy.textContent||'').replace(/\u200b/g,'').replace(/\u00a0/g,' ').trim();
  };
  const codeText=e=>{
    const copy=(e.querySelector('.pre--content')||e).cloneNode(true);
    copy.querySelectorAll('.u-ignoreBlock').forEach(n=>n.remove());
    copy.querySelectorAll('br').forEach(n=>n.replaceWith('\n'));
    return copy.textContent.replace(/\n+$/,'');
  };
  const profile=[...document.querySelectorAll('a[href]')].find(e=>/^https:\/\/medium\.com\/@[^/?]+(?:\?.*)?$/.test(e.href));
  const handle=location.pathname.match(/^\/@([^/]+)/)?.[1]||profile?.href.match(/\/@([^/?]+)/)?.[1];
  const buttons=[...document.querySelectorAll('button')].filter(visible);
  const button=names=>{const found=buttons.filter(e=>names.includes(label(e)));return found.length===1?control(found[0]):null};
  const action=name=>{const e=[...document.querySelectorAll('button[data-action]')].find(e=>e.dataset.action===name&&visible(e));return e?{target:'button[data-action='+JSON.stringify(name)+']',enabled:!e.disabled,stable:true}:null};
  const root=editor||document.querySelector('article');
  const title=editor?.querySelector('.graf--title,[data-testid="editorTitleParagraph"]')||root?.querySelector('h1');
  const blocks=editor?[...editor.querySelectorAll('.section-inner > p,.section-inner > h3,.section-inner > h4,.section-inner > blockquote,.section-inner > pre,.section-inner > figure,.section-inner > ul > li,.section-inner > ol > li')]
    .filter(e=>e!==title).map(e=>e.tagName==='FIGURE'?{kind:'image',loaded:!!e.querySelector('img')?.naturalWidth,src:e.querySelector('img')?.currentSrc,caption:textOf(e.querySelector('figcaption')),alt:e.querySelector('img')?.getAttribute('alt')||''}:
      {kind:({H3:'heading',H4:'subheading',BLOCKQUOTE:'quote',PRE:'code',LI:e.parentElement.tagName==='UL'?'bullet':'number'})[e.tagName]||'paragraph',text:e.tagName==='PRE'?codeText(e):textOf(e),formats:[...e.querySelectorAll('strong,b,em,i,a,code')].map(n=>({kind:({STRONG:'bold',B:'bold',EM:'italic',I:'italic',A:'link',CODE:'code'})[n.tagName],text:n.textContent,...(n.tagName==='A'?{url:n.getAttribute('href')}: {})}))}).filter(b=>b.kind==='image'||b.text):[];
  const alerts=[...document.querySelectorAll('[role="alert"],.alert')].filter(visible).map(e=>clean(e.innerText));
  const linkInput=[...document.querySelectorAll('input')].find(e=>visible(e)&&/link|url/i.test(e.placeholder));
  const articleTitle=!editor?document.querySelector('article h1')?.innerText:null;
  const articleText=!editor?[...document.querySelectorAll('article p,article h2,article h3,article pre,article blockquote,article li')].map(e=>e.innerText).filter(Boolean):[];
  const posts=[];
  for(const a of document.querySelectorAll('a[href]')) {
    if(!a.querySelector('h2,h3')&&!/^\/p\/[^/]+\/edit/.test(new URL(a.href).pathname))continue;
    const url=new URL(a.href);
    if(url.hostname!=='medium.com')continue;
    url.search='';url.hash='';
    const text=clean(a.innerText);if(text&&!posts.some(p=>p.url===url.href))posts.push({url:url.href,text,images:0});
  }
  return {url:location.href,host:location.hostname,path:location.pathname,account:handle||null,
    profileUrl:handle?'https://medium.com/@'+handle:null,
    loginRequired:/\/m\/signin|\/m\/register/.test(location.pathname)||(!editor&&!!button(['Sign in'])&&!handle),
    challenge:!!document.querySelector('#challenge-running,.cf-challenge')||/just a moment|请稍候/i.test(document.title),
    editor:!!editor,title:textOf(title),blocks,
    text:editor?textOf(editor):'',mediaCount:blocks.filter(b=>b.kind==='image').length,
    uploading:blocks.some(b=>b.kind==='image'&&!b.loaded)||!!document.querySelector('.is-uploading'),
    saved:!![...document.querySelectorAll('.saveState,.js-saveState,.metabar-message')].find(e=>/saved/i.test(e.innerText)),
    alerts,rejected:alerts.some(t=>/upload failed|couldn.t upload/i.test(t)),
    listReady:/\/me\/stories/.test(location.pathname)||(!editor&&!!handle),posts,
    articleTitle,articleText,articleImages:!editor?document.querySelectorAll('article figure img').length:0,
    prepublish:/\/submission$/.test(location.pathname)||!!button(['Publish now']),previewTitle:document.querySelector('[aria-label="Story preview title"]')?.value,imageMenuOpen:!!document.querySelector('.inlineTooltip.is-scaled'),
    actions:{title:control(title),body:control(editor),add:action('inline-menu'),image:action('inline-menu-image'),
      prepublish:action('show-prepublish'),post:button(['Publish now'])||(/\/submission$/.test(location.pathname)?button(['Publish']):null),userMenu:action('open-userActions'),link:control(linkInput)}}
})()
