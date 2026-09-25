#!/usr/bin/env python3
"""Render entrypoint: inject env vars into config.json placeholders at runtime.
Reads ONLY environment variables (set in Render dashboard).
Never reads, opens, or prints real secret values to logs - only replaces in memory/file.
Placeholder .env.example names -> real API connection without exposing keys in git.
"""
import json
import os
import sys

CONFIG_SRC = "/freqtrade/config.json"
# Writable at runtime even when the image files are read-only / owned by another user.
CONFIG_DST = "/tmp/freqtrade.config.json"

PLACEHOLDER_MAP = {
    "your_token_here": os.environ.get("TELEGRAM_TOKEN", ""),
    "your_chat_id_here": os.environ.get("TELEGRAM_CHAT_ID", ""),
    "your_jwt_secret_here_make_it_long_32_plus_chars_1234567890": os.environ.get("API_SERVER_JWT_KEY", ""),
    "your_api_password_here": os.environ.get("API_SERVER_PASSWORD", ""),
    "your_ws_token_here": os.environ.get("API_SERVER_WS_TOKEN", ""),
}

def inject(node):
    if isinstance(node, dict):
        for k, v in node.items():
            # telegram.token / chat_id
            if k == "token" and v == "your_token_here" and PLACEHOLDER_MAP["your_token_here"]:
                node[k] = PLACEHOLDER_MAP["your_token_here"]
            elif k == "chat_id" and v == "your_chat_id_here" and PLACEHOLDER_MAP["your_chat_id_here"]:
                node[k] = PLACEHOLDER_MAP["your_chat_id_here"]
            elif k == "jwt_secret_key" and v == "your_jwt_secret_here_make_it_long_32_plus_chars_1234567890" and PLACEHOLDER_MAP["your_jwt_secret_here_make_it_long_32_plus_chars_1234567890"]:
                node[k] = PLACEHOLDER_MAP["your_jwt_secret_here_make_it_long_32_plus_chars_1234567890"]
            elif k == "password" and v == "your_api_password_here" and PLACEHOLDER_MAP["your_api_password_here"]:
                node[k] = PLACEHOLDER_MAP["your_api_password_here"]
            elif k == "ws_token" and v == "your_ws_token_here" and PLACEHOLDER_MAP["your_ws_token_here"]:
                node[k] = PLACEHOLDER_MAP["your_ws_token_here"]
            else:
                inject(v)
    elif isinstance(node, list):
        for item in node:
            inject(item)

def main():
    # Port: Render sets $PORT, freqtrade config uses api_server.listen_port
    port = os.environ.get("PORT", "")
    # Username override (non-secret, safe to log)
    api_user = os.environ.get("API_SERVER_USERNAME", "")

    with open(CONFIG_SRC, "r", encoding="utf-8") as f:
        config = json.load(f)

    inject(config)

    if port and str(port).isdigit():
        config.setdefault("api_server", {})["listen_port"] = int(port)
    if api_user:
        config.setdefault("api_server", {})["username"] = api_user

    # Binance keys for later live trading (empty in dry-run) - only inject if set
    ex_key = os.environ.get("EXCHANGE_KEY", "")
    ex_secret = os.environ.get("EXCHANGE_SECRET", "")
    if ex_key:
        config.setdefault("exchange", {})["key"] = ex_key
    if ex_secret:
        config.setdefault("exchange", {})["secret"] = ex_secret

    with open(CONFIG_DST, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)

    print("entrypoint: config placeholders injected from env (values not logged).", flush=True)

    # Hand over to freqtrade, forcing --config to the writable runtime copy.
    # CMD is: trade --config config.json --strategy SampleStrategy
    args = sys.argv[1:]
    out = []
    skip_next = False
    for i, a in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if a == "--config":
            out += ["--config", CONFIG_DST]
            skip_next = True
        elif a.startswith("--config="):
            out += ["--config=" + CONFIG_DST]
        else:
            out.append(a)
    if "--config" not in args and not any(a.startswith("--config") for a in args):
        out += ["--config", CONFIG_DST]
    os.execvp("freqtrade", ["freqtrade"] + out)

if __name__ == "__main__":
    main()
