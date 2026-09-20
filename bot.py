"""Bot Telegram. Semua logika ada di core.py (sama persis dengan versi WhatsApp).

Jalankan:
    pip install -r requirements-telegram.txt
    export TELEGRAM_TOKEN="token-dari-BotFather"   # Windows: set TELEGRAM_TOKEN=...
    python bot.py
"""
import asyncio
import html
import logging
import os
import re

from telegram.ext import Application, ContextTypes, MessageHandler, filters

import core

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger("bot")


def to_html(text):
    """Ubah *tebal* gaya WhatsApp menjadi <b>tebal</b> untuk Telegram."""
    return re.sub(r"\*(.+?)\*", r"<b>\1</b>", html.escape(text, quote=False))


async def on_text(update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_chat.id)
    try:
        reply = await asyncio.to_thread(core.handle, uid, update.message.text or "")
    except Exception:
        logger.exception("gagal memproses pesan")
        reply = "Maaf, ada error di sisiku 😅 Coba lagi ya."
    await update.message.reply_text(to_html(reply), parse_mode="HTML")


async def job_tick(context: ContextTypes.DEFAULT_TYPE):
    for uid, text in await asyncio.to_thread(core.tick):
        try:
            await context.bot.send_message(int(uid), to_html(text), parse_mode="HTML")
        except Exception:
            logger.exception("gagal kirim pengingat ke %s", uid)


def build_app(token):
    app = Application.builder().token(token).build()
    # semua teks (termasuk /perintah) diteruskan ke core.handle
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, on_text))
    app.job_queue.run_repeating(job_tick, interval=60, first=10)
    return app


def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit("Set dulu environment variable TELEGRAM_TOKEN (token dari @BotFather).")
    core.init_db()
    logger.info("Bot Telegram berjalan...")
    build_app(token).run_polling()


if __name__ == "__main__":
    main()
