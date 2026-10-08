---
format: 1080x1920
duration: 60s
message: "油价成为第一宏观变量，AI硬件去拥挤，CPI前把进攻性再降半档"
arc: "降档 → 油价传导 → 内部分化 → 财报验证 → 个股动作 → 三档风控"
audience: "关注美股科技与AI产业链的个人投资者"
mode: autonomous
music: none
---

## Video direction

- 延续 Broadside 黑底与火橙单强调色，不加入行情截图或实时K线。
- 每帧只表达一个决策：数字负责证据，超大中文负责结论。
- 六段口播按估算窗口编排，但本版不合成音频；未来以真实TTS时长重排。
- 画面底部统一标注“简报数据 / 非实时 / 不构成投资建议”。

## Frame 1 — 进攻性再降半档

- status: animated
- src: compositions/frames/01-hook.html
- duration: 7s
- poster: 5.8s
- transition_in: cut
- scene: 市场轨迹从“进攻”分叉到“降档”，Risk-On 6.2成为结论
- voiceover: "今天最重要的不是纳指，而是布伦特原油。Risk-On 降到六点二，进攻性再降半档。"
- type: hook
- persuasion: contrast
- beat: caution
- blueprint: kinetic-type-beats
- focal: “进攻 / 降档”与6.2风险温度
- roles: 轨迹=决策变化 · 大字=结论 · 6.2=温度锚点
- sfx: none

Scene 1 (0.0–1.8s): 轨迹和日期标签进入。Scene 2 (1.8–4.3s): “进攻”被“降档”取代。Scene 3 (4.3–7.0s): 6.2落定并保持阅读。

## Frame 2 — 原油重写交易链

- status: animated
- src: compositions/frames/02-drivers.html
- duration: 9s
- poster: 7.5s
- transition_in: whip-pan
- scene: Brent约88美元，向通胀、美债和科技估值逐级传导
- voiceover: "油价约八十八美元，正在重写交易链：通胀预期抬头，美债承压，高估值科技降温。"
- type: mechanism
- persuasion: causal-chain
- beat: pressure
- blueprint: dataviz-countup
- focal: 88美元与五级传导条带
- roles: 88=触发器 · 条带=传导路径 · CPI=下一事件
- sfx: none

Scene 1 (0.0–2.3s): “原油接棒”与88出现。Scene 2 (2.3–6.8s): 五级条带依次填充。Scene 3 (6.8–9.0s): “压力传导”落定。

## Frame 3 — 指数小跌，内部更弱

- status: animated
- src: compositions/frames/03-cpi-chain.html
- duration: 11s
- poster: 9.2s
- transition_in: crossfade
- scene: SPX、NASDAQ与NVDA跌幅逐层放大，INTC作为最终警告
- voiceover: "指数跌得不多，内部却更弱。英伟达跌约百分之二点九，英特尔跌约百分之四点一。不要只看指数。"
- type: evidence
- persuasion: progressive-disclosure
- beat: divergence
- blueprint: compose
- focal: 三层跌幅与INTC -4.1%
- roles: 指数=表面 · NVDA和INTC=内部压力 · 橙色跌幅=警告
- sfx: none

Scene 1 (0.0–2.4s): 橙底“指数小跌”。Scene 2 (2.4–7.8s): 三层跌幅依次出现。Scene 3 (7.8–11.0s): “内部更弱”与INTC跌幅收尾。

## Frame 4 — 今晚验证AI ROI

- status: animated
- src: compositions/frames/04-ai-chain.html
- duration: 13s
- poster: 10.8s
- transition_in: zoom-through
- scene: CRWV与SMCI连接订单、毛利率、Capex和现金流，形成AI ROI测试
- voiceover: "今晚看 CoreWeave 和超微电脑。订单和指引要强，资本开支、现金流和毛利率也要守住。数字很好但股票跌，就是 ROI 警告。"
- type: validation
- persuasion: diagnostic-grid
- beat: scrutiny
- blueprint: center-outward-expansion
- focal: AI ROI中心与六个验证节点
- roles: CRWV/SMCI=事件 · 订单/毛利率/Capex/现金流=验证条件
- sfx: none

Scene 1 (0.0–2.6s): AI ROI核心出现。Scene 2 (2.6–9.7s): 六个节点逐项扩散。Scene 3 (9.7–13.0s): “缺产能→算ROI”结论落定。

## Frame 5 — 只做确认

- status: animated
- src: compositions/frames/05-stocks.html
- duration: 11s
- poster: 9.4s
- transition_in: crossfade
- scene: 个股按等待止跌、回踩承接、财报验证和短线回避分层
- voiceover: "操作上，AMD 和英伟达等止跌；博通看回踩；英特尔和存储先回避。现在不抢第一根下跌。"
- type: playbook
- persuasion: classification
- beat: discipline
- blueprint: grid-card-assemble
- focal: 四层执行阶梯
- roles: ticker=对象 · 状态=动作 · 不抢跌=纪律
- sfx: none

Scene 1 (0.0–2.1s): “操作只做确认”。Scene 2 (2.1–8.0s): 四层ticker依次归位。Scene 3 (8.0–11.0s): “不抢跌”落定。

## Frame 6 — CPI前的三档风控

- status: animated
- src: compositions/frames/06-close.html
- duration: 9s
- poster: 7.2s
- transition_in: flash-through-white
- scene: Brent三档阈值压缩成“降半档”，以财报与CPI两个事件收尾
- voiceover: "油价低于八十五，AI可进攻；八十五到九十，降低追涨；突破九十再叠加偏热CPI，就继续防守。今晚等财报，明晚等CPI。"
- type: close
- persuasion: rule-of-three
- beat: resolve
- blueprint: titlecard-reveal
- focal: Brent三档与“降半档”
- roles: 三档=条件 · 降半档=当前动作 · 财报/CPI=下一验证
- sfx: none

Scene 1 (0.0–3.0s): 三档Brent阈值依次出现。Scene 2 (3.0–5.6s): CPI前等待被竖线切断。Scene 3 (5.6–9.0s): 橙底“降半档”，落到今晚财报、明晚CPI。
