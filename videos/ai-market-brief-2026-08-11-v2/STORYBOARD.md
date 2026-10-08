---
format: 1080x1920
duration: 60s
message: "油价重新制造通胀压力，CPI前降低风险，AI高位去拥挤"
arc: "风险降档 → 指数与芯片分化 → 油价传导 → 个股确认 → 财报验证 → 留仓等CPI"
audience: "关注美股科技与AI产业链的个人投资者"
mode: autonomous
music: none
captions: burned-in
---

## Video direction

- 火橙、黑色、米白三色保持统一，六帧继续使用六种不同的信息图系统。
- 清新女声连续口播，句间只留 0.06 秒衔接；画面切换由真实 TTS 时间戳驱动。
- 字幕每次最多两行，位于竖屏下方黑色信息条，火橙色强调关键词。
- 播放顺序按台词重排为：风险仪表 → 指数分屏 → 油价管线 → 个股信号灯 → 财报控制室 → 决策地图。

## Frame 1 — 风险仪表盘

- status: animated
- src: compositions/frames/01-hook.html
- duration: 6.24s
- transition_in: cut
- scene: 栏目片头落下，风险仪表停在6.2，结论为进攻性再降半档
- voiceover: "美股每日资讯播报。今天把进攻性再降半档。"
- focal: 环形风险仪表、6.2、再降半档

## Frame 2 — 指数与芯片分屏

- status: animated
- src: compositions/frames/03-cpi-chain.html
- duration: 13.224s
- transition_in: blur-crossfade
- scene: 上半屏是温和指数跌幅，下半屏依次揭示NVDA与INTC更大跌幅
- voiceover: "昨夜标普跌零点零六，纳指跌零点三二，指数平静，内部却不好：英伟达跌近三个点，英特尔因增发跌超四个点，布伦特升到八十八美元。"
- focal: SPX、NASDAQ、NVDA、INTC跌幅对比

## Frame 3 — 油价传导管线

- status: animated
- src: compositions/frames/02-drivers.html
- duration: 18s
- transition_in: crossfade
- scene: 88美元油滴触发管线，通胀、美债、科技估值沿因果链逐步点亮
- voiceover: "市场正从弱就业利好科技，切换到油价推高通胀、CPI前降风险、AI高位去拥挤。我给市场Risk-On六点二分，没到全面Risk-Off，方向点仍是明晚七月CPI。"
- focal: 布伦特88美元与三节点因果管线

## Frame 4 — 个股交通灯

- status: animated
- src: compositions/frames/05-stocks.html
- duration: 4.332s
- transition_in: zoom-through
- scene: 快节奏信号灯把NVDA与AMD放进等待止跌区
- voiceover: "等AMD和英伟达止跌，不抢第一跌。"
- focal: WAIT、WATCH、EVENT、AVOID四条执行车道

## Frame 5 — 财报控制室

- status: animated
- src: compositions/frames/04-ai-chain.html
- duration: 11.436s
- transition_in: crossfade
- scene: CRWV与SMCI双列控制台验证订单、利润、Capex和现金流
- voiceover: "今晚看CoreWeave和超微电脑：订单、指引都强，股价涨，主线就健康；数字好但股票跌，就是警告。"
- focal: CRWV、SMCI、四项质量关卡与ROI警告

## Frame 6 — Brent决策地图与结尾

- status: animated
- src: compositions/frames/06-close.html
- duration: 9.768s
- transition_in: blur-crossfade
- scene: Brent三档地图先给出风险边界，随后橙色结尾落到降半档与等待CPI
- voiceover: "不追高，不抢英特尔反弹，也别忽略油价。等VWAP和五日线，把仓位留给CPI之后。"
- focal: 三档油价区域、降低追涨、CPI之后
