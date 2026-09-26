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
