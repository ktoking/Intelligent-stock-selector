# V3 runner research, 2026-09-23

Status: exploratory hourly proxy; Futu client backtest not run.

Artifact: `/Users/kaiyi.wang/Downloads/SOXL_MAIN_WAVE_RUNNER_V3_RESEARCH.quant`.
The working V2 Canvas file remains unchanged. Only its start-card parameters and
risk action were changed; all 98 cards and 98 edges remain in place.

Entry, 80% position, 8% initial stop, entry window and seven-session cooldown are
unchanged. Before a 20% peak gain, the hourly-close trailing threshold is 12%;
afterward it is 16%. The daily trend exit now requires two completed target closes
below their EMA20, or the reference's completed daily close at/below its EMA60.
The hard stop and trailing exit do not wait for daily confirmation. Thresholds
are signals evaluated on completed hourly bars, not guaranteed fill prices.

SOXL hourly proxy, QQQ reference, next-open fills, adjusted Yahoo data, 10 bps per
side, all windows start in cash. Figures are return / maximum drawdown:

| Window | V2 | V3 runner |
| --- | --- | --- |
| 2024-09-25 to 2025-09-19 | +8.079% / 22.056% | +12.378% / 18.956% |
| 2026-01-02 to 2026-06-30 | +49.730% / 16.978% | +43.304% / 23.334% |
| 2026-01-02 to 2026-09-22, continuous | +36.505% / 24.097% | +43.304% / 23.334% |

For the continuous 2026 period, raising costs to 40 bps per side yields +32.595%
for V2 and +39.856% for V3. Full-period trades decrease from 6 to 5. Do not combine
independently reset up/down tests to infer continuous performance: carry positions
can lose money after the up-period boundary even when a cash-start down test has
zero entries.

Cross-symbol continuous 2026 tests with the same QQQ reference and 10 bps per side:
ARMG V2 +251.063%, V3 +229.568%; SNXX V2 +16.779%, V3 +80.730%.
The previously observed large ARMG Futu/proxy discrepancy is unresolved, so these
are not calibrated Futu forecasts. The variant is not universally superior.

An earlier-entry variant (daily EMA10>EMA20 and rising EMA20 over two sessions,
without waiting for target EMA20>EMA60) worsened the prior SOXL window to -16.952%
with 36.363% drawdown, so it was not placed in the artifact. A 20% runner trail
also performed worse than 16% and was rejected. Several alternatives were viewed;
none of these windows is an untouched out-of-sample test of the selected variant.

Validation: Protobuf round-trip; identical graph edges; only start/risk cards
changed; numeric parameter representation; Python syntax; mocked generated action
tests for runner activation, 16% exit, initial hard stop, and one/two daily dips.
Combined targeted tests: 26 passed. No OpenD or Futu UI execution was performed.
