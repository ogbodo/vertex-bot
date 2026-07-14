"""Send a one-off test ping to confirm the v2 Telegram bot is wired up.

  .venv/bin/python scripts/test_telegram.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vertex.config import load_config
from vertex import notify


def main():
    cfg = load_config()
    sec = cfg.get("secrets", {})
    token, chat = sec.get("telegram_token"), sec.get("telegram_chat_id")
    if not token or not chat:
        print("No TELEGRAM_TOKEN / TELEGRAM_CHAT_ID in .env — add them first (see .env.example).")
        return
    ok = notify.send_message(token, chat,
                             "✅ <b>Eshu Forex Trader</b> Telegram is connected. Reports will arrive here.")
    print("sent ✓" if ok else "send FAILED — check the token/chat id (and that you've messaged the bot once).")


if __name__ == "__main__":
    main()
