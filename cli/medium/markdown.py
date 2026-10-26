"""A strict Markdown subset for deterministic article composition."""
from pathlib import Path
import re
from urllib.parse import unquote,urlparse
from ..common.automation import AutomationError
from ..common.web_publishing import local_image


def parse_markdown(path,title=None):
    path=Path(path).expanduser().resolve()
    source=path.read_text(encoding='utf-8').replace('\r\n','\n')
    if '\x00' in source:raise AutomationError('INVALID_MARKDOWN','NUL is not allowed',exit_code=2)
    lines=source.splitlines();blocks=[];pending=[];images=[];i=0;first_heading=True
    def flush():
        if pending:
            blocks.append({'kind':'paragraph','text':' '.join(pending)})
            pending.clear()
    while i<len(lines):
        line=lines[i];i+=1
        if not line.strip():flush();continue
        if line.startswith('```'):
            flush();code=[]
            while i<len(lines) and not lines[i].startswith('```'):code.append(lines[i]);i+=1
            if i==len(lines):raise AutomationError('INVALID_MARKDOWN','Unclosed fenced code block',exit_code=2)
            i+=1;blocks.append({'kind':'code','text':'\n'.join(code)});continue
        image=re.fullmatch(r'!\[([^\]]*)\]\((?:<([^>]+)>|([^\s)]+))(?:\s+"([^"]*)")?\)',line.strip())
        if image:
            flush();target=unquote(image[2] or image[3]);parsed=urlparse(target)
            if parsed.scheme or parsed.netloc:
                raise AutomationError('INVALID_IMAGE','Markdown images must be local files; download remote images first',exit_code=2)
            info=local_image(path.parent/target,limit=25*1024**2)
            if Path(info['path']).suffix.lower()=='.webp':raise AutomationError('INVALID_IMAGE','Medium articles require PNG, JPEG, or GIF',exit_code=2)
            if len(image[1])>500:raise AutomationError('INVALID_IMAGE','Image alt text must contain at most 500 characters',exit_code=2)
            images.append(info);blocks.append({'kind':'image','image':info,'alt':image[1],'caption':image[4] or ''});continue
        heading=re.match(r'^(#{1,3})\s+(.+)$',line)
        if heading:
            flush()
            if first_heading and not blocks and len(heading[1])==1:
                title=title or heading[2]
            else:blocks.append({'kind':'heading' if len(heading[1])<=2 else 'subheading','text':heading[2]})
            first_heading=False
            continue
        if re.match(r'^>\s?',line):
            flush();blocks.append({'kind':'quote','text':re.sub(r'^>\s?','',line)});continue
        if re.match(r'^[-*+]\s+',line):
            flush();blocks.append({'kind':'bullet','text':re.sub(r'^[-*+]\s+','',line)});continue
        if re.match(r'^\d+\.\s+',line):
            flush();blocks.append({'kind':'number','text':re.sub(r'^\d+\.\s+','',line)});continue
        if re.search(r'!\[|^\s{2,}\S|^#{4,}\s|^\||<\/?[A-Za-z]',line):
            raise AutomationError('UNSUPPORTED_MARKDOWN','Use standalone local images, paragraphs, headings up to level 3, flat lists, quotes, and fenced code; HTML, tables, and nested blocks are unsupported',exit_code=2)
        pending.append(line.strip())
    flush()
    title=(title or path.stem).strip()
    if not title or '\n' in title or len(title)>200 or not blocks:
        raise AutomationError('INVALID_MARKDOWN','Provide a title up to 200 characters and a nonempty article body',exit_code=2)
    return {'title':title,'blocks':blocks,'images':images,'text':title+'\n\n'+'\n\n'.join(b.get('text','') for b in blocks)}


def inline_parts(text):
    """Yield simple emphasis and links. Nested inline syntax is intentionally rejected."""
    pattern=re.compile(r'\*\*([^*\n]+)\*\*|(?<!\*)\*([^*\n]+)\*|`([^`\n]+)`|\[([^\]\n]+)\]\((https?://[^\s)]+)\)')
    parts=[];offset=0
    for match in pattern.finditer(text):
        if match.start()>offset:parts.append({'kind':'text','text':text[offset:match.start()]})
        if match[1]:parts.append({'kind':'bold','text':match[1]})
        elif match[2]:parts.append({'kind':'italic','text':match[2]})
        elif match[3]:parts.append({'kind':'code','text':match[3]})
        else:parts.append({'kind':'link','text':match[4],'url':match[5]})
        offset=match.end()
    if offset<len(text):parts.append({'kind':'text','text':text[offset:]})
    if any(re.search(r'\*|`|\]\(',p['text']) for p in parts if p['kind']=='text'):
        raise AutomationError('UNSUPPORTED_MARKDOWN','Nested or unmatched inline Markdown is unsupported',exit_code=2)
    return parts


def plain_text(block):
    if block['kind'] in {'image','code'}:return block.get('text','')
    return ''.join(p['text']+' ('+p['url']+')' if p['kind']=='link' else p['text'] for p in inline_parts(block['text']))
