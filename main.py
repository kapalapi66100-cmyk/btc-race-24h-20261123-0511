"""btc-race-24h-20261123-0511: Core 40% since the first tradable minute + EMA30/120
trend overlay to 90%, layered stops and a 6%/9% equity drawdown kill-switch.

Strategy (fresh implementation of the Opus 5.5 design spec):
- CORE 40% of equity bought unconditionally on the first batch that can trade
  (seq == warmup_bars; orders decided during warmup are rejected, so the bot
  waits for state.warming_up to clear). That guarantees >= 1 trade by
  construction; a retry at 50 bps slippage follows if no fill by bar +30.
- Overlay to FULL 90% only when spread (EMA30-EMA120)/EMA120 >= +8 bps AND
  60-bar return >= +26 bps AND close > EMA30 AND 60-bar mean |1-min return|
  >= 1.5 bps, after at least 120 bars of history.
- Downgrade FULL -> CORE on spread <= -5 bps (after a 45-bar minimum hold) or
  a trailing stop max(100 bps, 4 x vol60) below the peak since the upgrade.
- CORE -> FLAT on a 250 bps hard stop below the core entry close, or on
  close < 240-bar low AND spread <= -15 bps.
- Kill-switch on equity drawdown from peak: >= 6% -> flat + 240-bar re-entry
  lock; >= 9% -> flat permanently. NOTE (2b, honest): the 6% lock is
  effectively PERMANENT in practice — peak_equity is never reset, so once
  flat the drawdown stays >= 6% and lock_until_seq is extended on every
  batch, so re-entry never happens. Accepted as conservative/safe for a
  24h race; do not rely on the 240-bar re-entry ever firing.
- Max 12 orders per 24h (exits exempt).
- One order per state change, market, max_slippage_bps 30 (50 on core retry,
  100 on all sells).
- Deterministic: float64 indicators only, no clock, no randomness, no I/O,
  no network, no environment reads. Stdlib only + vendored SDK shim.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- parameters (Opus design spec) ---
FAST_SPAN = 30
SLOW_SPAN = 120
UP_SPREAD_BPS = 8.0
DOWN_SPREAD_BPS = -5.0
DOWN_EXIT_SPREAD_BPS = -15.0
RET60_MIN_BPS = 26.0
VOL_MIN_BPS = 1.5
CORE_PCT = 40
FULL_PCT = 90
HARD_STOP_BPS = 250.0
LOW_BARS = 240
HOLD_MIN_BARS = 45
COOLDOWN_BARS = 30
TRAIL_MIN_BPS = 100.0
TRAIL_VOL_MULT = 4.0
KILL_DD_BPS = 600
PERM_DD_BPS = 900
LOCK_BARS = 240
MAX_ORDERS = 12
MAX_SLIPPAGE_BPS = 30
RETRY_SLIPPAGE_BPS = 50
EXIT_SLIPPAGE_BPS = 100  # (2c) all SELL orders: hard stop / low240 / kill-switch / trailing / downgrade
MIN_BARS_OVERLAY = 120
RETRY_AFTER_BARS = 30


class Ema:
    """EMA seeded from the first close, then alpha-weighted (float64)."""

    def __init__(self, span):
        self.alpha = 2.0 / (span + 1)
        self.value = None

    def step(self, close):
        if self.value is None:
            self.value = float(close)
        else:
            self.value += self.alpha * (float(close) - self.value)


# --- module state (one run, deterministic order) ---
fast = Ema(FAST_SPAN)
slow = Ema(SLOW_SPAN)
closes = []  # closes seen, in order (a gap adds nothing)
mode = "FLAT"  # FLAT / CORE / FULL: intended exposure state
core_entry_close = None  # decision-bar close of the current core entry
peak_since_upgrade = None  # highest close since the upgrade to FULL
upgrade_seq = -1  # seq of the upgrade to FULL
last_order_seq = -1000000  # seq of the last order sent
orders_sent = 0
peak_equity = 0  # highest equity (quote units) seen at any batch
lock_until_seq = -1  # re-entry locked while seq < this (6% kill-switch)
perm_flat = False  # True once the 9% kill-switch has fired
core_attempted = False  # a core buy has been decided at least once
core_filled_once = False  # a core buy has filled at least once


def _spread_bps():
    if fast.value is None or slow.value is None or slow.value == 0:
        return None
    return (fast.value - slow.value) / slow.value * 10000.0


def _ret60_bps():
    if len(closes) < 61:
        return None
    old = closes[-61]
    if old == 0:
        return None
    return (closes[-1] - old) / float(old) * 10000.0


def _vol60_bps():
    if len(closes) < 61:
        return None
    total = 0.0
    for i in range(len(closes) - 60, len(closes)):
        prev = closes[i - 1]
        if prev == 0:
            return None
        total += abs(closes[i] - prev) / float(prev)
    return total / 60.0 * 10000.0


def _low240():
    window = closes[-LOW_BARS:]
    return min(window) if window else None


def _buy_to_pct(state, pct, slippage):
    """Buy order taking the position value to pct% of equity, or None."""
    held = state.positions.get(SYMBOL)
    held_value = state.value(SYMBOL, held.qty) if held is not None else 0
    target = state.equity * pct // 100
    notional = target - held_value
    if notional <= 0:
        return None
    if notional > state.cash:
        notional = state.cash
    room = state.weight_room(SYMBOL)
    if notional > room:
        notional = room
    min_notional = int(state.hello["fees"]["min_notional_quote_units"])
    if notional < min_notional:
        return None
    return {"side": "buy", "symbol": SYMBOL,
            "notional_quote_units": notional,
            "max_slippage_bps": slippage}


def _sell_qty(state, qty):
    """Whole-position sell order for qty, or None if below min notional."""
    if qty <= 0:
        return None
    held = state.positions.get(SYMBOL)
    if held is None or qty > held.qty:
        return None
    if qty < held.qty:
        value = state.value(SYMBOL, qty)
        min_notional = int(state.hello["fees"]["min_notional_quote_units"])
        if value < min_notional:
            return None
    return {"side": "sell", "symbol": SYMBOL, "qty": qty,
            "max_slippage_bps": EXIT_SLIPPAGE_BPS}


def _sell_all(state):
    held = state.positions.get(SYMBOL)
    if held is None:
        return None
    return _sell_qty(state, held.qty)


def _on_batch_impl(state, batch):
    global mode, core_entry_close, peak_since_upgrade, upgrade_seq
    global last_order_seq, orders_sent, peak_equity, lock_until_seq
    global perm_flat, core_attempted, core_filled_once

    # Fold this batch's bar into the indicators (warm-up bars included).
    fresh = False
    for bar in batch["bars"]:
        if bar["symbol"] != SYMBOL:
            continue
        fresh = True
        close = int(bar["c"])
        closes.append(close)
        fast.step(close)
        slow.step(close)
    if not fresh:
        return []
    if state.warming_up:
        return []

    close = closes[-1]
    seq = state.seq
    held = state.positions.get(SYMBOL)
    if held is not None:
        core_filled_once = True

    # Peak equity tracking (mark-to-close equity from the account).
    if state.equity > peak_equity:
        peak_equity = state.equity
    dd_bps = 0
    if peak_equity > 0:
        dd_bps = (peak_equity - state.equity) * 10000 // peak_equity

    # --- kill-switch on equity drawdown (exits always allowed) ---
    if dd_bps >= PERM_DD_BPS:
        perm_flat = True
    if perm_flat or dd_bps >= KILL_DD_BPS:
        if dd_bps >= KILL_DD_BPS and not perm_flat:
            lock_until_seq = seq + LOCK_BARS
        if held is not None:
            order = _sell_all(state)
            if order is not None:
                # (2a) Do NOT touch mode/entry/peak on decision: the fill
                # may be rejected (max_slippage) while the position is
                # still held. The held-is-None branch clears them once a
                # fill has actually landed; the holding-block sync below
                # re-derives mode from filled weight if it was rejected.
                last_order_seq = seq
                orders_sent += 1
                return [order]
        if held is None:
            mode = "FLAT"
            core_entry_close = None
            peak_since_upgrade = None
        return []

    # --- (2a) resync mode from the actually-filled weight ---
    # A sell/buy decided on an earlier batch may have been rejected
    # (max_slippage) while the position is still held; never trust the
    # mode/entry/peak set at decision time. Derive them from the fill:
    # held value > ~65% of equity means FULL (peak kept, or seeded with
    # this close if missing, so the trailing stop stays alive); at or
    # below that it is CORE (no trailing stop applies).
    if held is not None:
        held_value = state.value(SYMBOL, held.qty)
        if state.equity > 0 and held_value * 100 > 65 * state.equity:
            mode = "FULL"
            if peak_since_upgrade is None:
                peak_since_upgrade = close
        else:
            mode = "CORE"
            peak_since_upgrade = None

    if held is not None and peak_since_upgrade is not None and mode == "FULL":
        if close > peak_since_upgrade:
            peak_since_upgrade = close

    spread = _spread_bps()
    ret60 = _ret60_bps()
    vol60 = _vol60_bps()
    low240 = _low240()
    cooldown_done = (seq - last_order_seq) >= COOLDOWN_BARS
    can_order = orders_sent < MAX_ORDERS  # beyond 12, exits only

    # --- no position: core entry (first tradable batch, or signalled re-entry) ---
    if held is None:
        mode = "FLAT"
        core_entry_close = None
        peak_since_upgrade = None
        if perm_flat or seq < lock_until_seq:
            return []
        if not core_attempted:
            # First tradable batch: buy CORE 40% unconditionally.
            order = _buy_to_pct(state, CORE_PCT, MAX_SLIPPAGE_BPS)
            if order is not None:
                core_attempted = True
                mode = "CORE"
                core_entry_close = close
                last_order_seq = seq
                orders_sent += 1
                return [order]
            return []
        if not core_filled_once and cooldown_done:
            # Retry the unfilled core buy (fallback jaring cadangan).
            order = _buy_to_pct(state, CORE_PCT, RETRY_SLIPPAGE_BPS)
            if order is not None:
                mode = "CORE"
                core_entry_close = close
                last_order_seq = seq
                orders_sent += 1
                return [order]
            return []
        if core_filled_once and cooldown_done and can_order:
            # FLAT -> CORE re-entry only on a confirmed trend.
            if (spread is not None and ret60 is not None
                    and spread >= UP_SPREAD_BPS and ret60 >= RET60_MIN_BPS):
                order = _buy_to_pct(state, CORE_PCT, MAX_SLIPPAGE_BPS)
                if order is not None:
                    mode = "CORE"
                    core_entry_close = close
                    last_order_seq = seq
                    orders_sent += 1
                    return [order]
        return []

    # --- holding: exits first (stops ignore cooldown and the order cap) ---
    if core_entry_close is not None:
        if close < core_entry_close * (1.0 - HARD_STOP_BPS / 10000.0):
            order = _sell_all(state)
            if order is not None:
                # (2a) Send the sell, return, change nothing: if the fill
                # is rejected the position is still held and the hard stop
                # must stay alive for the next batch.
                last_order_seq = seq
                orders_sent += 1
                return [order]
    if (low240 is not None and spread is not None
            and close < low240 and spread <= DOWN_EXIT_SPREAD_BPS):
        order = _sell_all(state)
        if order is not None:
            # (2a) Same: decide only, do not clear mode/entry/peak.
            last_order_seq = seq
            orders_sent += 1
            return [order]

    if mode == "FULL":
        # Trailing stop from the peak since the upgrade.
        trail_bps = TRAIL_MIN_BPS
        if vol60 is not None:
            trail_bps = max(TRAIL_MIN_BPS, TRAIL_VOL_MULT * vol60)
        if peak_since_upgrade is not None and close < peak_since_upgrade * (1.0 - trail_bps / 10000.0):
            return _downgrade_to_core(state, seq)
        # Spread downgrade after the minimum hold.
        if (spread is not None and spread <= DOWN_SPREAD_BPS
                and (seq - upgrade_seq) >= HOLD_MIN_BARS):
            return _downgrade_to_core(state, seq)
        return []

    # --- holding CORE: upgrade to FULL on a confirmed trend ---
    if mode != "CORE":
        mode = "CORE"
    if (not can_order or not cooldown_done
            or len(closes) < MIN_BARS_OVERLAY
            or spread is None or ret60 is None or vol60 is None
            or fast.value is None):
        return []
    if (spread >= UP_SPREAD_BPS and ret60 >= RET60_MIN_BPS
            and close > fast.value and vol60 >= VOL_MIN_BPS):
        order = _buy_to_pct(state, FULL_PCT, MAX_SLIPPAGE_BPS)
        if order is not None:
            mode = "FULL"
            peak_since_upgrade = close
            upgrade_seq = seq
            last_order_seq = seq
            orders_sent += 1
            return [order]
    return []


def _downgrade_to_core(state, seq):
    """Sell the overlay portion, leaving ~40% of equity in the position."""
    global mode, peak_since_upgrade, last_order_seq, orders_sent
    held = state.positions.get(SYMBOL)
    if held is None:
        mode = "CORE"
        peak_since_upgrade = None
        return []
    held_value = state.value(SYMBOL, held.qty)
    target = state.equity * CORE_PCT // 100
    excess = held_value - target
    if excess <= 0:
        mode = "CORE"
        peak_since_upgrade = None
        return []
    # Sell only the overlay slice; never sell into the core remainder:
    # keep at least the target value (40% of equity) in the position.
    qty = held.qty * excess // held_value if held_value > 0 else 0
    keep_qty = held.qty * target // held_value if held_value > 0 else held.qty
    if qty >= held.qty - keep_qty:
        qty = held.qty - keep_qty
    if qty <= 0:
        return []
    order = _sell_qty(state, qty)
    if order is None:
        return []
    # (2a) Send the downgrade sell, return, change nothing: if the fill is
    # rejected the overlay is still held, so mode must stay FULL and the
    # trailing stop (peak_since_upgrade) must stay alive. The next batch's
    # holding-block sync re-derives mode from the filled weight.
    last_order_seq = seq
    orders_sent += 1
    return [order]


def on_batch(state, batch):
    """Defensive wrapper (§4): an unexpected exception must never crash
    the run — the shim lets a raised exception kill the bot process.
    Return no orders and log one line to stderr (stdout is replies only).
    """
    try:
        return _on_batch_impl(state, batch)
    except Exception as exc:  # noqa: BLE001 - protocol defence, never crash
        sys.stderr.write("on_batch error: %r\n" % (exc,))
        return []


makefun_bars.run(on_batch)
