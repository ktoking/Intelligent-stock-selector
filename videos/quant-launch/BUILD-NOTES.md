# Production notes

Built as six independent frame compositions using the inline serial fallback. No native subagents or extra headless Codex runs were launched.
Successful source captures: overview, market, research, review-v2. The blank first review capture was rejected.
Cloud TTS returned 403. Review narration uses local Kokoro ONNX Chinese male voice; public product copy only was sent in the failed request.
Music is an original deterministic 120-BPM electronic composition, with no third-party samples.
No original quant system files or business rules are modified by this video project.

## Caption-only revision

User rejected synthesized narration and explicitly requested the video without speech. All voice tracks have been removed from audio_meta.json and index.html. Fifteen authored caption intervals align with scene visual cues, independent of old TTS timings. Only original-score.mp3 plays. The final MP4 has burned-in Chinese captions; a matching SRT is exported separately. Full static/runtime/layout/contrast checks passed before rendering.
