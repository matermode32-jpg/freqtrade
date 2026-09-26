"""Public poster service: freqtrade webhook -> Telegram channel highlights.
Binance Square sender exists as an UNWIRED stub (disabled by env).
Runs on Render as a second Web Service from this same repo.

text-only v1 (no charts yet): photo captions need candle history the webhook
does not carry. Chart hook TODO is marked below.
"""
import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("poster")

DISCLAIMER = ("Paper trading result. Not financial advice. "
              "Past paper performance does not guarantee future results.")
CAPTION_LIMIT = 1000  # Telegram photo-caption cap; text cap is 4096
LOG_PATH = os.getenv("POSTER_LOG", "poster_posts.jsonl")


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


def _tag_score(payload: dict) -> int:
    try:
        tag = json.loads(payload.get("tag") or "{}")
        return int(tag.get("score", 0))
    except Exception:
        return 0


def compose_caption(event: str, payload: dict) -> str:
    if event == "entry_fill":
        head = ("[PAPER] LONG {pair} @ {rate} | score {score} | stake {stake}".format(
            pair=payload.get("pair", "?"),
            rate=payload.get("rate", "?"),
            score=_tag_score(payload),
            stake=payload.get("stake", "?")))
    else:
        try:
            pr = float(payload.get("profit_ratio", 0) or 0)
        except Exception:
            pr = 0.0
        head = ("[{w}] {d} {pair} closed @ {rate} | {pct:+.2%} ({amt} USDT)".format(
            w="WIN" if pr > 0 else "LOSS",
            d=payload.get("direction", "?"),
            pair=payload.get("pair", "?"),
            rate=payload.get("close_rate", "?"),
            pct=pr,
            amt=payload.get("profit_amount", "?")))
    text = head[:CAPTION_LIMIT - len(DISCLAIMER) - 2] + "\n\n" + DISCLAIMER
    # exact-disclaimer assert: the configured string itself must be present,
    # not merely the word "paper" somewhere in AI text.
    assert DISCLAIMER in text, "disclaimer missing - refusing to post publicly"
    return text


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


# ---- chart path: fetch public klines, render, sendPhoto (text fallback) ----

_KRAKEN_ALIAS = {"BTC": "XBT", "DOGE": "DOGE", "USDT": "USDT"}


def _kraken_pair(pair: str) -> str | None:
    """'BTC/USDT' -> 'XBTUSDT'. None when unmappable (e.g. TEST pairs)."""
    try:
        base, quote = pair.upper().split("/")
        base = _KRAKEN_ALIAS.get(base, base)
        return base + quote
    except Exception:
        return None


def fetch_closes(pair: str, limit: int = 100) -> tuple[list[float], float | None]:
    """Public Kraken klines, no key. Returns (closes, entry-agnostic)."""
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
    candles = result.get(key, []) if key else []
    closes = [float(c[4]) for c in candles[-limit:] if len(c) > 4]
    if len(closes) < 20:
        raise ValueError("too few candles")
    return closes, None


def render_chart(closes: list[float], entry_rate: float | None,
                 title: str, out_path: str = "/tmp/chart.png") -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(closes, label="close 5m")
    if entry_rate:
        ax.axhline(entry_rate, linestyle="--", label="entry")
    ax.set_title(title + " (paper)")
    ax.legend()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path


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
    """UNWIRED STUB. Do not call until Square API is specified + key issued.
    Guarded by ENABLE_BINANCE_SQUARE=true (default false)."""
    if _env("ENABLE_BINANCE_SQUARE", "false").lower() != "true":
        return False, "square_disabled"
    return False, "square_unwired"


def handle_hook(payload: dict) -> dict:
    event = str(payload.get("event", ""))
    day = _today()
    if event not in ("entry_fill", "exit_fill"):
        return {"status": "skipped_event", "event": event}
    if event == "entry_fill" and _tag_score(payload) < int(_env("PUBLIC_MIN_SCORE", "70")):
        _log_post({"day": day, "event": event, "status": "skipped_policy",
                   "pair": payload.get("pair")})
        return {"status": "skipped_policy"}
    if _posted_today() >= int(_env("PUBLIC_MAX_PER_DAY", "10")):
        _log_post({"day": day, "event": event, "status": "skipped_throttle",
                   "pair": payload.get("pair")})
        return {"status": "skipped_throttle"}
    try:
        text = compose_caption(event, payload)
    except AssertionError as e:
        return {"status": "refused", "error": str(e)}
    # photo first (chart), text fallback — chart needs public klines only
    photo_status = "no_chart"
    try:
        closes, _ = fetch_closes(str(payload.get("pair", "")))
        try:
            entry_rate = float(payload.get("rate") or 0) or None
        except Exception:
            entry_rate = None
        photo = render_chart(closes, entry_rate, "%s %s" % (
            payload.get("pair", "?"), payload.get("direction", "")))
        photo_status = "chart_ok"
        ok, info = telegram_send_photo(photo, text)
        if ok:
            _log_post({"day": day, "event": event, "status": "posted",
                       "kind": "photo", "pair": payload.get("pair"), "info": info})
            result = {"status": "posted", "kind": "photo", "info": info}
            if True:
                _, sq = binance_square_send(text)
                result["square"] = sq
            return result
    except Exception as e:
        photo_status = str(e)[:120]
        log.info("chart skipped (%s), text fallback", photo_status)
    ok, info = telegram_send(text)
    _log_post({"day": day, "event": event,
               "status": "posted" if ok else "failed",
               "pair": payload.get("pair"), "info": info})
    result = {"status": "posted" if ok else "failed", "info": info}
    # Square stays OFF: stub records the decision without sending.
    if ok:
        _, sq = binance_square_send(text)
        result["square"] = sq
    return result


# ---- HTTP layer (lazy imports: module stays importable without fastapi) ----

def create_app():
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="poster")

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
