"""Explicit operator reconciliation; never sends user messages or creates charges."""
import asyncio
import os
from dotenv import load_dotenv
from aiogram import Bot
from balans.service import Service
from balans.star_reconciliation import reconcile_stars

async def main():
    load_dotenv();service=Service(os.environ['DATABASE_URL'])
    try:
        async with Bot(os.environ['BOT_TOKEN']) as bot:print(await reconcile_stars(bot,service))
    finally:service.close()

if __name__=='__main__':asyncio.run(main())
