// DOM render smoke check against the local read-only API; no browser layout assertion.
const fs=require('fs'),vm=require('vm');
class Node{
 constructor(tag='div'){
  this.tag=tag;this.children=[];this.parentNode=null;this.style={};this.attrs={};this.dataset={};this.listeners={};this.hidden=false;this.value='';this.checked=false;this.className='';this._text='';
  this.classList={toggle:(k,on)=>{const classes=new Set(this.className.split(/\s+/).filter(Boolean));if(on)classes.add(k);else classes.delete(k);this.className=[...classes].join(' ');}};
 }
 get textContent(){return this._text+this.children.map(n=>n.textContent||'').join('');}
 set textContent(value){this.replaceChildren();this._text=String(value??'');}
 append(...nodes){for(const node of nodes)this.insertBefore(node,null);}
 insertBefore(node,reference){if(node.parentNode)node.parentNode.removeChild(node);const index=reference?this.children.indexOf(reference):this.children.length;if(index<0)throw Error('Missing reference');this.children.splice(index,0,node);node.parentNode=this;}
 removeChild(node){const index=this.children.indexOf(node);if(index<0)throw Error('Missing child');this.children.splice(index,1);node.parentNode=null;}
 replaceChildren(...nodes){for(const n of this.children)n.parentNode=null;this.children=[];this._text='';this.append(...nodes);}
 setAttribute(k,v){this.attrs[k]=v;if(k==='id')this.id=v;if(k==='href')this.href=v;if(k==='class')this.className=v;if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=v;}
 getAttribute(k){return this.attrs[k]??null;}
 removeAttribute(k){delete this.attrs[k];}
 addEventListener(k,v){const old=this.listeners[k];this.listeners[k]=old?(...args)=>{old(...args);v(...args);}:v;}
 scrollIntoView(){this.scrolled=true;}
 get options(){return this.children;}
 getBoundingClientRect(){return {left:0,top:100,width:800,height:286};}
}
function parseHTML(html){
 const root=new Node('document'),stack=[root],voidTags=new Set(['meta','link','input','br','hr','img']);
 for(const m of html.matchAll(/<!--[\s\S]*?-->|<![^>]*>|<\/?([\w-]+)([^>]*)>|([^<]+)/g)){
  if(m[0].startsWith('</')){stack.pop();continue;}
  if(m[1]){const node=new Node(m[1]);for(const a of m[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g)){node.setAttribute(a[1],a[2]||'');if(['hidden','checked','open'].includes(a[1]))node[a[1]]=true;}
   stack.at(-1).append(node);if(!voidTags.has(m[1])&&!m[2].trim().endsWith('/'))stack.push(node);
  }else if(m[3]){const text=new Node('#text');text._text=m[3];stack.at(-1).append(text);}
 }
 return root;
}
const descendants=node=>[node,...(node.children||[]).flatMap(descendants)];
const content=node=>[node.textContent||'',...(node.children||[]).map(content)].join(' ');
async function until(test,label){for(let i=0;i<800;i++){if(test())return;await new Promise(resolve=>setTimeout(resolve,25));}throw Error(label);}
(async()=>{
 const html=fs.readFileSync('quant_agent/static/dashboard.html','utf8'),tree=parseHTML(html),all=descendants(tree),nodes=new Map(all.filter(n=>n.id).map(n=>[n.id,n]));
 const modes=all.filter(n=>n.dataset.mode),pages=all.filter(n=>n.dataset.page),nav=all.filter(n=>n.tag==='nav').flatMap(n=>descendants(n).filter(c=>c.tag==='a'));
 const root=all.find(n=>n.tag==='html'),storage=new Map(),timers=[];
 const document={hidden:false,documentElement:root,getElementById:id=>{if(!nodes.has(id))throw Error('Missing element '+id);return nodes.get(id);},createElement:tag=>new Node(tag),createElementNS:(_,tag)=>new Node(tag),createTextNode:text=>{const n=new Node('#text');n._text=text;return n;},querySelectorAll:selector=>selector==='[data-mode]'?modes:selector==='[data-page]'?pages:selector==='nav a'?nav:selector==='a[href^="#"]'?all.filter(n=>n.tag==='a'&&n.href?.startsWith('#')):[],addEventListener(){}};
 const context={document,Intl,Date,Map,Set,console,URL,innerWidth:1024,innerHeight:768,localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},fetch:url=>fetch('http://127.0.0.1:8791'+url),location:{hash:'#overview'},scrollY:0,scrollTo({top}){this.scrollY=top;},setInterval(fn,ms){timers.push(ms);},setTimeout(fn,ms){const timer=setTimeout(fn,ms);timer.unref();return timer;},Error,Number,Math,JSON};vm.createContext(context);vm.runInContext(fs.readFileSync('quant_agent/static/dashboard.js','utf8'),context);
 for(let i=0;i<800&&vm.runInContext('loading',context);i++)await new Promise(resolve=>setTimeout(resolve,50));
 if(vm.runInContext('loading',context))throw Error('Render did not finish');
 const runtime=await (await fetch('http://127.0.0.1:8791/api/status')).json();
 if(!runtime.recovery?.logging?.file_enabled)throw Error('Runtime file logging unavailable');
 if(!nodes.get('recovery-summary').textContent.includes('恢复与检查点'))throw Error('Recovery panel missing');
 if(nodes.get('checkpoint-list').children.length!==Math.min(12,runtime.recovery.checkpoints.length))throw Error('Checkpoint rows missing');
 if(runtime.capital_mode==='broker_equity'){
  const rank=runtime.research_evidence?.screen?.ranking||[];
  if(nodes.get('account-ranking').children.length!==rank.length)throw Error('Account competition ranking missing');
  if(!nodes.get('account-policy').textContent.includes('不使用融资购买力'))throw Error('Account cash-only mandate missing');
  if(!nodes.get('account-rotation').textContent.includes(runtime.champion_rotation?.reason||'等待'))throw Error('Rotation state missing');
 }

 if(runtime.research_evidence){
  const lab=runtime.research_evidence,rows=lab.trials.filter(r=>r.is_factor||r.current_version);
  if(nodes.get('factor-evidence-table').children.length!==rows.length||nodes.get('factor-lab-steps').children.length!==5)throw Error('Factor evidence pipeline missing');
  const factor=rows.find(r=>r.is_factor);if(factor){
   const material=await (await fetch('http://127.0.0.1:8791'+factor.evidence_url)).json();
   if(material.strategy!==factor.full_id||!material.spec.factor_expression)throw Error('Factor evidence link mismatch');
  }
 }
 if(runtime.paused){
  if(nodes.get('warning').hidden||!nodes.get('warning').textContent.includes(runtime.pause_reason))throw Error('Execution pause not visible');
 }else if(nodes.get('warning').hidden!==true)throw Error('Render warning: '+nodes.get('warning').textContent);
 if(!nodes.get('equity-chart').children.some(n=>n.tag==='svg'))throw Error('Historical chart not rendered');
 if(nav.length!==6||pages.length!==6||pages.filter(n=>!n.hidden).length!==1||pages.find(n=>!n.hidden).dataset.page!=='overview')throw Error('View routing/default overview failed');
 const expectedNav=new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:2}).format(runtime.paper_activity.nav);if(!content(nodes.get('overview-nav')).includes(expectedNav))throw Error('Ledger NAV does not match actual account');if(!runtime.paper_activity.fill_count&&!content(nodes.get('overview-pnl-sub')).includes('暂无成交'))throw Error('Empty account label missing');
 if(!timers.includes(15000)||!timers.includes(1000))throw Error('Refresh/clock cadence changed');
 nodes.get('research-archive').open=true;
 nav.find(n=>n.href==='#runtime').listeners.click({preventDefault(){}});
 if(pages.find(n=>!n.hidden).dataset.page!=='runtime'||context.location.hash!=='#runtime')throw Error('Navigation did not reveal runtime: '+JSON.stringify([pages.map(n=>[n.dataset.page,n.hidden]),context.location.hash,vm.runInContext('currentView',context)]));
 nav.find(n=>n.href==='#overview').listeners.click({preventDefault(){}});
 if(!nodes.get('research-archive').open)throw Error('Navigation reset expanded details');
 const activeNames=runtime.strategies.filter(r=>['paper','shadow','live_review'].includes(r.stage)).map(r=>r.display_name);if(activeNames.some(n=>!n)||new Set(activeNames).size!==activeNames.length)throw Error('Active strategy identities are ambiguous');if(!nodes.get('timeline-title').textContent.includes('美股交易日'))throw Error('Timeline missing');
 if(nodes.get('timeline-nodes').children.length!==5)throw Error('Timeline phases missing');
 if(!nodes.get('news-scope').children.length)throw Error('News scope missing');
 const ranked=vm.runInContext('sortedReturns(scopedStrategies()).map(r=>returnFor(r)).filter(Number.isFinite)',context);
 if(ranked.some((n,i)=>i&&n>ranked[i-1]))throw Error('Historical sorting incorrect');
 if(vm.runInContext('highlights.size',context)!==0)throw Error('Initial events falsely marked new');
 if(!nodes.get('strategy-detail').children.some(n=>n.className==='logic-grid'))throw Error('Strategy logic missing');
 const expected=vm.runInContext('state.strategies.filter(r=>r.data_hash===state.focus_data_hash&&r.stage!=="rejected").length',context);if(nodes.get('strategy-table').children.length!==expected)throw Error('Current-version table incorrect');
 if(runtime.paper_activity.fill_count){if(!descendants(nodes.get('paper-chart')).some(n=>n.tag==='svg'))throw Error('Actual paper equity chart missing');}else if(!nodes.get('paper-chart').children.some(n=>n.className==='empty-chart'))throw Error('Paper empty state missing');
 if(!nodes.get('news-list').children.length)throw Error('News missing');
 if(!nodes.get('evolution-list').children.length)throw Error('Evolution history missing');
 if(!nodes.get('self-check-open')||!nodes.get('self-check-panel')||!nodes.get('self-check-run'))throw Error('Self-check entry missing');
 await nodes.get('self-check-open').listeners.click();
 if(pages.find(n=>!n.hidden).dataset.page!=='runtime'||!nodes.get('self-check-items').children.length)throw Error('Self-check results missing');
 if(!nodes.get('ai-runtime').textContent.includes('gpt-6.1-sol / high'))throw Error('Actual AI configuration missing');
 if(!nodes.get('review-text').children.length)throw Error('Review missing');
 if(nodes.get('review-text').children.filter(n=>n.className==='brief-section').length!==8)throw Error('Structured brief sections missing');
 if(!descendants(nodes.get('review-text')).some(n=>n.tag==='b'&&n.textContent==='观察触发条件：'))throw Error('Brief conditions not emphasized');
 if(!descendants(nodes.get('review-text')).some(n=>n.tag==='a'&&n.href?.startsWith('https://')))throw Error('Brief sources missing');
 if(root.dataset.theme!=='dark')throw Error('Default theme missing');nodes.get('theme-toggle').listeners.click();
 if(root.dataset.theme!=='light'||storage.get('quant-lab-theme')!=='light'||nodes.get('theme-label').textContent!=='黑夜')throw Error('Day theme or persistence failed');
 nodes.get('theme-toggle').listeners.click();if(root.dataset.theme!=='dark')throw Error('Night theme switch failed');
 // Archived versions can correctly have no current forecast. Exercise the
 // projection checks on a current authorized version when one is available.
 if(!descendants(nodes.get('equity-chart')).some(n=>n.attrs?.class==='forecast-divider')){
  const current=runtime.strategies.find(r=>r.stage==='paper');
  if(current){context.currentVersion=current.full_id;await vm.runInContext('openStrategy(currentVersion)',context);}
 }
 const forecastNodes=descendants(nodes.get('equity-chart')),divider=forecastNodes.find(n=>n.attrs?.class==='forecast-divider');
 if(divider){
  if(forecastNodes.filter(n=>n.attrs?.class?.startsWith('forecast-line')).length!==2||forecastNodes.filter(n=>n.attrs?.class?.startsWith('forecast-band')).length!==2)throw Error('Forecast lines or bands missing');
  if(nodes.get('forecast-summary').children.length!==2||!content(nodes.get('forecast-note')).includes('不计入'))throw Error('Strategy/QQQ forecast evidence labels missing');
 }else{
  const id=vm.runInContext('selected',context),forecast=await (await fetch('http://127.0.0.1:8791/api/forecast/'+id)).json();
  if(forecast.available!==false||!forecast.reason||!content(nodes.get('forecast-note')).includes(forecast.reason))throw Error('Unavailable current forecast lacks verified reason');
  if(forecastNodes.some(n=>n.attrs?.class?.startsWith('forecast-line')||n.attrs?.class?.startsWith('forecast-band')))throw Error('Unavailable forecast fabricated paths');
 }
 const journal=vm.runInContext('activityData',context);
 if(!journal.trades.length||!journal.daily.length)throw Error('Historical journal missing');
 await until(()=>content(nodes.get('strategy-holdings-table')).includes('冻结复权收盘'),'Historical holdings did not load');
 if(!content(nodes.get('activity-context')).includes('初始本金 $10,000')||!content(nodes.get('strategy-holdings-title')).includes('回测持仓')||!content(nodes.get('activity-metrics')).includes('回测假设订单'))throw Error('Historical evidence/capital labels missing');
 const markers=descendants(nodes.get('equity-chart')).filter(n=>n.attrs?.class==='trade-marker');
 const expectedMarkers=new Set(journal.trades.map(t=>t.date+':'+t.side)).size;
 if(markers.length!==expectedMarkers)throw Error('Historical trade markers do not match journal');
 if((divider&&markers.some(n=>Number(n.children[0].attrs.cx)>Number(divider.attrs.x1)))||markers.some(n=>descendants(n).some(c=>c.tag==='title')))throw Error('Forecast contains trades or native tooltip remains');
 const buy=markers.find(n=>n.children[0].attrs.fill==='var(--red)'),sell=markers.find(n=>n.children[0].attrs.fill==='var(--green)');if(!buy||!sell)throw Error('Buy/sell colors missing');
 const event={stopPropagation(){}};buy.listeners.pointermove(event);
 const tooltip=nodes.get('chart-tooltip');
 if(tooltip.hidden||!content(tooltip).includes('买入')||!content(tooltip).includes(' 股 @ '))throw Error('Trade tooltip details missing');
 const unchangedSVG=nodes.get('equity-chart').children[0],unchangedRow=nodes.get('mover-table').children[0];
 await vm.runInContext('refresh()',context);
 if(nodes.get('equity-chart').children[0]!==unchangedSVG||nodes.get('mover-table').children[0]!==unchangedRow||tooltip.hidden)throw Error('Unchanged refresh reset chart, quote row or tooltip');
 context.innerWidth=360;context.innerHeight=420;tooltip.offsetWidth=200;tooltip.offsetHeight=140;
 vm.runInContext('positionChartTooltip(document.getElementById("chart-tooltip"),355,415)',context);
 if(parseFloat(tooltip.style.left)+200>348||parseFloat(tooltip.style.top)+140>408)throw Error('Tooltip clips viewport');
 context.innerWidth=1024;context.innerHeight=768;
 buy.listeners.click(event);const selectedDay=vm.runInContext('activityDay',context);
 if(pages.find(n=>!n.hidden).dataset.page!=='research'||context.location.hash!=='#strategy-activity')throw Error('Trade marker did not reveal strategy journal');
 if(nodes.get('activity-daily-table').children.length!==1||!content(nodes.get('activity-window')).includes(selectedDay))throw Error('Marker click did not filter day');
 if(!nodes.get('activity-trade-table').children.every(n=>n.children[0].textContent===selectedDay))throw Error('Daily journal leaked another date');
 const journalRow=nodes.get('activity-daily-table').children[0];
 await vm.runInContext('refresh()',context);
 if(vm.runInContext('activityDay',context)!==selectedDay||nodes.get('activity-daily-table').children[0]!==journalRow)throw Error('Polling reset journal filters or rows');
 nodes.get('activity-day').listeners.change({target:{value:'all'}});nodes.get('activity-side').value='SELL';nodes.get('activity-side').listeners.change();
 if(!nodes.get('activity-trade-table').children.every(n=>n.children[1].textContent.includes('卖出')))throw Error('Trade side filter failed');
 const countBefore=nodes.get('activity-trade-table').children.length;nodes.get('activity-more-trades').listeners.click();
 if(journal.trades.filter(t=>t.side==='SELL').length>countBefore&&nodes.get('activity-trade-table').children.length<=countBefore)throw Error('Trade pagination failed');
 nodes.get('activity-side').value='all';nodes.get('activity-side').listeners.change();
 const dailyBefore=nodes.get('activity-daily-table').children.length;nodes.get('activity-more-days').listeners.click();
 if(journal.daily.length>dailyBefore&&nodes.get('activity-daily-table').children.length<=dailyBefore)throw Error('Daily pagination failed');
 const svg=nodes.get('equity-chart').children.find(n=>n.tag==='svg');svg.listeners.pointermove({clientX:780,clientY:220});
 if(divider&&(!content(tooltip).includes('预测第 7 个交易日')||!content(tooltip).includes('QQQ情景区间')))throw Error('Forecast hover details missing');
 nodes.get('show-forecast').checked=false;await vm.runInContext('renderChart()',context);
 if(descendants(nodes.get('equity-chart')).some(n=>n.attrs?.class==='forecast-divider')||!nodes.get('forecast-summary').hidden)throw Error('Forecast toggle did not hide projections');
 nodes.get('show-forecast').checked=true;await vm.runInContext('renderChart()',context);
 modes.find(b=>b.dataset.mode==='forward').listeners.click();await until(()=>vm.runInContext('activityData?.kind',context)==='forward_shadow','Forward journal did not load');
 if(!nodes.get('equity-chart').children.some(n=>n.className==='empty-chart'))throw Error('Forward empty state missing');
 if(vm.runInContext('activityData.trades.length',context)!==0)throw Error('Unsealed Shadow trades shown as results');
 if(!nodes.get('forecast-summary').hidden||!nodes.get('show-forecast').disabled)throw Error('Forecast mixed into observed Shadow');
 if(!nodes.get('strategy-holdings-panel').hidden)throw Error('Broker holdings mixed into Shadow');
 const paperOwner=runtime.strategies.find(r=>r.stage==='paper');
 if(paperOwner){context.paperOwnerId=paperOwner.full_id;await vm.runInContext('openStrategy(paperOwnerId,"paper")',context);}
 else modes.find(b=>b.dataset.mode==='paper').listeners.click();
 await until(()=>vm.runInContext('activityData?.kind',context)==='paper','Paper journal did not load');
 const observedPaper=vm.runInContext('activityData',context);if(paperOwner){const expectedOrders=runtime.orders.filter(o=>o.strategy===paperOwner.full_id&&o.filled>0);if(observedPaper.trades.length!==expectedOrders.length)throw Error('Paper trade count differs from real owner fills');const ownerHoldings=runtime.holdings.strategies.find(g=>g.id===paperOwner.full_id);for(const position of ownerHoldings.positions){if(!content(nodes.get('strategy-holdings-table')).includes(position.symbol.replace('US.','')))throw Error('Actual paper position missing '+position.symbol);}if(ownerHoldings.positions.length&&!descendants(nodes.get('strategy-holdings-table')).some(n=>n.attrs?.class==='cost-line'))throw Error('Actual holding cost line missing');}if(nodes.get('strategy-holdings-panel').hidden||!content(nodes.get('activity-context')).includes('券商模拟'))throw Error('Actual paper evidence mislabeled');
 modes.find(b=>b.dataset.mode==='forward').listeners.click();await until(()=>vm.runInContext('activityData?.kind',context)==='forward_shadow','Forward reload failed');
 // Inject observed changes into the in-memory rendering harness only.
 // The live database, broker, orders and strategy versions are never modified.
 const snapshot=vm.runInContext('JSON.parse(JSON.stringify(state))',context);
 if(!nodes.get('holdings-scope').options.some(o=>o.value==='option'))throw Error('Holdings entry missing');for(const position of snapshot.holdings.stock.positions){if(!content(nodes.get('paper-holdings-table')).includes(position.symbol.replace('US.','')))throw Error('Account holding missing '+position.symbol);}
 // Render-only fixture checks both attribution and the actual cost-line SVG.
 const holdingOwner=snapshot.holdings.strategies.find(g=>g.bucket==='stock');
 const holdingRow={symbol:'US.RENDERTEST',qty:2,multiplier:1,avg_cost:100.3,cost_basis:200.6,mark:110,market_value:220,weight_pct:6.5,unrealized_pnl:19.4,unrealized_pct:9.67,quote_time:new Date().toISOString(),quote_status:'fresh',series:[{time:'2026-10-12T13:30:00Z',price:100},{time:'2026-10-12T13:31:00Z',price:110}]};
 holdingOwner.positions=[holdingRow];holdingOwner.position_count=1;holdingOwner.unrealized_pnl=19.4;
 context.holdingsFixture=snapshot;context.holdingOwnerId=holdingOwner.id;
 vm.runInContext('state=holdingsFixture;selected=holdingOwnerId;holdingsScope=holdingOwnerId;chartMode="paper";renderCurrentHoldings()',context);
 for(const prefix of ['paper-holdings','strategy-holdings']){
  const row=nodes.get(prefix+'-table').children[0];
  if(row.children.length!==10||!content(row).includes('$100.30')||!content(row).includes('$19.40'))throw Error('Owner cost/PNL not rendered');
  if(!descendants(row).some(n=>n.attrs?.class==='cost-line')||!descendants(row).some(n=>n.attrs?.class==='price-line'))throw Error('Observed price/cost lines missing');
  if(row.children[6].className!=='up')throw Error('Holding profit is not red');
 }
 holdingRow.unrealized_pnl=-20;holdingRow.unrealized_pct=-10;holdingRow.quote_status='stale';
 vm.runInContext('renderCurrentHoldings()',context);
 if(nodes.get('paper-holdings-table').children[0].children[6].className!=='down'||!content(nodes.get('paper-holdings-table')).includes('报价已过期'))throw Error('Loss or stale quote label missing');
 nodes.get('holdings-scope').listeners.change({target:{value:'option'}});
 if(vm.runInContext('holdingsScope',context)!=='option'||!content(nodes.get('paper-holdings-table')).includes(runtime.option_funding_mode==='independent_pending'?'待绑定':'当前空仓'))throw Error('Option holdings scope missing');
 const stableMover=nodes.get('mover-table').children[1];
 const before=JSON.parse(JSON.stringify(snapshot)),seq=vm.runInContext('highestEventSeq',context)+1;
 const mover=snapshot.universe_scan.hits[0];mover.price+=1;snapshot.universe_scan.created_at=new Date().toISOString();
 snapshot.events.unshift({seq,created:new Date().toISOString(),kind:'price_anomaly',payload:{symbol:mover.symbol,change_pct:mover.change_pct,data_date:mover.data_date}});
 snapshot.orders=[{id:'render-only-order',symbol:'US.TEST',side:'BUY',qty:1,price:100,filled:1,fill_value:99,multiplier:1,status:'filled',created:new Date().toISOString()}];
 snapshot.paper_activity.fills=[{seq:Math.max(0,...before.paper_activity.fills.map(f=>f.seq))+1,symbol:'US.TEST',side:'BUY',qty:1,price:99,fee:.3,realized_pnl:0,observed:new Date().toISOString()}];
 context.before=before;context.after=snapshot;vm.runInContext('state=after;recordChanges(before);renderMarket();renderPaper();renderEvents();',context);
 if(nodes.get('mover-table').children[1]!==stableMover)throw Error('Changing one quote replaced unrelated quote row');
 if(!nodes.get('mover-table').children[0].className.includes('row-highlight'))throw Error('New mover not highlighted');
 if(!nodes.get('order-table').children[0].className.includes('fill'))throw Error('Filled order not highlighted');
 if(!nodes.get('fill-table').children[0].className.includes('fill'))throw Error('New fill not highlighted');
 if(!nodes.get('trigger-status').textContent.includes('新动态'))throw Error('Trigger strip missing new event');
 if(!nodes.get('trigger-list').children[0].className.includes('fresh'))throw Error('Trigger not highlighted');
 const basePrice=vm.runInContext('state.universe_scan.hits[0].price',context);
 const previousChange=vm.runInContext('state.universe_scan.hits[0].change_pct',context);
 context.overlay={price:basePrice+2,data_date:mover.data_date,source:'Futunn WebSocket',updated_at:new Date().toISOString()};
 vm.runInContext('state.focus_quotes={[state.universe_scan.hits[0].symbol]:overlay};renderMarket()',context);
 if(!nodes.get('mover-table').children[0].children[1].textContent.includes((basePrice+2).toFixed(2)))throw Error('Fresh focus price not displayed');
 if(!nodes.get('mover-table').children[0].children[1].children[0].textContent.includes('推送'))throw Error('Quote source not displayed');
 const first=vm.runInContext('scopedStrategies().find(r=>r.chart_available||r.stage==="shadow"||r.stage==="paper").full_id',context);
 context.firstRenderable=first;
 vm.runInContext('chartMode="forward";state.strategies.find(r=>r.full_id===firstRenderable).forward_return_pct=1.5;renderRanking()',context);
 if(nodes.get('strategy-select').options[0].value!==first)throw Error('Forward sorting used historical returns');
 await vm.runInContext('chartMode="history";renderRanking();renderChart()',context);
 if(!['up','down','flat'].includes(nodes.get('return-value').className))throw Error('Return sign not styled');
 // Session attribution must survive observations after midnight UTC.
 vm.runInContext('lineChart(document.getElementById("equity-chart"),[{date:"2026-10-13T00:01Z",session:"2026-10-12",equity:10020}],[],false,[{date:"2026-10-13T00:01Z",session:"2026-10-12",symbol:"US.TEST",side:"BUY",qty:1,price:100,amount:100}])',context);
 if(!descendants(nodes.get('equity-chart')).some(n=>n.attrs?.class==='trade-marker'))throw Error('UTC date hid session trade marker');
 vm.runInContext('lineChart(document.getElementById("equity-chart"),[{date:"2026-10-12",equity:1000},{date:"2026-10-13",equity:1100}],[],false,[],null,1000)',context);
 nodes.get('equity-chart').children.find(n=>n.tag==='svg').listeners.pointermove({clientX:780,clientY:220});
 if(!content(tooltip).includes('+10.00%'))throw Error('Allocated capital return denominator is wrong');
 console.log(JSON.stringify({render:'ok',holdingsCostLines:true,holdingsOwnerSelection:true,holdingsStaleLabels:true,themeToggleAndPersistence:true,viewportTooltip:true,noNativeTooltip:true,forecastLines:forecastNodes.filter(n=>n.attrs?.class?.startsWith("forecast-line")).length,forecastBands:forecastNodes.filter(n=>n.attrs?.class?.startsWith("forecast-band")).length,todayDivider:Boolean(divider),forecastHover:divider?true:"not available",forecastUnavailableReasonVerified:!divider,forecastToggle:true,timeline:true,returnSorting:true,quoteOverlay:true,incrementalHighlights:true,currentStrategies:nodes.get('strategy-table').children.length,historicalSVG:true,tradeMarkers:expectedMarkers,tradeTooltip:true,dateAndSideFilters:true,pagination:true,forwardEmpty:true,paperEmpty:!runtime.paper_activity.fill_count,paperActualFills:runtime.paper_activity.fill_count,structuredBriefSections:8,highlightedConditions:true,sourceLinks:true,newsItems:nodes.get('news-list').children.length,evolvedFactors:nodes.get('evolution-list').children.length}));
})().catch(e=>{console.error(e);process.exit(1);});
