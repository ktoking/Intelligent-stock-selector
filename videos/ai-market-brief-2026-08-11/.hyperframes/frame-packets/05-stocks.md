# Frame packet: 05-stocks

## Project inputs

- Project: /Users/kaiyi.wang/PycharmProjects/stock-agent/videos/ai-market-brief-silent
- Design tokens: /Users/kaiyi.wang/PycharmProjects/stock-agent/videos/ai-market-brief-silent/frame.md
- RULES_DIR: /Users/kaiyi.wang/PycharmProjects/stock-agent/.agents/skills/hyperframes-animation/rules

## Assigned storyboard block

## Frame 5 — 个股进入分化

- status: outline
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
- asset_candidates: []
- sfx: none

Adapt: 保留列表逐项自组装并最终形成完整分类的签名动作；使用纵向四层阶梯适配竖屏，不做六宫格卡片。

Scene 1 (0.0–2.1s): 标题“不是齐涨，是分化”占据顶部，四条细横线从上到下依次绘制成阶梯，只显示状态词不显示ticker。

Scene 2 (2.1–5.3s): NVDA落入“震荡消化”，AMD落入“等待确认”；每个ticker以mono标签滑入对应层，状态标题保持大字层级，不出现实时价格。

Scene 3 (5.3–8.0s): AVGO与NET依次进入“趋势回踩”，标签沿同一方向排列，形成同类对比而非两张独立卡。

Scene 4 (8.0–11.0s): SMCI与AMAT进入“财报再定价”；四层全景在9.0秒完成，橙色强调词切换为“不追高”，其余元素全部停住供阅读。

narrativeRole: 让观众看见同一AI主线内部已经出现不同交易阶段。

keyMessage: 个股不再同步上涨，策略应从追龙头转向等待二次确认。
