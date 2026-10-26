(() => {
  // WeChat's Wujie microfrontend keeps a patched document in a same-origin
  // iframe and renders its elements in an open shadow root. Actions already
  // traverse that root; read from the frame document rather than outer body.
  const d=document.querySelector('iframe[name="content"]')?.contentDocument||document;
  const text=d.body?.innerText||'';
  const all=s=>[...d.querySelectorAll(s)];
  const one=s=>{const found=all(s).filter(visible);return found.length===1?found[0]:null};
  const target=(s,hidden=false)=>{const found=all(s).filter(e=>hidden||visible(e));return found.length===1?{target:s,stable:true,enabled:!found[0].disabled&&!found[0].classList.contains('weui-desktop-btn_disabled')}:null};
  const btn=t=>{const found=all('button').filter(e=>visible(e)&&clean(e.innerText)===t);return found.length===1?{target:{selector:'button',text:t,exact:true},stable:true,enabled:!found[0].disabled&&!found[0].classList.contains('weui-desktop-btn_disabled')}:null};
  const editor=one('.input-editor[contenteditable]'), title=one('input[placeholder="填写短标题有机会获得更多流量"]');
  const post=btn('发表');
  const panels=all('.weui-desktop-dialog').filter(visible);
  const dialogs=panels.map(e=>clean(e.innerText));
  const rows=all('.post-feed-item').map(e=>({text:clean(e.querySelector('.post-title')?.innerText),time:clean(e.querySelector('.time-label')?.innerText),body:clean(e.innerText)}));
  const total=Number(text.match(/视频\s*\((\d+)\)/)?.[1]);
  const labels=all('.weui-desktop-picker__panel__label').filter(visible).map(e=>clean(e.innerText));
  const days={};
  for(const e of all('.weui-desktop-picker__table a:not(.weui-desktop-picker__faded):not(.weui-desktop-picker__disabled)').filter(visible))days[clean(e.innerText)]={target:{selector:'.weui-desktop-picker__table a:not(.weui-desktop-picker__faded):not(.weui-desktop-picker__disabled)',text:clean(e.innerText),exact:true},enabled:true};
  const date=one('input[placeholder="请选择发表时间"]');
  const video=all('.video-content video').find(visible);
  const alerts=all('[role="alert"],.weui-desktop-toast,.weui-desktop-msg__content').filter(visible).map(e=>clean(e.innerText)).filter(Boolean);
  const validationErrors=all('.error-title,.weui-desktop-form__tips_error').filter(visible).map(e=>clean(e.innerText)).filter(Boolean);
  const account=clean(document.querySelector('.account-info .name')?.innerText);
  const pageNode=one('.weui-desktop-pagination__num_current');
  const page=pageNode?Number(clean(pageNode.innerText)):null;
  const nextNumber=page?all('.weui-desktop-pagination__num').find(e=>visible(e)&&clean(e.innerText)===String(page+1)):null;
  const nextAnchor=all('.weui-desktop-pagination__nav a').find(e=>visible(e)&&['下一页','Next'].includes(clean(e.innerText)));
  const nextPage=nextNumber?{target:{selector:'.weui-desktop-pagination__num',text:String(page+1),exact:true},enabled:true}:
    nextAnchor?{target:{selector:'.weui-desktop-pagination__nav a',text:clean(nextAnchor.innerText),exact:true},enabled:!nextAnchor.classList.contains('weui-desktop-btn_disabled')}:null;
  return {host:location.hostname,url:location.href,path:location.pathname,account,text,rows,page,total:Number.isFinite(total)?total:null,
    loggedIn:!!account,loginRequired:/\/login(?:\.html)?(?:[/?#]|$)/.test(location.href)||/扫码登录|重新登录|登录已过期/.test(document.body.innerText+' '+text),
    challenge:!!first(d,['iframe[src*="captcha"]','[class*="captcha_verify"]']),
    listReady:!!one('.post-list'),listEnd:rows.length>0&&Number.isFinite(total)&&rows.length>=total||/暂无视频|暂无内容|没有更多/.test(text),
    title:title?.value,description:editor?.innerText?.replace(/\r\n/g,'\n').replace(/\n+$/,''),
    validationErrors,editor:!!editor,uploaded:!!video&&video.readyState>=2&&(!!post?.enabled||validationErrors.length>0)&&!/上传中|正在上传/.test(text),
    progress:clean(one('.upload-progress')?.innerText),alerts,rejected:alerts.some(t=>/失败|错误|异常/.test(t)),
    dialogs,scheduleChecked:!!one('input[type="radio"][value="1"]')?.checked,scheduleValue:date?.value||null,
    calendar:labels.length>=2?{year:Number(labels[0].replace(/\D/g,'')),month:Number(labels[1].replace(/\D/g,''))}:null,dayActions:days,
    actions:{upload:target('input[type="file"][accept*="video"]',true),title:target('input[placeholder="填写短标题有机会获得更多流量"]'),description:target('.input-editor[contenteditable]'),
      post,draft:btn('保存草稿'),directPost:btn('直接发表'),
      schedule:target('label:has(input[type="radio"][value="1"])'),immediate:target('label:has(input[type="radio"][value="0"])'),
      date:target('input[placeholder="请选择发表时间"]'),time:target('input[placeholder="请选择时间"]'),
      nextMonth:target('.weui-desktop-picker__panel__hd .weui-desktop-btn__icon__right'),prevMonth:target('.weui-desktop-picker__panel__hd .weui-desktop-btn__icon__left'),
      search:target('input[placeholder="搜索视频"]'),nextPage}};
})()
