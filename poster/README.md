# Poster channel bot (Telegram live, Binance Square stubbed).
# Public highlights only: curated posts, mandatory paper disclaimer.
# Secrets via env (Render dashboard), never committed:
#   CHANNEL_BOT_TOKEN - separate bot, channel admin, post rights only
#   CHANNEL_ID        - e.g. @your_channel or -100xxxxxxxxxx
#   PUBLIC_MIN_SCORE  - entry posts need tag score >= this (default 70)
#   PUBLIC_MAX_PER_DAY- throttle cap (default 10)
#   ENABLE_BINANCE_SQUARE - MUST stay false until Square is wired (default false)
#   BINANCE_SQUARE_API_KEY - placeholder only, UNUSED while disabled
