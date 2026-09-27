"""Public poster service v2: freqtrade webhook -> curated Telegram highlights.
Binance Square: generated + validated, NEVER sent (stub).

Pipeline: fill/close event -> gate -> enrich snapshot -> queue (priority:
entries first) -> worker: chart pre-compute + validate loop (template writer +
deterministic 6-check scorer, LLM swap points marked) -> Telegram photo/text.
Square path stops at 'generated'.

Secrets via env (Render dashboard), never committed:
  CHANNEL_BOT_TOKEN, CHANNEL_ID, PUBLIC_MIN_SCORE=70 (calibrated: top-decile
    value over 31 scored trades; bimodal 70/55 so 70 admits most scorers),
  PUBLIC_MAX_PER_DAY=10, STALENESS_MINUTES=15 (entry clock),
  OUTCOME_BUDGET_S=300, WIN_MIN_PROFIT=0.004 (see R-note below),
  ENABLE_BINANCE_SQUARE=false (must stay false: Square unwired).

R-NOTE (validated contradiction, do not "fix" back to R>=1): with a fixed
-10% stop and an ROI ladder capping wins at +4%, R>=1 (needs +10% moves) is
mathematically unreachable - it would post losses only. Wins gate on
absolute profit_ratio instead; approx-R vs the fixed stop is computed and
displayed for honesty.
"""
import json
import logging
import os
import queue as _queue
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("poster")

DISCLAIMER = ("Paper trading result. Not financial advice. "
              "Past paper performance does not guarantee future results.")
CAPTION_LIMIT = 1000
TELEGRAM_TEXT_LIMIT = 4096
SQUARE_LIMIT = 500
LOG_PATH = os.getenv("POSTER_LOG", "poster_posts.jsonl")
STOP_PCT = 0.10      # strategy fixed catastrophe stop: R denominator
TARGET_PCT = 0.04    # ROI ladder tier-0: snapshot target


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _log_post(record: dict) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        log.warning("poster log skip: %s", e)


def _posted_today() -> int:
    today = _today()
    n = 0
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("status") == "posted" and str(r.get("day", "")) == today:
                    n += 1
    except FileNotFoundError:
        pass
    return n


def _tag(payload: dict) -> dict:
    try:
        t = json.loads(payload.get("tag") or "{}")
        return t if isinstance(t, dict) else {}
    except Exception:
        return {}


def _tag_score(payload: dict) -> int:
    try:
        return int(_tag(payload).get("score", 0))
    except Exception:
        return 0


def _pnl(payload: dict) -> float:
    try:
        return float(payload.get("profit_ratio", 0) or 0)
    except Exception:
        return 0.0


# ---- Fix 1: R-gate split (losses bypass, wins need quality) ----

def passes_gate(result: str, pnl: float) -> tuple[bool, str]:
    """Losses always post (spec rule 6). Wins need profit >= WIN_MIN_PROFIT.
    (R>=1 literally unreachable here: ROI caps wins at +4% < 10% stop.)"""
    if result == "loss":
        return True, "loss_always"
    if result == "win":
        try:
            min_p = float(_env("WIN_MIN_PROFIT", "0.004"))
        except Exception:
            min_p = 0.004
        if pnl >= min_p:
            return True, "win_quality"
        return False, "win_noise"
    return False, "unknown_result"


# ---- Fix 3: real freqtrade exit_reason -> result/reason ----

# Source of truth: freqtrade.enums.ExitType
# ('', custom_exit, emergency_exit, exit_signal, force_exit, liquidation,
#  partial_exit, roi, sold_on_exchange, stop_loss, stoploss_on_exchange,
#  trailing_stop_loss) PLUS custom strings (our 'timeout_96') PLUS
# comma-compounds (freqtradebot appends: exit_reason + ", " + reason).
_EXIT_LOSS = {"stop_loss": "stop hit",
              "trailing_stop_loss": "trailing stop hit",
              "stoploss_on_exchange": "exchange stop hit",
              "liquidation": "liquidated"}
_EXIT_LABEL = {
    "roi": "ROI target reached",
    "exit_signal": "exit signal fired",
    "force_exit": "manual exit",
    "emergency_exit": "emergency exit",
    "custom_exit": "custom exit",
    "sold_on_exchange": "sold on exchange",
    "partial_exit": "partial exit",
    "timeout_96": "8h timeout, no target",
    "": "exit",
}


def normalize_exit(exit_reason: str, pnl: float) -> dict:
    parts = [p.strip() for p in str(exit_reason or "").split(",") if p.strip()]
    labels: list[str] = []
    hard_loss = False
    saw_roi = False
    for part in parts:
        if part in _EXIT_LOSS:
            hard_loss = True
            labels.append(_EXIT_LOSS[part])
        elif part == "roi":
            saw_roi = True
            labels.append(_EXIT_LABEL[part])
        elif part in _EXIT_LABEL:
            labels.append(_EXIT_LABEL[part])
        else:
            log.warning("poster: unmapped exit-reason part %r", part)
            labels.append(part)
    if hard_loss:
        result = "loss"
    elif saw_roi:
        result = "win"
    else:
        result = "win" if pnl > 0 else "loss"
    r_multiple = round(pnl / STOP_PCT, 3)
    return {"result": result,
            "reason_text": " + ".join(labels) if labels else "exit",
            "r_multiple": r_multiple}


def confidence_of(score: int) -> str:
    if score >= int(_env("PUBLIC_MIN_SCORE", "70")):
        return "high"
    if score >= 55:
        return "medium"
    return "low"


def enrich_snapshot(event: str, payload: dict, exit_data: dict | None) -> dict:
    tag = _tag(payload)
    try:
        entry = float(payload.get("rate") or payload.get("open_rate") or 0)
    except Exception:
        entry = 0.0
    snap = {
        "coin": payload.get("pair", "?"),
        "timeframe": "5m",
        "direction": str(payload.get("direction", "?")).lower(),
        "entry_price": entry,
        "target_price": round(entry * (1 + TARGET_PCT), 6) if entry else 0,
        "stop_price": round(entry * (1 - STOP_PCT), 6) if entry else 0,
        "trend_bias": "up",
        "conditions_fired": tag.get("conds", []),
        "indicator_values": {k: tag.get(k) for k in ("rsi", "z", "gk", "h")
                             if tag.get(k) is not None},
        "confidence_tier": confidence_of(_tag_score(payload)),
    }
    if exit_data:
        snap.update({
            "result": exit_data["result"],
            "reason": exit_data["reason_text"],
            "r_multiple": exit_data["r_multiple"],
            "close_price": payload.get("close_rate"),
            "profit_ratio": _pnl(payload),
        })
    return snap


# ---- writers (template now; LLM swap point marked) ----

def _fmt(n) -> str:
    try:
        return ("%g" % float(n))
    except Exception:
        return str(n)


def write_telegram(snap: dict) -> str:
    if snap.get("result"):
        win = snap["result"] == "win"
        head = ("%s | %s (Paper)\nEntry: %s\n%s\nStop was: %s\nSetup: %s\nReason: %s" % (
            snap["coin"], snap["direction"].upper(),
            _fmt(snap["entry_price"]),
            ("TP: %s [check] hit" % _fmt(snap.get("close_price")) if win
             else "Stop/outcome: %s [x]" % _fmt(snap.get("close_price"))),
            _fmt(snap["stop_price"]),
            ", ".join(snap.get("conditions_fired", []) or ["confluence"]),
            "%s, R %s" % (snap.get("reason", "?"), snap.get("r_multiple", "?"))))
    else:
        head = ("%s | %s (Paper)\nEntry: %s\nTarget: %s\nStop: %s\nSetup: %s" % (
            snap["coin"], snap["direction"].upper(),
            _fmt(snap["entry_price"]), _fmt(snap["target_price"]),
            _fmt(snap["stop_price"]),
            ", ".join(snap.get("conditions_fired", []) or ["confluence"])))
    return head + "\n[!] " + DISCLAIMER


def write_square(snap: dict) -> str:
    coin = snap["coin"].replace("/", "")
    why = ", ".join(snap.get("conditions_fired", []) or ["confluence setup"])
    if snap.get("result"):
        mark = "[check]" if snap["result"] == "win" else "[x]"
        body = ("$%s Signal Result %s (Paper Trade)\nEntry: %s | Close: %s | Stop was: %s\n"
                "Setup feder: %s." % (
                    coin, mark, _fmt(snap["entry_price"]),
                    _fmt(snap.get("close_price")), _fmt(snap["stop_price"]), why))
    else:
        return ""  # Square posts outcomes only (entries would go stale there)
    tags = "#%s #TechnicalAnalysis $%s" % (coin.split("USDT")[0], coin)
    return body + "\n%s\nFull track record: [link]\n%s" % (DISCLAIMER, tags)


# ---- deterministic 6-check scorer (LLM scorer swap point marked) ----

_HYPE = ["moon", "rocket", "explosive", "massive gains", "insane", " gem ",
         "easy money", "can't lose", "100%", "guaranteed", "risk-free",
         "sure profit", "assured"]
_GUARANTEE = ["guarantee", " assured", "certain profit", "can't lose",
              "risk-free", "100% win", "sure profit"]


def score_post(text: str, snap: dict, platform: str) -> dict:
    checks = {}
    checks["disclaimer_present"] = DISCLAIMER in text
    # disclaimer text itself contains trigger words ("guarantee") - judge body only
    body = text.replace(DISCLAIMER, "")
    low = body.lower()
    checks["no_guaranteed_language"] = not any(g in low for g in _GUARANTEE)
    conds = snap.get("conditions_fired", []) or []
    checks["names_specific_condition"] = bool(
        conds) and any(c.lower() in low for c in conds)
    nums = [str(_fmt(snap.get("entry_price")))]
    if snap.get("result"):
        nums.append(str(_fmt(snap.get("close_price"))))
    checks["numbers_match_source"] = all(n and n in text for n in nums)
    checks["no_hype_language"] = not any(h in low for h in _HYPE)
    limit = TELEGRAM_TEXT_LIMIT if platform == "telegram" else SQUARE_LIMIT
    checks["correct_length_for_platform"] = len(text) <= limit
    score = sum(1 for v in checks.values() if v)
    fb = "; ".join(k for k, v in checks.items() if not v)
    return {"checks": checks, "score": score,
            "feedback": fb or "clean"}


def generate_and_validate_post(snap: dict, platform: str,
                               deadline_ts: float | None,
                               max_attempts: int = 5) -> dict:
    """Template writer + rule scorer loop. Attempt loop re-checks the deadline
    (Fix 2): attempts never start past it. LLM writer/scorer plug in here."""
    feedback = None
    for _ in range(max(1, max_attempts)):
        if deadline_ts is not None and time.monotonic() > deadline_ts:
            return {"status": "dropped_stale", "reason": "deadline"}
        draft = (write_telegram(snap) if platform == "telegram"
                 else write_square(snap))
        if not draft:
            return {"status": "skipped_empty"}
        result = score_post(draft, snap, platform)
        if result["score"] == 6:
            return {"status": "approved", "post": draft, "attempts": 1}
        feedback = result["feedback"]
    _log_post({"day": _today(), "event": "held_for_review",
               "status": "held", "snapshot": snap, "reason": feedback})
    return {"status": "held_for_review", "reason": feedback}


# ---- Telegram I/O + unwired Square ----

def telegram_send(text: str) -> tuple[bool, str]:
    token = _env("CHANNEL_BOT_TOKEN")
    chat_id = _env("CHANNEL_ID")
    if not token or not chat_id:
        return False, "missing CHANNEL_BOT_TOKEN/CHANNEL_ID"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % token, data=data,
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        if r.status == 200 and body.get("ok"):
            return True, str((body.get("result") or {}).get("message_id", ""))
        return False, str(body)[:200]
    except Exception as e:
        return False, str(e)[:200]


def telegram_send_photo(photo_path: str, caption: str) -> tuple[bool, str]:
    import requests as _rq
    token = _env("CHANNEL_BOT_TOKEN")
    chat_id = _env("CHANNEL_ID")
    if not token or not chat_id:
        return False, "missing CHANNEL_BOT_TOKEN/CHANNEL_ID"
    try:
        with open(photo_path, "rb") as f:
            r = _rq.post("https://api.telegram.org/bot%s/sendPhoto" % token,
                         data={"chat_id": chat_id, "caption": caption},
                         files={"photo": ("chart.png", f, "image/png")},
                         timeout=30)
        body = r.json() if hasattr(r, "json") else {}
        if r.status_code == 200 and body.get("ok"):
            return True, str((body.get("result") or {}).get("message_id", ""))
        return False, str(body)[:200]
    except Exception as e:
        return False, str(e)[:200]


def binance_square_send(_text: str) -> tuple[bool, str]:
    """UNWIRED STUB. Guarded by ENABLE_BINANCE_SQUARE=true (default false)."""
    if _env("ENABLE_BINANCE_SQUARE", "false").lower() != "true":
        return False, "square_disabled"
    return False, "square_unwired"


# ---- chart: §6 candlesticks, entry/target/stop, ≤3 overlays ----

_KRAKEN_ALIAS = {"BTC": "XBT"}


def _kraken_pair(pair: str) -> str | None:
    try:
        base, quote = pair.upper().split("/")
        return _KRAKEN_ALIAS.get(base, base) + quote
    except Exception:
        return None


def fetch_ohlc(pair: str, limit: int = 130) -> list:
    kp = _kraken_pair(pair)
    if not kp:
        raise ValueError("unmappable pair")
    url = ("https://api.kraken.com/0/public/OHLC?pair=%s&interval=5"
           % urllib.parse.quote(kp))
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read().decode("utf-8", "replace"))
    if body.get("error"):
        raise ValueError(str(body["error"])[:120])
    result = body.get("result", {})
    key = next((k for k in result.keys() if k != "last"), None)
    rows = result.get(key, []) if key else []
    # [time, o, h, l, c, v] — last ~10h only: 300 crammed bars were mush
    out = [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]),
            float(c[6])] for c in rows[-limit:] if len(c) > 6]
    if len(out) < 60:
        raise ValueError("too few candles")
    return out


def _ema50(values: list[float], period: int = 50) -> list[float]:
    """Pure-python EMA (poster image has no talib/pandas - and must not gain
    them for one line). Callers: pass the REAL 50-period series; an old demo
    once passed EMA10 mislabeled as EMA50 - do not repeat that."""
    out: list[float] = []
    k = 2 / (period + 1)
    s = values[0] if values else 0.0
    for v in values:
        s = v * k + s * (1 - k)
        out.append(s)
    return out


def render_chart(ohlc: list, snap: dict, out_path: str = "/tmp/chart.png") -> str:
    """AQVION high-quality render: supersampled 2x, Lanczos downscale to
    exactly 1200x675. Header (brand + direction badge), candlesticks with
    time axis, risk gauge (TP/entry/SL/current + honest R:R), footer facts.
    Overlays ONLY for conditions_fired (trend->EMA50, volume->bars)."""
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.patches import Rectangle, FancyBboxPatch
    import numpy as np
    from datetime import datetime as _dt
    from PIL import Image

    BG, PANEL_BG, GRID = "#0a0f1a", "#0d1520", "#182233"
    TEXT, TEXT_DIM = "#dbe2ec", "#5c6779"
    ORANGE, GREEN, RED, BLUE = "#f5a623", "#26d6a3", "#f04763", "#4d8fe0"
    SS = 2

    pair = str(snap.get("coin", "?"))
    direction = str(snap.get("direction", "long")).upper()
    conds = set(snap.get("conditions_fired", []) or [])
    entry = float(snap.get("entry_price") or 0)
    target = entry * 1.04 if entry else 0.0
    stop = entry * 0.90 if entry else 0.0

    rows = [(_dt.fromtimestamp(c[0], tz=timezone.utc),
             float(c[1]), float(c[2]), float(c[3]), float(c[4]))
            for c in ohlc]
    closes = [c[4] for c in rows]
    ema = _ema50(closes, 50)

    W, H = 12, 6.75
    dpi = 100 * SS
    fig = plt.figure(figsize=(W, H), dpi=dpi, facecolor=BG)
    gs = fig.add_gridspec(3, 2, height_ratios=[0.09, 0.72, 0.19],
                          width_ratios=[0.87, 0.13],
                          hspace=0.07, wspace=0.025,
                          left=0.045, right=0.97, top=0.97, bottom=0.035)
    ax_head = fig.add_subplot(gs[0, :])
    ax_head.axis("off")
    ax_price = fig.add_subplot(gs[1, 0])
    ax_gauge = fig.add_subplot(gs[1, 1])
    ax_foot = fig.add_subplot(gs[2, :])
    ax_foot.axis("off")
    ax_foot = fig.add_subplot(gs[2, :])
    ax_foot.axis("off")
    for ax in (ax_price, ax_gauge):
        ax.set_facecolor(PANEL_BG)
        for spine in ax.spines.values():
            spine.set_color(GRID)
            spine.set_linewidth(1.1)
            spine.set_visible(True)

    ax_head.text(0.0, 0.48, "AQVION", fontsize=23, fontweight="bold",
                 color=ORANGE, va="center", ha="left")
    ax_head.text(0.158, 0.48, "%s  \u00b7  5m  \u00b7  paper" % pair,
                 fontsize=11.5, color=TEXT_DIM, va="center", ha="left")
    dir_color = GREEN if direction == "LONG" else RED
    ax_head.add_patch(FancyBboxPatch(
        (0.905, 0.1), 0.085, 0.8,
        boxstyle="round,pad=0.006,rounding_size=0.15",
        transform=ax_head.transAxes, facecolor=dir_color,
        edgecolor="none", alpha=0.16))
    ax_head.text(0.9475, 0.5, direction, fontsize=11.5, fontweight="bold",
                 color=dir_color, va="center", ha="center",
                 transform=ax_head.transAxes)

    dates = [d for d, *_ in rows]
    xnum = mdates.date2num(dates)
    width = (xnum[1] - xnum[0]) * 0.58 if len(xnum) > 1 else 0.0015
    lows = [row[3] for row in rows]
    highs = [row[2] for row in rows]
    minh = max(highs) - min(lows)
    for i, (d, o, h, l, c) in enumerate(rows):
        x = xnum[i]
        up = c >= o
        color = GREEN if up else RED
        ax_price.plot([x, x], [l, h], color=color, linewidth=1.15, zorder=2,
                      solid_capstyle="round", alpha=0.95)
        body_low, body_high = (o, c) if up else (c, o)
        body_h = max(body_high - body_low, minh * 0.0018)
        ax_price.add_patch(Rectangle(
            (x - width / 2, body_low), width, body_h,
            facecolor=color, edgecolor=color, linewidth=0,
            zorder=3, alpha=0.95))
    if "trend" in conds:
        ax_price.plot(xnum, ema, color=ORANGE, linewidth=1.6, alpha=0.9,
                      zorder=4, solid_capstyle="round", label="EMA50")
        leg = ax_price.legend(loc="upper left", frameon=False, fontsize=9.5)
        for t in leg.get_texts():
            t.set_color(TEXT_DIM)
    if entry:
        ax_price.axhline(entry, color=BLUE, linestyle=(0, (5, 3)),
                         linewidth=1.3, alpha=0.9, zorder=5)
        ax_price.text(xnum[-1], entry, "  entry %s" % _fmt(entry), color=BLUE,
                      fontsize=9.5, va="center", ha="left", fontweight="bold")
    ax_price.set_xlim(xnum[0], xnum[-1] + (xnum[-1] - xnum[0]) * 0.115)
    pad = minh * 0.09
    ax_price.set_ylim(min(lows) - pad, max(highs) + pad)
    ax_price.grid(True, color=GRID, linewidth=0.7, alpha=0.55)
    ax_price.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax_price.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda v, p: "%s" % _fmt(v)))
    ax_price.tick_params(colors=TEXT_DIM, labelsize=9, length=0)
    ax_price.text(0.012, 0.025, "%s \u00b7 5m" % pair,
                  transform=ax_price.transAxes,
                  fontsize=8.5, color=TEXT_DIM, va="bottom", ha="left", alpha=0.8)
    ax_price.text(0.5, 0.5, "AQVION", transform=ax_price.transAxes,
                  fontsize=46, color=TEXT_DIM, alpha=0.022, ha="center",
                  va="center", fontweight="bold", zorder=0)

    is_long = direction == "LONG"
    gauge_lo, gauge_hi = (stop, target) if is_long else (target, stop)
    span = gauge_hi - gauge_lo
    current = closes[-1]
    ax_gauge.set_xlim(0, 1)
    ax_gauge.set_ylim(0, 1)
    ax_gauge.axis("off")
    bar_x0, bar_w = 0.40, 0.18
    ax_gauge.add_patch(FancyBboxPatch(
        (bar_x0, 0.05), bar_w, 0.90,
        boxstyle="round,pad=0,rounding_size=0.02",
        facecolor="#101b2b", edgecolor=GRID, linewidth=1.1, zorder=1))

    def y_of(price):
        return 0.05 + 0.90 * (price - gauge_lo) / span if span else 0.5

    cur_y = float(np.clip(y_of(current), 0.06, 0.94))
    ax_gauge.add_patch(FancyBboxPatch(
        (bar_x0, 0.05), bar_w, cur_y - 0.05,
        boxstyle="round,pad=0,rounding_size=0.02",
        facecolor=dir_color, alpha=0.28, edgecolor="none", zorder=2))

    def mark(price, label, color, side, y_off):
        y = float(np.clip(y_of(price), 0.05, 0.95))
        ax_gauge.plot([bar_x0, bar_x0 + bar_w], [y, y], color=color,
                      linewidth=2.2, zorder=4, solid_capstyle="round")
        if side == "right":
            ax_gauge.text(bar_x0 + bar_w + 0.025, y, label, color=color,
                          fontsize=9, fontweight="bold", ha="left",
                          va="center", zorder=5)
        else:
            ax_gauge.text(bar_x0 + bar_w / 2, y + y_off, label, color=color,
                          fontsize=9, fontweight="bold", ha="center",
                          va="bottom" if y_off > 0 else "top", zorder=5)

    entry_close = abs(y_of(entry) - cur_y) < 0.07 if entry and span else False
    if target:
        mark(target, "TP %s" % _fmt(target), GREEN, "top", 0.035)
    if entry:
        mark(entry, "E %s" % _fmt(entry), BLUE,
             "right" if entry_close else "top", 0.0 if entry_close else 0.035)
    if stop:
        mark(stop, "SL %s" % _fmt(stop), RED, "bottom", -0.045)
    ax_gauge.plot(bar_x0 - 0.035, cur_y, marker=">", color=TEXT,
                  markersize=8, zorder=6)
    cur_label_y = cur_y - 0.055 if entry_close else cur_y
    ax_gauge.text(bar_x0 - 0.055, cur_label_y, "%s" % _fmt(current),
                  color=TEXT, fontsize=9, ha="right", va="center",
                  fontweight="bold")
    dist_to_tp = abs(target - current) / current * 100 if current else 0
    dist_to_sl = abs(current - stop) / current * 100 if current else 0
    rr = dist_to_tp / dist_to_sl if dist_to_sl else 0
    ax_gauge.text(0.5, 0.99, "R:R %.2f" % rr, color=TEXT_DIM, fontsize=8.5,
                  ha="center", va="top", transform=ax_gauge.transAxes)

    if entry and current:
        dist_line = ("Entry: %s     Target: %s  (+%.2f%%)     Stop: %s  (-%.2f%%)"
                     "     R:R %.2f" % (
                         _fmt(entry), _fmt(target), dist_to_tp,
                         _fmt(stop), dist_to_sl, rr))
    else:
        dist_line = "Entry %s" % _fmt(entry)
    lines = [
        ("%s | %s (Paper)" % (pair, direction), TEXT, 12.5, "bold"),
        (dist_line, TEXT, 10.5, "normal"),
        ("Setup: %s" % ", ".join(snap.get("conditions_fired", [])
                                 or ["confluence"]), TEXT_DIM, 9.7, "normal"),
        ("[!] " + DISCLAIMER, TEXT_DIM, 8.3, "normal"),
    ]
    y = 0.87
    for text, color, size, weight in lines:
        ax_foot.text(0.0, y, text, fontsize=size, color=color,
                     fontweight=weight, va="top", ha="left",
                     transform=ax_foot.transAxes)
        y -= 0.30

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG, dpi=dpi)
    plt.close(fig)
    buf.seek(0)
    from PIL import Image
    img = Image.open(buf).resize((1200, 675), Image.LANCZOS)
    img.save(out_path)
    return out_path


# ---- paths: entry-elite + outcome volume ----

def handle_fill(payload: dict) -> dict:
    """Entry path: elite-only (calibrated PUBLIC_MIN_SCORE), staleness clock.
    NOTE: no veto check here on purpose - vetoed trades never fill, so every
    fill reaching this function was already approved by confirm_trade_entry in
    the trader. Do not add a veto path at this layer; it would be dead code."""
    if _tag_score(payload) < int(_env("PUBLIC_MIN_SCORE", "70")):
        return {"status": "skipped_policy"}
    snap = enrich_snapshot("entry_fill", payload, None)
    try:
        window_min = float(_env("STALENESS_MINUTES", "15"))
    except Exception:
        window_min = 15.0
    return {"status": "queued_entry", "snapshot": snap,
            "deadline_ts": time.monotonic() + window_min * 60}


def handle_close(payload: dict) -> dict:
    """Outcome path: R-split gate (Fix 1), generous fixed clock (Fix 2)."""
    exit_data = normalize_exit(
        str(payload.get("exit_reason", payload.get("reason", ""))),
        _pnl(payload))
    ok, why = passes_gate(exit_data["result"], _pnl(payload))
    if not ok:
        _log_post({"day": _today(), "event": "exit_fill",
                   "status": "skipped_" + why, "pair": payload.get("pair")})
        return {"status": "skipped_" + why}
    snap = enrich_snapshot("exit_fill", payload, exit_data)
    try:
        budget = float(_env("OUTCOME_BUDGET_S", "300"))
    except Exception:
        budget = 300.0
    return {"status": "queued_outcome", "snapshot": snap,
            "deadline_ts": time.monotonic() + budget}


def process_job(kind: str, snap: dict, deadline_ts: float | None) -> dict:
    """Worker body: chart pre-compute + validate loop + send. Returns receipt."""
    day = _today()
    if deadline_ts is not None and time.monotonic() > deadline_ts:
        _log_post({"day": day, "event": kind, "status": "dropped_stale",
                   "pair": snap.get("coin")})
        return {"status": "dropped_stale"}
    res = generate_and_validate_post(snap, "telegram", deadline_ts)
    if res["status"] != "approved":
        return res
    text = res["post"]
    photo_status = "no_chart"
    try:
        ohlc = fetch_ohlc(str(snap.get("coin", "")))
        photo = render_chart(ohlc, snap)
        photo_status = "chart_ok"
        ok, info = telegram_send_photo(photo, text)
        if ok:
            _log_post({"day": day, "event": kind, "status": "posted",
                       "kind": "photo", "pair": snap.get("coin"), "info": info})
            out = {"status": "posted", "kind": "photo", "info": info}
            _, sq = binance_square_send(text)
            out["square"] = sq
            return out
    except Exception as e:
        photo_status = str(e)[:120]
    ok, info = telegram_send(text)
    _log_post({"day": day, "event": kind,
               "status": "posted" if ok else "failed",
               "kind": "text", "pair": snap.get("coin"), "info": info,
               "chart": photo_status})
    out = {"status": "posted" if ok else "failed", "kind": "text", "info": info}
    if ok:
        _, sq = binance_square_send(text)
        out["square"] = sq
    return out


# ---- queue: entries jump outcomes; single worker (volume is ~pairs/day) ----

_jobq: "_queue.PriorityQueue" = _queue.PriorityQueue()
_worker_started = False


def enqueue(kind: str, snap: dict, deadline_ts: float | None) -> dict:
    prio = 0 if kind == "entry" else 1
    _jobq.put((prio, time.monotonic(), kind, snap, deadline_ts))
    return {"status": "queued_" + kind}


def _worker_loop() -> None:
    while True:
        try:
            _, _, kind, snap, deadline = _jobq.get()
            try:
                process_job(kind, snap, deadline)
            finally:
                _jobq.task_done()
        except Exception as e:
            log.warning("poster worker: %s", e)


def start_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    threading.Thread(target=_worker_loop, daemon=True,
                     name="poster-worker").start()


def handle_hook(payload: dict) -> dict:
    event = str(payload.get("event", ""))
    if event == "entry_fill":
        r = handle_fill(payload)
    elif event == "exit_fill":
        r = handle_close(payload)
    else:
        return {"status": "skipped_event", "event": event}
    if r["status"] in ("queued_entry", "queued_outcome"):
        kind = "entry" if r["status"] == "queued_entry" else "outcome"
        start_worker()
        enqueue(kind, r["snapshot"], r["deadline_ts"])
        return {"status": r["status"]}
    return r


# ---- HTTP layer (lazy imports: module stays importable without fastapi) ----

def create_app():
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="poster")

    @app.on_event("startup")
    async def _startup():
        start_worker()

    @app.get("/")
    async def health():
        return {"ok": True, "service": "poster", "square": "disabled"}

    @app.post("/hook")
    async def hook(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"status": "bad_request"}, status_code=400)
        return JSONResponse(handle_hook(payload if isinstance(payload, dict) else {}))

    return app


app = None
try:
    app = create_app()
except Exception:  # fastapi absent (tests / syntax checks)
    app = None
