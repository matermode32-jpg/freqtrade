# Nova Trading Bot — Complete Manual
Prepared for: Rifat | Last updated: 2026-09-26 (quant v2 live)

> Live state: `main` @ ConfluenceStrategy + quant pack (maker pricing, Z-gate,
> Kelly sizing, Groq-120B veto, signal ledger, Hurst analysis). Branch
> `quant-upgrade`/`mtf-matrix` merged. `Bot_Manual.md` local-only until next
> functional push (not pushed alone — every main push restarts live).

---

## 1. What This Bot Is

A 24/7 crypto paper-trading (dry-run) bot. It watches Kraken spot prices on 5-minute
candles, opens simulated Kelly-sized long positions when its confluence engine fires,
and manages them with take-profit ladders, a fixed catastrophe stop, a time stop, and
loss-guard protections. A Groq-120B veto judges every entry. It talks to you over
Telegram (`/status`, `/profit`,
`/balance`, entry/exit alerts). No real money moves anywhere: `dry_run: true`.

## 2. Framework & Hosting

| Piece | Choice | Why |
|---|---|---|
| Engine | Freqtrade 2026.8 (stable Docker image) | Tested data feed, paper engine, Telegram, backtester — replaces ~5 custom modules |
| Exchange (live) | Kraken spot | Binance bans Render's shared IPs (HTTP 418); Kraken does not |
| Exchange (30d research) | Binance klines | Kraken history caps at ~8 days; Binance gives 30d+ fast |
| Host | Render.com free Web Service (Docker) | Free, no card; Dockerfile at repo root |
| Keep-alive | cron-job.org every 10 min → `/api/v1/ping` | Free plan sleeps after 15 min idle; root URL returns oversized HTML, ping returns tiny JSON |
| Repo | github.com/matermode32-jpg/freqtrade, branch `main` | Render auto-deploys on push |
| Secrets | Render Dashboard → Environment only | Repo holds placeholders (`your_token_here`); `.env.example` lists names; real `.env` never committed, never opened |

## 3. Every Feature, How It Works

### 3.1 Money management (position sizing)
- Virtual wallet 1000 USDT (`dry_run_wallet`). **Kelly fractional sizing**
  (λ=0.25, calibrated p=0.774 / b=0.450 from the 31-trade 30d window → ~6.8% of
  wallet, clamped 20–300 USDT) via `custom_stake_amount`. Observed live stakes
  ~58–67 USDT instead of the old fixed 100.
- Max **3 concurrent** (`max_open_trades`); `tradable_balance_ratio: 0.99`.

### 3.2 Market universe
- 3 pairs, `StaticPairList`: BTC/USDT, ETH/USDT, SOL/USDT. Nothing else is watched.
- 5-minute candles, decisions on closed candles only (`process_only_new_candles`).
- 300 startup candles (~25 h) warm up before first signal.

### 3.3 Entry engine (ConfluenceStrategy, scored)
Four weighted conditions, total 100, enter LONG at **score ≥ 55** (tune gate
tested 45/55/65; 45 overtraded to −3.5%, 65 ≈ 55 with fewer samples):
- 4h trend bias — 30 pts (EMA50 > EMA200 + ≥0.3% spread + 4h close above EMA50,
  via native `informative_pairs`, no custom tables)
- RSI(14) < 30 — 25 pts
- Volume > 20-candle average — 15 pts
- Structure (bullish order block or unfilled FVG) — 30 pts
- Each entry carries a JSON snapshot tag (`score`, `z`, `gk`, `h`, `conds`)
  used by the veto hook, ledger, logs, and the future posting service.
- Maker execution: post-only bid/ask pricing, 10%-into-spread entry/exit
  callbacks, 60 s re-slide of stale entries, **15-minute unfilled timeout**.
  (Spec's `candles` timeout unit doesn't exist in 2026.8 — 15 min = 3×5m.)
  Known behavior: limits can cancel + re-fire in drift — fills improved vs 10-min era.

### 3.4 Exit engine (first hit wins)
- ROI ladder: +4% anytime, +2% after 30 min, +1% after 60 min.
- RSI > 70 exit signal (works even at a loss).
- Fixed −10% catastrophe stop through the enabled `custom_stoploss` path.
  (Garman-Klass proportional stops were validated and REJECTED: 2× → −0.55% win
  21%, 6× → +0.14% — both worse than fixed. GK stays computed+tagged.)
- Time stop: force exit after 96 candles (8 h) via `custom_exit` ("timeout_96").

### 3.5 Protections (loss guards)
- `StoplossGuard`: 3 losing stops within 24 candles → pause entries 12 candles.
- `MaxDrawdown`: 3% drawdown within 288 candles (24 h) → pause 48 candles.
- (Freqtrade 2026 requires protections inside the strategy file, not config.)

### 3.6 Master-engine veto hook (LIVE)
- `confirm_trade_entry()` judges every entry via **GPT OSS 120B on Groq**
  (2.5 s cap, fail-open, verdict cache, `v1-quant` prompt). Key from Render env
  `GROQ_API_KEY` (read via `os.getenv`, never in config/git).
- Every decision appended to `user_data/logs/signal_ledger.csv` (git-ignored,
  2 MB rotation): time, pair, rate, tag, verdict, reason, model, latency.
- Rolling Hurst (`hurst_100`) computed + tagged, **zero weight** until
  walk-forward proof (same rule that killed the MTF gate).

### 3.7 Telegram (private control bot)
- Auto-push: startup burst, entries, fills, exits, warnings.
- Commands: `/status /profit /balance /daily /weekly /monthly /count /performance
  /trades /forcebuy /forcesell /start /stop /pause /reload_config /show_config
  /logs /health /help` and more.
- Only the authorized chat ID is obeyed; all others get "rejected unauthorized".

### 3.8 API server (keep-alive surface)
- Listens 0.0.0.0:$PORT (Render injects PORT; entrypoint maps it into config).
- Public `/api/v1/ping` is the cron target. Auth (user/password/JWT) guards the rest.

### 3.9 Runtime secret injection
- `entrypoint.sh` (python) reads Render env vars, swaps placeholders in
  `config.json`, writes `/tmp/freqtrade.config.json`, and forces `--config` there
  (image files are read-only at runtime). Real values never touch git or logs.

### 3.10 Backtesting & data (local, Docker)
- `download-data` (Kraken needs `--dl-trades`; ~8-day cap) and `backtesting`
  commands run locally for research. 30-day/7-token behavior tests used Binance
  klines on a throwaway config (deleted after; never deployed).

## 4. Strategies Compared

| | SampleStrategy (was live) | ConfluenceStrategy (live now) |
|---|---|---|
| Logic | Template RSI dip-buy | Scored confluence (trend+RSI+volume+structure) |
| 7d Kraken backtest | 6 trades, −0.3%, 50% win | 2 trades, −0.19% (sample too small — bias warmup eats the window) |
| 30d Binance/7-pair test | — | 31 trades, +0.40%, 77.4% win, PF 1.54 (quant v2: Z-gate + Kelly) |
| Stops/protections | Fixed −10%, none | Fixed −10% via enabled path, time stop, StoplossGuard, MaxDrawdown |
| Sizing | fixed 100 | Kelly ¼ (p=0.774, b=0.450 → ~6.8% wallet) |
| Veto | — | GPT OSS 120B live, ledgered |
| Verdict | Placeholder | Thin but real edge; fee-fragile (avg +0.21%/trade vs 0.26% Kraken fees) |

## 5. Problems Faced & Fixes

| # | Problem | Cause | Fix | State |
|---|---|---|---|---|
| 1 | `docker` not found after install | Desktop installed to user profile, bin not on PATH | Full path `.../DockerDesktop/resources/bin` + PATH prepend | Solved |
| 2 | Render build fail: `chmod` not permitted | Image builds as non-root user | Entrypoint via `python3`, dropped `chmod` | Solved |
| 3 | `PermissionError` writing config | Image files read-only at runtime | Runtime copy at `/tmp/freqtrade.config.json` | Solved |
| 4 | Binance 418 IP ban, 4 deploys straight | Render shared exit IP banned | Live moved to Kraken; Binance kept for local research only | Solved |
| 5 | `pairlists is required` crash | Freqtrade 2026.8 new required section | Added `StaticPairList` | Solved |
| 6 | Bot silent, `/status` ignored | `TELEGRAM_CHAT_ID` was the bot's own ID | Set to the user chat ID from logs/`getUpdates` | Solved |
| 7 | `Conflict: other getUpdates` spam | Two pollers (browser tab open, or deploy overlap) | Close tab; single instance; restart | Solved/transient |
| 8 | CoinGecko 429 spam | Shared IP over free fiat-convert quota | `fiat_display_currency: ""` (USDT rejected by schema — must be empty) | Solved |
| 9 | Cron `Failed (output too large)` → sleep → silence | Root URL returns big HTML; failures auto-disabled the job | Ping `/api/v1/ping`; keep job enabled | Solved |
| 10 | Deploy ran SampleStrategy after merge | Dockerfile `CMD --strategy SampleStrategy` overrides config | Dropped `--strategy`; config is source of truth | Solved |
| 11 | `protections` in config = deploy error | 2026.8 moved protections into strategy file | Moved to class attribute | Solved |
| 12 | Skeleton column crash `ema50_4h` | Double `_4h` suffix (merge adds its own) | Plain names pre-merge | Solved |
| 13 | 0 trades / 30 days | Hard-AND of 4 conditions never coincides | Restored Neon scoring (min 55) | Solved |
| 14 | Threshold 45 → −3.5% | Overtrading, fees eat churn | Kept 55 (65 equal, fewer samples) | Solved |
| 15 | Entries cancel loop | Limit-at-close + timeout in drift | Maker pack live: post-only + spread callbacks + 15-min timeout | Solved, observe fills |
| 16 | Kraken history caps ~8d | Exchange-side limit | Accept; long tests use Binance klines with caveats | Accepted |
| 17 | `use_custom_stoploss: False` — method dead | 2026.8 needs explicit flag | Flag set (verified in image source); GK stops validated & rejected (2× −0.55%, 6× +0.14% vs fixed +0.40%) | Solved |
| 18 | MTF matrix scored −0.25% | Late trigger + filters cut winners | Reverted to validated weights; MTF cols kept unweighted for study | Solved |
| 19 | `candles` timeout unit invalid | Doesn't exist in 2026.8 | 15 min (= 3×5m) | Solved |

## 6. Results Ledger

- Live dry-run: entries firing with JSON tags (e.g. BTC long score 60 @83905.8),
  fills + timeout-cancels behaving per design; `/status`, `/balance` responsive.
- Backtests: §4 table. Standing warning — edge is fee-fragile at Kraken costs.
- Repo hygiene: placeholders only, `.env` never committed; PDFs/samples committed
  as docs; test config deleted before merge.

## 7. Roadmap (one by one)

1. ✅ Kraken-fee reality check (inconclusive, tiny sample) → 2. ✅ Threshold tune
   (55 kept) → 3. ✅ Merged live → 4. ✅ Quant pack live (maker pricing, Z-gate,
   Kelly, 120B veto, ledger) → 5. ✅ Walk-forward (2/3 windows green, worst −0.02%)
   → 6. 🔄 Observe live fills/exits (now) → 7. External posting service
   (separate channel bot, exact-disclaimer assert, caption ≤1000 chars,
   fail-closed auth — needs your channel + Square API details) → 8. Binance-Square /
   Telegram auto-posting. (40d Kraken 4h bias-true download: impossible, cap ~8d.)
