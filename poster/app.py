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

EXIT_MAP = {
    "roi": ("win", "ROI target reached"),
    "exit_signal": (None, "exit signal fired"),       # sign decides below
    "stop_loss": ("loss", "stop hit"),
    "trailing_stop_loss": ("loss", "trailing stop hit"),
    "force_exit": (None, "manual exit"),
    "emergency_exit": (None, "emergency exit"),
    "timeout_96": (None, "8h timeout, no target"),    # our custom_exit id
}


def normalize_exit(exit_reason: str, pnl: float) -> dict:
    base_result, reason = EXIT_MAP.get(
        str(exit_reason or ""), (None, str(exit_reason or "exit")))
    if base_result is None:
        base_result = "win" if pnl > 0 else "loss"
    r_multiple = round(pnl / STOP_PCT, 3)
    return {"result": base_result, "reason_text": reason,
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


def render_chart(ohlc: list, snap: dict, out_path: str = "/tmp/chart.png") -> str:
    """1200x675 candlesticks, last ~10h. Y-axis fits CANDLES (padded);
    target/stop draw only inside the view, else edge arrows. (Old version let
    the fixed +/-10% bands squash everything flat, and 300 bars were mush.)
    Overlays ONLY for conditions_fired (<=3): trend->EMA50, volume->bars."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as patches
    from datetime import datetime as _dt
    conds = set(snap.get("conditions_fired", []) or [])
    times = [_dt.fromtimestamp(c[0], tz=timezone.utc).strftime("%H:%M") for c in ohlc]
    opens = [c[1] for c in ohlc]
    highs = [c[2] for c in ohlc]
    lows = [c[3] for c in ohlc]
    closes = [c[4] for c in ohlc]
    vols = [c[5] for c in ohlc]

    lo, hi = min(lows), max(highs)
    pad = (hi - lo) * 0.15 or hi * 0.001
    lo -= pad
    hi += pad

    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=100)
    ax.set_facecolor("#0f1115")
    fig.patch.set_facecolor("#0f1115")
    for i, (o, h, l, c) in enumerate(zip(opens, highs, lows, closes)):
        color = "#26a69a" if c >= o else "#ef5350"
        ax.plot([i, i], [l, h], color=color, linewidth=1.2)
        ax.add_patch(patches.Rectangle(
            (i - 0.35, min(o, c)), 0.7, max(abs(c - o), (hi - lo) * 0.0008),
            facecolor=color, edgecolor=color))
    n_over = 0
    if "trend" in conds and n_over < 3:
        ema, k, s = [], 2 / (50 + 1), closes[0]
        for v in closes:
            s = v * k + s * (1 - k)
            ema.append(s)
        ax.plot(ema, label="EMA50", linewidth=1.4, color="#e2a63d")
        n_over += 1
    entry = snap.get("entry_price") or 0

    def _level(price: float, style: str, label: str, color: str) -> None:
        if not price:
            return
        if lo <= price <= hi:
            ax.axhline(price, linestyle=style, linewidth=1.2, color=color)
            ax.text(len(ohlc) - 1, price, " %s %s " % (label, _fmt(price)),
                    color="white", fontsize=9, va="center", ha="right",
                    bbox=dict(facecolor=color, alpha=0.85, edgecolor="none", pad=1))
        else:
            arrow = "$\u2191$" if price > hi else "$\u2193$"
            ax.annotate("%s %s %s" % (label, arrow, _fmt(price)),
                        xy=(len(ohlc) - 1, hi if price > hi else lo),
                        color=color, fontsize=10, ha="right",
                        va="top" if price > hi else "bottom")

    if entry:
        _level(entry, "--", "entry", "#4da3ff")
        _level(entry * 1.04, ":", "target", "#26a69a")
        _level(entry * 0.90, ":", "stop", "#ef5350")
    if "volume" in conds and n_over < 3:
        ax2 = ax.twinx()
        ax2.bar(range(len(ohlc)), vols, alpha=0.22, color="gray")
        ax2.set_ylim(0, max(vols) * 4)
        ax2.tick_params(labelsize=7, colors="gray")
        n_over += 1
    step = max(1, len(ohlc) // 6)
    ax.set_xticks(range(0, len(ohlc), step))
    ax.set_xticklabels([times[i] for i in range(0, len(ohlc), step)],
                       fontsize=8, color="gray")
    ax.set_xlim(-1, len(ohlc))
    ax.set_ylim(lo, hi)
    ax.tick_params(labelsize=8, colors="gray")
    ax.grid(True, alpha=0.15)
    ax.set_title("%s - 5m (paper)" % snap.get("coin", "?"), loc="left",
                 color="white", fontsize=12)
    ax.legend(fontsize=8, facecolor="#0f1115", labelcolor="white")
    fig.savefig(out_path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path


# ---- paths: entry-elite + outcome volume ----

def handle_fill(payload: dict) -> dict:
    """Entry path: elite-only (calibrated PUBLIC_MIN_SCORE), staleness clock."""
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
