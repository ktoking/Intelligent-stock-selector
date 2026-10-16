'use strict';
const $=id=>document.getElementById(id);
const format=new Intl.NumberFormat('en-US',{maximumFractionDigits:0});
const money=n=>n==null?'—':'$'+format.format(n);
const pct=n=>Number.isFinite(n)?(n>0?'+':'')+n.toFixed(2)+'%':'—';
const dateTime=v=>v?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(v)):'—';
const stageNames={proposed:'候选',backtested:'已回测',validated:'已验证',shadow:'未来观察',paper:'模拟运行',live_review:'人工审查',rejected:'未通过',retired:'已退出模拟'};
const modeNames={market_open:'开市监控',idle:'休市待机',startup_reconcile:'启动对账',pre_open:'开盘预检',daily_research:'收盘研究',close_settlement:'收盘封存',order_recovery:'订单核对',paper_promotion:'模拟晋升核对'};
const gateNames={test_sample:'样本数量',test_activity:'交易活跃度',drawdown:'回撤门槛',validation_and_test_profit:'验证盈利',high_cost_profit:'高成本盈利',delayed_profit:'延迟敏感性',qqq_excess:'QQQ 超额收益'};
const eventNames={paper_plan:'生成模拟交易计划',order_deferred_risk:'风控推迟委托',order_skipped_price_drift:'价格偏离，跳过委托',cancel_requested:'申请撤销模拟委托',radar_failed:'全池扫描失败',news_failed:'资讯更新失败',evolution_failed:'因子迭代失败',universe_scanned:'完成全池扫描',ai_review:'Codex 完成每日复盘',ai_discovery:'Codex 提出研究候选',stage:'策略阶段更新',price_anomaly:'观察到价格异动',strategy_proposed:'注册策略候选',duplicate_proposal:'合并重复策略',daily_universe_intake:'完成异动标的入池',research_trial_cap:'达到当前版本试验上限',factor_code_generated:'生成新因子代码',news_refreshed:'更新资讯证据',index_members_refreshed:'刷新指数成分股',ai_unavailable:'Codex 分析暂不可用',pause:'执行已暂停',operator_resume:'人工恢复监控',shadow_observed_plan:'记录未来观察计划',refresh_failed:'历史更新失败',daily_job_failed:'每日任务失败',research_failed:'候选生成失败',validation_failed:'验证失败'};
Object.assign(eventNames,{preflight_ready:'开盘前检查通过',preflight_blocked:'开盘前检查有阻断',manual_preflight:'完成当前执行检查',quote_batch_degraded:'部分报价不可用',strategy_quote_deferred:'策略等待执行条件',supervisor_deferred:'本轮读取受阻，等待重试',supervisor_failed:'监督循环检查失败',cancel_uncertain:'撤单结果待核对',cancel_retry_cap:'撤单重试达上限'});
Object.assign(eventNames,{account_capital_expanded:'模拟账户资金授权已扩展',champion_rotation_queued:'优胜策略交接已排队',champion_rotation_status:'账户策略交接进度',rotation_cancel_pending:'旧委托撤销等待核对',paper_order_quote_deferred:'该标的报价阻断委托',paper_window_missed:'入场窗口结束，存在未执行订单',shadow_window_missed:'Shadow未取得有效观察计划',data_finalization_checked:'收盘日线再次核对完成',execution_data_appended:'追加冻结策略观察数据',execution_history_recovered:'完成冻结数据修订核对',option_execution_status:'期权执行状态变化'});
let state=null, selected=null, chartMode='history', chartCache=new Map(), loading=false, chartRequest=0;
let highestEventSeq=0, receivedAt=0, serverOffset=0;
const highlights=new Map(), FLASH_MS=90000;
let activityData=null, activityDay='all', activityStrategy=null, dailyLimit=20, tradeLimit=30;
const activityCache=new Map();
const forecastCache=new Map();
let holdingsScope='stock';
const historicalHoldingsCache=new Map();
let historicalHoldingsPending=null;
const renderKeys=new Map(),rowCaches=new Map(),scrollPositions=new Map();
let graphRenderedKey=null,activityRenderedKey=null,currentView='overview',tooltipOwner=null;
const viewInfo={overview:['总览','运行、收益与需要关注的变化。'],research:['策略','先观察，再用未来记录验证。'],market:['市场','从全池异动到值得跟踪的线索。'],paper:['模拟','委托、成交与实际账本收益。'],review:['每日复盘','重点、观察条件与可能改变判断的信息。'],runtime:['运行','数据、任务与每一步验证。']};
const viewAliases={'strategy-activity':'research',evolution:'research',news:'market',strategies:'research'};
function renderPart(key,inputs,render){const before=JSON.stringify(inputs());if(renderKeys.get(key)===before)return;render();renderKeys.set(key,JSON.stringify(inputs()));}
function reconcileChildren(container,nodes){
 if(!container.insertBefore){container.replaceChildren(...nodes);return;}
 for(let i=0;i<nodes.length;i++)if(container.children[i]!==nodes[i])container.insertBefore(nodes[i],container.children[i]||null);
 for(const node of [...container.children])if(!nodes.includes(node))container.removeChild(node);
}
function keyedRows(id,items,key,build){
 const cache=rowCaches.get(id)||new Map(),next=new Map(),nodes=[];
 for(const item of items){const ident=key(item),signature=JSON.stringify(item),old=cache.get(ident);const node=old?.signature===signature?old.node:build(item);next.set(ident,{signature,node});nodes.push(node);}
 reconcileChildren($(id),nodes);rowCaches.set(id,next);
}
function showView(target,scroll=true){
 const view=viewAliases[target]||(target in viewInfo?target:'overview');
 if(view!==currentView){scrollPositions.set(currentView,globalThis.scrollY||0);$('chart-tooltip').hidden=true;tooltipOwner=null;}
 currentView=view;
 document.querySelectorAll('[data-page]').forEach(node=>node.hidden=node.dataset.page!==view);
 document.querySelectorAll('nav a').forEach(link=>{const active=link.getAttribute('href')==='#'+view;link.classList.toggle('active',active);if(active)link.setAttribute('aria-current','page');else link.removeAttribute?.('aria-current');});
 text('page-title',viewInfo[view][0]);text('page-subtitle',viewInfo[view][1]);
 if(target==='evolution')$('evolution-details').open=true;
 if(scroll){if(target in viewAliases&&target!=='strategies')$(target).scrollIntoView?.({block:'start'});else globalThis.scrollTo?.({top:scrollPositions.get(view)||0,behavior:'instant'});}
}
function navigate(target){showView(target);if(globalThis.location&&globalThis.location.hash!=='#'+target)globalThis.location.hash='#'+target;}
const importantEvents=new Set(['price_anomaly','fill','stage','pause','ai_unavailable','refresh_failed','daily_job_failed','research_failed','validation_failed','reserved','shadow_observed_plan','paper_plan','order_deferred_risk','order_skipped_price_drift','cancel_requested','radar_failed','news_failed','evolution_failed']);
for(const name of ['paper_order_quote_deferred','paper_window_missed','option_execution_status'])importantEvents.add(name);
const direction=n=>Number.isFinite(n)?n>0?'up':n<0?'down':'flat':'muted';
const strategyName=r=>r.display_name||state?.strategies?.find(s=>s.full_id===(r.full_id||r.strategy||r.id))?.display_name||({'trend_buffer15':'趋势缓冲','momentum_blend20':'多周期动量','residual_momentum20':'残差动量','residual_pressure_discount':'压力回撤'}[r.name]||r.name.replaceAll('_',' '));
function signedValue(id,value,label){text(id,label);$(id).className=direction(value);}
function highlight(key,tone='notice',label='已更新'){highlights.set(key,{until:Date.now()+FLASH_MS,tone,label});}
function flagged(key){const h=highlights.get(key);return h&&h.until>Date.now()?h:null;}
function markRow(row,key){const h=flagged(key);if(h)row.className='row-highlight '+h.tone;}
function returnFor(r){return chartMode==='forward'?r.forward_return_pct:chartMode==='paper'?r.paper_return_pct:r.stats?.retrospective_test?.return_pct;}
function sortedReturns(records){return records.slice().sort((a,b)=>{const av=returnFor(a),bv=returnFor(b),af=Number.isFinite(av),bf=Number.isFinite(bv);return af!==bf?(af?-1:1):af&&av!==bv?bv-av:a.name.localeCompare(b.name);});}
function scopedStrategies(){return state.strategies.filter(r=>$('all-versions').checked||r.data_hash===state.focus_data_hash);}
function marketHits(snapshot){
 return (snapshot.universe_scan?.hits||[]).map(hit=>{const q=snapshot.focus_quotes?.[hit.symbol];if(!q||q.data_date!==hit.data_date||!Number.isFinite(q.price)||q.price<=0)return hit;const previousClose=hit.price/(1+hit.change_pct/100);return {...hit,price:q.price,change_pct:(q.price/previousClose-1)*100,quote_source:q.source,quote_updated_at:q.updated_at};});
}
function recordChanges(previous){
 const events=state.events||[],newEvents=events.filter(e=>e.seq>highestEventSeq);
 if(previous){
  for(const e of newEvents){if(importantEvents.has(e.kind))highlight('event:'+e.seq,e.kind==='fill'?'fill':e.kind==='price_anomaly'?direction(e.payload.change_pct):e.kind==='pause'||e.kind.endsWith('failed')||e.kind==='ai_unavailable'?'alert':'notice','新触发');if(e.kind==='price_anomaly')highlight('mover:'+e.payload.symbol,direction(e.payload.change_pct),'新异动');if(e.kind==='stage')highlight('strategy:'+e.payload.id,'notice','阶段变化');}
  const oldHits=new Map(marketHits(previous).map(r=>[r.symbol,r]));
  for(const r of marketHits(state)){const old=oldHits.get(r.symbol);if(!old)highlight('mover:'+r.symbol,direction(r.change_pct),'新入选');else if(old.price!==r.price)highlight('mover:'+r.symbol,direction(r.price-old.price),'报价更新');}
  const oldOrders=new Map((previous.orders||[]).map(o=>[o.id,o]));
  for(const o of state.orders||[]){const old=oldOrders.get(o.id);if(!old||old.status!==o.status||old.filled!==o.filled)highlight('order:'+o.id,o.filled>(old?.filled||0)?'fill':o.status==='unknown'||o.status==='rejected'?'alert':'notice',o.filled>(old?.filled||0)?'成交增加':'状态变化');}
  const oldFills=new Set((previous.paper_activity?.fills||[]).map(f=>f.seq));
  for(const f of state.paper_activity?.fills||[])if(!oldFills.has(f.seq))highlight('fill:'+f.seq,'fill','新成交');
 }
 highestEventSeq=Math.max(highestEventSeq,...events.map(e=>e.seq||0));
}
function eventLabel(e){
 const p=e.payload||{},order=(state.orders||[]).find(o=>o.id===p.id);
 if(e.kind==='price_anomaly')return p.symbol.replace('US.','')+' · 日涨跌 '+pct(p.change_pct)+' · 数据日 '+(p.data_date||'—');
 if(e.kind==='fill')return (order?.symbol?.replace('US.','')||'模拟订单')+' · '+(order?.side==='SELL'?'卖出':'买入')+'成交增量 '+p.delta_qty;
 if(e.kind==='stage')return (state.strategies.find(r=>r.full_id===p.id)?.name||'策略')+' · '+(stageNames[p.from]||p.from)+' → '+(stageNames[p.to]||p.to);
 if(['shadow_observed_plan','paper_plan'].includes(e.kind))return (eventNames[e.kind]||'生成计划')+' · '+(p.orders?.length||0)+' 项计划'+(e.kind==='shadow_observed_plan'?'（假设成交）':'');
 if(e.kind==='reserved')return p.symbol?.replace('US.','')+' · 已预留 '+p.qty+' 股';
 return eventNames[e.kind]||'研究记录';
}
function renderTriggers(){
 const rows=(state.events||[]).filter(e=>importantEvents.has(e.kind)).slice(0,4),fresh=(state.events||[]).filter(e=>flagged('event:'+e.seq)).length;
 text('trigger-status',fresh?fresh+' 条新动态 · 高亮 90 秒':'最近已记录 · 等待新触发');$('trigger-list').replaceChildren();
 for(const e of rows){const h=flagged('event:'+e.seq),item=element('div','trigger-item '+(h?'fresh '+h.tone:''));item.append(element('time',null,dateTime(e.created)),element('span',null,eventLabel(e)));if(h)item.append(element('b','trigger-badge',e.kind==='fill'?'新成交':'新触发'));$('trigger-list').append(item);}
 if(!rows.length)$('trigger-list').append(element('span','muted small','尚无异动、成交或策略阶段记录。'));
 text('refresh-proof','页面读取 '+dateTime(receivedAt)+' · 每 15 秒刷新 · 全池快照采集 '+dateTime(state.universe_scan?.created_at));
}
function updateClock(){
 const now=Date.now()+serverOffset;
 const clock=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(now);
 text('clock',clock+' / CST');text('timeline-clock',clock+' 北京时间');
 if(!state?.timeline)return;
 const t=state.timeline,nodes=t.milestones,stamps=nodes.map(n=>new Date(n.at).getTime());
 let index=-1;stamps.forEach((at,i)=>{if(now>=at)index=i;});
 const names=['开盘预检','开盘 · 日线计划窗口','持续监控','收盘封存','研究与复盘'];
 text('timeline-title',t.session+' · 美股交易日');text('timeline-phase',index<0?'等待开盘':index===4?'研究时点已到达 · '+(modeNames[state.schedule?.mode]||'等待任务'):names[index]);
 const progress=index<0?0:index===stamps.length-1?100:(index+(now-stamps[index])/(stamps[index+1]-stamps[index]))/(stamps.length-1)*100;
 $('timeline-progress').style.width=progress+'%';$('timeline-cursor').style.left=progress+'%';
 $('timeline-track').classList.toggle('upcoming',index<0);
 for(const [i,node] of [...$('timeline-nodes').children].entries()){node.classList.toggle('reached',i<=index);node.classList.toggle('current',i===index);}
 const target=index<1?new Date(t.open).getTime():index<3?new Date(t.close).getTime():index<4?stamps[4]:new Date(state.calendar.next_open).getTime();
 let seconds=Math.max(0,Math.ceil((target-now)/1000));const days=Math.floor(seconds/86400);seconds%=86400;const hours=Math.floor(seconds/3600),minutes=Math.floor(seconds%3600/60),secs=seconds%60;
 const countdown=(days?days+' 天 ':'')+String(hours).padStart(2,'0')+':'+String(minutes).padStart(2,'0')+':'+String(secs).padStart(2,'0');
 text('timeline-countdown',(index<1?'距开盘 ':index<3?'距收盘 ':index<4?'距研究时点 ':'距下次开盘 ')+countdown);
 text('timeline-note','时间节点进度 · 任务状态见「任务与运行」 · 等距阶段轴');
}
function renderTimeline(){
 $('timeline-nodes').replaceChildren();for(const n of state.timeline?.milestones||[]){const item=element('div','timeline-node');item.append(element('i'),element('b',null,n.label),element('time',null,dateTime(n.at)));$('timeline-nodes').append(item);}updateClock();
}
function renderRanking(){
 const records=sortedReturns(scopedStrategies().filter(r=>r.chart_available||r.stage==='shadow'||r.stage==='paper'));
 const options=$('strategy-select');options.replaceChildren();for(const r of records){const option=element('option',null,strategyName(r)+' · '+stageNames[r.stage]+' · '+pct(returnFor(r)));option.value=r.full_id;options.append(option);}
 if(!records.some(r=>r.full_id===selected))selected=records[0]?.full_id;
 options.value=selected||'';
 const benchmark=chartMode==='history'?records.find(r=>r.full_id===selected)?.benchmark?.return_pct:null;
 const entries=records.map(r=>({name:strategyName(r),value:returnFor(r),record:r}));if(chartMode==='history')entries.push({name:'QQQ 基准',value:benchmark});
 entries.sort((a,b)=>Number.isFinite(a.value)!==Number.isFinite(b.value)?Number.isFinite(a.value)?-1:1:Number.isFinite(a.value)&&a.value!==b.value?b.value-a.value:a.name.localeCompare(b.name));
 text('ranking-note',(chartMode==='history'?'历史回测收益降序 · 同一数据版本 · 预测单独展示':chartMode==='forward'?'Shadow 已观察收益降序 · 等待实际观察记录':'券商模拟收益 · 无实际成交列末')+($('all-versions').checked?' · 已显示全部版本，区间不同不能直接比较':''));
 $('return-ranking').replaceChildren();for(const [i,r] of entries.entries()){const item=element(r.record?'button':'div','ranking-item '+(r.record?.full_id===selected?'selected':'')+(!r.record?' benchmark':''));item.append(element('span','rank-number',String(i+1).padStart(2,'0')),element('span','rank-name',r.name),element('b',direction(r.value),pct(r.value)));if(r.record)item.addEventListener('click',()=>{selected=r.record.full_id;renderRanking();renderStrategies();renderChart();});$('return-ranking').append(item);}
}

function element(tag,className,text){const n=document.createElement(tag);if(className)n.className=className;if(text!=null)n.textContent=text;return n;}
function text(id,value){const node=$(id),next=String(value??'');if(node.textContent!==next)node.textContent=next;}
function setWarning(message){$('warning').hidden=!message;text('warning',message||'');}
function svgEl(tag,attrs={}){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,String(v));return n;}
function emptyChart(container,title,description){container.replaceChildren();const box=element('div','empty-chart');box.append(element('span','empty-icon','⌁'),element('b',null,title),element('p',null,description));container.append(box);}
function applyTheme(mode){
 mode=mode==='light'?'light':'dark';document.documentElement.dataset.theme=mode;
 $('theme-toggle').setAttribute('aria-pressed',String(mode==='light'));$('theme-toggle').setAttribute('aria-label','当前'+(mode==='light'?'白天':'黑夜')+'主题，切换到'+(mode==='light'?'黑夜':'白天'));
 text('theme-icon',mode==='light'?'☾':'☀');text('theme-label',mode==='light'?'黑夜':'白天');
 try{localStorage.setItem('quant-lab-theme',mode);}catch(_){}
}
function initTheme(){let mode='dark';try{mode=localStorage.getItem('quant-lab-theme')||mode;}catch(_){}applyTheme(mode);}
function positionChartTooltip(tip,cx,cy){
 tip.hidden=false;const vw=globalThis.innerWidth||1024,vh=globalThis.innerHeight||768;
 const width=tip.offsetWidth||Math.min(340,vw-24),height=tip.offsetHeight||150;
 let left=cx+14,top=cy+14;if(left+width>vw-12)left=cx-width-14;if(top+height>vh-12)top=cy-height-14;
 tip.style.left=Math.max(12,Math.min(left,vw-width-12))+'px';tip.style.top=Math.max(12,Math.min(top,vh-height-12))+'px';
}
function renderOverview(){
 const p=state.paper_activity||{},positions=Object.values(state.portfolios.stock.positions),active=state.strategies.filter(r=>['shadow','paper','live_review'].includes(r.stage));
 const shadow=active.filter(r=>r.stage==='shadow'),paper=active.filter(r=>r.stage==='paper');
 const d=state.dashboard||{session:state.timeline.session,today_pnl:p.fill_count?null:0,today_return_pct:p.fill_count?null:0,position_count:positions.length,exposure_pct:positions.reduce((s,r)=>s+r.qty*r.mark*r.multiplier,0)/(p.nav||10000)*100,today_orders:0,today_fills:0,working_orders:state.orders.filter(r=>!['filled','cancelled','rejected'].includes(r.status)).length,valuation_note:'等待最新账本核对。',attention:[],freshness:[]};
 text('overview-state',state.paused?'执行已暂停':paper.length?paper.length+' 个策略模拟运行':shadow.length?shadow.length+' 个策略未来观察':'等待策略验证');
 const progress=shadow.map(r=>r.shadow_sessions),min=progress.length?Math.min(...progress):0,max=progress.length?Math.max(...progress):0;
 text('overview-progress',shadow.length?'已观察 '+(min===max?min:min+'–'+max)+' / '+state.min_shadow_sessions+' 个交易日':'股票预算 $10,000 · 期权研究 $1,000');
 text('overview-day-label','今日损益 · '+d.session.slice(5).replace('-','/'));signedValue('overview-pnl',d.today_pnl,dollars(d.today_pnl));text('overview-pnl-sub',p.fill_count?(Number.isFinite(d.today_return_pct)?pct(d.today_return_pct)+' · 含持仓估值':'等待新估值'):'初始资金 · 暂无成交');
 text('overview-nav',dollars(p.nav));text('overview-nav-sub',p.fill_count?'累计收益 '+pct(p.return_pct):'股票模拟预算 $10,000');
 text('overview-exposure',pct(d.exposure_pct).replace(/^\+/,''));text('overview-exposure-sub',d.position_count+' 个标的 · 可用 '+dollars(state.portfolios.stock.cash));
 text('overview-orders',d.today_orders+' / '+d.today_fills);text('overview-orders-sub',d.working_orders+' 笔未完成委托');text('overview-active',shadow.length+' / '+paper.length);text('overview-active-sub',state.strategies.length+' 个研究版本 · '+state.strategies.filter(r=>r.stage==='rejected').length+' 个已淘汰');text('overview-valuation-note',d.valuation_note);
 renderPart('freshness',()=>d.freshness,()=>{
  $('freshness-list').replaceChildren();$('freshness-detail').replaceChildren();
  for(const r of d.freshness){const item=element('span','freshness-item '+r.tone);item.append(element('i'),document.createTextNode(r.name+' · '+r.label+(r.asof?' '+r.asof.slice(5).replace('-','/'):'')));$('freshness-list').append(item);const row=element('div');row.append(element('b',null,r.name+' · '+r.label),element('small',null,'采集 / 生成 '+dateTime(r.observed_at)),element('small',null,r.asof?'数据基准 '+r.asof:r.id==='service'?'本地服务心跳，与行情时间独立':'标题证据，仍需原文核实'));$('freshness-detail').append(row);}
 });
 renderPart('attention',()=>[d.attention,state.paused,shadow.length,p.fill_count],()=>{
  $('attention-list').replaceChildren();text('attention-count',d.attention.length?d.attention.length+' 项':'正常');
  if(!d.attention.length){const row=element('div','attention-clear'),title=element('b');title.append(element('span','check-circle','✓'),document.createTextNode('没有待处理异常'));row.append(title,element('p',null,p.fill_count?'继续核对成交与风险预算。':shadow.length?'策略仍在未来观察阶段，尚未产生模拟成交。':'等待候选通过验证。'));$('attention-list').append(row);}
  for(const r of d.attention){const row=element('a','attention-item '+r.tone);row.href='#'+r.target;row.addEventListener('click',e=>{e.preventDefault();navigate(r.target);});row.append(element('b',null,r.title),element('p',null,r.detail));$('attention-list').append(row);}
 });
 text('universe-count',String(state.universe_scan?.scanned||0));text('history-count',state.daily_brief?.coverage?.history_stocks??'—');text('universe-note','纳指100 '+(state.universe_scan?.nasdaq100_count||0)+' · 标普500 '+(state.universe_scan?.sp500_count||0)+' · 自选去重 · 日线统计截至 '+(state.daily_brief?.coverage?.asof||'—'));
}
function renderActiveStrategies(){
 const records=state.strategies.filter(r=>['shadow','paper','live_review'].includes(r.stage));text('active-count',records.filter(r=>r.stage==='paper').length+' 个模拟 · '+records.filter(r=>r.stage==='shadow').length+' 个观察');
 keyedRows('active-strategy-list',records,r=>r.full_id,r=>{
  const card=element('button','active-strategy');card.setAttribute('aria-label','查看策略 '+strategyName(r));card.append(element('strong',null,strategyName(r)),element('small',null,r.identity_note||'版本 '+r.id),element('p',null,r.logic.summary),element('span','status-tag '+r.stage,stageNames[r.stage]));
  if(r.stage==='paper'&&r.paper_allocation!=null){const rotation=state.champion_rotation,pending=rotation&&rotation.status!=='completed';card.append(element('small',null,pending?(rotation.target===r.full_id?'账户择优通过 · 交收后集中股票资金':'退出队列 · 撤旧单并卖出本策略持仓'):'股票分配 '+dollars(r.paper_allocation)+' · 用户授权跳过观察等待；不代表未来验证通过'));}
  if(r.stage==='shadow'){const track=element('div','observation-progress'),bar=element('i');bar.style.width=Math.min(100,r.shadow_sessions/state.min_shadow_sessions*100)+'%';track.append(bar);card.append(track,element('small',null,r.shadow_sessions+' / '+state.min_shadow_sessions+' 个观察日 · '+(r.forward_return_pct==null?'尚无已封存收益':pct(r.forward_return_pct))));}
  else card.append(element('small',null,'策略可归属模拟收益 '+pct(r.paper_return_pct)));
  card.addEventListener('click',()=>openStrategy(r.full_id,r.stage==='paper'?'paper':'forward'));return card;
 });if(!records.length)$('active-strategy-list').append(element('p','empty-note','还没有进入未来观察或模拟的策略。'));
}
function renderBriefSummary(){
 text('brief-title','复盘重点'+(state.daily_brief?.asof?' · '+state.daily_brief.asof.slice(5).replace('-','/')+' 收盘':''));
 const b=state.daily_brief?.brief;$('brief-glance').replaceChildren();$('review-summary').replaceChildren();
 if(!b){$('brief-glance').append(element('p','empty-note','等待收盘复盘。'));$('review-summary').append(element('p','empty-note','暂无已生成简报。'));return;}
 for(const r of b.key_events.slice(0,3)){const item=element('div');item.append(element('b',null,r.title),element('p',null,r.judgment));$('brief-glance').append(item);}
 for(const [key,title] of [['watch','重点观察'],['avoid','注意风险'],['changes','判断变化']]){const card=element('article');card.append(element('h3',null,title));for(const line of b.conclusions[key].slice(0,2)){const p=element('p');richText(p,line);card.append(p);}if(!b.conclusions[key].length)card.append(element('p',null,'暂无足够证据支持新增结论。'));$('review-summary').append(card);}
}
function renderForecastAudit(){
 const t=state.forecast_tracking||{archived:0,evaluated:0,pending:0,rows:[],stats:{}};text('forecast-audit-count',t.archived+' 份封存 · '+t.evaluated+' 份核对');$('forecast-audit-summary').replaceChildren();$('forecast-audit-list').replaceChildren();
 for(const [label,value,note] of [['已核对样本',String(t.evaluated),t.pending+' 份等待到期'+(t.unavailable?' · '+t.unavailable+' 份材料待核对':'')],['策略误差',t.stats.strategy_mae_pp==null?'—':t.stats.strategy_mae_pp.toFixed(2)+' pp','平均绝对误差 · 冻结规则复算'],['QQQ 区间命中率',t.stats.benchmark_coverage_pct==null?'—':t.stats.benchmark_coverage_pct.toFixed(0)+'%',(t.stats.benchmark_samples||0)+' 个去重样本 · 未校准区间']]){const card=element('div');card.append(element('span',null,label),element('strong',null,value),element('small',null,note));$('forecast-audit-summary').append(card);}
 for(const r of (t.rows||[]).slice(0,12)){const row=element('div','audit-row');row.append(element('div',null,strategyName(r)+' · '+r.origin_date),element('div',null,'预测至 '+r.end_date),element('div',null,r.state==='evaluated'?'策略误差 '+r.strategy_error_pp.toFixed(2)+' pp':r.state==='unavailable'?'材料待核对':'等待行情到期'));row.children[0].append(element('small',null,'版本 '+r.strategy.slice(0,12)));row.children[2].append(element('small',null,r.reason||'封存时间 '+dateTime(r.created_at)));$('forecast-audit-list').append(row);}
 if(!t.archived)$('forecast-audit-list').append(element('p','empty-note','预测将在第一场未来交易日开盘前封存；不回填已经发生的行情。'));
}
function lineChart(container,curve,base,drawdown=false,trades=[],forecast=null,initialCapital=10000){
 container.replaceChildren();if(!curve.length){emptyChart(container,chartMode==='forward'?'等待首个未来观察日':chartMode==='paper'?'等待券商模拟成交':'尚无可核验曲线','未形成实际证据，暂不绘制净值路径。');return;}
 const future=!drawdown&&forecast?.available&&forecast.origin_date===curve.at(-1).date;
 const W=800,H=drawdown?158:286,margin={l:39,r:15,t:future?35:14,b:27},pw=W-margin.l-margin.r,ph=H-margin.t-margin.b;
 let peak=initialCapital;const points=curve.map(r=>{peak=Math.max(peak,r.equity);return {...r,value:drawdown?(r.equity/peak-1)*100:(r.equity/initialCapital-1)*100};});
 const qqq=base.map(r=>({...r,value:(r.equity/10000-1)*100}));let values=points.map(r=>r.value);if(!drawdown)values=values.concat(qqq.map(r=>r.value));
 const mapPrediction=rows=>rows.map(r=>({...r,value:(r.equity/10000-1)*100,low:(r.low_equity/10000-1)*100,high:(r.high_equity/10000-1)*100}));
 const predicted=future?mapPrediction(forecast.strategy_curve):[],predictedQQQ=future?mapPrediction(forecast.benchmark_curve):[];
 if(future)values=values.concat([...predicted,...predictedQQQ].flatMap(r=>[r.low,r.high]));
 let low=Math.min(0,...values),high=drawdown?0:Math.max(0,...values);const span=high-low||1;low-=span*.1;if(!drawdown)high+=span*.12;
 const pastWidth=future?pw*.72:pw,split=margin.l+pastWidth;
 const x=i=>margin.l+i/Math.max(1,points.length-1)*pastWidth,fx=i=>split+i/7*(pw-pastWidth),y=v=>margin.t+(high-v)/(high-low)*ph;
 const svg=svgEl('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':drawdown?'每日净值回撤路径':future?'策略和QQQ历史收益与未来七个交易日统计预测':'策略和 QQQ 的历史收益路径'});
 const defs=svgEl('defs'),gradient=svgEl('linearGradient',{id:container.id+'-fill',x1:0,x2:0,y1:0,y2:1});gradient.append(svgEl('stop',{offset:'0%','stop-color':drawdown?'var(--green)':'var(--red)','stop-opacity':'.16'}),svgEl('stop',{offset:'100%','stop-color':drawdown?'var(--green)':'var(--red)','stop-opacity':'.015'}));defs.append(gradient);svg.append(defs);
 for(let i=0;i<5;i++){const v=low+(high-low)*i/4,yy=y(v);svg.append(svgEl('line',{x1:margin.l,x2:W-margin.r,y1:yy,y2:yy,class:'chart-gridline'}));const label=svgEl('text',{x:margin.l-9,y:yy+3,'text-anchor':'end',class:'chart-axis'});label.textContent=v.toFixed(0)+'%';svg.append(label);}
 const line=points.map((p,i)=>(i?'L':'M')+x(i).toFixed(2)+','+y(p.value).toFixed(2)).join(' ');
 const area=line+` L ${x(points.length-1)},${y(drawdown?0:low)} L ${x(0)},${y(drawdown?0:low)} Z`;
 svg.append(svgEl('path',{d:area,fill:`url(#${container.id}-fill)`}));
 if(!drawdown&&qqq.length){const indexes=new Map(points.map((r,i)=>[r.date,i]));const path=qqq.filter(r=>indexes.has(r.date)).map((p,i)=>(i?'L':'M')+x(indexes.get(p.date)).toFixed(2)+','+y(p.value).toFixed(2)).join(' ');svg.append(svgEl('path',{d:path,fill:'none',stroke:'var(--benchmark)','stroke-width':1.6,'stroke-dasharray':'4 3'}));}
 svg.append(svgEl('path',{d:line,fill:'none',stroke:drawdown?'var(--green)':'var(--red)','stroke-width':drawdown?1.7:2.3,'stroke-linejoin':'round'}));
 if(future){
  svg.append(svgEl('rect',{x:split,y:margin.t,width:pw-pastWidth,height:ph,class:'forecast-region'}));
  for(const [rows,kind] of [[predicted,'strategy'],[predictedQQQ,'benchmark']]){
   const upper=rows.map((p,i)=>(i?'L':'M')+fx(i).toFixed(2)+','+y(p.high).toFixed(2)).join(' '),lower=rows.slice().reverse().map((p,i)=>'L'+fx(rows.length-1-i).toFixed(2)+','+y(p.low).toFixed(2)).join(' ');
   svg.append(svgEl('path',{d:upper+' '+lower+' Z',class:'forecast-band '+kind}));
   svg.append(svgEl('path',{d:rows.map((p,i)=>(i?'L':'M')+fx(i).toFixed(2)+','+y(p.value).toFixed(2)).join(' '),class:'forecast-line '+kind}));
  }
  svg.append(svgEl('line',{x1:split,x2:split,y1:margin.t-6,y2:H-margin.b,class:'forecast-divider'}));
  const today=svgEl('text',{x:split-6,y:15,'text-anchor':'end',class:'forecast-label'});today.textContent='今天 '+forecast.today.slice(5).replace('-','/')+' · 北京时间';
  const label=svgEl('text',{x:split+8,y:29,class:'forecast-label'});label.textContent='未来 7 个交易日 · 预测';svg.append(today,label);
 }
 const ticks=future?[0,Math.round((points.length-1)/2),points.length-1]:[0,Math.round((points.length-1)/3),Math.round(2*(points.length-1)/3),points.length-1];for(const i of [...new Set(ticks)]){const label=svgEl('text',{x:x(i),y:H-6,'text-anchor':i===0?'start':i===points.length-1?'end':'middle',class:'chart-axis'});label.textContent=points[i].date.slice(5).replace('-','/');svg.append(label);}
 if(future)for(const i of [3,7]){const label=svgEl('text',{x:fx(i),y:H-6,'text-anchor':i===7?'end':'middle',class:'chart-axis'});label.textContent=predicted[i].date.slice(5).replace('-','/');svg.append(label);}
 const cross=svgEl('line',{x1:0,x2:0,y1:margin.t,y2:H-margin.b,class:'chart-crosshair',visibility:'hidden'}),dot=svgEl('circle',{r:3.8,fill:drawdown?'var(--green)':'var(--red)',stroke:'var(--paper)','stroke-width':2,visibility:'hidden'});svg.append(cross,dot);container.append(svg);
 const tip=$('chart-tooltip');if(tooltipOwner===container.id){tip.hidden=true;tooltipOwner=null;}
 svg.addEventListener('pointermove',event=>{
  const rect=svg.getBoundingClientRect(),px=(event.clientX-rect.left)/rect.width*W;let p,b,cx;
  if(future&&px>split){const i=Math.max(1,Math.min(7,Math.round((px-split)/(pw-pastWidth)*7)));p=predicted[i];b=predictedQQQ[i];cx=fx(i);tip.replaceChildren(element('b',null,p.date+' · 预测第 '+i+' 个交易日'),element('div',null,'策略：预测段 '+pct(p.return_pct)+' · 累计 '+pct(p.value)),element('div',null,'策略情景区间 '+pct(p.low_return_pct)+' ～ '+pct(p.high_return_pct)),element('div',null,'QQQ：预测段 '+pct(b.return_pct)+' · 累计 '+pct(b.value)),element('div',null,'QQQ情景区间 '+pct(b.low_return_pct)+' ～ '+pct(b.high_return_pct)),element('div','muted','80%模拟区间 · 未经未来样本验证'));}
  else{const i=Math.max(0,Math.min(points.length-1,Math.round((px-margin.l)/pastWidth*(points.length-1))));p=points[i];b=qqq.find(r=>r.date===p.date);cx=x(i);tip.replaceChildren(element('b',null,p.date),element('div',null,drawdown?'回撤 '+pct(p.value):'策略 '+money(p.equity)+' · '+pct(p.value)));if(b&&!drawdown)tip.append(element('div',null,'QQQ '+pct(b.value)));}
  cross.setAttribute('x1',cx);cross.setAttribute('x2',cx);cross.setAttribute('visibility','visible');dot.setAttribute('cx',cx);dot.setAttribute('cy',y(p.value));dot.setAttribute('visibility','visible');tooltipOwner=container.id;positionChartTooltip(tip,event.clientX,event.clientY??(rect.top||0)+25);
 });
 svg.addEventListener('pointerleave',()=>{if(tooltipOwner===container.id){tip.hidden=true;tooltipOwner=null;}cross.setAttribute('visibility','hidden');dot.setAttribute('visibility','hidden');});
 if(!drawdown&&trades.length){
  const dayIndexes=new Map();points.forEach((p,i)=>dayIndexes.set((p.session||p.date).slice(0,10),i));const grouped=new Map();
  for(const trade of trades){const day=(trade.session||trade.date).slice(0,10);if(!dayIndexes.has(day))continue;const key=day+':'+trade.side;if(!grouped.has(key))grouped.set(key,{day,side:trade.side,rows:[]});grouped.get(key).rows.push(trade);}
  for(const group of grouped.values()){
   const i=dayIndexes.get(group.day),color=group.side==='BUY'?'var(--red)':'var(--green)',marker=svgEl('g',{class:'trade-marker',tabindex:0,role:'button','aria-label':group.day+' '+(group.side==='BUY'?'买入':'卖出')+' '+group.rows.map(r=>r.symbol).join('、')});
   const cy=y(points[i].value)+(group.side==='BUY'?7:-7),cx=x(i);marker.append(svgEl('circle',{cx,cy,r:5.3,fill:color,stroke:'var(--paper)','stroke-width':1.5}));
   const show=event=>{event.stopPropagation();cross.setAttribute('visibility','hidden');dot.setAttribute('visibility','hidden');tip.replaceChildren(element('b',null,group.day+' · '+(group.side==='BUY'?'买入':'卖出')+' '+group.rows.length+' 笔'));for(const r of group.rows.slice(0,6))tip.append(element('div',null,r.symbol.replace('US.','')+' · '+r.qty+' 股 @ '+dollars(r.price)+' · '+dollars(r.amount)));if(group.rows.length>6)tip.append(element('div',null,'另有 '+(group.rows.length-6)+' 笔，点击查看当天全部'));tip.append(element('div','muted','净值上的交易日标记 · 点击查看明细'));const rect=svg.getBoundingClientRect();tooltipOwner=container.id;positionChartTooltip(tip,rect.left+cx/W*rect.width,(rect.top||0)+cy/W*rect.width);};
   marker.addEventListener('pointermove',show);marker.addEventListener('focus',show);const hide=()=>{if(tooltipOwner===container.id){tip.hidden=true;tooltipOwner=null;}};marker.addEventListener('pointerleave',hide);marker.addEventListener('blur',hide);
   const open=event=>{event.stopPropagation();activityDay=group.day;renderActivity();navigate('strategy-activity');};marker.addEventListener('click',open);marker.addEventListener('keydown',event=>{if(event.key==='Enter')open(event);});svg.append(marker);
  }
 }

}
function stressView(record){const test=record?.stats?.retrospective_test||{},rows=[['标准成本 · 单边 25 bps',test.return_pct],['高成本 · 单边 50 bps',record?.stress?.retrospective_test?.return_pct],['额外延迟 1 个交易日',record?.delayed?.retrospective_test?.return_pct]];const max=Math.max(1,...rows.map(r=>Math.abs(r[1]||0)));$('stress-bars').replaceChildren();for(const [name,value] of rows){const item=element('div','stress-item'),cap=element('div','stress-caption'),track=element('div','bar-track'),bar=element('i');cap.append(element('span',null,name),element('b',null,pct(value)));bar.style.width=(Math.abs(value||0)/max*100)+'%';bar.style.background=value<0?'var(--green)':'var(--red)';cap.children[1].className=direction(value);track.append(bar);item.append(cap,track);$('stress-bars').append(item);}text('max-dd',Number.isFinite(test.max_drawdown_pct)?test.max_drawdown_pct.toFixed(2)+'%':'—');text('exposure',Number.isFinite(test.avg_exposure_pct)?test.avg_exposure_pct.toFixed(1)+'%':'—');text('cost',money(test.cost_dollars));}
function renderForecast(forecast){
 const visible=chartMode==='history'&&$('show-forecast').checked;
 $('show-forecast').disabled=chartMode!=='history';$('forecast-summary').replaceChildren();$('forecast-summary').hidden=!visible||!forecast?.available;
 $('forecast-note').hidden=!visible;$('forecast-method').hidden=!visible||!forecast?.available;
 if(!visible)return;
 if(!forecast?.available){text('forecast-note','七日预测：'+(forecast?.reason||'正在读取预测材料。'));return;}
 for(const [rows,title,kind] of [[forecast.strategy_curve,'策略七日预测','strategy'],[forecast.benchmark_curve,'QQQ 七日预测','benchmark']]){
  const p=rows.at(-1),card=element('div','forecast-card '+kind),cap=element('span',null,title),body=element('div');body.append(element('strong',direction(p.return_pct),pct(p.return_pct)),element('span','forecast-range',pct(p.low_return_pct)+' ～ '+pct(p.high_return_pct)));
  card.append(cap,body,element('small',null,'从 '+forecast.origin_date+' 收盘起 · 80%模拟情景区间'));$('forecast-summary').append(card);
 }
 text('forecast-note','虚线为预测中位数，色带为未校准的80%情景区间。行情截止 '+forecast.origin_date+'；预测 '+forecast.future_dates[0]+' — '+forecast.future_dates.at(-1)+'。历史与未来各区内交易日等距，未来区单独展开；预测不计入历史收益或Shadow实绩。');
 text('forecast-method-text',forecast.method+' 训练 '+forecast.training_start+' — '+forecast.training_end+'，'+forecast.training_returns+' 个日收益。 '+forecast.limitation);
}
async function renderChart(){
 document.querySelectorAll('[data-mode]').forEach(b=>{b.classList.toggle('active',b.dataset.mode===chartMode);b.setAttribute('aria-pressed',String(b.dataset.mode===chartMode));});
 const request=++chartRequest,record=state?.strategies.find(r=>r.full_id===selected);if(!record)return;
 if(activityStrategy!==selected+':'+chartMode){graphRenderedKey=null;activityStrategy=selected+':'+chartMode;activityDay='all';dailyLimit=20;tradeLimit=30;activityData=null;renderActivity();emptyChart($('equity-chart'),'正在读取交易证据','加载所选策略的净值与买卖记录。');emptyChart($('drawdown-chart'),'正在读取回撤路径','');}
 renderPart('stress',()=>[record.stats,record.stress,record.delayed],()=>stressView(record));const key=selected+':'+chartMode+':'+record.shadow_sessions;
 const forecastKey=selected+':'+state.daily_asof+':'+new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai'}).format(new Date(state.calendar.now));
 const forecastRequested=chartMode==='history'&&$('show-forecast').checked;if(!graphRenderedKey)renderForecast(null);
 try{
  const [data,activity,forecast]=await Promise.all([chartMode!=='paper'&&chartCache.has(key)?chartCache.get(key):fetch('/api/chart/'+selected+'?mode='+chartMode).then(r=>{if(!r.ok)throw Error('chart');return r.json();}),chartMode==='history'&&activityCache.has(key)?activityCache.get(key):fetch('/api/activity/'+selected+'?mode='+chartMode).then(r=>{if(!r.ok)throw Error('activity');return r.json();}),forecastRequested?(forecastCache.get(forecastKey)||fetch('/api/forecast/'+selected).then(r=>{if(!r.ok)throw Error('forecast');return r.json();}).catch(()=>({available:false,reason:'预测材料暂不可用，历史净值仍可查看。'}))):null]);
  if(request!==chartRequest)return;if(chartMode!=='paper')chartCache.set(key,data);if(chartMode==='history')activityCache.set(key,activity);
  if(forecast?.available)forecastCache.set(forecastKey,forecast);
  activityData=activity;const initialCapital=data.initial_capital||10000,curve=data.curve||[],base=data.benchmark_curve||[],last=curve.at(-1),b=base.at(-1),ret=last?(last.equity/initialCapital-1)*100:null,benchmark=b?(b.equity/10000-1)*100:null;
  text('return-label',chartMode==='forward'?'未来观察收益':chartMode==='paper'?'券商模拟收益':'回顾测试收益');signedValue('return-value',ret,pct(ret));signedValue('benchmark-value',benchmark,pct(benchmark));signedValue('excess-value',Number.isFinite(ret)&&Number.isFinite(benchmark)?ret-benchmark:null,Number.isFinite(ret)&&Number.isFinite(benchmark)?(ret-benchmark>0?'+':'')+(ret-benchmark).toFixed(2)+' pp':'—');
  $('metric-strategy').style.order=Number.isFinite(benchmark)&&(!Number.isFinite(ret)||benchmark>ret)?2:1;$('metric-benchmark').style.order=Number.isFinite(benchmark)&&(!Number.isFinite(ret)||benchmark>ret)?1:2;$('metric-excess').style.order=3;
  text('chart-window',curve.length?curve[0].date.slice(0,10)+' — '+last.date.slice(0,10):chartMode==='paper'?'尚无实际模拟成交':chartMode==='forward'?'尚无未来观察数据':'尚无可核验历史数据');text('chart-source',activity.source||activity.unavailable||'等待交易证据');text('sample-info',curve.length?curve.length+' 个观察点 · 红买 / 绿卖 · 悬浮或点击查看':'暂无成交或观察证据');
  const graphKey=JSON.stringify([selected,chartMode,curve,base,activity.trades||[],forecast,initialCapital]);
  if(graphKey!==graphRenderedKey){renderForecast(forecast);lineChart($('equity-chart'),curve,base,false,activity.trades||[],forecast,initialCapital);lineChart($('drawdown-chart'),curve,[],true,[],null,initialCapital);graphRenderedKey=graphKey;}
  text('performance-title',chartMode==='history'?'历史回测与七日预测':chartMode==='forward'?'未来观察路径':'券商模拟净值');renderActivity();
 }catch(e){if(request!==chartRequest)return;graphRenderedKey=null;activityData={unavailable:'无法读取当前策略的交易材料。',trades:[],daily:[],summary:{}};renderActivity();renderForecast({available:false,reason:activityData.unavailable});emptyChart($('equity-chart'),'交易证据暂不可用','请检查服务与对应冻结版本。');emptyChart($('drawdown-chart'),'回撤证据暂不可用','');for(const id of ['return-value','benchmark-value','excess-value'])signedValue(id,null,'—');text('chart-source',activityData.unavailable);text('chart-window','证据读取失败');text('sample-info','');}
 renderPart('details',()=>[selected,record.logic,record.hypothesis,record.falsification,record.gates],()=>renderDetails(record));
}
function renderActivity(){
 renderCurrentHoldings();
 const renderedKey=JSON.stringify([selected,chartMode,activityData,activityDay,$('activity-side').value,dailyLimit,tradeLimit]);if(renderedKey===activityRenderedKey)return;
 const r=state.strategies.find(r=>r.full_id===selected),a=activityData||{},summary=a.summary||{},daily=a.daily||[],trades=a.trades||[];
 text('activity-title',(r?strategyName(r):'策略')+' · 交易详情');text('activity-kind',chartMode==='history'?'历史假设交易':chartMode==='forward'?'Shadow 假设成交':'券商模拟成交');text('activity-source',a.source||a.unavailable||'等待交易证据');
 const evidenceLabel=chartMode==='history'?'回测':chartMode==='forward'?'Shadow':'模拟';
 text('activity-daily-title',evidenceLabel+'每日收益与资金');text('activity-trades-title',evidenceLabel+'每日买卖明细');
 $('activity-context').className='activity-context '+chartMode;
 const owned=state.holdings?.strategies?.find(g=>g.id===selected);
 text('activity-context',chartMode==='history'?'历史回测 · 初始本金 $10,000 · 买卖为历史假设成交，持仓与收益按选中回测日期核算；不计入券商模拟账户。':chartMode==='forward'?'Shadow 观察 · 假设成交，仅纳入已封存观察日；不计入券商模拟持仓。':'券商模拟 · 该策略预算 '+dollars(owned?.budget)+' · 仅实际模拟委托与已核对成交 · 当前持仓 '+(owned?.position_count??'—')+' 个。');
 const dates=daily.map(r=>r.date).reverse();if(activityDay!=='all'&&!dates.includes(activityDay))activityDay='all';
 $('activity-day').replaceChildren();for(const day of ['all',...dates]){const o=element('option',null,day==='all'?'全部交易日':day);o.value=day;$('activity-day').append(o);}$('activity-day').value=activityDay;
 const filteredDaily=daily.filter(r=>activityDay==='all'||r.date===activityDay),filteredTrades=trades.filter(r=>(activityDay==='all'||(r.session||r.date).slice(0,10)===activityDay)&&($('activity-side').value==='all'||!$('activity-side').value||r.side===$('activity-side').value));
 const end=filteredDaily.at(-1);$('activity-metrics').replaceChildren();
 for(const [label,value,sign] of [[evidenceLabel+(chartMode==='paper'?'委托数':'假设订单'),String(summary.order_count??0)],[evidenceLabel+(chartMode==='paper'?'账本净值':'区间末净值'),dollars(summary.nav)],[evidenceLabel+'买入总额',dollars(summary.buy_amount)],[evidenceLabel+'卖出总额',dollars(summary.sell_amount)],[evidenceLabel+'费用合计',dollars(summary.fees)],[evidenceLabel+'已实现损益',dollars(summary.realized_pnl),summary.realized_pnl],[evidenceLabel+'未实现损益',dollars(summary.unrealized_pnl),summary.unrealized_pnl],['选中'+evidenceLabel+'日损益',activityDay==='all'?'选择交易日':dollars(end?.pnl),activityDay==='all'?null:end?.pnl]]){const item=element('div');item.append(element('span',null,label),element('strong',sign!=null?direction(sign):null,value));$('activity-metrics').append(item);}
 text('activity-window',filteredDaily.length+' 个交易日 · '+filteredTrades.length+' 笔买卖'+(activityDay!=='all'?' · '+activityDay:''));
 $('activity-daily-table').replaceChildren();for(const d of filteredDaily.slice().reverse().slice(0,dailyLimit)){const tr=element('tr');tr.tabIndex=0;tr.append(element('td','strategy-name',d.date),element('td',direction(d.return_pct),pct(d.return_pct)),element('td',direction(d.pnl),dollars(d.pnl)),element('td',null,dollars(d.nav)+' / '+dollars(d.cash)),element('td',null,dollars(d.held_value)),element('td',null,dollars(d.buy_amount)+' / '+dollars(d.sell_amount)),element('td',null,dollars(d.fees)),element('td',null,String(d.order_count)));const choose=()=>{activityDay=d.date;renderActivity();};tr.addEventListener('click',choose);tr.addEventListener('keydown',e=>{if(e.key==='Enter')choose();});$('activity-daily-table').append(tr);}
 if(!filteredDaily.length)blankRow('activity-daily-table',8,a.unavailable||'尚无该策略的已记录交易日。');$('activity-more-days').hidden=filteredDaily.length<=dailyLimit;
 $('activity-trade-table').replaceChildren();for(const t of filteredTrades.slice().reverse().slice(0,tradeLimit)){const tr=element('tr');tr.append(element('td',null,t.date.includes('T')?dateTime(t.date):t.date),element('td',t.side==='BUY'?'buy':'sell',t.symbol.replace('US.','')+' / '+(t.side==='BUY'?'买入':'卖出')),element('td',null,String(t.qty)),element('td',null,dollars(t.price)),element('td',null,dollars(t.amount)),element('td',null,dollars(t.fee)),element('td',t.side==='SELL'?direction(t.realized_pnl):'muted',t.side==='SELL'?dollars(t.realized_pnl):'—'),element('td','muted',chartMode==='paper'?'券商成交核对':t.reason==='rank_and_risk'?'排名与仓位':t.reason==='exit_or_rebalance'?'退出 / 再平衡':'假设成交'));$('activity-trade-table').append(tr);}
 if(!filteredTrades.length)blankRow('activity-trade-table',8,a.unavailable||'当前筛选条件下无交易记录。');$('activity-more-trades').hidden=filteredTrades.length<=tradeLimit;
 const orders=a.orders||[];$('activity-orders').hidden=chartMode!=='paper';$('activity-order-table').replaceChildren();for(const o of orders.slice().reverse()){const tr=element('tr');tr.append(element('td',null,dateTime(o.created)),element('td',null,o.symbol+' / '+o.side),element('td',null,String(o.qty)),element('td',null,dollars(o.price)),element('td',null,String(o.filled)),element('td',null,({reserved:'已预留',submitting:'提交中',unknown:'待核对',submitted:'已委托',partial:'部分成交',filled:'已成交',cancelled:'已撤单',rejected:'已拒绝'}[o.status]||o.status)));$('activity-order-table').append(tr);}if(!orders.length)blankRow('activity-order-table',6,'该策略尚无券商模拟委托。');
 text('activity-note',(a.limitation||'每日损益包含未平仓市值变化；已实现损益按含费用的平均成本核账。')+' 买卖点按交易日标在净值曲线上，纵坐标不代表标的成交价。'+(a.pending_plan?' 另有当天未封存 Shadow 计划，暂未计入每日已完成记录。':''));
 activityRenderedKey=JSON.stringify([selected,chartMode,activityData,activityDay,$('activity-side').value,dailyLimit,tradeLimit]);
}
function openStrategy(id,mode){selected=id;if(mode)chartMode=mode;const r=state.strategies.find(r=>r.full_id===id);if(r&&r.data_hash!==state.focus_data_hash)$('all-versions').checked=true;renderRanking();renderStrategies();return renderChart().then(()=>navigate('strategy-activity'));}

function holdingChart(position){
 const points=(position.series||[]).filter(p=>Number.isFinite(p.price)&&p.price>0),cost=position.avg_cost,mark=position.mark;
 const svg=svgEl('svg',{viewBox:'0 0 154 66',role:'img','aria-label':'平均成本 '+dollars(cost)+'；最新估值 '+dollars(mark)});
 const prices=[cost,mark,...points.map(p=>p.price)].filter(Number.isFinite),min=Math.min(...prices),max=Math.max(...prices),pad=Math.max((max-min)*.2,Math.abs(mark)*.006,.01),lo=min-pad,hi=max+pad;
 const x=i=>10+(points.length>1?i/(points.length-1)*132:132),y=value=>8+(hi-value)/(hi-lo)*50;
 svg.append(svgEl('line',{x1:7,y1:y(cost),x2:148,y2:y(cost),class:'cost-line'}));
 if(points.length>1){const path=points.map((p,i)=>(i?'L':'M')+x(i).toFixed(1)+','+y(p.price).toFixed(1)).join(' ');svg.append(svgEl('path',{d:path,class:'price-line',stroke:position.unrealized_pnl>=0?'var(--red)':'var(--green)'}));}
 svg.append(svgEl('line',{x1:7,y1:y(mark),x2:148,y2:y(mark),class:'mark-line'}),svgEl('circle',{cx:142,cy:y(mark),r:3,fill:position.unrealized_pnl>=0?'var(--red)':'var(--green)'}));
 return svg;
}
function renderHoldingsPanel(prefix,view,emptyMessage){
 const positions=view?.positions||[];text(prefix+'-count',view?.available===false?'待核验':view?positions.length+' 个持仓':'—');
 $(prefix+'-metrics').replaceChildren();
 for(const [label,value,sign] of [['组合净值',dollars(view?.nav)],['账本现金',dollars(view?.cash)],['仓位 · 市值 / 净值',pct(view?.exposure_pct)],['浮动盈亏',dollars(view?.unrealized_pnl),view?.unrealized_pnl]]){const item=element('div');item.append(element('span',null,label),element('strong',sign==null?null:direction(sign),value));$(prefix+'-metrics').append(item);}
 $(prefix+'-table').replaceChildren();
 for(const p of positions){
  const tr=element('tr'),symbol=element('td','holding-symbol strategy-name',p.symbol.replace('US.',''));
  symbol.append(element('small',null,p.multiplier===100?'标准期权 · 每张 ×100':'股票 · '+p.qty+' 股'));
  const cost=element('td',null,dollars(p.avg_cost));cost.append(element('small',null,'成本金额 '+dollars(p.cost_basis)));
  const quote=element('td','holding-quote '+p.quote_status,p.quote_status==='historical'?p.mark_date:p.quote_time?dateTime(p.quote_time):'尚无已核验报价');
  quote.append(element('small',null,({historical:'冻结复权收盘',fresh:'近期REST估值',closed:'休市缓存',stale:'报价已过期',unverified:'估值时间待核验'}[p.quote_status]||'估值待核验')));
  const chart=element('td','holding-chart');chart.append(holdingChart(p),element('small',null,'虚线 · 平均成本'+(p.series?.length>1?' · '+p.series.length+' 个观测点':' · 等待报价序列')));
  tr.append(symbol,element('td',null,p.qty+(p.multiplier===100?' 张':' 股')),cost,element('td',null,dollars(p.mark)),element('td',null,dollars(p.market_value)),element('td',null,pct(p.weight_pct)),element('td',direction(p.unrealized_pnl),dollars(p.unrealized_pnl)),element('td',direction(p.unrealized_pct),pct(p.unrealized_pct)),chart,quote);$(prefix+'-table').append(tr);
 }
 if(!positions.length)blankRow(prefix+'-table',10,emptyMessage||'当前空仓；券商模拟成交后显示实际持仓。');
 text(prefix+'-note',view?(view.cost_note+(view.kind==='retrospective_test'?'':' 数量与成本来自已核对的模拟成交；报价时间单独列示。')):'等待所选口径的持仓证据。');
}
function renderCurrentHoldings(){
 const holdings=state.holdings||{},groups=holdings.strategies||[],selector=$('holdings-scope');
 const options=[{id:'stock',name:'全部股票组合'},{id:'option',name:'全部期权组合'},...groups.map(g=>({id:g.id,name:(g.bucket==='option'?'期权 · ':'')+(g.display_name||g.name+' · '+g.id.slice(0,8))}))];
 if(!options.some(o=>o.id===holdingsScope))holdingsScope='stock';
 renderPart('holdings-selector',()=>options,()=>{selector.replaceChildren();for(const item of options){const option=element('option',null,item.name);option.value=item.id;selector.append(option);}});selector.value=holdingsScope;
 const view=holdings[holdingsScope]||groups.find(g=>g.id===holdingsScope);
 renderPart('holdings-paper',()=>[holdingsScope,view],()=>renderHoldingsPanel('paper-holdings',view,view?.available===false?'独立期权账户待绑定，资产与持仓尚未核实。':'当前空仓 · 账本现金 '+dollars(view?.cash)+'；成交后展示持仓、成本线与浮盈亏。'));
 $('strategy-holdings-panel').hidden=chartMode==='forward';
 if(chartMode==='history'){
  text('strategy-holdings-eyebrow','历史回测 · 假设组合 · 初始本金 $10,000');text('strategy-holdings-title','回测持仓组合');
  text('strategy-holdings-mark-label','该日复权收盘价');text('strategy-holdings-time-label','回测估值日期');
  renderHistoricalHoldings();
 }else if(chartMode==='paper'){
  text('strategy-holdings-eyebrow','券商模拟 · 当前组合');text('strategy-holdings-title','当前模拟持仓组合');
  text('strategy-holdings-mark-label','最新价格');text('strategy-holdings-time-label','报价时间 · 北京');
  const own=groups.find(g=>g.id===selected);
  renderPart('holdings-strategy',()=>[chartMode,selected,own],()=>renderHoldingsPanel('strategy-holdings',own,own?'该策略当前空仓 · 账本现金 '+dollars(own.cash)+'；暂无持仓。':'该版本尚无可归属的模拟持仓；历史与Shadow交易不会计入此组合。'));
 }
}

function renderHistoricalHoldings(){
 const day=activityDay==='all'?activityData?.daily?.at(-1)?.date:activityDay;
 const key=selected+':'+(day||'loading');
 if(!day){renderPart('holdings-strategy',()=>[chartMode,key],()=>renderHoldingsPanel('strategy-holdings',null,'正在读取冻结回测持仓…'));return;}
 text('strategy-holdings-title','回测持仓组合 · '+day);
 const cached=historicalHoldingsCache.get(key);
 if(cached){renderPart('holdings-strategy',()=>[chartMode,key,cached],()=>renderHoldingsPanel('strategy-holdings',cached,cached.reason||'该回测日为空仓。'));return;}
 renderPart('holdings-strategy',()=>[chartMode,key],()=>renderHoldingsPanel('strategy-holdings',null,'正在核对 '+day+' 的冻结回测持仓…'));
 if(historicalHoldingsPending===key)return;historicalHoldingsPending=key;
 const identity=selected;
 fetch('/api/holdings/'+identity+'?mode=history&asof='+day).then(r=>{if(!r.ok)throw Error('holdings');return r.json();}).catch(()=>({available:false,reason:'冻结回测持仓暂不可用。',cost_note:'等待原始交易与同版本行情核验，未用实际模拟持仓替代。'})).then(view=>{
  if(!view.available)view.cost_note=view.cost_note||view.reason;
  historicalHoldingsCache.set(key,view);if(historicalHoldingsCache.size>128)historicalHoldingsCache.delete(historicalHoldingsCache.keys().next().value);
  if(historicalHoldingsPending===key)historicalHoldingsPending=null;
  if(selected===identity&&chartMode==='history'&&(activityDay==='all'?activityData?.daily?.at(-1)?.date:activityDay)===day)renderCurrentHoldings();
 });
}

function renderDetails(r){
 const box=$('strategy-detail');box.replaceChildren();if(!r)return;
 box.append(element('b',null,strategyName(r)+' · '+(['ai','codex_evolution'].includes(r.origin)?'Codex 候选':'固定对照策略')));
 const logic=element('div','logic-grid');for(const step of r.logic?.steps||[]){const item=element('div','logic-step');item.append(element('span',null,step.label),element('p',null,step.text));logic.append(item);}box.append(logic);
 if(r.hypothesis){const detail=element('details','hypothesis-detail');detail.append(element('summary',null,'Codex 研究假设与反证'),element('p',null,r.hypothesis));if(r.falsification)detail.append(element('p',null,'反证：'+r.falsification));box.append(detail);}
 box.append(element('p','gate-proof',Object.entries(r.gates).map(([name,passed])=>(passed?'✓ ':'× ')+(gateNames[name]||name)).join('　')),element('div','muted','上述为冻结规则的解释；候选是否可执行取决于验证阶段。当前不含期权历史链、现金股息或历史资讯因子。'));
}
function renderStrategies(){
 const all=$('all-versions').checked,records=sortedReturns(scopedStrategies()).filter(r=>$('include-rejected').checked||r.stage!=='rejected');
 text('research-note',(all?'所有数据版本，收益仅可在相同区间比较。':'当前冻结数据版本。')+' '+records.length+' 个展示版本 · 按回顾收益降序 · 点击查看交易详情。');
 // This table always labels historical returns; forward sorting belongs to the chart above.
 records.sort((a,b)=>{const av=a.stats?.retrospective_test?.return_pct,bv=b.stats?.retrospective_test?.return_pct;return Number.isFinite(av)!==Number.isFinite(bv)?Number.isFinite(av)?-1:1:Number.isFinite(av)&&av!==bv?bv-av:a.name.localeCompare(b.name);});
 keyedRows('strategy-table',records.map(r=>({...r,selected:r.full_id===selected,highlight:flagged('strategy:'+r.full_id)})),r=>r.full_id,r=>{const tr=element('tr');tr.tabIndex=0;tr.setAttribute('role','button');tr.setAttribute('aria-label','查看策略 '+strategyName(r));markRow(tr,'strategy:'+r.full_id);if(r.selected)tr.className+=' selected-row';
 const name=element('td','strategy-label');name.append(element('span','strategy-name',strategyName(r)),element('span','strategy-id',r.id+' · '+(['ai','codex_evolution'].includes(r.origin)?'Codex':'固定对照')),element('p','strategy-summary',r.logic?.summary||'等待规则说明'),element('span','view-activity','查看交易、每日收益与资金 ↗'));
 const phase=element('td');phase.append(element('span','status-tag '+r.stage,stageNames[r.stage]||r.stage));if(flagged('strategy:'+r.full_id))phase.append(element('span','change-label','阶段更新'));
 const t=r.stats.retrospective_test||{};tr.append(name,phase,element('td',direction(t.return_pct),pct(t.return_pct)),element('td',null,t.max_drawdown_pct!=null?t.max_drawdown_pct.toFixed(2)+'%':'—'),element('td',null,t.orders??'—'),element('td','gate-reason',r.failed_gates.length?r.failed_gates.map(g=>gateNames[g]||({'Duplicate executable strategy':'重复版本'}[g])||'版本失效 / 规则变更').join('、'):r.stage==='shadow'?'历史门槛通过，等待未来验证':r.stage==='paper'?(r.paper_authorization?.forward_wait_waived?'用户授权提前模拟；未来验证尚未通过':'通过未来门槛，模拟运行'):'尚未形成结论'));
 const choose=()=>openStrategy(r.full_id);tr.addEventListener('click',choose);tr.addEventListener('keydown',e=>{if(e.key==='Enter')choose();});return tr;});
 if(!records.length)blankRow('strategy-table',6,'当前筛选条件下没有研究版本。');
}
function renderMarket(){
 const scan=state.universe_scan||{},hits=marketHits(state);
 text('market-note','纳指100 '+(scan.nasdaq100_count||0)+' 只 · 标普500 '+(scan.sp500_count||0)+' 只 · 自选 '+(scan.watchlist_stocks||0)+' 只 · 去重扫描 '+(scan.scanned||0)+' 只 · 价格 ≥ $5，成交额 ≥ $2,000 万，日涨跌绝对值 ≥ 3%');
 text('market-date','数据日 '+(hits[0]?.data_date||state.last_tick?.signal_cache_asof||'—')+' · 采集 '+dateTime(scan.created_at));
 const top=hits.slice(0,7),max=Math.max(1,...top.map(r=>Math.abs(r.change_pct)));
 keyedRows('mover-bars',top.map(r=>({...r,scale:max})),r=>r.symbol,r=>{const row=element('div','mover-row '+direction(r.change_pct)),track=element('div','mover-track'),bar=element('i');bar.style.width=Math.abs(r.change_pct)/r.scale*100+'%';track.append(bar);row.append(element('span',null,r.symbol.replace('US.','')),track,element('span',null,pct(r.change_pct)));return row;});
 keyedRows('mover-table',hits.slice(0,10).map(r=>({...r,highlight:flagged('mover:'+r.symbol)})),r=>r.symbol,r=>{
  const tr=element('tr'),name=element('td','strategy-name',r.symbol.replace('US.',''));markRow(tr,'mover:'+r.symbol);if(r.highlight)name.append(element('span','change-label',r.highlight.label));
  const quoteCell=element('td',null,'$'+r.price.toFixed(2));quoteCell.append(element('span','quote-source',(r.quote_source==='Futunn WebSocket'?'推送':r.quote_source?'REST 核对':'全池快照')+' · '+dateTime(r.quote_updated_at||r.observed_at)));
  tr.append(name,quoteCell,element('td',direction(r.change_pct),pct(r.change_pct)),element('td',null,r.volume_ratio!=null?r.volume_ratio.toFixed(2)+'×':'待补'),element('td','muted',(r.memberships||[]).map(v=>({watchlist:'自选',nasdaq100:'纳指100',sp500:'标普500'}[v]||v)).join(' / ')));return tr;
 });
 if(!hits.length&& !$('mover-table').children.length)blankRow('mover-table',5,'尚无符合条件的异动快照。');
}
function richText(target,value){
 for(const part of String(value||'').split(/(\*\*[^*]+\*\*)/g)){if(part.startsWith('**')&&part.endsWith('**'))target.append(element('strong',null,part.slice(2,-2)));else target.append(document.createTextNode(part));}
}
function briefField(card,label,value,className=''){
 if(!value)return;const p=element('p','brief-field '+className);p.append(element('b',null,label+'：'));richText(p,value);card.append(p);
}
function briefSources(card,ids,evidence){
 const links=element('div','brief-sources');for(const id of (ids||[]).slice(0,4)){const r=evidence.get(id);if(!r)continue;const source=element(r.url?'a':'span',null,r.title||r.name||r.symbol||id);if(r.url){try{const url=new URL(r.url);if(url.protocol==='https:'){source.href=r.url;source.target='_blank';source.rel='noopener noreferrer';}}catch(_){}}source.title=(r.source||'')+' · '+(r.data_date||dateTime(r.published_at));links.append(source);}card.append(links);
}
function briefSection(box,number,title){const section=element('section','brief-section'),head=element('h3');head.append(element('span','brief-number',number),document.createTextNode(title));section.append(head);box.append(section);return section;}
function renderReview(){
 const value=state.daily_brief,raw=state.review_markdown||'',box=$('review-text');box.replaceChildren();$('factor-ideas').replaceChildren();
 if(!value?.brief){
  text('review-date',state.daily_asof+' · '+(state.daily_completed?'已记录':'待完成'));const body=raw.split('## AI 复盘')[1]?.split('## 因子待办')[0]?.trim()||raw||'等待每日研究任务生成复盘。';
  for(const para of body.split(/\n+/).filter(Boolean)){const node=element(para.startsWith('## ')?'h3':'p');richText(node,para.replace(/^#+\s*/,''));box.append(node);}
  const ideas=raw.split('## 因子待办')[1]?.split('资讯：')[0]?.trim();for(const idea of (ideas||'尚无新的因子待办。').split('\n').filter(Boolean)){const p=element('p');richText(p,idea.replace(/^-\s*/,''));$('factor-ideas').append(p);}return;
 }
 text('review-date','行情基准 '+value.asof+' · 生成 '+dateTime(value.generated_at)+(value.asof!==state.daily_asof?' · 待更新':''));const b=value.brief,evidence=new Map(value.evidence.map(r=>[r.id,r]));
 const headline=element('div','brief-headline');headline.append(element('span','eyebrow','PRIVATE INTELLIGENCE / '+value.model));richText(headline,b.headline);box.append(headline);
 const coverage=element('p','brief-coverage','全池快照 '+(value.coverage.snapshot_universe||0)+' 只 · 完整日线 '+(value.coverage.history_stocks||0)+' 只 · 各市场日期单独列示 · 标题催化仍需全文核实');box.append(coverage);
 const events=briefSection(box,'01','今天最重要的变化');for(const r of b.key_events){const card=element('article','brief-card');card.append(element('h4',null,r.title));briefField(card,'判断',r.judgment,'key-judgment');briefField(card,'影响方向',r.impact);briefField(card,'风险点',r.risk,'risk-condition');briefSources(card,r.evidence_ids,evidence);events.append(card);}if(!b.key_events.length)events.append(element('p','muted','暂无足够证据支持的重要事件。'));
 const temperature=briefSection(box,'02','市场温度'),facts=element('div','temperature-grid');for(const r of value.markets||[]){const item=element('div','temperature-item');item.append(element('span',null,r.name),element('strong',direction(r.change_pct),pct(r.change_pct)),element('small',null,Number(r.value).toFixed(r.unit==='%'?3:2)+' '+r.unit),element('small',null,r.data_date+' · '+r.source));facts.append(item);}temperature.append(facts);for(const r of b.market_temperature){const card=element('div','temperature-comment');briefField(card,r.market,r.judgment);briefSources(card,r.evidence_ids,evidence);temperature.append(card);}
 const themes=briefSection(box,'03','主线判断');for(const r of b.themes){const card=element('article','brief-card');card.append(element('h4',null,r.direction));briefField(card,'强度 / 持续性',r.strength,'key-judgment');briefField(card,'催化',r.catalyst);briefField(card,'触发条件',r.trigger,'trigger-condition');briefField(card,'失效条件',r.invalid,'invalid-condition');briefField(card,'风险点',r.risk,'risk-condition');briefSources(card,r.evidence_ids,evidence);themes.append(card);}
 const scan=briefSection(box,'04','美股短线扫描');for(const [key,title] of [['bottom','短期抄底观察'],['catalyst','资讯利好 / 催化'],['strong','强势延续'],['avoid','走坏 / 回避']]){const group=element('div','scan-group '+key);group.append(element('h4','scan-title',title+' · '+b.us_scan[key].length+' 个候选'));
 for(const r of b.us_scan[key]){const fact=evidence.get('tech:'+r.symbol),card=element('article','brief-card scan-card');card.append(element('h4',null,r.symbol.replace('US.','')+' · '+(fact?.name||r.symbol.replace('US.',''))),element('span','scan-sector',fact?.sector||'方向待核实'));
 if(fact)card.append(element('p','scan-levels','日线 '+fact.data_date+' · 收盘 '+dollars(fact.close)+' · MA5 '+dollars(fact.ma5)+' / MA10 '+dollars(fact.ma10)+' · 前20日低点 '+dollars(fact.low20_previous)+' · 量比 '+(Number.isFinite(fact.volume_ratio)?fact.volume_ratio.toFixed(2)+'×':'—')));
 briefField(card,'入选原因',r.reason,'key-judgment');briefField(card,'价格反映',r.reflection);briefField(card,'观察触发条件',r.trigger,'trigger-condition');briefField(card,'失效条件',r.invalid,'invalid-condition');briefField(card,'风险点',r.risk,'risk-condition');briefSources(card,r.evidence_ids,evidence);group.append(card);}
 if(!b.us_scan[key].length)group.append(element('p','muted small','暂无足够证据支持的候选；不补齐数量。'));scan.append(group);}
 const cn=briefSection(box,'05','A股 / 港股候选观察方向');for(const r of b.cn_hk){const card=element('article','brief-card');card.append(element('h4',null,r.direction));briefField(card,'入选原因',r.reason);briefField(card,'触发条件',r.trigger,'trigger-condition');briefField(card,'失效条件',r.invalid,'invalid-condition');briefField(card,'风险点',r.risk,'risk-condition');briefSources(card,r.evidence_ids,evidence);cn.append(card);}
 if(!b.cn_hk.length)cn.append(element('p','muted small','暂无有证据支持的候选方向。'));
 const conclusions=briefSection(box,'06','对我有用的结论');for(const [key,label] of [['watch','重点观察'],['avoid','避免'],['changes','可能改变判断的信息']]){const card=element('article','brief-card conclusion '+key);card.append(element('h4',null,label));for(const r of b.conclusions[key]){const p=element('p');richText(p,r);card.append(p);}conclusions.append(card);}
 const execution=briefSection(box,'07','模拟操作与策略复盘');const paper=state.paper_activity||{};execution.append(element('div','execution-facts','模拟净值 '+dollars(paper.nav)+' · 收益 '+pct(paper.return_pct)+' · 成交增量 '+(paper.fill_count||0)+' 次 · 已实现 '+dollars(paper.realized_pnl)+' · 未实现 '+dollars(paper.unrealized_pnl)));const review=element('p','execution-review');richText(review,b.execution_review);execution.append(review);
 const gaps=briefSection(box,'08','证据与覆盖缺口');for(const gap of [...new Set([...(b.coverage_gaps||[]),...(value.data_gaps||[])])])gaps.append(element('p','coverage-gap',gap));gaps.append(element('p','caution','所有投资内容仅供研究，不构成投资建议。'));
 for(const idea of value.factor_ideas||[]){const p=element('p');richText(p,idea);$('factor-ideas').append(p);}
}
function renderFactorLab(){
 const lab=state.research_evidence;if(!lab)return;
 text('factor-lab-note','行情截至 '+lab.asof+' · '+(lab.pending?'研究流程待补齐':'本版本研究已完成')+' · 数据 '+(lab.data_sha256||'待核验').slice(0,12));
 text('review-factor-summary','本版本 '+lab.current_trials+' 个研究试验 · '+lab.current_factors+' 个新表达式因子 · '+lab.current_rejected+' 个淘汰 · '+lab.queued+' 个晋升候选');
 $('factor-lab-stats').replaceChildren();for(const [label,value] of [['本版本试验',lab.current_trials],['新表达式因子',lab.current_factors+' / 1'],['本版本淘汰',lab.current_rejected],['晋升排队',lab.queued]]){const item=element('div');item.append(element('span',null,label),element('strong',null,value));$('factor-lab-stats').append(item);}
 const statuses={pending:'未执行',running:'运行中',completed:'已完成',failed:'失败待重试',blocked:'有阻断'};
 $('factor-lab-steps').replaceChildren();for(const step of lab.steps){const item=element('div','lab-step');item.append(element('b',null,step.label),document.createTextNode(statuses[step.status]||step.status));if(step.reason)item.title=step.reason;$('factor-lab-steps').append(item);}
 text('factor-lab-policy',(lab.promotion_policy.mode==='account_champion'?'账户优胜策略：候选并行观察，同区间成本与风险检验通过后按收益择优；自动撤旧单、退出被淘汰持仓，交收后交接账户资金。小幅领先须超过切换门槛。':lab.promotion_policy.enabled?'自动晋升：同区间评分择优，仅交接空仓、无委托且已交收的槽位。':'自动晋升未启用。')+' 股票授权本金 '+dollars(state.stock_budget)+(state.option_funding_mode==='independent_pending'?'，期权独立账户待绑定':state.option_funding_mode==='shared_ratio'?'，期权使用同账户10%额度':'，期权预留 '+dollars(state.option_budget))+'。历史择优不保证未来最优。'+(lab.recovery_note?' '+lab.recovery_note:''));
 const rows=lab.trials.filter(r=>r.is_factor||r.current_version);
 keyedRows('factor-evidence-table',rows,r=>r.full_id,r=>{
  const tr=element('tr'),name=element('td','strategy-name'),button=element('button','strategy-open',r.name);button.addEventListener('click',()=>openStrategy(r.full_id,'history'));name.append(button,element('small',null,r.id+' · '+(r.current_version?'本版本':'历史尝试')));
  const details=element('details','lab-hypothesis');details.append(element('summary',null,'假设与公式'));
  if(r.hypothesis)details.append(element('p',null,r.hypothesis));details.append(element('p','lab-formula',r.formula));if(r.falsification)details.append(element('p',null,'反证提议（尚非已执行的未来验证）：'+r.falsification));name.append(details);
  const stage=element('td');stage.append(element('span','status-tag '+r.stage,stageNames[r.stage]||r.stage));
  const gates=Object.entries(r.gates||{}),passed=gates.filter(([_,v])=>v).length;
  const judgment=element('td','lab-judgment',gates.length?'历史门槛 '+passed+'/'+gates.length+(r.ledger_audited?' · 独立核账通过':''):(r.causal_status==='rejected_causal_checks'?'因果检查失败':'尚无回测结果'));
  if(r.failed_gates.length)judgment.append(element('p',null,'淘汰：'+r.failed_gates.map(k=>gateNames[k]||k).join('、')));if(r.reason)judgment.append(element('p',null,r.reason));
  if(r.stress)judgment.append(element('p',null,'50bps '+pct(r.stress.return_pct)+' · 延迟 '+pct(r.delay?.return_pct)));
  const promotion=element('td','lab-judgment'),q=r.promotion;
  promotion.textContent=r.stage==='paper'?'已进入券商模拟':r.stage==='retired'?'已退出并交接资金':r.stage==='rejected'?'已淘汰，不晋升':q?(q.status==='promoted'?'已晋升':q.status==='waiting'?'排队：'+q.reason:q.eligible?'账户择优通过':q.reason||'未优于在跑策略'):'等待筛选';
  if(q)promotion.append(element('p',null,'评分 '+q.candidate_score?.toFixed(2)+' / 原策略 '+q.incumbent_score?.toFixed(2)));promotion.append(element('p',null,'真实 Shadow '+r.shadow_sessions+' 日'));
  const links=element('td','lab-links');for(const [label,url] of [['实验材料 ↗',r.evidence_url],['因子代码 ↗',r.code_url]]){if(!url)continue;const a=element('a',null,label);a.href=url;a.target='_blank';a.rel='noopener';links.append(a);}
  tr.append(name,stage,element('td',direction(r.historical_return_pct),pct(r.historical_return_pct)),element('td',null,r.max_drawdown_pct!=null?r.max_drawdown_pct.toFixed(2)+'%':'—'),judgment,promotion,links);return tr;
 });if(!rows.length)blankRow('factor-evidence-table',7,'本版本暂无研究试验；未运行时不会显示为已验证。');
}
function renderRecovery(){
 const r=state.recovery||{},components=(r.components||[]).filter(c=>c.status!=='ok'),checkpoints=r.checkpoints||[];
 text('recovery-summary','恢复与检查点 · '+(components.length?components.length+' 个环节降级':'无已记录故障')+' · '+checkpoints.filter(c=>c.status==='completed').length+' 个步骤已保存');
 text('logging-note',r.logging?.file_enabled?'本地轮转日志已启用 · HTTP 状态 / 耗时 / 检查点；敏感请求内容不记录。'+(r.logging.write_failures?' 日志写入异常 '+r.logging.write_failures+' 次。':''):'日志状态待服务验证。');
 $('recovery-components').replaceChildren();for(const c of components){const row=element('div','task');row.append(element('p',null,c.component+' · '+(c.reason||c.error_type||'待恢复')+(c.http_status?' · HTTP '+c.http_status:'')),element('span','status-tag rejected',c.scope==='execution'?'执行等待恢复':'局部降级'));$('recovery-components').append(row);}
 if(r.data_gate){const row=element('div','task');row.append(element('p',null,'执行数据待复核 · '+r.data_gate.reason));$('recovery-components').append(row);}
 for(const e of r.artifact_errors||[]){const row=element('div','task');row.append(element('p',null,'档案读取失败 · '+e.file+' · '+e.error_type));$('recovery-components').append(row);}
 $('checkpoint-list').replaceChildren();for(const c of checkpoints.slice(0,12)){const row=element('div','task'),info=element('div');info.append(element('b',null,c.job+' · '+c.asof),element('p',null,'尝试 '+c.attempts+(c.retry_after?' · 下次 '+dateTime(c.retry_after):'')+(c.state?.recovered_interruption?' · 已从中断续跑':'')));row.append(info,element('span','status-tag '+(c.status==='completed'?'shadow':c.status==='failed'?'rejected':''),({completed:'结果已保存',running:'运行中',failed:'等待重试'})[c.status]||c.status));$('checkpoint-list').append(row);}
}
function renderRuntime(){renderRecovery();const session=state.paper_execution_session;const box=$('execution-session-note');if(box){box.replaceChildren();if(session){box.append(element('b',null,'最近交易日 '+session.session+' · 实际模拟执行'));const statuses={checked:'计划已检查',partial:'部分委托被阻断',missed_window:'窗口结束，存在未执行意图',observing:'已记录观察计划',waiting_next_session:'等待下一交易日观察',blocked:'执行条件未通过',closed:'休市',paused:'暂停',rotating:'账户策略交接中'};for(const r of session.strategies||[]){const out=r.outcome||{};const placed=(state.orders||[]).filter(o=>o.strategy.startsWith(r.id)&&o.session===session.session&&o.status!=='reserved');const counts=r.stage==='paper'?' · 已委托 '+(out.submitted_orders??placed.length)+' 单 / 成交 '+(out.filled_orders??placed.filter(o=>o.status==='filled').length)+' 单':'';const reasons=(out.deferred_orders||[]).map(o=>o.symbol+'：'+o.reason);box.append(element('p',null,r.id+' · '+(statuses[r.status]||r.status)+(out.planned_orders!=null?' · 计划 '+out.planned_orders+' 单':'')+counts+(reasons.length?' · '+reasons.join('；'):r.reason?' · '+r.reason:'')));}if(session.option)box.append(element('p',null,'期权 · '+(session.option.detail||session.option.status)));}}const ready=state.execution_readiness;text('execution-readiness-note',ready?(ready.ready?'执行前检查已通过':'执行前检查存在阻断')+' · 日线 '+(ready.data_asof||'待核验')+' · '+dateTime(ready.observed_at)+(ready.ready?(ready.order_endpoint_tested?' · 已观察到券商模拟成交；开盘前5分钟自动复检。':' · 开盘前5分钟自动复检；模拟成交链路仍待盘中确认。'):' · '+ready.blockers.join('；')):'等待执行前检查；开盘前5分钟自动核对冻结策略、模拟账户和预算。');text('runtime-mode',modeNames[state.schedule?.mode]||'读取运行节奏');$('task-list').replaceChildren();const labels={running:'运行中',completed:'已完成',recorded:'已记录',pending:'等待触发',failed:'失败待重试',blocked:'前置条件阻断'};for(const j of state.tasks){const task=element('div','task'),info=element('div');info.append(element('b',null,j.name),element('p',null,j.trigger));if(j.state.finished_at)info.append(element('p',null,dateTime(j.state.finished_at)+' · '+j.state.duration_seconds+' 秒'));task.append(info,element('span','status-tag '+(j.state.status==='failed'?'rejected':j.state.status==='completed'||j.state.status==='recorded'?'shadow':''),j.enabled?labels[j.state.status]||j.state.status:'未启用'));$('task-list').append(task);}}
function renderReferences(){const box=$('reference-list');box.replaceChildren();for(const r of state.strategy_references){box.append(element('p','reference-title',r.name+'.quant'),element('p','muted small',r.canvas_cards+' 个原生节点 · 5 分钟 / 日线 · 仅研究参考'));const vars=element('div','reference-vars');for(const label of ['分档仓位 30 / 50 / 70 / 90%','RSI 45 / 35 / 30 / 25','止盈 3% · 止损 2%','QQQ EMA 20 / 60'])vars.append(element('span',null,label));box.append(vars,element('p','caution','按整账户资产计算仓位；移植需改为分配预算。原生 5 分钟逻辑尚未忠实移植或回测。'));}}
function renderEvents(){
 $('event-list').replaceChildren();for(const e of state.events.slice(0,12)){const h=flagged('event:'+e.seq),row=element('div','event '+(h?'fresh '+h.tone:''));row.append(element('i'),element('time',null,dateTime(e.created)),element('span',null,eventLabel(e)));$('event-list').append(row);}renderTriggers();
}
function renderCapital(){const stock=state.portfolios.stock,held=Object.values(stock.positions).reduce((sum,p)=>sum+p.qty*p.mark,0),nav=stock.cash+held;const working=Object.entries(state.order_counts).filter(([key])=>!['filled','cancelled','rejected'].includes(key)).reduce((sum,[_,n])=>sum+n,0);text('stock-nav',money(nav));text('cash',money(stock.cash));text('positions-note',Object.keys(stock.positions).length+' 个持仓 · '+(Object.values(state.order_counts).reduce((a,b)=>a+b,0))+' 个累计订单');text('working-orders',working);text('stock-note','账本现金 '+money(stock.cash)+' · 无融资');const svg=svgEl('svg',{viewBox:'0 0 90 90','aria-label':'股票账本现金与持仓占比'});svg.append(svgEl('circle',{cx:45,cy:45,r:33,fill:'none',stroke:'var(--line)','stroke-width':8}));const fraction=nav>0?stock.cash/nav:0;svg.append(svgEl('circle',{cx:45,cy:45,r:33,fill:'none',stroke:'var(--accent)','stroke-width':8,'stroke-dasharray':`${fraction*207.35} 207.35`,transform:'rotate(-90 45 45)'}));const label=svgEl('text',{x:45,y:43,'text-anchor':'middle',fill:'var(--ink)','font-size':15});label.textContent=Math.round(fraction*100)+'%';const sub=svgEl('text',{x:45,y:58,'text-anchor':'middle',fill:'var(--muted)','font-size':8});sub.textContent='现金占比';svg.append(label,sub);$('capital-ring').replaceChildren(svg);}
function renderState(){
 const health=state.health,healthy=health?.status==='ok'&&Date.now()-new Date(health.time).getTime()<180000;
 renderPart('connection',()=>healthy,()=>{$('connection').replaceChildren(element('i'),document.createTextNode(healthy?'服务在线':'需要检查'));});
 setWarning(state.paused?'新增执行已暂停：'+state.pause_reason:healthy?'':'服务心跳异常，请检查常驻进程。');
 text('session-label',state.calendar.session?'美股正常交易时段':'下次美股开盘');text('next-open',state.calendar.session?'正在监控':dateTime(state.calendar.next_open));text('session-sub','北京时间 · '+(state.calendar.session?'按交易日运行':'休市待机'));
 renderPart('timeline',()=>state.timeline,renderTimeline);renderOverview();
 renderPart('ranking',()=>[state.strategies,state.focus_data_hash,selected,chartMode,$('all-versions').checked],renderRanking);
 renderPart('active',()=>state.strategies,renderActiveStrategies);
 renderPart('capital',()=>[state.portfolios,state.order_counts],renderCapital);
 renderPart('strategies',()=>[state.strategies,selected,$('all-versions').checked,$('include-rejected').checked,[...highlights]],renderStrategies);
 renderPart('market',()=>[state.universe_scan,state.focus_quotes,[...highlights]],renderMarket);
 renderPart('news',()=>[state.news_packet,state.intervals.news],renderNews);
 renderPart('paper',()=>[state.paper_activity,state.orders,state.strategies.filter(r=>r.stage==='paper'),state.champion_rotation,state.broker_capital,state.option_funding_mode,state.option_account_status,state.option_data_status,state.research_evidence?.screen,[...highlights]],renderPaper);
 renderCurrentHoldings();
 renderPart('evolution',()=>state.evolutions,renderEvolution);
 renderPart('factor-lab',()=>state.research_evidence,renderFactorLab);
 renderPart('review',()=>[state.daily_brief,state.review_markdown,state.daily_asof,state.paper_activity.nav,state.paper_activity.fill_count],renderReview);
 renderPart('review-summary',()=>state.daily_brief,renderBriefSummary);
 renderPart('runtime',()=>[state.tasks,state.schedule?.mode,state.execution_readiness,state.paper_execution_session,state.recovery],renderRuntime);
 renderPart('references',()=>state.strategy_references,renderReferences);
 renderPart('events',()=>[state.events,[...highlights]],renderEvents);
 renderPart('cadence',()=>[state.intervals,state.push_status,state.push_probe,state.ai_runtime,state.ai_requested],renderCadence);
 renderPart('audit',()=>state.forecast_tracking,renderForecastAudit);
 renderPart('pipeline',()=>state.strategies.map(r=>[r.stage,r.full_id]),()=>{
  const active=state.strategies.find(r=>r.full_id===state.focus_id);$('pipeline').replaceChildren();
  const steps=[['01','假设与因子'],['02','因果回测'],['03','核账与筛选'],['04','未来观察'],['05','模拟交易'],['06','实盘审查']];
  for(const [i,[number,label]] of steps.entries()){const step=element('div','pipeline-step '+(i===3&&active?.stage==='shadow'||i===4&&active?.stage==='paper'?'current':''));step.append(element('b',null,number),element('span',null,label));$('pipeline').append(step);if(i<steps.length-1)$('pipeline').append(element('span','pipeline-arrow','›'));}
 });
 text('refresh-proof','页面每15秒读取 · 最近收到 '+dateTime(receivedAt)+' · 行情扫描 '+dateTime(state.universe_scan?.created_at));
 text('last-updated','服务心跳 '+dateTime(health?.time)+' · 日线基准 '+(state.last_tick?.signal_cache_asof||'—'));
}
async function refresh(){
 if(loading)return;loading=true;
 try{const response=await fetch('/api/status');if(!response.ok)throw Error('status');const previousState=state;state=await response.json();receivedAt=Date.now();serverOffset=new Date(state.calendar.now).getTime()-receivedAt;recordChanges(previousState);renderState();await renderChart();}
 catch(e){console.error('Dashboard update failed',e);setWarning('页面暂时无法更新，将自动重试。');text('connection','更新待重试');}
 finally{loading=false;}
}

const dollars=n=>Number.isFinite(n)?new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:2}).format(n):'—';
function blankRow(target,columns,message){const row=element('tr'),cell=element('td','empty-note',message);cell.colSpan=columns;row.append(cell);$(target).append(row);}
function renderCadence(){const i=state.intervals||{},p=state.push_status||{},probe=state.push_probe||{};const status={closed:'休市待机',subscribed:'已订阅',receiving:'正在接收',fallback_rest:'连接失败，REST 补充'}[p.status]||'等待开市';$('market-cadence').replaceChildren();for(const label of ['订单 / 风控 '+i.orders+'s','全池扫描 '+i.broad_scan+'s · 重点新鲜报价优先显示','资讯 '+i.news+'s','推送 · '+status])$('market-cadence').append(element('span',null,label));if(probe.subscribe_ok)$('market-cadence').append(element('span',null,'鉴权 / 订阅已验证 · 重点最多 '+i.push_focus_limit+' 只'));const ai=state.ai_runtime||{},requested=state.ai_requested||{};text('ai-runtime','Codex 设置 '+requested.model+' / '+requested.reasoning_effort+' · 最近运行核验 '+(ai.model||'尚无')+' / '+(ai.reasoning_effort||'未记录')+' · '+dateTime(ai.observed_at));}
function renderNews(){
 const n=state.news_packet||{};text('news-date','最近采集 '+dateTime(n.observed_at));$('news-scope').replaceChildren();
 const info=element('div','news-scope-head');info.append(element('b',null,'动态候选 + 跨市场主题'),element('span',null,'开市每 '+Math.round((state.intervals?.news||1800)/60)+' 分钟 · 收盘每日任务一次 · 休市停止正常采集'));$('news-scope').append(info);
 const targets=element('div','news-targets');for(const symbol of n.targets||[])targets.append(element('span',null,symbol.replace('US.','')));$('news-scope').append(targets);
 const sources=[...new Set([...(n.items||[]),...(n.global_items||[])].map(r=>r.source))];$('news-scope').append(element('p','muted small','已保存标题来源：'+(sources.join(' / ')||'等待采集')+(n.errors?.length?' · 本次 '+n.errors.length+' 个目标采集失败':'')));
 $('news-list').replaceChildren();for(const r of [...(n.items||[]).slice(0,5),...(n.global_items||[]).slice(0,3)]){const card=element('article','news-item'),link=element('a',null,r.title);try{const u=new URL(r.url);if(u.protocol==='https:'&&['news.futunn.com','www.futunn.com','wallstreetcn.com','finance.eastmoney.com'].includes(u.hostname)){link.href=r.url;link.target='_blank';link.rel='noopener noreferrer';}}catch(_){}card.append(element('span','news-symbol',r.symbol.replace('US.','')),link,element('p',null,'发布 '+dateTime(r.published_at)+' · 首次观察 '+dateTime(r.available_at)),element('span','news-source',r.source||'Futunn'));$('news-list').append(card);}
 if(!(n.items||[]).length)$('news-list').append(element('p','empty-note','尚无已记录资讯证据。'));
}
function renderPaper(){
 const r=state.champion_rotation,c=state.broker_capital||{},champion=state.strategies.find(s=>s.full_id===(r&&r.status!=='completed'?r.target:state.account_champion));
 text('account-policy',state.capital_mode==='broker_equity'?'账户总资产 '+dollars(c.total_asset)+' · 股票当前资产额度 '+dollars(c.stock_equity_cap)+(state.option_funding_mode==='independent_pending'?' · 期权使用独立账户，等待绑定':state.option_funding_mode==='shared_ratio'?' · 期权10%上限 '+dollars(c.option_equity_cap)+' / 股票90%':' · 期权预留 '+dollars(c.option_reserve))+' · 使用现金，不使用融资购买力':'股票授权 '+dollars(state.stock_budget)+' · 期权 '+dollars(state.option_budget));
 const stages={queued:'等待开盘',cancelling:'撤销旧委托',liquidating:'退出旧策略持仓',settling:'等待资金交收',blocked:'交接阻断',completed:'交接完成'};
 text('account-rotation',(champion?'优胜策略：'+champion.display_name+' · ':'')+(r?(stages[r.status]||r.status)+' · '+r.reason:'等待统一比较与晋升'));
 const screen=state.research_evidence?.screen||{},rank=screen.ranking||[];
 text('account-ranking-note','统一行情与区间 '+(screen.windows?.retrospective_test||[]).join(' 至 ')+' · 单边25bps，另验50bps与额外延迟；此处为历史比较，不是模拟实绩。');
 $('account-ranking').replaceChildren();for(const row of rank){const name=state.strategies.find(s=>s.full_id===row.strategy);const tr=element('tr');tr.append(element('td',null,(row.strategy===screen.champion?'当前择优 · ':'')+(name?.display_name||row.strategy.slice(0,12))),element('td',direction(row.return_pct),pct(row.return_pct)),element('td',null,pct(row.stats.validation.return_pct)),element('td',null,pct(row.stats.retrospective_test.max_drawdown_pct)),element('td',null,row.eligible?'通过':'未通过'));$('account-ranking').append(tr);}

 text('capital-policy','股票授权本金 '+dollars(state.stock_budget)+(state.option_funding_mode==='independent_pending'?' · 美股期权独立账户待绑定，额度待核实':state.option_funding_mode==='shared_ratio'?' · 同账户期权10%，初始分配 '+dollars(state.option_budget):' · 期权预留 '+dollars(state.option_budget))+'；账户模式以最新券商资产和现金约束执行。');
 const option=state.paper_activity?.option||{};text('option-paper-nav',dollars(option.nav));signedValue('option-paper-return',option.return_pct,pct(option.return_pct));text('option-paper-fills',option.fill_count||0);text('option-paper-status',state.options_status+' '+(option.status?.detail||({'closed':'休市等待开盘',monitoring:'监控中',holding:'持有合约',working:'等待委托核对',error:'期权检查失败，请查看运行状态'}[option.status?.status]||'等待运行')));renderPart('option-paper-chart',()=>[option.fill_count,option.curve],()=>{if(option.fill_count)lineChart($('option-paper-chart'),(option.curve||[]).map(r=>({date:r.observed,session:r.session,equity:r.performance_equity??r.nav})),[],false,[],null,option.performance_initial_capital||option.budget||1000);else emptyChart($('option-paper-chart'),'等待首笔期权模拟成交',state.option_funding_mode==='independent_pending'?'等待独立美股期权模拟账户身份及资金核验。':'真实合约报价、流动性、预算与退出条件共同决定是否开仓。');});
 const od=state.option_data_status;
 text('option-data-status',od?'已验证 '+od.underlying+' · '+od.chain_contracts+' 份期权链 / '+od.expirations+' 个到期日 · 报价日期 '+od.quote_date+' · 买卖价、IV、Greeks、OI、成交量可读；抽查日线 '+od.daily_bars+' 根，5分钟 '+od.minute5_bars+' 根。'+od.limitation:'等待期权行情核验。');
 const p=state.paper_activity||{},orders=state.orders||[],fills=p.fills||[];
 text('paper-phase',p.fill_count?'已记录 '+p.fill_count+' 次股票成交增量':'尚未产生模拟成交');text('paper-nav',dollars(p.nav));signedValue('paper-return',p.return_pct,pct(p.return_pct));signedValue('paper-realized',p.realized_pnl,dollars(p.realized_pnl));signedValue('paper-unrealized',p.unrealized_pnl,dollars(p.unrealized_pnl));text('paper-fees',dollars(p.estimated_fees));
 renderPart('paper-chart',()=>[p.fill_count,p.curve],()=>{if(p.fill_count){lineChart($('paper-chart'),(p.curve||[]).map(r=>({date:r.observed,session:r.session,equity:r.performance_equity??r.nav})),[]);}else{emptyChart($('paper-chart'),'等待首笔模拟成交',state.paper_authorization?'已授权并行模拟；这里将展示券商实际模拟成交后的资金曲线。':'当前候选仍需未来验证；这里将展示券商模拟成交后的资金曲线。');}});
 text('paper-note','券商模拟成交与账本估值；费用按预留口径估算，尚非券商结算值。平均成本核账；成交时间显示程序核对时间；资金划入不计盈利，曲线已剔除资金变动。'+(p.fill_count?'':'当前收益为初始资金状态。'));
 const names={reserved:'已预留',submitting:'提交中',unknown:'待核对',submitted:'已委托',partial:'部分成交',filled:'已成交',cancelled:'已撤单',rejected:'已拒绝'};
 keyedRows('order-table',orders.slice().reverse().map(o=>({...o,highlight:flagged('order:'+o.id)})),o=>o.id,o=>{const tr=element('tr');markRow(tr,'order:'+o.id);tr.append(element('td',null,dateTime(o.created)),element('td','strategy-name',o.symbol.replace('US.','')+' / '+(o.side==='BUY'?'买入':'卖出')),element('td',null,String(o.qty)),element('td',null,dollars(o.price)),element('td',null,o.filled+' / '+(o.filled?dollars(o.fill_value/o.filled/o.multiplier):'—')),element('td',null,names[o.status]||o.status));if(o.highlight)tr.children[5].append(element('span','change-label',o.highlight.label));return tr;});
 if(!orders.length&&!$('order-table').children.length)blankRow('order-table',6,'尚无委托记录。');
 keyedRows('fill-table',fills.map(f=>({...f,highlight:flagged('fill:'+f.seq)})),f=>f.seq,f=>{const tr=element('tr');markRow(tr,'fill:'+f.seq);tr.append(element('td',null,dateTime(f.observed)),element('td','strategy-name',f.symbol.replace('US.','')+' / '+(f.side==='BUY'?'买入':'卖出')),element('td',null,String(f.qty)),element('td',null,dollars(f.price)),element('td',null,dollars(f.fee)),element('td',direction(f.realized_pnl),dollars(f.realized_pnl)));return tr;});
 if(!fills.length&&!$('fill-table').children.length)blankRow('fill-table',6,'尚无券商成交增量；不会用回测交易填充。');
}
function renderEvolution(){const records=state.evolutions||[];text('evolution-count',records.length+' 个代码版本');$('evolution-list').replaceChildren();for(const r of records.slice(0,8)){const card=element('article','evolution-item'),head=element('div','evolution-head'),link=element('a','code-link','查看生成代码 ↗');if(r.code_url){link.href=r.code_url;link.target='_blank';link.rel='noopener';}else{link.textContent='结构检查未通过';}head.append(element('b',null,r.name),element('span','status-tag '+r.stage,stageNames[r.stage]||r.stage),link);card.append(head,element('p',null,r.hypothesis),element('pre','factor-expression',JSON.stringify(r.expression,null,2)),element('p','muted small','因果检查 '+(r.checks.prefix_invariance&&r.checks.future_perturbation?'已通过':'待验证')+' · 有效覆盖 '+(r.checks.finite_coverage*100).toFixed(1)+'% · '+dateTime(r.created_at)),element('p','muted small','反证：'+r.falsification));if(r.failed_gates?.length)card.append(element('p','failure-note small','未通过：'+r.failed_gates.map(g=>gateNames[g]||g).join('、')));$('evolution-list').append(card);}if(!records.length)$('evolution-list').append(element('p','empty-note','等待 Codex 生成首个新因子表达式和代码。'));}

$('theme-toggle').addEventListener('click',()=>applyTheme(document.documentElement.dataset.theme==='light'?'dark':'light'));
$('show-forecast').addEventListener('change',()=>renderChart());
document.addEventListener('scroll',()=>{$('chart-tooltip').hidden=true;},true);
$('refresh').addEventListener('click',()=>{chartCache.clear();activityCache.clear();forecastCache.clear();historicalHoldingsCache.clear();refresh();});
$('activity-day').addEventListener('change',e=>{activityDay=e.target.value;dailyLimit=20;tradeLimit=30;renderActivity();});
$('activity-side').addEventListener('change',()=>{tradeLimit=30;renderActivity();});
$('activity-more-days').addEventListener('click',()=>{dailyLimit+=40;renderActivity();});
$('activity-more-trades').addEventListener('click',()=>{tradeLimit+=50;renderActivity();});
$('holdings-scope').addEventListener('change',e=>{holdingsScope=e.target.value;renderCurrentHoldings();});
$('strategy-select').addEventListener('change',e=>{selected=e.target.value;renderRanking();renderStrategies();renderChart();});
$('all-versions').addEventListener('change',()=>{renderRanking();renderStrategies();renderChart();});
$('include-rejected').addEventListener('change',()=>renderStrategies());
document.querySelectorAll('[data-mode]').forEach(button=>button.addEventListener('click',()=>{chartMode=button.dataset.mode;document.querySelectorAll('[data-mode]').forEach(b=>b.classList.toggle('active',b.dataset.mode===chartMode));renderRanking();renderStrategies();renderChart();}));
document.querySelectorAll('a[href^="#"]').forEach(link=>link.addEventListener('click',e=>{e.preventDefault();navigate(link.getAttribute('href').slice(1));}));
if(globalThis.addEventListener)globalThis.addEventListener('hashchange',()=>showView((globalThis.location?.hash||'#overview').slice(1)));

initTheme();showView((globalThis.location?.hash||'#overview').slice(1),false);updateClock();refresh();setInterval(()=>{if(!document.hidden)refresh();},15000);
setInterval(()=>{if(document.hidden)return;updateClock();let expired=false;for(const [key,h] of highlights)if(h.until<=Date.now()){highlights.delete(key);expired=true;}if(expired&&state)renderState();},1000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});

let selfCheckPolling=false;
const selfCheckNames={ok:'通过',failed:'失败',warning:'注意',unconfigured:'未配置',unverified:'未验证',skipped:'已跳过',pending:'等待',running:'检查中'};
function renderSelfCheck(report){
 const busy=report.status==='running';$('self-check-run').disabled=busy;$('self-check-run').textContent=busy?'检查中…':'重新自检';
 const rows=report.items||[],done=rows.filter(r=>r.status!=='pending').length,c=report.counts||{};
 text('self-check-summary',busy?'正在检查 '+done+' / '+rows.length:report.status==='completed'?(report.overall==='ok'?'检查通过':report.overall==='failed'?'核心服务需要处理':'部分服务需要关注')+' · '+(c.ok||0)+' 项通过'+((c.failed||0)?' · '+c.failed+' 项失败':'')+((c.unverified||0)?' · '+c.unverified+' 项待验证':''):report.status==='idle'?'尚未自检':report.detail||'自检未完成');
 text('self-check-time',dateTime(report.finished_at||report.started_at));
 const evidenceLabels={heartbeat_age_seconds:'心跳间隔（秒）',database_ok:'账本完整性通过',mode:'运行模式',provider:'模型提供方',model:'运行模型',reasoning_effort:'推理强度',requested_model:'请求模型',requested_reasoning_effort:'请求推理强度',ret_code:'业务返回码',symbol:'探测标的',data_date:'行情日期',update_time:'行情时间戳',entries:'本次返回条数',complete_universe_verified:'已校验全量成分',bars:'日线条数',requested_end:'请求截止日期',account_verified:'模拟账户已核实',read_endpoints_verified:'已验证只读接口数',order_endpoint_tested:'已测试下单接口',paper_enabled:'模拟框架已启用',auth_ok:'推送鉴权通过',subscribe_ok:'订阅确认通过',streaming_freshness_verified:'已验证持续实时行情',code:'业务返回码',items:'资讯条数',raw:'原始条数',accepted:'可用条数',error_type:'错误类型',expected_completed_session:'最近完成交易日',symbols:'日线覆盖标的数',reachable:'服务有响应',http_status:'HTTP 状态',authenticated:'本探针使用登录凭据',protocol_initialized:'协议初始化通过',tool_call_verified:'工具调用已验证'};
 const build=(r)=>{const row=element('div','self-check-item'),head=element('div');head.append(element('b',null,r.name),element('small',null,[r.group||r.transport,r.latency_ms!=null?(r.latency_ms>=1000?(r.latency_ms/1000).toFixed(1)+' 秒':r.latency_ms+' ms'):null,r.checked_at?dateTime(r.checked_at):null].filter(Boolean).join(' · ')));const detail=element('div');detail.append(element('p','self-check-detail',r.detail));if(r.evidence&&Object.keys(r.evidence).length){const more=element('details','self-check-evidence');more.append(element('summary',null,'验证细节'));for(const [k,v] of Object.entries(r.evidence)){if(v==null)continue;const value=typeof v==='boolean'?(v?'是':'否'):k==='mode'?(modeNames[v]||v):k==='update_time'?dateTime(v):String(v);more.append(element('small',null,(evidenceLabels[k]||k)+'：'+value));}detail.append(more);}row.append(head,detail,element('span','self-check-tag '+r.status,selfCheckNames[r.status]||r.status));return row;};
 $('self-check-items').replaceChildren(...rows.map(build));$('self-check-mcp').replaceChildren(...(report.mcp_servers||[]).map(build));
 if(!rows.length)$('self-check-items').append(element('p','empty-note','暂无实测结果。服务在线不代表所有接口均已通过。'));
}
async function loadSelfCheck(){try{const r=await fetch('/api/self-check');if(!r.ok)throw Error();const report=await r.json();renderSelfCheck(report);return report;}catch(e){text('self-check-summary','无法读取自检状态');return null;}}
async function pollSelfCheck(){if(selfCheckPolling)return;selfCheckPolling=true;try{for(let i=0;i<90;i++){await new Promise(resolve=>setTimeout(resolve,2000));const r=await loadSelfCheck();if(!r||r.status!=='running')break;}}finally{selfCheckPolling=false;}}
$('self-check-open').addEventListener('click',async()=>{navigate('runtime');const r=await loadSelfCheck();if(r?.status==='running')pollSelfCheck();$('self-check-panel').scrollIntoView({block:'start',behavior:'smooth'});});
$('self-check-run').addEventListener('click',async()=>{$('self-check-run').disabled=true;try{const r=await fetch('/api/self-check',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(!r.ok)throw Error();const report=await r.json();renderSelfCheck(report);if(report.status==='running')pollSelfCheck();}catch(e){text('self-check-summary','自检未能启动，请稍后重试');$('self-check-run').disabled=false;}});
loadSelfCheck().then(r=>{if(r?.status==='running')pollSelfCheck();});
