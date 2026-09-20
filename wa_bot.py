"""Bot WhatsApp (WhatsApp Cloud API resmi dari Meta). Semua logika ada di core.py.

Environment variable:
    WA_TOKEN          access token Meta (WhatsApp > API Setup)
    WA_PHONE_ID       Phone Number ID
    WA_VERIFY_TOKEN   string bebas buatanmu, dipakai saat mendaftarkan webhook
  Opsional:
    WA_APP_SECRET     App Secret (Settings > Basic) -> verifikasi tanda tangan webhook (disarankan)
    WA_TEMPLATE_NAME  nama template pesan untuk pengingat di luar jendela 24 jam
    WA_TEMPLATE_LANG  kode bahasa template (default: id)
    WA_GRAPH_VERSION  versi Graph API (default: v23.0)
    PORT              port server (default 5000)

Jalankan:  python wa_bot.py   (satu proses saja)
"""
import hashlib
import hmac
import logging
import os
import re
import threading
import time

import requests
from flask import Flask, request

import core

WA_TOKEN = os.environ.get("WA_TOKEN")
WA_PHONE_ID = os.environ.get("WA_PHONE_ID")
VERIFY_TOKEN = os.environ.get("WA_VERIFY_TOKEN", "")
APP_SECRET = os.environ.get("WA_APP_SECRET")
TEMPLATE_NAME = os.environ.get("WA_TEMPLATE_NAME")
TEMPLATE_LANG = os.environ.get("WA_TEMPLATE_LANG", "id")
GRAPH_VERSION = os.environ.get("WA_GRAPH_VERSION", "v23.0")
REENGAGEMENT_ERROR = 131047  # kode error Meta: lebih dari 24 jam sejak pengguna terakhir membalas

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger("wa_bot")


# ---------- Kirim pesan ----------
def _post(payload):
    return requests.post(
        f"https://graph.facebook.com/{GRAPH_VERSION}/{WA_PHONE_ID}/messages",
        headers={"Authorization": f"Bearer {WA_TOKEN}"},
        json={"messaging_product": "whatsapp", "to": payload.pop("to"), **payload},
        timeout=15,
    )


def _error_code(resp):
    try:
        return resp.json().get("error", {}).get("code")
    except ValueError:
        return None


def kirim(to, text):
    """Kirim teks biasa. Kalau ditolak karena sudah >24 jam sejak pengguna terakhir membalas
    dan WA_TEMPLATE_NAME diisi, kirim ulang lewat template."""
    try:
        r = _post({"to": to, "type": "text", "text": {"body": text}})
        if r.ok:
            return True
        code = _error_code(r)
        if code == REENGAGEMENT_ERROR and TEMPLATE_NAME:
            body = re.sub(r"\s+", " ", text.replace("\n", " | ")).strip()[:1000]  # template: tanpa baris baru
            r2 = _post({
                "to": to, "type": "template",
                "template": {
                    "name": TEMPLATE_NAME, "language": {"code": TEMPLATE_LANG},
                    "components": [{"type": "body", "parameters": [{"type": "text", "text": body}]}],
                },
            })
            if r2.ok:
                return True
            logger.error("Kirim template gagal (%s): %s", r2.status_code, r2.text)
            return False
        logger.error("Kirim WA gagal (%s): %s", r.status_code, r.text)
    except requests.RequestException:
        logger.exception("Kirim WA error")
    return False


# ---------- Webhook ----------
app = Flask(__name__)
_seen = set()  # id pesan yang sudah diproses (Meta kadang mengirim ulang)


def process(msg):
    uid = msg["from"]
    try:
        if msg.get("type") != "text":
            kirim(uid, "Aku baru bisa membaca pesan teks 🙏 Ketik *menu* untuk bantuan.")
            return
        kirim(uid, core.handle(uid, msg["text"]["body"]))
    except Exception:
        logger.exception("gagal memproses pesan")
        kirim(uid, "Maaf, ada error di sisiku 😅 Coba lagi ya.")


@app.get("/webhook")
def verify():
    if (
        VERIFY_TOKEN
        and request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == VERIFY_TOKEN
    ):
        return request.args.get("hub.challenge", ""), 200
    return "Forbidden", 403


@app.post("/webhook")
def incoming():
    if APP_SECRET:
        sig = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(APP_SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return "Bad signature", 403
    data = request.get_json(silent=True) or {}
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                mid = msg.get("id")
                if mid in _seen:
                    continue
                if len(_seen) > 2000:
                    _seen.clear()
                _seen.add(mid)
                # diproses di thread terpisah supaya webhook langsung membalas 200 ke Meta
                threading.Thread(target=process, args=(msg,), daemon=True).start()
    return "ok", 200


@app.get("/")
def index():
    return "Bot WhatsApp aktif ✅"


# ---------- Penjadwal pengingat ----------
def kirim_pengingat():
    for uid, text in core.tick():
        kirim(uid, text)


def reminder_loop():
    while True:
        try:
            kirim_pengingat()
        except Exception:
            logger.exception("reminder_loop error")
        time.sleep(max(1, 60 - (time.time() % 60)))  # tepat di awal setiap menit


core.init_db()

if __name__ == "__main__":
    if not (WA_TOKEN and WA_PHONE_ID and VERIFY_TOKEN):
        raise SystemExit("Set dulu WA_TOKEN, WA_PHONE_ID, dan WA_VERIFY_TOKEN.")
    if not APP_SECRET:
        logger.warning("WA_APP_SECRET belum diisi: tanda tangan webhook tidak diverifikasi.")
    threading.Thread(target=reminder_loop, daemon=True).start()
    logger.info("Bot WhatsApp berjalan...")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
