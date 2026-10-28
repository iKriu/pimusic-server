import logging
import os
import sys


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    for noisy in ("httpx", "telegram.ext.Updater", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if len(sys.argv) > 1 and sys.argv[1] == "bootstrap":
        import asyncio

        from .bootstrap import bootstrap
        sys.exit(asyncio.run(bootstrap()))

    if len(sys.argv) > 1 and sys.argv[1] == "lyrics-check":  # разовая перепроверка текстов песен
        from .lyrics_fix import main as lyrics_check
        sys.exit(lyrics_check(sys.argv[2:]))

    from .bot import PiMusicBot
    from .config import CFG, scrub_secret_env
    scrub_secret_env()  # секреты уже в CFG — из окружения процесса их убираем
    if not CFG.telegram_token:
        sys.exit("TELEGRAM_BOT_TOKEN не задан")
    if not CFG.jellyfin_api_key():
        sys.exit("Нет API-ключа Jellyfin — сначала выполните bootstrap (install.sh делает это сам)")
    PiMusicBot().run()


if __name__ == "__main__":
    main()
