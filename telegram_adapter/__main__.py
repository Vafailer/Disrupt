import argparse
import asyncio
import logging
import sys

import httpx

from .bot import Bot
from .clients import CoreClient, RemoteFailure, TelegramClient
from .config import Settings
from .delivery import DeliveryJournal, DeliverySender
from .state import InstanceLock, OffsetStore


async def run():
    settings = Settings.from_env()
    with InstanceLock(settings.state_file):
        return await poll(settings)


async def poll(settings):
    state = OffsetStore(settings.state_file, settings.bot_id)
    async with (
        httpx.AsyncClient(
            timeout=httpx.Timeout(35, connect=5), follow_redirects=False, trust_env=False
        ) as telegram_http,
        httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=5), follow_redirects=False, trust_env=False
        ) as core_http,
    ):
        bot = Bot(
            settings,
            CoreClient(core_http, settings),
            TelegramClient(telegram_http, settings.bot_token),
            state,
        )
        sender = DeliverySender(
            settings, bot.core, bot.telegram,
            DeliveryJournal(settings.state_file.with_suffix(".delivery.json"), settings.bot_id),
        )
        return await serve(bot, sender)


async def poll_loop(bot):
    while True:
        try:
            await bot.poll_once()
        except RemoteFailure as error:
            logging.getLogger(__name__).warning("bot_paused code=%s", error.code)
            if error.fatal:
                return 1
            await asyncio.sleep(error.retry_after)


async def delivery_loop(sender):
    while True:
        try:
            worked = await sender.run_once()
            await asyncio.sleep(0.05 if worked else 1)
        except RemoteFailure as error:
            logging.getLogger(__name__).warning("delivery_paused code=%s", error.code)
            if error.fatal:
                return 1
            await asyncio.sleep(error.retry_after)


async def serve(bot, sender):
    tasks = [asyncio.create_task(poll_loop(bot)), asyncio.create_task(delivery_loop(sender))]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        return next(iter(done)).result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description="Beresta Telegram adapter (network disabled by default)")
    parser.add_argument("--demo", action="store_true", help="Run an offline HTTP-transport demonstration")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    # HTTPX info logs contain the Telegram token in the request URL.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    try:
        if args.demo:
            from .demo import demo

            return asyncio.run(demo())
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Never print exception text/tracebacks from transports or malformed private payloads.
        logging.getLogger(__name__).error("bot_stopped configuration_or_internal_error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
