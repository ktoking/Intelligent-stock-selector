"""Build six seekable product shots, using captured UI and frozen evidence."""
import html
import json
from pathlib import Path
import urllib.request

ROOT=Path(__file__).resolve().parent.parent

STYLE='''
@font-face{font-family:PromoNoto;src:url("assets/fonts/NotoSansSC-Variable.ttf") format("truetype");font-weight:100 900}
#root{position:absolute;inset:0;width:100%;height:100%;font-family:-apple-system,PromoNoto,sans-serif;color:#F5F5F7;overflow:hidden}
.q-bg{position:absolute;inset:0;background:#0D0D0F}.q-field{position:absolute;left:890px;top:120px;width:860px;height:680px;background:radial-gradient(ellipse,#80B9FF18,transparent 70%)}
.q-content{position:absolute;inset:0}.q-top{position:absolute;left:96px;right:96px;top:62px;display:flex;align-items:center;justify-content:space-between}
.q-wordmark{display:flex;align-items:center;gap:17px;font-size:36px;font-weight:650;letter-spacing:-1px}.q-wordmark img{width:38px;height:38px}.q-chapter{font-size:22px;color:#A5ACB8;letter-spacing:2px}
.q-rule{position:absolute;left:96px;right:96px;top:129px;height:1px;background:#2D2D31}.q-footer{position:absolute;left:96px;right:96px;bottom:111px;display:flex;justify-content:space-between;gap:30px;font-size:22px;color:#A5ACB8}
.q-title{font-size:86px;letter-spacing:-3px;font-weight:650;line-height:1.18;margin:0}.q-blue{color:#80B9FF}.q-kicker{font-size:24px;color:#80B9FF;letter-spacing:2px;margin-bottom:25px}.q-muted{color:#A5ACB8}.q-body{font-size:32px;line-height:1.65;margin:24px 0 0}
.q-panel{position:absolute;border:1px solid #2D2D31;background:#19191C;border-radius:22px;overflow:hidden}.q-screen{position:absolute;overflow:hidden;border-radius:20px;border:1px solid #3a3a41;background:#19191C}.q-screen img{display:block;width:100%;height:auto}.q-screenhead{height:50px;border-bottom:1px solid #2D2D31;display:flex;align-items:center;gap:8px;padding:0 20px;background:#19191C}.q-screenhead i{width:9px;height:9px;border-radius:50%;background:#65656d}.q-screenhead span{font-size:19px;color:#A5ACB8;margin-left:10px}.q-screenbody{overflow:hidden;position:relative}.q-chip{display:inline-block;padding:12px 20px;border:1px solid #36363e;border-radius:11px;font-size:24px;color:#F5F5F7;background:#19191C}.q-label{font-size:24px;color:#A5ACB8}.q-line{height:2px;background:#80B9FF;transform-origin:left center}.q-stack{position:absolute;left:96px;top:214px}.q-stat{font-size:174px;font-weight:650;letter-spacing:-8px;font-variant-numeric:tabular-nums;line-height:1.03}.q-up{color:#F16B74}.q-down{color:#53BA91}
'''


def common(number, title, foot):
    return f'''<div class="q-top"><div class="q-wordmark"><img src="assets/quant-mark.svg" alt="Quant"><span>Quant</span></div><div class="q-chapter">0{number} / 06 · {title}</div></div><div class="q-rule"></div><div class="q-footer"><span>{foot}</span><span>AI 量化研究工作台</span></div>'''


def screen(name, asset, x,y,w,h,body_extra=''):
    return f'''<div class="q-screen {name}" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px"><div class="q-screenhead"><i></i><i></i><i></i><span>Quant / {asset.split('.')[0]}</span></div><div class="q-screenbody" style="height:{h-50}px;{body_extra}"><img src="assets/{asset}" alt="真实 Quant 界面截图" data-layout-allow-overflow></div></div>'''


def write(number,name,duration,body,extra,animation):
    prefix='f'+str(number)
    content=common(number,['一个工作台','市场观察','研究与筛选','验证与追溯','观察与控制','产品全景'][number-1],
                   ['产品功能演示 · 当前处于未来观察阶段','富途快照截至 2026-10-09 · 扫描动效为演示','累计研究版本状态 · 截至 2026-10-12','历史回测 · 假设成交 · 2026-05-01—10-09','当前模拟成交 0 · 实盘未启用','研究用途 · 不构成投资建议'][number-1])+body
    content=content.replace('q-',prefix+'-')
    css=(STYLE+extra).replace('q-',prefix+'-')
    js=animation.replace('q-',prefix+'-')
    path=ROOT/'compositions/frames'/f'{number:02d}-{name}.html'
    path.parent.mkdir(parents=True,exist_ok=True)
    ident=path.stem
    path.write_text(f'''<template>
<style>{css}</style>
<div id="root" data-composition-id="{ident}" data-width="1920" data-height="1080" data-duration="{duration}">
 <div id="{prefix}-background" class="clip {prefix}-bg" data-start="0" data-duration="{duration}" data-track-index="0"><div class="{prefix}-field" data-layout-ignore></div></div>
 <div id="{prefix}-foreground" class="clip {prefix}-content" data-start="0" data-duration="{duration}" data-track-index="1">{content}</div>
</div>
<script>
(()=>{{const tl=gsap.timeline({{paused:true}});
tl.fromTo('.{prefix}-rule',{{scaleX:0}},{{scaleX:1,duration:.7,ease:'power3.out'}},0);
{js}
window.__timelines['{ident}']=tl;
}})();
</script>
</template>''')


def main():
    proof=json.loads((ROOT/'evidence.json').read_text())
    chart=json.loads((ROOT/'assets/chart-data.json').read_text())
    # 01: immediate benefit, typography on the beat, real screen advancing.
    body='''<div class="q-stack"><div class="q-kicker q-lead">你的 AI 研究工作台</div><h1 class="q-title"><span class="q-hook1">选股 · 回测 · 复盘</span><br><span class="q-hook2">交给 <span class="q-blue">AI 工作台。</span></span></h1><p class="q-body q-hook3">从寻找机会，到验证策略。<br><span class="q-muted">一个入口，连起整套研究流程。</span></p></div>'''+screen('q-product','dashboard.png',1000,226,825,645)
    write(1,'hook',7,body,'.q-stack{width:880px}.q-screenbody img{margin-top:-385px}.q-title{font-size:83px}.q-hook1,.q-hook2{display:inline-block}', '''
tl.fromTo('.q-hook1',{x:-65,opacity:0},{x:0,opacity:1,duration:.46,ease:'power4.out'},0);
tl.fromTo('.q-lead',{opacity:0},{opacity:1,duration:.3},.3);
tl.fromTo('.q-hook2',{y:58,opacity:0},{y:0,opacity:1,duration:.55,ease:'power3.out'},1.12);
tl.fromTo('.q-product',{x:160,y:42,scale:.86,rotationY:-9,opacity:0},{x:0,y:0,scale:1,rotationY:0,opacity:1,duration:1.35,ease:'power3.out'},.3);
tl.to('.q-product',{scale:1.075,x:-22,duration:2.6,ease:'power2.inOut'},2.1);
tl.fromTo('.q-hook3',{y:30,opacity:0},{y:0,opacity:1,duration:.65,ease:'power2.out'},4.35);
tl.to('.q-product',{scale:1.035,x:0,duration:1.2,ease:'power3.inOut'},5.5);
''')
    # 02: scope is a dated observation, never a streaming/current-price claim.
    count=proof['universe_count']
    body='''<div class="q-stack"><div class="q-kicker">动态研究池</div><h1 class="q-title">让机会，<br>持续进入视野。</h1><div class="q-source-tags"><span class="q-chip q-source">自选股</span><span class="q-chip q-source">纳指100</span><span class="q-chip q-source">标普500</span></div><div class="q-countblock"><div class="q-stat q-blue q-count">0</div><span class="q-label">去重快照覆盖 · 非完整历史覆盖</span></div><p class="q-body q-newsline">异动捕捉 <span class="q-blue">→</span> 资讯线索 <span class="q-blue">→</span> 新候选</p></div>'''+screen('q-market','market.png',827,235,1000,606)+'''<div class="q-scan" data-layout-ignore></div>'''
    write(2,'market',11,body,'.q-stack{top:188px;width:725px}.q-title{font-size:75px}.q-source-tags{display:flex;gap:12px;margin-top:26px}.q-source-tags .q-chip{font-size:23px;padding:11px 16px}.q-countblock{margin-top:27px}.q-countblock .q-stat{font-size:141px}.q-newsline{font-size:27px;margin-top:22px}.q-scan{position:absolute;left:866px;top:350px;width:920px;height:2px;background:#80B9FF;box-shadow:0 0 12px #80B9FF80}',f'''
tl.fromTo('.q-title',{{y:45,opacity:0}},{{y:0,opacity:1,duration:.6,ease:'power3.out'}},.1);
tl.fromTo('.q-source',{{y:18,opacity:0}},{{y:0,opacity:1,duration:.45,stagger:.38,ease:'power2.out'}},.55);
tl.fromTo('.q-countblock',{{opacity:0}},{{opacity:1,duration:.3}},1.5);
tl.fromTo('.q-count',{{innerText:0}},{{innerText:{count},snap:{{innerText:1}},duration:1.6,ease:'power3.out'}},1.6);
tl.fromTo('.q-market',{{x:110,opacity:0,scale:.94}},{{x:0,opacity:1,scale:1,duration:1.05,ease:'power3.out'}},2.5);
tl.fromTo('.q-scan',{{opacity:0,y:0}},{{opacity:.65,y:385,duration:3.5,ease:'none'}},3.45);
tl.to('.q-scan',{{opacity:0,duration:.25}},7);
tl.fromTo('.q-newsline',{{x:-35,opacity:0}},{{x:0,opacity:1,duration:.65,ease:'power3.out'}},6.4);
tl.to('.q-market',{{y:-15,scale:1.018,duration:1.25,ease:'power3.inOut'}},7.6);
''')
    # 03: an explanatory diagram plus actual cumulative status, not a fabricated run.
    labels=[('假设','Codex 研究'),('因子','受限表达式'),('回测','成本与延迟'),('核账','账本一致性'),('筛选','未来观察门槛')]
    nodes=''.join(f'<div class="q-node q-n{i}" style="left:{96+i*348}px"><span class="q-label">0{i+1}</span><b>{a}</b><small>{b}</small></div>' for i,(a,b) in enumerate(labels))
    paths=''.join(f'<path class="q-connect q-c{i}" d="M{96+i*348+268} 519H{96+(i+1)*348}"/>' for i in range(4))
    body='<div class="q-stack"><div class="q-kicker">AUTONOMOUS RESEARCH</div><h1 class="q-title">研究，有一条<span class="q-blue">验证链。</span></h1></div><svg class="q-network" viewBox="0 0 1920 1080" data-layout-ignore>'+paths+'</svg>'+nodes+'''<div class="q-proof"><div><strong class="q-reject">20</strong><span>个已淘汰研究版本</span></div><i></i><div><strong class="q-observe">3</strong><span>个未来观察策略</span></div><p>保留失败记录 · 每个结论可追溯<br><small>累计状态含历史版本，不是胜率。</small></p></div>'''
    write(3,'research',11,body,'.q-stack{top:190px}.q-title{font-size:82px}.q-network{position:absolute;inset:0;width:100%;height:100%;fill:none;stroke:#80B9FF;stroke-width:2}.q-node{position:absolute;top:422px;width:268px;height:204px;padding:27px 32px;border:1px solid #3d444f;border-radius:16px;background:#19191C}.q-node b{display:block;font-size:44px;font-weight:650;margin:17px 0 14px}.q-node small{font-size:23px;color:#A5ACB8}.q-proof{position:absolute;left:96px;right:96px;top:728px;display:flex;align-items:center;gap:46px}.q-proof strong{font-size:73px;font-weight:650;letter-spacing:-3px}.q-proof>div{display:flex;align-items:center;gap:22px}.q-proof span{font-size:27px;color:#A5ACB8;max-width:220px}.q-proof>i{height:67px;width:1px;background:#36363e}.q-proof p{font-size:26px;line-height:1.6;color:#A5ACB8;margin-left:auto}.q-proof small{font-size:22px}', '''
tl.fromTo('.q-title',{y:35,opacity:0},{y:0,opacity:1,duration:.6,ease:'power3.out'},.1);
const times=[.9,2.25,3.8,5.1,6.35];
times.forEach((at,i)=>{tl.fromTo('.q-n'+i,{y:45,opacity:0,scale:.96},{y:0,opacity:1,scale:1,duration:.6,ease:'power3.out'},at);});
document.querySelectorAll('.q-connect').forEach((p,i)=>{const len=p.getTotalLength();p.style.strokeDasharray=len;p.style.strokeDashoffset=len;tl.to(p,{strokeDashoffset:0,duration:.65,ease:'power2.out'},times[i+1]-.45);});
tl.fromTo('.q-proof',{y:30,opacity:0},{y:0,opacity:1,duration:.65,ease:'power3.out'},7.25);
tl.fromTo('.q-reject',{innerText:0},{innerText:20,snap:{innerText:1},duration:.8,ease:'power2.out'},7.4);
tl.fromTo('.q-observe',{innerText:0},{innerText:3,snap:{innerText:1},duration:.8,ease:'power2.out'},7.8);
''')
    # 04: all coordinates and tooltip text derive from the same frozen record.
    curves=[chart['curve'],chart['benchmark_curve']]
    values=[(r['equity']/10000-1)*100 for rows in curves for r in rows];low=min(0,min(values));high=max(values)*1.12
    def coords(rows):return [(50+i*900/(len(rows)-1),430-(r['equity']/10000*100-100-low)/(high-low)*330) for i,r in enumerate(rows)]
    points=[coords(rows) for rows in curves]
    traces=''.join('<path class="q-curve q-series'+str(i)+'" d="'+' '.join(('M' if j==0 else 'L')+f'{x:.2f},{y:.2f}' for j,(x,y) in enumerate(p))+'"/>' for i,p in enumerate(points))
    axes=''.join(f'<line x1="50" x2="950" y1="{100+i*82.5}" y2="{100+i*82.5}" class="q-grid"/>' for i in range(5))
    trade=chart['trades'][0];index=next(i for i,r in enumerate(curves[0]) if r['date']==trade['date']);tx,ty=points[0][index]
    tip=f"{trade['date']} · 历史假设买入<br><b>{html.escape(trade['symbol'].replace('US.',''))} · {trade['qty']:g} 股</b><br>价格 ${trade['price']:.2f} · 金额 ${trade['amount']:.2f}"
    body='''<div class="q-stack"><div class="q-kicker">可追溯的研究证据</div><h1 class="q-title">曲线背后，<br>每一步都能<span class="q-blue">追溯。</span></h1><div class="q-checks"><span class="q-chip q-check">成本</span><span class="q-chip q-check">回撤</span><span class="q-chip q-check">延迟</span></div><p class="q-body q-detail">红点买入 · 绿点卖出<br><span class="q-muted">点击看当天交易明细。</span></p></div><div class="q-chart q-panel"><div class="q-charthead"><b>策略净值与 QQQ</b><span>历史回测 / 假设成交</span></div><svg viewBox="0 0 1000 500" class="q-plot">'''+axes+traces+f'''<circle class="q-mark" cx="{tx:.2f}" cy="{ty:.2f}" r="7"/><path class="q-pointer" d="M0 0L0 28L8 21L15 36L21 33L14 18L25 17Z"/></svg><div class="q-tip" style="right:30px;top:150px">{tip}</div><div class="q-chartlabels"><span>{chart['window'][0]}</span><span>策略 <i class="q-up">━</i> · QQQ <i class="q-muted">━</i></span><span>{chart['window'][1]}</span></div></div>'''
    write(4,'evidence',11,body,'.q-stack{top:220px;width:695px}.q-title{font-size:75px}.q-checks{display:flex;gap:15px;margin-top:35px}.q-chart{left:820px;top:238px;width:1004px;height:620px}.q-charthead{display:flex;justify-content:space-between;gap:25px;padding:29px 35px 0;font-size:25px}.q-charthead span{font-size:23px;color:#A5ACB8}.q-plot{width:1000px;height:500px;display:block}.q-curve{fill:none;stroke-width:4;stroke-linecap:round;stroke-linejoin:round}.q-series0{stroke:#F16B74}.q-series1{stroke:#A5ACB8;stroke-width:2.5}.q-grid{stroke:#36363e;stroke-width:1}.q-mark{fill:#F16B74;stroke:#F5F5F7;stroke-width:2}.q-pointer{fill:#F5F5F7;stroke:#0D0D0F;stroke-width:2}.q-tip{position:absolute;background:#222225;border:1px solid #5b626e;padding:18px 23px;border-radius:14px;font-size:24px;line-height:1.8;color:#F5F5F7}.q-tip b{font-size:29px}.q-chartlabels{position:absolute;bottom:19px;left:35px;right:35px;display:flex;justify-content:space-between;font-size:21px;color:#A5ACB8}.q-chartlabels i{font-style:normal}',f'''
tl.fromTo('.q-title',{{x:-45,opacity:0}},{{x:0,opacity:1,duration:.65,ease:'power3.out'}},.1);
tl.fromTo('.q-chart',{{y:60,opacity:0,scale:.94}},{{y:0,opacity:1,scale:1,duration:.8,ease:'power3.out'}},.3);
document.querySelectorAll('.q-curve').forEach((p,i)=>{{const len=p.getTotalLength();p.style.strokeDasharray=len;p.style.strokeDashoffset=len;tl.to(p,{{strokeDashoffset:0,duration:3.4,ease:'power1.inOut'}},.7+i*.25);}});
tl.fromTo('.q-check',{{y:30,opacity:0}},{{y:0,opacity:1,duration:.5,stagger:.55,ease:'power3.out'}},3.7);
tl.fromTo('.q-mark',{{opacity:0,scale:0}},{{opacity:1,scale:1,duration:.35,transformOrigin:'center center'}},6.7);
tl.fromTo('.q-pointer',{{x:700,y:300,opacity:0}},{{x:{tx+7:.3f},y:{ty+10:.3f},opacity:1,duration:.9,ease:'power3.inOut'}},6.45);
tl.fromTo('.q-tip',{{opacity:0,y:10,scale:.96}},{{opacity:1,y:0,scale:1,duration:.45,ease:'power3.out'}},7.4);
tl.fromTo('.q-detail',{{opacity:0,y:18}},{{opacity:1,y:0,duration:.55,ease:'power3.out'}},7.7);
''')
    # 05: current state remains visible; no fake execution.
    body='''<div class="q-stack"><div class="q-kicker">让策略经受时间检验</div><h1 class="q-title">先<span class="q-blue">观察。</span><br>再模拟。</h1><div class="q-approval"><span>实盘运行</span><b>需要人工授权</b></div></div><div class="q-stages"><div class="q-stage q-active"><span>01 · 进行中</span><b>未来观察</b><small>3 个策略 · 0 / 20 日</small></div><div class="q-stage q-wait"><span>02 · 等待验证</span><b>模拟盘</b><small>当前模拟成交 0</small></div><div class="q-stage q-lock"><span>03 · 未启用</span><b>实盘审查</b><small>人工授权门槛</small></div></div>'''+screen('q-review','review.png',799,581,1026,279)+'''<div class="q-iteration">复盘 → 新假设 → 新版本验证</div>'''
    write(5,'control',11,body,'.q-stack{top:216px;width:640px}.q-title{font-size:99px}.q-stages{position:absolute;left:799px;top:242px;display:flex;gap:19px}.q-stage{width:329px;height:216px;border:1px solid #363e49;border-radius:17px;background:#19191C;padding:25px}.q-stage>span{color:#A5ACB8;font-size:23px}.q-stage b{display:block;font-size:38px;font-weight:650;margin:25px 0 18px}.q-stage small{color:#A5ACB8;font-size:24px}.q-active{border-color:#80B9FF}.q-active b{color:#80B9FF}.q-approval{margin-top:40px;border-top:1px solid #36363e;padding-top:22px}.q-approval>span{display:block;font-size:24px;color:#A5ACB8;margin-bottom:12px}.q-approval>b{font-size:36px;font-weight:600}.q-iteration{position:absolute;left:804px;top:498px;font-size:28px;color:#80B9FF}.q-screenhead span{font-size:21px}.q-review .q-screenbody img{width:100%;margin-top:-3px}', '''
tl.fromTo('.q-title',{y:40,opacity:0},{y:0,opacity:1,duration:.65,ease:'power3.out'},.1);
tl.fromTo('.q-active',{y:55,opacity:0},{y:0,opacity:1,duration:.6,ease:'power3.out'},.5);
tl.fromTo('.q-wait',{x:65,opacity:0},{x:0,opacity:1,duration:.65,ease:'power3.out'},2.3);
tl.fromTo('.q-review',{y:55,opacity:0},{y:0,opacity:1,duration:.85,ease:'power3.out'},4.05);
tl.fromTo('.q-iteration',{x:-35,opacity:0},{x:0,opacity:1,duration:.7,ease:'power3.out'},5.35);
tl.fromTo('.q-lock',{x:50,opacity:0},{x:0,opacity:1,duration:.6,ease:'power3.out'},7.45);
tl.fromTo('.q-approval',{y:25,opacity:0},{y:0,opacity:1,duration:.6,ease:'power3.out'},8.0);
''')
    # 06: real complete UI is the closing hero, not invented mock trading performance.
    body='''<div class="q-stack"><div class="q-kicker">FROM HYPOTHESIS TO EVIDENCE</div><h1 class="q-title q-brand">Quant</h1><h2 class="q-productname">AI 量化研究工作台</h2><p class="q-body q-promise">把研究，变成<br><span class="q-blue">可追踪的证据。</span></p><div class="q-cta">查看完整演示 <span>↗</span></div></div>'''+screen('q-dashboard','dashboard.png',732,193,1090,659)+'''<div class="q-sections"><span>市场</span><span>策略</span><span>交易</span><span>复盘</span></div>'''
    write(6,'close',9,body,'.q-stack{top:205px;width:670px}.q-brand{font-size:147px;letter-spacing:-7px}.q-productname{font-size:40px;font-weight:550;margin:16px 0 0}.q-promise{font-size:43px;line-height:1.55;margin-top:28px}.q-cta{display:inline-flex;align-items:center;gap:42px;padding:17px 27px;margin-top:34px;border:1px solid #5a697e;border-radius:13px;font-size:28px}.q-cta span{color:#80B9FF}.q-sections{position:absolute;left:749px;top:873px;display:flex;gap:26px;color:#A5ACB8;font-size:24px}.q-dashboard .q-screenbody img{margin-top:-270px}', '''
tl.fromTo('.q-brand',{y:40,opacity:0},{y:0,opacity:1,duration:.7,ease:'power3.out'},.1);
tl.fromTo('.q-productname',{opacity:0,y:22},{opacity:1,y:0,duration:.6,ease:'power3.out'},.55);
tl.fromTo('.q-dashboard',{scale:.86,x:105,y:45,rotationY:-7,opacity:0},{scale:1,x:0,y:0,rotationY:0,opacity:1,duration:1.4,ease:'power3.out'},.3);
tl.fromTo('.q-sections span',{y:16,opacity:0},{y:0,opacity:1,duration:.4,stagger:.36,ease:'power2.out'},1.25);
tl.to('.q-dashboard',{scale:1.035,x:-18,duration:2.3,ease:'power2.inOut'},2.0);
tl.to('.q-dashboard img',{y:-305,duration:1.9,ease:'power3.inOut'},3.65);
tl.fromTo('.q-promise',{y:26,opacity:0},{y:0,opacity:1,duration:.75,ease:'power3.out'},4.5);
tl.fromTo('.q-cta',{y:14,opacity:0},{y:0,opacity:1,duration:.6,ease:'power3.out'},5.7);
tl.to('.q-dashboard',{scale:1,x:0,duration:1.3,ease:'power3.inOut'},6.05);
''')
    storyboard=ROOT/'STORYBOARD.md';storyboard.write_text(storyboard.read_text().replace('- status: outline','- status: animated'))
    # Vendor GSAP for an offline, reproducible composition.
    vendor=ROOT/'assets/gsap.min.js'
    if not vendor.exists():vendor.write_bytes(urllib.request.urlopen('https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js').read())
    (ROOT/'BUILD-NOTES.md').write_text('''# Production notes

Built as six independent frame compositions using the inline serial fallback. No native subagents or extra headless Codex runs were launched.
Successful source captures: overview, market, research, review-v2. The blank first review capture was rejected.
Cloud TTS returned 403. Review narration uses local Kokoro ONNX Chinese male voice; public product copy only was sent in the failed request.
Music is an original deterministic 120-BPM electronic composition, with no third-party samples.
No original quant system files or business rules are modified by this video project.
''')
    print('Built 6 seekable frame compositions.')


if __name__=='__main__':main()
