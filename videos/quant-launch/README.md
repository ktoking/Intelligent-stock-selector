# Quant — 60秒产品宣传片

1920×1080 横屏，中文字幕、原创120 BPM配乐，无旁白、六组代码动效。项目独立于量化系统。

## 叙事

1. 0–7秒：选股、回测、复盘，交给一个AI工作台。
2. 7–18秒：动态研究池、市场异动和资讯。
3. 18–29秒：假设 → 因子 → 回测 → 核账 → 筛选。
4. 29–40秒：真实历史曲线、成本回撤延迟、历史买卖点提示。
5. 40–51秒：先未来观察，再模拟；复盘产生新版本；实盘须人工授权。
6. 51–60秒：完整看板聚焦与Quant收尾。

## 查看

`npm run check` 执行静态、运行、布局、动效和对比度检查。

`npx hyperframes@0.8.149 preview --background --port 8792` 启动本地Studio。

本次预览：`http://localhost:8792/#project/quant-launch`。

用户已明确要求去掉语音并生成视频。最终文件：`renders/quant-promo-captioned.mp4`；独立字幕：`renders/quant-promo.zh-CN.srt`。

## 事实与素材

`evidence.json` 保留采集基准及公开能力统计。所有截图来自实际本地页面，冻结历史曲线来自只读接口。
目前模拟成交0，3个策略处于未来观察；不展示虚构盈利、实盘成交或期权执行。
图中的买卖点是回测假设成交，不代表实际账户交易。预测区域只存在于原页面截图。

首次复盘页面捕获为空白，未使用；采用成功的capture-review-v2。

语音：无。字幕根据画面节奏独立排布，`captions/captions-meta.json` 为时间基准；旧配音及试听素材不进入成片。
音乐：`scripts/build_audio.py` 原创合成，无第三方音乐录音或采样。
配音尝试已停止。此前云配音返回资源未授权，旧本地配音存在发音转换错误；成片只包含原创音乐。

成片已核验：60.000秒、1920×1080、60fps、3600帧，16,986,788字节。H.264视频＋AAC双声道音乐；成片音频与原创音乐的相关系数为0.999844，没有旁白轨。`renders/verification.json` 与 `renders/delivery-contact-sheet.jpg` 保留实际成片核验结果。

`check-final.json` 记录最后一次检查。唯一提示为15条字幕集中在同一字幕轨道，属有意安排。
GSAP与字体子集均本地保存；预览播放不调用行情、模型或交易接口。

## 重新制作

在仓库根目录执行：

```bash
.venv/bin/python videos/quant-launch/scripts/build_visuals.py
HYPERFRAMES_NO_TELEMETRY=1 .venv/bin/python videos/quant-launch/scripts/finalize_preview.py
```

本版本无需语音模型。图形输入已经冻结在本项目内；只有重新捕获或重新stage_assets才会读取本地看板。

确认导出后，在本项目目录执行：

```bash
HYPERFRAMES_NO_TELEMETRY=1 npx hyperframes@0.8.149 render --output renders/quant-promo-captioned.mp4 --fps 60 --quality delivery
```

未发布到云端，未改量化业务逻辑、预算或冻结策略，未提交或推送Git。
