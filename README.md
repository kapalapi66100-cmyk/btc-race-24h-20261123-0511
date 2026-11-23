# btc-race-24h-20261123-0511

[![make.fun](https://make.fun/badge/btc-race-24h-20261123-0511.svg)](https://make.fun/competitions/btc-race-24h-20261123-0511)
[![rank](https://make.fun/badge/btc-race-24h-20261123-0511/kapalapi66100-cmyk/btc-race-24h-20261123-0511/rank.svg)](https://make.fun/competitions/btc-race-24h-20261123-0511)
[![reward](https://make.fun/badge/btc-race-24h-20261123-0511/kapalapi66100-cmyk/btc-race-24h-20261123-0511/reward.svg)](https://make.fun/competitions/btc-race-24h-20261123-0511)

A BTC trading bot for the [make.fun competition btc-race-24h-20261123-0511](https://make.fun/competitions/btc-race-24h-20261123-0511): race a BTC trading bot live for 24 hours, $50 prize pool, paper money only (nothing traded for real).

## Strategy in one sentence

Core 40% since the first tradable minute + EMA30/120 trend overlay to 90%, with layered stops and a 6%/9% equity drawdown kill-switch.

## How it works

- **Core 40% from minute one:** buys 40% of equity unconditionally on the first batch that can trade (orders decided during warm-up are rejected, so it waits for `state.warming_up` to clear). This guarantees ≥ 1 trade by construction — the failure mode of the 18 Nov entry (EMA60/240 cross + 4h momentum never coinciding → 0 trades → dev PnL 0 → failed dev gate) cannot recur. If the first buy somehow never fills, it is retried at 50 bps slippage after the 30-bar cooldown.
- **Trend overlay to 90%:** upgrades CORE → FULL only when the EMA30/EMA120 spread ≥ +8 bps **and** the 60-bar return ≥ +26 bps **and** close > EMA30 **and** 60-bar mean |1-min return| ≥ 1.5 bps, after at least 120 bars of history.
- **Downgrade FULL → CORE:** spread ≤ −5 bps (after a 45-bar minimum hold) or a trailing stop of max(100 bps, 4 × vol60) below the peak since the upgrade.
- **CORE → FLAT:** hard stop 250 bps below the core entry close, or close below the 240-bar low while spread ≤ −15 bps.
- **Kill-switch:** equity drawdown from peak ≥ 6% → flat + 240-bar re-entry lock; ≥ 9% → flat permanently. Realistic max drawdown ≈ 9–10%, far under the 25% guardrail.
- **Activity cap:** at most 12 orders per 24h (exits exempt). One order per state change, market, max slippage 30 bps.

## Files

- `main.py` — the bot (Python 3.13, stdlib only; speaks agent-fun/bars-replay/v1 via the vendored SDK shim)
- `vendor/makefun_bars.py` — byte-identical copy of the kit's Python SDK shim

## Local grade

```sh
node <kit>/cli/makefun.mjs grade-local --kit <kit> --bot .
```

Deterministic: two runs on the same data produce byte-identical action logs (no clock, no randomness, no network, no files outside the repo).
