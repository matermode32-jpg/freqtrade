# ConfluenceStrategy — quant upgrade (branch quant-upgrade, NOT live).
# Merges: Neon port base + spec upgrades (maker pricing, Z-score, Garman-Klass
# stops, Groq veto, Kelly sizing). Live bot on main still runs the pre-quant
# version until this branch is validated + merged.
#
# Design log (review flags resolved here):
#  - GK sqrt clipped (was NaN-prone); Hurst DEFERRED (needs rolling impl);
#  - margin_mode stays null (spot); config is source of truth (no CLI override);
#  - protections live in-strategy (2026.8); enter_tag real JSON, NaN-safe;
#  - Groq key via os.getenv (no config-schema risk); veto fail-open, cached;
#  - Kelly needs custom_stake_amount (formula alone sizes nothing).

import json
import logging
import os
from datetime import datetime

import numpy as np
import pandas as pd
import requests
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, merge_informative_pair

try:
    import talib.abstract as ta
except ImportError:  # pragma: no cover - freqtrade image always has talib
    ta = None

log = logging.getLogger(__name__)

TIME_STOP_CANDLES = 96  # 96 x 5m = 8h (Neon TIMEOUT port)
VETO_PROMPT_VERSION = "v1-quant"
VETO_CACHE_MAX = 500


def _json_snapshot(payload: dict) -> str:
    """JSON-safe snapshot: NaN/inf -> null, compact, rounded floats."""

    def _clean(v):
        if isinstance(v, float):
            if np.isnan(v) or np.isinf(v):
                return None
            return round(v, 4)
        if isinstance(v, dict):
            return {k: _clean(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [_clean(x) for x in v]
        return v

    return json.dumps(_clean(payload), separators=(",", ":"))


def _kalman_state(close: pd.Series) -> pd.Series:
    """1-D random-walk Kalman level on normalized closes (NaN-safe, O(n))."""
    c = close.astype(float)
    first = c.iloc[0] if len(c) else np.nan
    base = first if np.isfinite(first) and first != 0 else 1.0
    y = (c / base).ffill().fillna(1.0).values
    n = len(y)
    est = np.empty(n)
    est[0] = y[0]
    p, q = 1.0, 1e-6
    d = np.diff(y)
    finite = d[np.isfinite(d)] if n > 2 else np.array([])
    r = float(np.var(finite)) if len(finite) else 1e-6
    if not np.isfinite(r) or r <= 0:
        r = 1e-6
    for i in range(1, n):
        p = p + q
        k = p / (p + r)
        est[i] = est[i - 1] + k * (y[i] - est[i - 1])
        p = (1 - k) * p
    return pd.Series(est, index=close.index)


def _rolling_hurst(close: pd.Series, window: int = 100, max_lag: int = 20) -> pd.Series:
    """Rolling Hurst exponent (analysis only, zero weight until proven).
    H > 0.55 trending, 0.45-0.55 noise. O(n*window) python loop: fine for
    live/backtest cadence, expensive for hyperopt grids (don't grid it)."""
    vals = close.astype(float).values
    n = len(vals)
    out = np.full(n, np.nan)
    lags = np.arange(2, max_lag)
    log_lags = np.log(lags)
    for i in range(window, n):
        seg = vals[i - window:i]
        if not np.all(np.isfinite(seg)):
            continue
        tau = []
        ok = True
        for lag in lags:
            s = np.std(seg[lag:] - seg[:-lag])
            if not np.isfinite(s) or s <= 0:
                ok = False
                break
            tau.append(np.sqrt(s))
        if not ok:
            continue
        slope = np.polyfit(log_lags, np.log(tau), 1)[0]
        out[i] = slope * 2.0
    return pd.Series(out, index=close.index)


def _append_ledger(path: str, row: dict, max_bytes: int) -> None:
    """Append-only signal ledger (ignored by git). Rotates once past cap."""
    import csv
    import os as _os
    try:
        d = _os.path.dirname(path)
        if d:
            _os.makedirs(d, exist_ok=True)
        if _os.path.exists(path) and _os.path.getsize(path) > max_bytes:
            _os.replace(path, path + ".bak")
        new = not _os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        log.debug("ledger skip: %s", e)


class ConfluenceStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = False
    minimal_roi = {"60": 0.01, "30": 0.02, "0": 0.04}
    stoploss = -0.10
    startup_candle_count = 300
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    # ---- quant knobs (tune gate owns these numbers) ----
    confluence_min_score = 55
    z_low, z_high = -2.0, 1.5
    gk_stop_mult = 2.0
    # Kelly: calibrated from 30d Binance behavior test (31 trades, p=0.774,
    # b=0.450, PF 1.54). Recompute after each validation window.
    kelly_p = 0.774
    kelly_b = 0.450
    kelly_lambda = 0.25
    kelly_min_stake = 20.0
    kelly_max_stake = 300.0
    # GPT OSS 120B via Groq is slower than 8b-instant: 2.5s budget. Signals are
    # rare (~1/day live) so the loop-block is harmless; fail-open still guards.
    veto_timeout_s = 2.5
    veto_model_default = "openai/gpt-oss-120b"
    ledger_path = "user_data/logs/signal_ledger.csv"
    ledger_max_bytes = 2_000_000
    hurst_window = 100
    hurst_max_lag = 20

    protections = [
        {
            "method": "StoplossGuard",
            "lookback_period_candles": 24,
            "trade_limit": 3,
            "stop_duration_candles": 12,
            "only_per_pair": False,
        },
        {
            "method": "MaxDrawdown",
            "lookback_period_candles": 288,  # 288 x 5m = 24h
            "trade_limit": 10,
            "stop_duration_candles": 48,
            "max_allowed_drawdown": 0.03,
        },
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._veto_cache: dict = {}

    # ---- 4h trend bias ----

    def informative_pairs(self):
        pairs = self.dp.current_whitelist()
        return [(pair, tf) for pair in pairs for tf in ("4h", "1h", "15m")]

    # ---- indicators + structure + quant metrics ----

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        if ta is None:
            raise RuntimeError("talib missing in this image")

        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        macd = ta.MACD(dataframe)
        dataframe["macd_hist"] = macd["macdhist"]
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["vol_avg20"] = dataframe["volume"].rolling(20).mean()
        # 5m micro trigger parts (MTF matrix row 3)
        cross = ((dataframe["ema9"] > dataframe["ema21"])
                 & (dataframe["ema9"].shift(1) <= dataframe["ema21"].shift(1)))
        dataframe["ema_cross_up"] = cross.fillna(False).astype(bool)
        dataframe["vol_spike15"] = (
            dataframe["volume"] > 1.5 * dataframe["vol_avg20"]).fillna(False).astype(bool)

        # Z-Score volatility normalization (gate: z_low < z < z_high for longs)
        sma_20 = ta.SMA(dataframe["close"], timeperiod=20)
        std_20 = dataframe["close"].rolling(20).std()
        dataframe["z_score"] = (dataframe["close"] - sma_20) / std_20.replace(0, np.nan)

        # Garman-Klass volatility (clipped: raw c1-c2 can go negative -> NaN)
        log_hl = np.log(dataframe["high"] / dataframe["low"].replace(0, np.nan))
        log_co = np.log(dataframe["close"] / dataframe["open"].replace(0, np.nan))
        gk_var = 0.5 * (log_hl ** 2) - (2 * np.log(2) - 1) * (log_co ** 2)
        dataframe["garman_klass"] = np.sqrt(np.maximum(gk_var.fillna(0), 0))

        # Rolling Hurst (analysis only: computed, tagged, never scored).
        dataframe["hurst_100"] = _rolling_hurst(
            dataframe["close"],
            window=int(getattr(self, "hurst_window", 100)),
            max_lag=int(getattr(self, "hurst_max_lag", 20)))

        # --- structure: swings / order blocks / FVG (closed candles only) ---
        lookback = 5
        highs = dataframe["high"]
        lows = dataframe["low"]
        swing_high = pd.Series(index=dataframe.index, dtype=float)
        swing_low = pd.Series(index=dataframe.index, dtype=float)
        for i in range(lookback, len(dataframe) - lookback):
            w = slice(i - lookback, i + lookback + 1)
            if highs.iloc[i] == highs.iloc[w].max():
                swing_high.iloc[i] = highs.iloc[i]
            if lows.iloc[i] == lows.iloc[w].min():
                swing_low.iloc[i] = lows.iloc[i]
        dataframe["swing_high"] = swing_high.ffill()
        dataframe["swing_low"] = swing_low.ffill()

        atr_roll = dataframe["atr"]
        body_bull = dataframe["close"] > dataframe["open"]
        body_bear = dataframe["close"] < dataframe["open"]
        rng = highs - lows
        impulse = rng > 1.5 * atr_roll
        prev_bear = body_bear.shift(1).fillna(False)
        prev_bull = body_bull.shift(1).fillna(False)
        dataframe["bull_ob"] = (impulse & body_bull & prev_bear).astype(bool)
        dataframe["bear_ob"] = (impulse & body_bear & prev_bull).astype(bool)

        fvg_bull_raw = dataframe["low"] > highs.shift(2)
        fvg_bear_raw = dataframe["high"] < lows.shift(2)
        gap_bot_bull = highs.shift(2)
        gap_top_bear = lows.shift(2)
        filled_bull = pd.Series(False, index=dataframe.index)
        filled_bear = pd.Series(False, index=dataframe.index)
        for i in dataframe.index[fvg_bull_raw.fillna(False)]:
            later = dataframe.loc[i + 1:] if i + 1 in dataframe.index else dataframe.iloc[0:0]
            if len(later) and (later["low"] <= gap_bot_bull.loc[i]).any():
                filled_bull.loc[i] = True
        for i in dataframe.index[fvg_bear_raw.fillna(False)]:
            later = dataframe.loc[i + 1:] if i + 1 in dataframe.index else dataframe.iloc[0:0]
            if len(later) and (later["high"] >= gap_top_bear.loc[i]).any():
                filled_bear.loc[i] = True
        dataframe["bull_fvg"] = (fvg_bull_raw.fillna(False) & ~filled_bull).astype(bool)
        dataframe["bear_fvg"] = (fvg_bear_raw.fillna(False) & ~filled_bear).astype(bool)

        # --- 4h informative bias merge ---
        dataframe["trend_up"] = False
        if self.dp is not None:
            informative_4h = self.dp.get_pair_dataframe(
                pair=metadata["pair"], timeframe="4h")
            if informative_4h is not None and not informative_4h.empty:
                # plain names: merge_informative_pair appends _4h itself
                informative_4h["ema50"] = ta.EMA(informative_4h, timeperiod=50)
                informative_4h["ema200"] = ta.EMA(informative_4h, timeperiod=200)
                dataframe = merge_informative_pair(
                    dataframe, informative_4h, self.timeframe, "4h", ffill=True)
                spread_ok = (
                    (dataframe["ema50_4h"] - dataframe["ema200_4h"]).abs()
                    / dataframe["close"] * 100 >= 0.3
                )
                ema_up = dataframe["ema50_4h"] > dataframe["ema200_4h"]
                price_ok = dataframe["close_4h"] > dataframe["ema50_4h"]
                dataframe["trend_up"] = (
                    spread_ok & ema_up & price_ok).fillna(False).astype(bool)

        # --- 1h macro regime: Kalman slope > 0 and ADX > 20 (MTF row 1) ---
        # Caveat: 1h EMA200 needs 200h history; Kraken caps ~192 1h bars, so
        # early-window values are warming. Kalman+ADX carry the gate meanwhile.
        dataframe["h1_regime"] = False
        if self.dp is not None:
            informative_1h = self.dp.get_pair_dataframe(
                pair=metadata["pair"], timeframe="1h")
            if informative_1h is not None and not informative_1h.empty:
                informative_1h["ema200"] = ta.EMA(informative_1h, timeperiod=200)
                informative_1h["adx"] = ta.ADX(informative_1h, timeperiod=14)
                informative_1h["kstate"] = _kalman_state(informative_1h["close"])
                dataframe = merge_informative_pair(
                    dataframe, informative_1h, self.timeframe, "1h", ffill=True)
                slope_up = dataframe["kstate_1h"] > dataframe["kstate_1h"].shift(1)
                dataframe["h1_regime"] = (
                    slope_up & (dataframe["adx_1h"] > 20)).fillna(False).astype(bool)

        # --- 15m structure: above swing low + RSI 45-65 (MTF row 2) ---
        dataframe["m15_setup"] = False
        if self.dp is not None:
            informative_15m = self.dp.get_pair_dataframe(
                pair=metadata["pair"], timeframe="15m")
            if informative_15m is not None and not informative_15m.empty:
                informative_15m["rsi"] = ta.RSI(informative_15m, timeperiod=14)
                lh = informative_15m["high"]
                ll = informative_15m["low"]
                sw = pd.Series(index=informative_15m.index, dtype=float)
                for i in range(5, len(informative_15m) - 5):
                    w = slice(i - 5, i + 5 + 1)
                    if ll.iloc[i] == ll.iloc[w].min():
                        sw.iloc[i] = ll.iloc[i]
                informative_15m["swing_low"] = sw.ffill()
                dataframe = merge_informative_pair(
                    dataframe, informative_15m, self.timeframe, "15m", ffill=True)
                dataframe["m15_setup"] = (
                    (dataframe["close"] > dataframe["swing_low_15m"])
                    & dataframe["rsi_15m"].between(45, 65)).fillna(False).astype(bool)

        return dataframe

    # ---- scored entries + Z-gate ----

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""

        # Validated scoring (30d: +0.40%, 77% win). MTF columns (h1_regime,
        # m15_setup, ema_cross_up) stay computed + available for analysis but
        # carry NO weight: wiring them into the score LOST money (-0.25% @55,
        # 2-trade noise @65). Re-promote only with walk-forward proof.
        w_trend, w_rsi, w_vol, w_struct = 30, 25, 15, 30
        c_trend = (dataframe["trend_up"] == True).fillna(False)  # noqa: E712
        c_rsi = (dataframe["rsi"] < 30).fillna(False)
        c_vol = (dataframe["volume"] > dataframe["vol_avg20"]).fillna(False)
        c_struct = (dataframe["bull_ob"] | dataframe["bull_fvg"]).fillna(False)

        score = (c_trend.astype(int) * w_trend + c_rsi.astype(int) * w_rsi
                 + c_vol.astype(int) * w_vol + c_struct.astype(int) * w_struct)
        dataframe["confluence_score"] = score
        z = dataframe["z_score"]
        z_ok = ((z > self.z_low) & (z < self.z_high)).fillna(False)
        long_mask = (score >= int(getattr(self, "confluence_min_score", 55))) & z_ok

        dataframe.loc[long_mask, "enter_long"] = 1
        for idx in dataframe.index[long_mask]:
            row = dataframe.loc[idx]
            conds = []
            if bool(c_trend.loc[idx]):
                conds.append("trend")
            if bool(c_rsi.loc[idx]):
                conds.append("rsi")
            if bool(c_vol.loc[idx]):
                conds.append("volume")
            if bool(c_struct.loc[idx]):
                conds.append("structure")
            dataframe.at[idx, "enter_tag"] = _json_snapshot({
                "score": int(score.loc[idx]),
                "z": row.get("z_score"),
                "gk": row.get("garman_klass"),
                "h": row.get("hurst_100"),
                "conds": conds,
            })[:200]
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["exit_long"] = 0
        dataframe.loc[dataframe["rsi"] > 70, "exit_long"] = 1
        return dataframe

    # ---- maker execution callbacks (live/dry only; no-op in backtests) ----

    def custom_entry_price(self, pair: str, current_time: datetime, proposed_rate: float,
                           entry_tag: str, side: str, **kwargs) -> float:
        try:
            ob = self.dp.market_to_orderbook(pair, 1) if self.dp else None
            if ob and ob.get("bids") and ob.get("asks"):
                bid, ask = ob["bids"][0][0], ob["asks"][0][0]
                if (ask - bid) / bid > 0.0008:
                    return proposed_rate  # wide spread: fall back, post_only guards
                return bid + (ask - bid) * 0.10
        except Exception as e:
            log.debug("custom_entry_price fallback: %s", e)
        return proposed_rate

    def adjust_entry_price(self, trade: Trade, order: dict, pair: str, current_time: datetime,
                           proposed_rate: float, current_order_rate: float, entry_tag: str,
                           side: str, **kwargs) -> float:
        try:
            if (current_time - trade.open_date_utc).total_seconds() > 60:
                ob = self.dp.market_to_orderbook(pair, 1) if self.dp else None
                if ob and ob.get("bids"):
                    return ob["bids"][0][0]
        except Exception as e:
            log.debug("adjust_entry_price fallback: %s", e)
        return current_order_rate

    def custom_exit_price(self, pair: str, trade: Trade, current_time: datetime,
                          proposed_rate: float, entry_tag: str, side: str,
                          **kwargs) -> float:
        try:
            ob = self.dp.market_to_orderbook(pair, 1) if self.dp else None
            if ob and ob.get("asks") and ob.get("bids"):
                bid, ask = ob["bids"][0][0], ob["asks"][0][0]
                return ask - (ask - bid) * 0.10
        except Exception as e:
            log.debug("custom_exit_price fallback: %s", e)
        return proposed_rate

    # ---- Kelly sizing (fractional, calibrated, clamped) ----

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                            proposed_stake: float, min_stake: float | None,
                            max_stake: float, leverage: float, entry_tag: str | None,
                            side: str, **kwargs) -> float:
        try:
            p = float(self.kelly_p)
            b = float(self.kelly_b) or 1.0
            lam = float(self.kelly_lambda)
            f_star = lam * (p - (1 - p) / b)
            if f_star <= 0:
                return proposed_stake
            try:
                wallet = float(self.wallets.get_available_stake_amount())
            except Exception:
                wallet = 0.0
            if wallet <= 0:
                return proposed_stake
            stake = wallet * f_star
            lo = max(float(self.kelly_min_stake), float(min_stake or 0))
            return max(lo, min(stake, float(self.kelly_max_stake), float(max_stake or stake)))
        except Exception as e:
            log.debug("custom_stake_amount fallback: %s", e)
            return proposed_stake

    # ---- Groq veto (fail-open, cached, versioned) ----

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                            time_in_force: str, current_time: datetime, entry_tag: str | None,
                            side: str, **kwargs) -> bool:
        import time as _time

        def _ledger(verdict: bool, why: str, latency_ms: float) -> None:
            _append_ledger(
                getattr(self, "ledger_path", "user_data/logs/signal_ledger.csv"),
                {"time_utc": current_time.isoformat() if hasattr(current_time, "isoformat")
                 else str(current_time),
                 "pair": pair, "rate": rate, "amount": amount, "tag": entry_tag or "",
                 "verdict": "approve" if verdict else "veto", "why": why,
                 "model": os.getenv("GROQ_MODEL", getattr(
                     self, "veto_model_default", "openai/gpt-oss-120b")),
                 "prompt_version": VETO_PROMPT_VERSION,
                 "latency_ms": round(latency_ms, 1)},
                int(getattr(self, "ledger_max_bytes", 2_000_000)))

        t0 = _time.monotonic()
        try:
            key = os.getenv("GROQ_API_KEY", "")
            if not key:
                _ledger(True, "no_key", 0.0)
                return True  # no key configured (local/backtest): approve silently
            model = os.getenv("GROQ_MODEL", getattr(
                self, "veto_model_default", "openai/gpt-oss-120b"))
            try:
                signal_data = json.loads(entry_tag) if entry_tag else {}
            except Exception:
                signal_data = {}
            cache_key = (pair, json.dumps(signal_data, sort_keys=True, default=str))
            if cache_key in self._veto_cache:
                _ledger(self._veto_cache[cache_key], "cached", 0.0)
                return self._veto_cache[cache_key]
            prompt_content = {
                "prompt_version": VETO_PROMPT_VERSION,
                "pair": pair,
                "confluence_score": signal_data.get("score", 0),
                "z_score": signal_data.get("z", 0.0),
                "gk_volatility": signal_data.get("gk", 0.0),
            }
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system",
                         "content": "You are a quant trade validator. Respond ONLY "
                                    "with single character '1' (approve) or '0' (veto)."},
                        {"role": "user", "content": json.dumps(prompt_content)},
                    ],
                    "max_tokens": 1,
                    "temperature": 0.0,
                },
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                timeout=float(self.veto_timeout_s),
            )
            verdict = True
            if r.status_code == 200:
                out = (r.json()["choices"][0]["message"]["content"] or "").strip()
                verdict = not out.startswith("0")
            if len(self._veto_cache) >= VETO_CACHE_MAX:
                self._veto_cache.clear()
            self._veto_cache[cache_key] = verdict
            _ledger(verdict, "groq:%d" % r.status_code,
                    (_time.monotonic() - t0) * 1000)
            return verdict
        except Exception as e:
            log.debug("veto fail-open (%s) [%s]", e, VETO_PROMPT_VERSION)
            _ledger(True, "fail_open", (_time.monotonic() - t0) * 1000)
            return True

    # ---- Garman-Klass stop frozen at entry ----

    def custom_stoploss(self, pair: str, trade, current_time,
                        current_rate: float, current_profit: float, **kwargs) -> float:
        try:
            gk = None
            try:
                tag = json.loads(trade.enter_tag) if getattr(trade, "enter_tag", None) else {}
                gk = float(tag.get("gk")) if tag.get("gk") is not None else None
            except Exception:
                gk = None
            if gk and gk > 0:
                stop_dist = float(self.gk_stop_mult) * gk
                return max(-0.25, min(-0.005, -stop_dist))
        except Exception as e:
            log.debug("custom_stoploss fallback: %s", e)
        return self.stoploss

    # ---- time stop (Neon TIMEOUT 96 port) ----

    def custom_exit(self, pair: str, trade, current_time,
                    current_rate: float, current_profit: float, **kwargs):
        try:
            held_s = (current_time - trade.open_date_utc).total_seconds()
            if held_s >= TIME_STOP_CANDLES * 5 * 60:
                return "timeout_96"
        except Exception as e:
            log.debug("custom_exit fallback: %s", e)
        return None
