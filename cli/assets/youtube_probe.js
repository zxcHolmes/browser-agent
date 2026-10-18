(() => {
  // Read-only DOM adapter (YouTube Studio). Shared helpers — visible, clean,
  // label, control, first, locate — come from dom_helpers.js; controls are
  // returned as stable selectors that the extension resolves again on use.
  // The ytcp-uploads-dialog host can have a zero-height box while its inner
  // tp-yt-paper-dialog is the visible, fixed-position window.
  const wizard=[...document.querySelectorAll('ytcp-uploads-dialog')].find(e=>visible(e)||visible(e.querySelector('tp-yt-paper-dialog')));
  const editor=first(document,['ytcp-video-metadata-editor','ytcp-video-editor']);
  const root=wizard||editor||document;
  const scopes=wizard?['ytcp-uploads-dialog']:editor?[editor.tagName.toLowerCase()]:[];
  const ctl=e=>control(e,scopes);
  const buttons=[...root.querySelectorAll('button,ytcp-button,ytcp-icon-button,tp-yt-paper-item,[role="button"]')]
    .filter(visible).filter(e=>e.tagName==='BUTTON'||!e.querySelector('button'));
  const button=(ids,names)=>ctl(first(root,ids.map(id=>'#'+id))||buttons.find(e=>names.includes(label(e))));
  const pageButtons=[...document.querySelectorAll('button,ytcp-icon-button,tp-yt-paper-item,[role="button"]')].filter(visible);
  const findPageButton=names=>control(pageButtons.find(e=>names.includes(label(e))));
  const fields=[...root.querySelectorAll('[contenteditable="true"],input,textarea')].filter(visible);
  const field=kind=>{
    const patterns=kind==='title'?/title|标题|標題/i:/description|说明|說明|描述/i;
    const candidates=fields.filter(e=>e.isContentEditable||e.tagName==='TEXTAREA'||['text',''].includes(e.type));
    const e=first(root,kind==='title'?['#title-textarea #textbox','ytcp-video-title #textbox','[data-field="title"]']:['#description-textarea #textbox','[data-field="description"]'])||
      candidates.find(e=>patterns.test([e.id,e.getAttribute('aria-label'),e.getAttribute('placeholder'),e.parentElement?.id].join(' ')));
    return e?{...ctl(e),value:e.isContentEditable?e.innerText:e.value,invalid:!!e.closest('[invalid],[aria-invalid="true"]')}:null;
  };
  const radios=[...root.querySelectorAll('tp-yt-paper-radio-button,[role="radio"],input[type="radio"]')].filter(visible);
  const radio=names=>{
    const e=radios.find(e=>names.includes(e.getAttribute('name'))||names.includes(e.getAttribute('value'))||names.includes(label(e)));
    return e?{...ctl(e),selected:e.checked===true||e.hasAttribute('checked')||e.getAttribute('aria-checked')==='true'}:null;
  };
  const progress=[...root.querySelectorAll('ytcp-video-upload-progress,ytcp-video-processing-progress,#progress-label,#upload-progress,#processing-progress,#processing-status,#checks-status,#footer,[role="progressbar"]')]
    .filter(visible).map(e=>clean([e.innerText,...[...e.querySelectorAll('[aria-label],[title]')].map(n=>n.getAttribute('aria-label')||n.title)].join(' '))).filter(Boolean).join('\n').slice(0,5000);
  const headers=[...root.querySelectorAll('#dialog-header,#header,.header,.save-status,.draft-status,#save-state')]
    .filter(visible).map(e=>clean(e.innerText)).join('\n');
  const alerts=[...root.querySelectorAll('[role="alert"],#error-message,ytcp-error-dialog,[invalid],[aria-invalid="true"]')]
    .filter(visible).map(e=>clean(e.innerText)).filter(Boolean).map(x=>x.slice(0,1500));
  const idFromUrl=value=>{
    try{
      const u=new URL(value,location.href);
      let id=null;
      if(u.hostname==='youtu.be')id=u.pathname.split('/')[1];
      else if(/(^|\.)youtube\.com$/.test(u.hostname))id=u.searchParams.get('v')||u.pathname.match(/\/(?:video|shorts)\/([\w-]{11})(?:\/|$)/)?.[1];
      return id&&/^[\w-]{11}$/.test(id)?id:null;
    }catch{return null;}
  };
  const confirmations=[...document.querySelectorAll('ytcp-video-share-dialog,ytcp-video-publish-dialog,[role="dialog"]')].filter(visible);
  const sources=[...(wizard?[wizard]:[]),...confirmations];
  const videoIds=[...new Set(sources.flatMap(r=>[...r.querySelectorAll('a[href]')].map(a=>idFromUrl(a.href))).filter(Boolean))];
  const confirmationText=confirmations.map(e=>clean(e.innerText)).join('\n').slice(0,3000);
  const rows=[...document.querySelectorAll('ytcp-video-row,[role="row"]')].filter(visible).map(e=>{
    const links=[...e.querySelectorAll('a[href]')];
    // Draft rows have no links; their ID is only in the thumbnail (i.ytimg.com/vi/<id>/).
    const thumbs=[...e.querySelectorAll('img[src*="ytimg.com/vi"]')].map(i=>i.src.match(/\/vi(?:_webp)?\/([\w-]{11})\//)?.[1]);
    const ids=[...new Set([...links.map(a=>idFromUrl(a.href)),...thumbs].filter(Boolean))];
    const title=e.querySelector('a#video-title,#video-title,.video-title');
    const privacy=clean(e.querySelector('.tablecell-visibility,.tablecell-privacy,[data-visibility]')?.innerText);
    const draftButton=e.querySelector('#edit-draft-button')||[...e.querySelectorAll('button,ytcp-button,[role=button]')].find(b=>/^(Edit draft|编辑草稿|修改草稿|編輯草稿)$/i.test(label(b)));
    const rowScope=ids.length?[`ytcp-video-row:has(img[src*="/vi/${ids[0]}/"])`,`ytcp-video-row:has(a[href*="${ids[0]}"])`]:[];
    const draft=!!draftButton||/^(Draft|草稿)$/i.test(privacy);
    const visibility=({'Public':'public','公开':'public','公開':'public','Unlisted':'unlisted','不公开':'unlisted','不公开列出':'unlisted','不公開列出':'unlisted','不公開':'unlisted','Private':'private','私享':'private','私人':'private'})[privacy]||null;
    return {videoIds:ids,text:clean(e.innerText).slice(0,2000),title:clean(title?.getAttribute('title')||title?.textContent),
      draft,visibility,date:clean(e.querySelector('.tablecell-date')?.innerText),editDraft:control(draftButton,rowScope)};
  }).filter(r=>r.videoIds.length);
  // The video picker; the thumbnail uploader's image input is reported separately.
  const file=[...root.querySelectorAll('input[type="file"]')].find(e=>!e.disabled&&!e.closest('ytcp-thumbnail-uploader')&&!/^image\//.test(e.accept||''));
  const thumbUploader=[...root.querySelectorAll('ytcp-thumbnail-uploader')].find(e=>visible(e)||visible(e.parentElement));
  const thumbInput=thumbUploader?.querySelector('input[type="file"]');
  const thumbPreview=[...(thumbUploader?.closest('ytcp-video-custom-still-editor')||thumbUploader||document).querySelectorAll('img')].map(i=>i.src).find(Boolean)||null;
  const saveEl=wizard?null:first(document,['ytcp-button#save button','ytcp-button#save','#save']);
  const body=clean(document.body?.innerText);
  return {
    url:location.href,host:location.hostname,ready:document.readyState,
    channelId:location.pathname.match(/\/channel\/(UC[\w-]+)/)?.[1]||
      [...document.querySelectorAll('a[href]')].map(a=>a.href.match(/\/channel\/(UC[\w-]+)/)?.[1]).find(Boolean)||null,
    loginRequired:location.hostname==='accounts.google.com'||/\/ServiceLogin/.test(location.pathname)||
      (!wizard&&!!document.querySelector('input[type="password"],input[type="email"]')),
    channelSetupRequired:/Create your channel|创建您的频道|创建你的频道|建立你的頻道/.test(body)&&!wizard,
    accessDenied:/You don.t have permission|Access denied|无权访问|没有权限|無權存取/.test(body),
    challenge:/\/challenge\//.test(location.pathname)||!!document.querySelector('iframe[src*="recaptcha"],#captcha'),
    studioReady:!!document.querySelector('ytcp-app,ytcp-studio-app'),
    wizard:!!wizard,fileInput:file?{target:locate(file,scopes).selector,multiple:file.multiple,selected:[...(file.files||[])].map(f=>({name:f.name,size:f.size}))}:null,
    title:field('title'),description:field('description'),
    thumbnail:thumbInput?{target:locate(thumbInput,scopes).selector,
      enabled:!thumbInput.disabled&&!thumbUploader.hasAttribute('disabled')&&!thumbUploader.closest('[disabled]'),
      custom:thumbUploader.hasAttribute('selected'),pendingUpload:/^(data|blob):/.test(thumbPreview||''),
      text:clean(thumbUploader.closest('ytcp-video-thumbnail-editor')?.innerText).slice(0,300)}:null,
    editSave:saveEl?control(saveEl):null,
    audience:{yes:radio(['VIDEO_MADE_FOR_KIDS_MFK','MADE_FOR_KIDS','Yes, it’s made for kids','是，专为儿童打造']),
      no:radio(['VIDEO_MADE_FOR_KIDS_NOT_MFK','NOT_MADE_FOR_KIDS','No, it’s not made for kids','否，内容不是面向儿童的'])},
    privacy:{public:radio(['PUBLIC','public','Public','公开','公開']),unlisted:radio(['UNLISTED','unlisted','Unlisted','不公开','不公開']),private:radio(['PRIVATE','private','Private','私享','私人'])},
    actions:{create:findPageButton(['Create','创建','建立']),openUpload:findPageButton(['Upload videos','Upload video','上传视频','上傳影片']),
      next:button(['next-button'],['Next','下一步','继续','繼續']),done:button(['done-button'],['Publish','发布','發佈','Save','保存','儲存','Done','完成']),
      close:button(['ytcp-uploads-dialog-close-button'],['Close','关闭','關閉']),
      details:button(['step-badge-0','step-title-0'],['Details','详细信息','詳情','詳細資料']),
      more:button(['toggle-button'],['Show more','展开','显示更多','顯示更多'])},
    step: (()=>{const e=first(root,['[role=tab][aria-selected=true]','[aria-current=step]','ytcp-stepper [selected]','[data-step]','ytcp-video-metadata-editor','ytcp-video-elements','ytcp-video-checks','ytcp-video-visibility-select']);
      return e?clean(e.getAttribute('data-step')||e.id||(e.tagName.startsWith('YTCP-VIDEO-')?e.tagName:e.innerText)):null;})(),
    contentList:({'video-list-uploads-tab':'videos','video-list-shorts-tab':'shorts'})[document.querySelector('[role=tab][aria-selected=true]')?.id]||null,
    contentTabs:{shorts:control([...document.querySelectorAll('[role=tab],button,tp-yt-paper-tab')].filter(visible).find(e=>/^(Shorts|短视频|短影片)$/i.test(label(e))))},
    progress,headers,alerts,videoIds,confirmationText,rows,
    uploadComplete:/Upload complete|Upload finished|Processing complete|Processing finished|Checks complete|检查完毕|检查完成|檢查完成|处理完成|处理完毕|上传完毕|上传完成|上傳完成/i.test(progress),
    processingComplete:/Processing complete|Processing finished|SD processing complete|已完成处理|处理完毕|处理完成|标清处理完毕|標準畫質處理完畢/i.test(progress)&&
      !/Processing \d|Processing will|Processing has started|Processing is|正在处理|正在處理/.test(progress),
    checksComplete:/Checks complete|No issues found|检查完毕|检查完成|檢查完成|未发现任何问题|未發現任何問題/i.test(progress),
    checksBusy:/Checks running|Checking|正在检查|正在檢查/.test(progress),
    checksIssue:/Copyright claim|Copyright-protected|Restrictions found|版权声明|版权主张|版权保护|版權聲明|发现问题|發現問題/i.test(progress),
    saving:/Saving|正在保存|正在儲存/i.test(headers),
    draftSaved:/Saved as draft|Draft saved|已保存为草稿|草稿已保存|已儲存為草稿/i.test(headers),
    publicationConfirmed:/Video published|Video saved|Published on|视频已发布|视频已保存|视频发布时间|发布于 ?\d|影片已發佈|影片已發布|影片已儲存|發佈於|發布於/i.test(confirmationText),
    preview:body.slice(0,500)
  };
})()
