# SOXL / ARMG / SNXX alternative strategy screen (2026-09-23)

Status: research only; no Futu `.quant` promotion.

The comparison uses Yahoo adjusted regular-session 60-minute bars, completed prior-day
daily conditions, next-hour-open fills, 80% target position, and 10 bps per-side costs.
It is not a Futu 5-minute backtest. SNXX has no prior-year sample.

Each cell below is return % / max drawdown % / completed trades. The prior period is
2024-09-25 to 2025-09-19; the up period is 2026-01-02 to 2026-06-30. ARMG's
prior-period history starts in January 2025. No-trade down-period results are 0% for
these three new, long-only trend filters and do not demonstrate bear-market alpha.

| Trading symbol | Rule | Prior | 2026 up |
| --- | --- | --- | --- |
| SOXL | V2 strict baseline (separate proxy engine) | +8.08 / 22.06 / 4 | +49.73 / 16.98 / 6 |
| SOXL | Hourly EMA reclaim | -17.48 / 17.94 / 11 | -10.70 / 31.38 / 11 |
| SOXL | Six-hour channel breakout | -10.40 / 24.10 / 13 | +34.75 / 23.64 / 12 |
| SOXL | Strict trend hold | -9.50 / 34.74 / 6 | +58.52 / 34.03 / 11 |
| ARMG | Hourly EMA reclaim | -1.55 / 18.08 / 4 | +73.23 / 23.47 / 6 |
| ARMG | Six-hour channel breakout | +5.00 / 14.88 / 6 | +99.41 / 29.43 / 12 |
| ARMG | Strict trend hold | -2.38 / 25.37 / 3 | +147.22 / 32.95 / 9 |
| SNXX | Hourly EMA reclaim | no data | +20.66 / 28.41 / 9 |
| SNXX | Six-hour channel breakout | no data | +24.15 / 28.58 / 11 |
| SNXX | Strict trend hold | no data | +62.45 / 30.24 / 9 |

Rules were defined before comparing these windows, but they have not been validated
on an untouched Futu sample. Hourly EMA reclaim buys after a completed hourly close
reclaims EMA10 inside a rising daily regime; channel breakout buys above the prior
six hourly highs; strict trend hold uses daily/short-hourly confirmation with a 12%
hourly-close peak trail. All sell on a regime break and use next-hour-open fills.

The separate pre-existing regime-switch research produced +72.85% over its full
two-year hourly proxy, but 28.17% drawdown, 13.71% win rate and one trade's profit
larger than total net profit. Its five walk-forward validation windows were
+1.43%, -0.13%, -2.72%, +36.69%, and 0%; it failed its own acceptance gate.

The local V2 proxy is not calibrated to Futu: for ARMG with QQQ reference over
2026-01-01 through 2026-09-22 it reported +251.06% with 3 completed trades,
whereas the user's Futu screenshot showed +50.25% and 6 orders. Therefore the
proxy is useful to reject obviously weak candidates, not to certify the best one.
No tested new rule met the cross-period / drawdown evidence bar for a final `.quant`.
