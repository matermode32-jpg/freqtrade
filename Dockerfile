FROM freqtradeorg/freqtrade:stable

# Copy entrypoint that injects Render env vars into config placeholders at runtime.
# No real secrets are ever baked into the image.
COPY --chown=freqtrade:freqtrade entrypoint.sh /freqtrade/entrypoint.sh
COPY --chown=freqtrade:freqtrade user_data/config.json /freqtrade/config.json
COPY --chown=freqtrade:freqtrade user_data/ /freqtrade/user_data/

ENTRYPOINT ["python3", "/freqtrade/entrypoint.sh"]
# No --strategy flag: config.json is the single source of truth (a hard-coded
# CLI --strategy would silently override it on every deploy).
CMD ["trade", "--config", "config.json"]
