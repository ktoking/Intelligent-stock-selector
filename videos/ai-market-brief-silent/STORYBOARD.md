---
format: 1080x1920
duration: 60s
message: "AI 主线仍在，但行情已经从趋势市进入验证市"
arc: "concept-explainer"
audience: "关注美股科技与 AI 主线的个人投资者"
mode: autonomous
music: none
---

## Video direction

- Palette: Broadside dark register为主，`ink-black`全屏底，`cream`承担正文，`fire-orange`只用于每帧唯一的风险或结论强调；转折帧允许短暂使用橙色满屏 register。
- Type: Barlow display作为画面主体，IBM Plex Mono只承担时间戳、标签和示意声明；中文使用系统无衬线回退但保持同样的重量与比例层级。
- Motion grammar: 所有动画使用平滑长尾收束；静音帧按信息节拍逐项揭示，重点内容分布在每帧后半程；不使用弹跳、无限循环、懒惰呼吸或后半段慢推镜。
- Rhythm: 快速提问 → 逐步收窄 → 因果链加速 → 产业链扩散峰值 → 个股分层缓冲 → 结论长停留。Frame 5 是有意的阅读缓冲，Frame 6 在最终结论上保持最长静止。
- Safety: 全片标注“示意样片 / 非实时数据”；不出现实时价格、收益承诺或来源不明的精确行情。关键内容控制在画面上方约83%，为未来字幕轨预留底部空间。
- Negative list: 禁止网页仪表盘、六宫格小卡、复杂K线、蓝紫AI渐变、漂浮粒子屏保感、全部元素前25%同时入场后冻结。

## Frame 1 — 加速，还是拐点？

- status: animated
- src: compositions/frames/01-hook.html
- duration: 7s
- poster: 5.8s
- transition_in: cut
- scene: “加速”和“拐点”在同一条市场轨迹上分叉，问题本身成为画面主体
- voiceover: ""
- type: hook
- persuasion: Rhetorical question + contrast
- beat: tension
- blueprint: kinetic-type-beats (Adapt)
- focal: “加速 / 拐点”两组超大关键词与中央分叉轨迹
- roles: 分叉轨迹 = foreground subject · 两组关键词 = foreground statement · 周期刻度与示意声明 = supporting · 黑色平面 = background
- sfx: none

Adapt: 保留连续文字节拍落到最终问题的签名动作；把品牌落版改成市场轨迹的二选一分叉。

Scene 1 (0.0–1.8s): 黑色平面只出现一条细橙线从画面下方向上绘制，顶部mono标签“AI MARKET BRIEF / SILENT SAMPLE”进入；纵向rule-of-thirds，轨迹占画面高度约55%，SVG self-draw（`svg-path-draw`）平滑展开。

Scene 2 (1.8–4.3s): “加速”先以超大cream文字从左侧落定，轨迹向右上偏转；随后“拐点”以fire-orange从右侧替换进入，轨迹出现第二条下弯分支；关键词逐项而非同时出现，使用hard-cut word-swap（`discrete-text-sequence`）。

Scene 3 (4.3–7.0s): 两条分支同时停在上半区，中央问号与“美股现在在哪一边？”逐词组装；橙色只保留在“拐点”和问号，最终读屏保持1.4秒，只有轨迹端点一次轻微glow后静止。

narrativeRole: 用一个明确的二选一问题建立观看张力，而不是从市场背景讲起。

keyMessage: 当前最重要的不是日内涨跌，而是行情正在加速还是接近结构拐点。

## Frame 2 — 上涨驱动正在变少

- status: animated
- src: compositions/frames/02-drivers.html
- duration: 9s
- poster: 7.5s
- transition_in: whip-pan
- scene: 五条上涨驱动逐一熄灭，只剩CPI变量保持高亮
- voiceover: ""
- type: pain_point
- persuasion: Progressive disclosure + narrowing funnel
- beat: unease
- blueprint: dataviz-countup (Adapt)
- focal: 从五格收窄到一格的驱动力条带
- roles: 驱动力条带 = foreground subject · CPI高亮格 = foreground payoff · 宽度刻度 = supporting · 黑色数据平面 = background
- sfx: none

Adapt: 保留数据逐步变化并落在唯一英雄指标的签名动作；把数字上涨改成驱动力数量下降。

Scene 1 (0.0–2.3s): 只显示标题“Risk-On 仍在”，下方五条cream短柱按顺序从底部填满；full-width vertical strip，条带占上方60%，bars/fills（`stat-bars-and-fills`）依次建立。

Scene 2 (2.3–6.8s): “就业 / 流动性 / 龙头 / 风险偏好”四条依次降低为暗灰空轨，每熄灭一条，左侧计数从5降到1；最后一条CPI由cream切换为fire-orange，计数变化使用in-place token cycle（`discrete-text-sequence`）。

Scene 3 (6.8–9.0s): 画面只保留一条占据上半区的橙色CPI柱和大字“唯一变量”；其余项目作为20%透明度背景证据停留，读屏保持不再漂移。

narrativeRole: 把“驱动变少”从抽象判断变成可见的收窄过程。

keyMessage: 市场表面仍然偏Risk-On，但决定方向的变量正在集中到CPI。

## Frame 3 — CPI 如何重定价科技股

- status: animated
- src: compositions/frames/03-cpi-chain.html
- duration: 11s
- poster: 9.2s
- transition_in: crossfade
- scene: CPI从上到下触发三段因果链，最终压到科技估值
- voiceover: ""
- type: feature_showcase
- persuasion: Causal chain + progressive disclosure
- beat: comprehension
- blueprint: compose
- focal: CPI → 降息预期 → 10Y美债 → 科技估值的纵向因果链
- roles: CPI节点 = foreground trigger · 三段因果节点 = foreground mechanism · 箭头与方向标识 = supporting · 黑色规则网 = background
- sfx: none

Scene 1 (0.0–2.4s): 橙色满屏register短暂出现，中央只显示“CPI ↑？”；随后橙色平面向上擦除，露出黑底纵向因果舞台，使用scale-swap（`scale-swap-transition`）完成触发。

Scene 2 (2.4–5.1s): 第一条橙色箭头向下自绘，节点“降息预期 ↓”在画面上半区锁定；节点为超大数字方向符号加短标签，SVG self-draw（`svg-path-draw`）和per-word reveal（`dynamic-content-sequencing`）分开落点。

Scene 3 (5.1–7.8s): 第二条箭头继续向下，“10Y 美债 ↑”进入中央黄金位置，4.7%仅以“风险阈值示意”小标签出现，不伪装成实时读数。

Scene 4 (7.8–11.0s): 最后一条箭头压向底部上方安全区，“科技估值 ↓”占满画面宽度；前三个节点形成完整链路，关键词“重新定价”在9.2秒时由cream切成fire-orange并保持静止。

narrativeRole: 清晰解释CPI为什么会穿透到科技股估值，而不是只把CPI当作事件标签。

keyMessage: CPI的真正风险是通过降息预期和美债收益率改变科技股估值锚点。

## Frame 4 — AI 没结束，结构变了

- status: animated
- src: compositions/frames/04-ai-chain.html
- duration: 13s
- poster: 10.8s
- transition_in: zoom-through
- scene: GPU作为核心节点，产业链六个方向依次向外展开
- voiceover: ""
- type: product_intro
- persuasion: Concept naming + progressive disclosure
- beat: expansion
- blueprint: grid-card-assemble (Adapt)
- focal: 中央GPU核心与六个产业链分支
- roles: GPU核心 = foreground subject · ASIC/光通信/服务器/HBM/电力/液冷 = foreground branches · 连接线 = supporting · 黑色平面与大字AI水印 = background
- sfx: none

Adapt: 保留项目逐项自组装成完整阵列的签名动作；把等权卡片改为从GPU中心向外扩散的纵向产业链网络。

Scene 1 (0.0–2.6s): 中央上方出现超大“AI”，随后缩为背景水印；“GPU / CORE”橙色核心块在画面约42%高度锁定，使用scale-swap（`scale-swap-transition`）从概念切入结构。

Scene 2 (2.6–6.1s): ASIC、光通信、服务器三个cream分支依次沿左/右/下方连接线展开，连接线先画、标签后到；cluster→outward expansion（`center-outward-expansion`）但不同时入场。

Scene 3 (6.1–9.7s): HBM、电力、液冷继续补齐第二层网络，屏幕形成有方向的六分支阵列；最后加入的“液冷”保持橙色0.8秒后回到cream，表示资金扩散而非单一热点。

Scene 4 (9.7–13.0s): 网络整体静止，“AI 没结束”先出现，“单边阶段已过去”随后在下方安全区上沿出现；第二句中的“单边”切为fire-orange，最终持读1.7秒。

narrativeRole: 将AI主线从单一GPU叙事升级为产业链扩散结构。

keyMessage: AI仍是主线，但资金已经从单一龙头阶段转向产业链内部扩散。

## Frame 5 — 个股进入分化

- status: animated
- src: compositions/frames/05-stocks.html
- duration: 11s
- poster: 9.4s
- transition_in: crossfade
- scene: 六只股票按“消化 / 等待 / 回踩 / 再定价”四种状态分层
- voiceover: ""
- type: social_proof
- persuasion: Classification + comparison
- beat: discernment
- blueprint: grid-card-assemble (Adapt)
- focal: 四层状态阶梯与六个ticker标签
- roles: 状态阶梯 = foreground structure · ticker标签 = foreground evidence · 层级编号与非实时声明 = supporting · 黑色平面 = background
- sfx: none

Adapt: 保留列表逐项自组装并最终形成完整分类的签名动作；使用纵向四层阶梯适配竖屏，不做六宫格卡片。

Scene 1 (0.0–2.1s): 标题“不是齐涨，是分化”占据顶部，四条细横线从上到下依次绘制成阶梯，只显示状态词不显示ticker。

Scene 2 (2.1–5.3s): NVDA落入“震荡消化”，AMD落入“等待确认”；每个ticker以mono标签滑入对应层，状态标题保持大字层级，不出现实时价格。

Scene 3 (5.3–8.0s): AVGO与NET依次进入“趋势回踩”，标签沿同一方向排列，形成同类对比而非两张独立卡。

Scene 4 (8.0–11.0s): SMCI与AMAT进入“财报再定价”；四层全景在9.0秒完成，橙色强调词切换为“不追高”，其余元素全部停住供阅读。

narrativeRole: 让观众看见同一AI主线内部已经出现不同交易阶段。

keyMessage: 个股不再同步上涨，策略应从追龙头转向等待二次确认。

## Frame 6 — 从趋势市，进入验证市

- status: animated
- src: compositions/frames/06-close.html
- duration: 9s
- poster: 7.2s
- transition_in: flash-through-white
- scene: 三个风险条件逐项锁定，最终压缩成“验证市”结论
- voiceover: ""
- type: branding
- persuasion: Rule of three + distillation + callback
- beat: resolve
- blueprint: titlecard-reveal (Adapt)
- focal: 三个风险阈值与最终“验证市”大字
- roles: AI/10Y/原油三条件 = foreground evidence · 验证市 = foreground payoff · 问题回调轨迹 = supporting · 橙色终场平面 = background payoff
- sfx: none

Adapt: 保留多张近静止信息卡压缩到最终落版的签名动作；结尾不是品牌logo，而是全片核心结论。

Scene 1 (0.0–3.0s): 黑底上三条条件纵向出现：“AI 不走弱 / 10Y 不破风险阈值 / 原油不过热”；每条只用一次per-word stagger（`dynamic-content-sequencing`），前一条保持低透明度。

Scene 2 (3.0–5.6s): 三条条件向中央压缩成一条细规则线，Frame 1的分叉轨迹以低透明度回归；“趋势市”在轨迹左侧出现后被一条橙色竖线切断。

Scene 3 (5.6–9.0s): 画面切换为fire-orange满屏register，“验证市”以最大display占据上半区，“接下来比的不是谁涨得快，而是谁能活下来”分两行落在其下；7.2秒后完全静止，底部保留“示意样片 / 非实时数据”。

narrativeRole: 回收前面的风险条件，并把复杂判断压缩成一个可记忆的市场阶段名称。

keyMessage: AI主线仍在，但后续行情依赖数据与个股确认，已经从趋势交易进入验证交易。
