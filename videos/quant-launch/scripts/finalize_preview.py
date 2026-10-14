"""Caption rail, local runtime and a small font subset for the preview."""
import html
import json
from pathlib import Path
import re
import subprocess

from fontTools import subset

ROOT=Path(__file__).resolve().parent.parent
WORKFLOW=Path.home()/'.agents/skills/product-launch-video'


def main():
    caption_meta=ROOT/'captions/captions-meta.json'
    captions_only=caption_meta.exists()
    meta=json.loads((caption_meta if captions_only else ROOT/'assets/audio/tts-meta.json').read_text())
    content=[];animations=[]
    for i,r in enumerate(meta['lines']):
        d=r['end']-r['start'];ident='subtitle-'+r['id']
        content.append(f'<div id="{ident}" class="clip promo-caption" data-start="{r["start"]}" data-duration="{d:.3f}" data-track-index="0" data-track-kind="captions"><span class="{ident}-text">{html.escape(r["text"])}</span></div>')
        animations.append(f'tl.fromTo(".{ident}-text",{{opacity:0,y:5}},{{opacity:1,y:0,duration:.12,ease:"power1.out"}},{r["start"]});')
        animations.append(f'tl.to(".{ident}-text",{{opacity:0,duration:.10}},{r["end"]-.1:.3f});')
    caption='''<template><style>
@font-face{font-family:PromoNoto;src:url("assets/fonts/PromoSansSC.ttf");font-weight:100 900}
#root{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
.promo-caption{position:absolute;left:96px;top:987px;width:1728px;height:64px;display:flex;align-items:center;justify-content:center;}
.promo-caption span{font-family:-apple-system,PromoNoto,sans-serif;color:#F5F5F7;font-size:38px;font-weight:500;line-height:1.4;background:#0D0D0F;padding:3px 22px;border-radius:6px}
</style><div id="root" data-composition-id="captions" data-width="1920" data-height="1080" data-duration="60">'''+''.join(content)+'</div><script>(()=>{const tl=gsap.timeline({paused:true});'+''.join(animations)+'window.__timelines["captions"]=tl;})();</script></template>'
    (ROOT/'compositions/captions.html').write_text(caption)
    frames=list((ROOT/'compositions/frames').glob('*.html'))
    for path in frames:path.write_text(path.read_text().replace('NotoSansSC-Variable.ttf','PromoSansSC.ttf'))
    chars=''.join(p.read_text() for p in frames)+caption+''.join(chr(i) for i in range(32,127))
    options=subset.Options();options.name_IDs=['*'];options.name_languages=['*'];options.name_legacy=True
    font=subset.load_font(str(ROOT/'assets/fonts/NotoSansSC-Variable.ttf'),options)
    cutter=subset.Subsetter(options=options);cutter.populate(text=chars);cutter.subset(font)
    subset.save_font(font,str(ROOT/'assets/fonts/PromoSansSC.ttf'),options)
    audio=json.loads((ROOT/'audio_meta.json').read_text())
    if captions_only:
        audio['voices']=[]
        audio['narration']='none'
    audio['bgm']['volume']=.75 if captions_only else .24
    (ROOT/'audio_meta.json').write_text(json.dumps(audio,ensure_ascii=False,indent=2))
    subprocess.run(['node',str(WORKFLOW/'scripts/assemble-index.mjs'),'--storyboard',str(ROOT/'STORYBOARD.md'),'--hyperframes',str(ROOT)],check=True)
    index=ROOT/'index.html';s=index.read_text()
    s=re.sub(r'<script src="https://cdn.jsdelivr.net/npm/gsap[^>]+></script>','<script src="assets/gsap.min.js"></script>',s)
    s=s.replace('class="scene"','class="clip scene"').replace('id="el-captions"','id="el-captions" data-track-kind="captions"')
    s=s.replace('width: 1920px;\n        height: 1080px;\n        overflow: hidden;\n        background: #0D0D0F;','width: 100%;\n        height: 100%;\n        overflow: hidden;\n        background: #0D0D0F;')
    index.write_text(s)
    summary={'voice_provider':'none' if captions_only else meta['provider'],
             'voice':'none' if captions_only else meta['voice'],'voice_lines':0 if captions_only else len(meta['lines']),
             'voice_sum_seconds':0 if captions_only else round(sum(r['duration'] for r in meta['lines']),2),
             'caption_lines':len(meta['lines']),'caption_source':str(caption_meta.relative_to(ROOT)) if captions_only else 'tts-meta.json',
             'last_caption_end':meta['lines'][-1]['end'],
             'music_seconds':60,'timeline':60,'font_subset_bytes':(ROOT/'assets/fonts/PromoSansSC.ttf').stat().st_size}
    (ROOT/'audio-meta-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps(summary))


if __name__=='__main__':main()
