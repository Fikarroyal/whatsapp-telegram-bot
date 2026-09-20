"""Inti logika bot (tidak bergantung platform).

Dipakai bersama oleh wa_bot.py (WhatsApp) dan bot.py (Telegram):
  handle(uid, teks) -> str          membalas satu pesan pengguna
  tick()            -> [(uid, str)] dipanggil tiap menit, mengembalikan pengingat yang harus dikirim
"""
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger("core")

DB_PATH = os.environ.get("BOT_DB", "botdata.db")
DEFAULT_CITY = "Yogyakarta"
DEFAULT_TZ = "Asia/Jakarta"
HTTP_TIMEOUT = 15
WINDOW = 5          # toleransi (menit) kalau tick terlambat
ALARM_REPEAT = 3    # alarm diulang maksimal 3x sampai pengguna membalas "bangun"
ALARM_INTERVAL = 3  # jeda (menit) antar ulangan alarm

PRAYERS = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]
NAMA = {"Fajr": "Subuh", "Dhuhr": "Dzuhur", "Asr": "Ashar", "Maghrib": "Maghrib", "Isha": "Isya"}
TZ_LABEL = {"Asia/Jakarta": "WIB", "Asia/Makassar": "WITA", "Asia/Jayapura": "WIT"}


# ---------- Waktu (bisa diganti saat pengujian) ----------
def utcnow():
    return datetime.now(timezone.utc)


def local_now(tz):
    return utcnow().astimezone(ZoneInfo(tz))


def tz_label(tz):
    return TZ_LABEL.get(tz, tz)


def to_min(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_window(now, hhmm, window=WINDOW):
    """True kalau jam sekarang berada 0..window menit setelah hhmm."""
    return 0 <= (now.hour * 60 + now.minute) - to_min(hhmm) < window


def fmt_delta(td):
    mins = max(0, -(-int(td.total_seconds()) // 60))  # dibulatkan ke atas
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h} jam {m} menit"
    return f"{h} jam" if h else f"{m} menit"


def extract_time(text, strict=False):
    """Ambil jam dari teks. strict=True butuh awalan '@' atau 'jam/pukul'.
    Mengembalikan (\"HH:MM\" atau None, teks tanpa bagian jam)."""
    pre = r"(?:@\s*|\b(?:jam|pukul)\s+)" + ("" if strict else "?")
    for pat, has_min in (
        (pre + r"(?<![\d:.])([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)", True),
        (pre + r"(?<![\d:.])([01]?\d|2[0-3])(?![\d:]|\.\d)", False),
    ):
        m = re.search(pat, text, re.I)
        if m:
            hh = int(m.group(1))
            mm = m.group(2) if has_min else "00"
            rest = re.sub(r"\s+", " ", text[: m.start()] + " " + text[m.end():]).strip()
            return f"{hh:02d}:{mm}", rest
    return None, text


# ---------- Database ----------
def run(sql, params=(), fetch=False):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if fetch else None
        last = cur.lastrowid
        conn.commit()
        return rows if fetch else last
    finally:
        conn.close()


def init_db():
    run("""CREATE TABLE IF NOT EXISTS users (
        uid TEXT PRIMARY KEY, city TEXT DEFAULT 'Yogyakarta', tz TEXT DEFAULT 'Asia/Jakarta',
        sholat_on INTEGER DEFAULT 0, sholat_lead INTEGER DEFAULT 15,
        tidur_time TEXT, alarm_time TEXT, cuaca_time TEXT, agenda_time TEXT,
        ring_left INTEGER DEFAULT 0, ring_next TEXT)""")
    run("""CREATE TABLE IF NOT EXISTS trx (
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, kind TEXT, amount INTEGER,
        note TEXT, category TEXT, created_at TEXT)""")
    run("""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, text TEXT, remind_time TEXT,
        due_date TEXT, repeat INTEGER DEFAULT 0, done_date TEXT)""")
    run("CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, ts TEXT)")


def user(uid):
    run("INSERT OR IGNORE INTO users(uid) VALUES (?)", (uid,))
    return run("SELECT * FROM users WHERE uid=?", (uid,), True)[0]


def set_user(uid, **fields):
    user(uid)
    cols = ", ".join(f"{k}=?" for k in fields)
    run(f"UPDATE users SET {cols} WHERE uid=?", (*fields.values(), uid))


def was_sent(key):
    return bool(run("SELECT 1 FROM sent WHERE key=?", (key,), True))


def once(key):
    """True hanya pada pemanggilan pertama untuk key ini (tahan restart)."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        cur = conn.execute("INSERT OR IGNORE INTO sent(key, ts) VALUES (?,?)", (key, utcnow().isoformat()))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def rupiah(n):
    return ("-" if n < 0 else "") + "Rp" + f"{abs(n):,}".replace(",", ".")


def selisih(n):
    return ("+" if n >= 0 else "") + rupiah(n)


# ---------- Jadwal sholat ----------
_prayer_cache = {}


def get_prayer_times(city, tz):
    """Jadwal sholat Kemenag (method 20) untuk tanggal lokal pengguna.
    Mengembalikan ({Fajr: 'HH:MM', ...}, nama_timezone)."""
    today = local_now(tz).date()
    key = (city.lower(), today)
    if key in _prayer_cache:
        return _prayer_cache[key]
    r = requests.get(
        f"https://api.aladhan.com/v1/timingsByCity/{today.strftime('%d-%m-%Y')}",
        params={"city": city, "country": "Indonesia", "method": 20},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()["data"]
    result = ({p: data["timings"][p][:5] for p in PRAYERS}, data["meta"]["timezone"])
    for old in [k for k in _prayer_cache if k[1] != today]:
        del _prayer_cache[old]
    _prayer_cache[key] = result
    return result


def next_prayer(timings, now):
    """(nama, datetime, besok?) — setelah Isya, Subuh besok (perkiraan dari jadwal hari ini)."""
    for p in PRAYERS:
        h, m = map(int, timings[p].split(":"))
        dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if dt > now:
            return p, dt, False
    h, m = map(int, timings["Fajr"].split(":"))
    return "Fajr", (now + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0), True


def info_berikutnya(timings, now):
    p, dt, besok = next_prayer(timings, now)
    tag = " besok" if besok else ""
    return f"{NAMA[p]}{tag} {dt.strftime('%H:%M')} ({fmt_delta(dt - now)} lagi)"


def cmd_sholat(uid, arg=""):
    u = user(uid)
    a = arg.lower().split()
    if a and a[0] in ("on", "off"):
        set_user(uid, sholat_on=1 if a[0] == "on" else 0)
        if a[0] == "off":
            return "Pengingat sholat dimatikan 🔕"
        lead = u["sholat_lead"]
        extra = (
            f"Pengingat awal {lead} menit sebelum waktu sholat (ubah: *sholat menit 10*, matikan: *sholat menit 0*)."
            if lead else "Pengingat awal (sebelum waktu sholat) nonaktif."
        )
        return f"Pengingat sholat aktif 🔔 untuk wilayah {u['city']}.\n{extra}"
    if a and a[0] in ("menit", "sebelum"):
        if len(a) < 2 or not a[1].isdigit():
            return "Contoh: *sholat menit 15* (0 = matikan, atau 5 sampai 60)"
        n = int(a[1])
        if n != 0 and not 5 <= n <= 60:
            return "Isi 0 (mati) atau 5 sampai 60 menit ya."
        set_user(uid, sholat_lead=n)
        return f"Pengingat awal diatur {n} menit sebelum sholat ✅" if n else "Pengingat awal dimatikan ✅"
    try:
        timings, tzname = get_prayer_times(u["city"], u["tz"])
    except Exception:
        logger.exception("gagal ambil jadwal sholat")
        return "Gagal mengambil jadwal sholat, coba lagi nanti ya."
    now = local_now(u["tz"])
    nxt = next_prayer(timings, now)[0]
    lines = [f"🕌 *Jadwal sholat {u['city']}* — {now.strftime('%d-%m-%Y')} ({tz_label(tzname)})"]
    for p in PRAYERS:
        lines.append(f"{'▶' if p == nxt else '•'} {NAMA[p]}: {timings[p]}")
    lines.append(f"\nSelanjutnya: {info_berikutnya(timings, now)}")
    return "\n".join(lines)


def cmd_kota(uid, name):
    if not name:
        return "Contoh: *kota Bandung*"
    u = user(uid)
    try:
        _, tzname = get_prayer_times(name, u["tz"])
    except Exception:
        return "Kota tidak ditemukan (atau layanan sedang gangguan). Coba nama kota/kabupaten lain."
    set_user(uid, city=name, tz=tzname)
    return f"Oke, kota diatur ke {name} ({tz_label(tzname)}) ✅"


def _f_sholat(u):
    if not u["sholat_on"]:
        return []
    uid = u["uid"]
    timings, _ = get_prayer_times(u["city"], u["tz"])
    now = local_now(u["tz"])
    today = now.date().isoformat()
    nm = now.hour * 60 + now.minute
    lead = u["sholat_lead"] or 0
    out = []
    for p in PRAYERS:
        t_m = to_min(timings[p])
        if lead and 0 <= nm - (t_m - lead) < WINDOW and once(f"{uid}|pre|{p}|{today}"):
            out.append((uid, f"⏳ {lead} menit lagi masuk waktu {NAMA[p]} ({timings[p]}) — wilayah {u['city']}. "
                             f"Siap-siap wudhu 💧"))
        if 0 <= nm - t_m < WINDOW and once(f"{uid}|sholat|{p}|{today}"):
            out.append((uid, f"🕌 Waktunya sholat {NAMA[p]} ({timings[p]}) untuk wilayah {u['city']}.\n"
                             f"▶ Selanjutnya: {info_berikutnya(timings, now)}"))
    return out


# ---------- Cuaca ----------
_geo_cache = {}
_weather_cache = {}
STORM = {95, 96, 99}
RAIN_HEAVY = {65, 67, 82}
RAIN_MILD = {51, 53, 55, 56, 57, 61, 63, 66, 80, 81}
FOG = {45, 48}
WMO = {
    0: "Cerah", 1: "Cerah berawan", 2: "Berawan sebagian", 3: "Mendung",
    45: "Berkabut", 48: "Berkabut", 51: "Gerimis ringan", 53: "Gerimis", 55: "Gerimis lebat",
    56: "Gerimis beku", 57: "Gerimis beku lebat", 61: "Hujan ringan", 63: "Hujan sedang",
    65: "Hujan lebat", 66: "Hujan beku", 67: "Hujan beku lebat",
    80: "Hujan lokal ringan", 81: "Hujan lokal sedang", 82: "Hujan lokal lebat",
    95: "Badai petir", 96: "Badai petir + es", 99: "Badai petir + es lebat",
}


def geocode(city):
    key = city.lower()
    if key in _geo_cache:
        return _geo_cache[key]
    place = None
    for extra in ({"countryCode": "ID"}, {}):  # utamakan Indonesia, lalu seluruh dunia
        r = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "id", **extra},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        res = r.json().get("results")
        if res:
            place = res[0]
            break
    if place:
        _geo_cache[key] = place
    return place


def get_weather(city):
    """(place, data) atau None kalau kota tidak ditemukan. Di-cache 15 menit."""
    bucket = int(utcnow().timestamp() // 900)
    key = (city.lower(), bucket)
    if key in _weather_cache:
        return _weather_cache[key]
    place = geocode(city)
    if not place:
        return None
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
            "daily": "precipitation_probability_max",
            "timezone": "auto",
        },
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    result = (place, r.json())
    for old in [k for k in _weather_cache if k[1] != bucket]:
        del _weather_cache[old]
    _weather_cache[key] = result
    return result


def nilai_cuaca(code, precip, feels, wind, prob):
    """Penilaian cuaca SAAT INI -> (level 0=baik/1=kurang baik/2=buruk, label, saran)."""
    level, saran = 0, []

    def naik(lv, tip):
        nonlocal level
        level = max(level, lv)
        saran.append(tip)

    if code in STORM:
        naik(2, "Badai petir: tunda aktivitas di luar dan jauhi pohon/tiang ⚡")
    if code in RAIN_HEAVY:
        naik(2, "Hujan lebat: sebaiknya berteduh, waspada genangan/banjir 🌧")
    if code in RAIN_MILD:
        naik(1, "Bawa payung atau jas hujan ☂️")
    if code in FOG:
        naik(1, "Jarak pandang terbatas, hati-hati berkendara 🌫")
    if precip and precip > 0 and code not in STORM | RAIN_HEAVY | RAIN_MILD:
        naik(1, "Sedang turun hujan, bawa payung ☂️")
    if wind >= 50:
        naik(2, "Angin sangat kencang, hindari area terbuka 💨")
    elif wind >= 30:
        naik(1, "Angin cukup kencang, hati-hati di jalan 💨")
    if feels >= 42:
        naik(2, "Panas ekstrem: hindari aktivitas di luar siang hari, banyak minum 🥵")
    elif feels >= 38:
        naik(1, "Terik: banyak minum dan pakai tabir surya 🧴")
    if level == 0 and prob is not None and prob >= 50:
        saran.append(f"Saat ini aman, tapi peluang hujan hari ini {prob}% — siapkan payung ☂️")
    if not saran:
        saran.append("Aman untuk beraktivitas di luar 👍")
    return level, ["✅ *Cuaca baik*", "⚠️ *Cuaca kurang baik*", "❌ *Cuaca buruk*"][level], saran


def cuaca_text(city, judul="Cuaca"):
    res = get_weather(city)
    if not res:
        return None
    place, data = res
    cur = data["current"]
    prob = ((data.get("daily") or {}).get("precipitation_probability_max") or [None])[0]
    level, label, saran = nilai_cuaca(
        cur["weather_code"], cur.get("precipitation") or 0,
        cur["apparent_temperature"], cur["wind_speed_10m"], prob,
    )
    lines = [
        f"🌤 *{judul} {place['name']}*" + (f", {place['admin1']}" if place.get("admin1") else ""),
        f"{WMO.get(cur['weather_code'], 'Kondisi tidak diketahui')} • {cur['temperature_2m']}°C "
        f"(terasa {cur['apparent_temperature']}°C)",
        f"💧 Kelembapan {cur['relative_humidity_2m']}% • 💨 Angin {cur['wind_speed_10m']} km/jam",
    ]
    if prob is not None:
        lines.append(f"☔ Peluang hujan hari ini {prob}%")
    lines += ["", label] + saran
    return "\n".join(lines)


def cmd_cuaca(uid, city):
    city = city or user(uid)["city"]
    try:
        text = cuaca_text(city)
    except Exception:
        logger.exception("gagal ambil cuaca")
        return "Gagal mengambil data cuaca, coba lagi nanti ya."
    return text or f"Kota '{city}' tidak ditemukan."


def _f_cuaca(u):
    if not u["cuaca_time"]:
        return []
    uid = u["uid"]
    now = local_now(u["tz"])
    key = f"{uid}|cuaca|{now.date().isoformat()}"
    if not in_window(now, u["cuaca_time"]) or was_sent(key):
        return []
    text = cuaca_text(u["city"], "Pengingat cuaca ☀️ —")  # kalau gagal, dicoba lagi menit berikutnya
    if text and once(key):
        return [(uid, text)]
    return []


# ---------- Keuangan ----------
KATEGORI_OUT = {
    "Makan & Minum": "makan minum kopi sarapan lunch dinner nasi bakso mie warteg snack jajan gofood grabfood resto cafe kafe".split(),
    "Transport": "bensin parkir tol ojek gojek grab bus krl kereta tiket transport angkot servis service".split(),
    "Belanja": "belanja baju sepatu shopee tokopedia lazada beli".split(),
    "Tagihan": "listrik air pulsa internet wifi kos kost sewa cicilan tagihan token bpjs pdam".split(),
    "Kesehatan": "obat dokter apotek klinik vitamin".split(),
    "Hiburan": "nonton bioskop game netflix spotify hobi liburan".split(),
    "Pendidikan": "buku kursus kuliah spp les sekolah".split(),
    "Sedekah": "sedekah infaq zakat donasi amal".split(),
}
KATEGORI_IN = {
    "Gaji": "gaji salary".split(),
    "Bonus": "bonus thr insentif".split(),
    "Usaha": "jual usaha untung omset dagang".split(),
}


def kategori(note, kind):
    tokens = set(re.findall(r"[a-z]+", note.lower()))
    for nama, kata in (KATEGORI_IN if kind == "in" else KATEGORI_OUT).items():
        if tokens & set(kata):
            return nama
    return "Lainnya"


def parse_amount(text):
    """'25000 makan', '25rb bensin', '1,5jt kos', 'Rp 12.500 kopi' -> (jumlah, catatan)"""
    m = re.match(r"^\s*(?:rp\.?\s*)?([\d.,]+)\s*(rb|ribu|k|jt|juta)?(?:\s+(.*))?$", text.strip(), re.I | re.S)
    if not m:
        return None
    num, suffix, note = m.groups()
    try:
        if suffix:
            mult = 1_000_000 if suffix.lower() in ("jt", "juta") else 1_000
            amount = int(round(float(num.replace(",", ".")) * mult))
        else:
            amount = int(re.sub(r"[.,]", "", num))
    except ValueError:
        return None
    if amount <= 0:
        return None
    return amount, (note or "").strip() or "Lainnya"


def sums(uid, prefix=None):
    sql, params = "SELECT kind, COALESCE(SUM(amount),0) AS s FROM trx WHERE uid=?", [uid]
    if prefix:
        sql += " AND created_at LIKE ?"
        params.append(prefix + "%")
    d = {r["kind"]: r["s"] for r in run(sql + " GROUP BY kind", params, True)}
    return d.get("in", 0), d.get("out", 0)


def saldo_total(uid):
    masuk, keluar = sums(uid)
    return masuk - keluar


def tambah_trx(uid, kind, text):
    parsed = parse_amount(text)
    if not parsed:
        return "Format: *masuk 5jt gaji* atau *keluar 25rb makan siang*\n(bisa juga 25rb, 1,5jt, Rp12.500)"
    amount, note = parsed
    now = local_now(user(uid)["tz"])
    kat = kategori(note, kind)
    tid = run(
        "INSERT INTO trx(uid, kind, amount, note, category, created_at) VALUES (?,?,?,?,?,?)",
        (uid, kind, amount, note, kat, now.strftime("%Y-%m-%d %H:%M:%S")),
    )
    judul = "💰 Pemasukan" if kind == "in" else "💸 Pengeluaran"
    return f"{judul} tercatat #{tid}\n{rupiah(amount)} — {note} ({kat})\nSaldo sekarang: {rupiah(saldo_total(uid))}"


def cmd_saldo(uid):
    now = local_now(user(uid)["tz"])
    b_in, b_out = sums(uid, now.strftime("%Y-%m"))
    return (f"💰 *Saldo kamu: {rupiah(saldo_total(uid))}*\n"
            f"Bulan ini: masuk {rupiah(b_in)} • keluar {rupiah(b_out)}")


def cmd_rekap(uid):
    now = local_now(user(uid)["tz"])
    hari, bulan = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    h_in, h_out = sums(uid, hari)
    b_in, b_out = sums(uid, bulan)
    lines = [
        "📊 *Rekap keuangan*",
        f"Hari ini: masuk {rupiah(h_in)} • keluar {rupiah(h_out)}",
        f"Bulan ini: masuk {rupiah(b_in)} • keluar {rupiah(b_out)} (selisih {selisih(b_in - b_out)})",
        f"💰 Saldo total: {rupiah(saldo_total(uid))}",
    ]
    top = run(
        "SELECT category, SUM(amount) AS s FROM trx WHERE uid=? AND kind='out' AND created_at LIKE ? "
        "GROUP BY category ORDER BY s DESC LIMIT 5", (uid, bulan + "%"), True)
    if top:
        lines.append("\nPengeluaran terbesar bulan ini:")
        lines += [f"• {r['category']}: {rupiah(r['s'])}" for r in top]
    return "\n".join(lines)


def cmd_riwayat(uid):
    rows = run("SELECT * FROM trx WHERE uid=? ORDER BY id DESC LIMIT 10", (uid,), True)
    if not rows:
        return "Belum ada catatan. Coba: *25rb makan siang* atau *+5jt gaji*"
    lines = ["🧾 *10 catatan terakhir*"]
    for r in rows:
        tanda = "➕" if r["kind"] == "in" else "➖"
        lines.append(f"#{r['id']} {r['created_at'][5:10]} {tanda} {rupiah(r['amount'])} {r['note']}")
    lines.append("\nHapus: *hapus <nomor>*")
    return "\n".join(lines)


def cmd_hapus(uid, arg):
    arg = arg.strip().lstrip("#")
    if not arg.isdigit():
        return "Contoh: *hapus 12* (nomor lihat di *riwayat*)"
    tid = int(arg)
    if not run("SELECT 1 FROM trx WHERE id=? AND uid=?", (tid, uid), True):
        return "Catatan tidak ditemukan."
    run("DELETE FROM trx WHERE id=? AND uid=?", (tid, uid))
    return f"Catatan #{tid} dihapus 🗑\nSaldo sekarang: {rupiah(saldo_total(uid))}"


# ---------- Tugas ----------
def tugas_tambah(uid, text, repeat=False):
    u = user(uid)
    t, text = extract_time(text, strict=True)
    besok = bool(re.search(r"\bbesok\b", text, re.I))
    text = re.sub(r"\s+", " ", re.sub(r"\bbesok\b", "", text, flags=re.I)).strip()
    if not text:
        return "Contoh: *tugas kirim laporan @14:00*\n*tugas harian minum obat @08:00*"
    if repeat and not t:
        return "Tugas harian butuh jam pengingat. Contoh: *tugas harian minum obat @08:00*"
    now = local_now(u["tz"])
    today = now.date()
    if besok:
        due = today + timedelta(days=1)
    elif t and not repeat and t <= now.strftime("%H:%M"):
        due = today + timedelta(days=1)  # jam sudah lewat hari ini -> besok
    else:
        due = today
    tid = run(
        "INSERT INTO tasks(uid, text, remind_time, due_date, repeat) VALUES (?,?,?,?,?)",
        (uid, text, t, due.isoformat(), 1 if repeat else 0),
    )
    if repeat:
        return f"🔁 Tugas harian #{tid} ditambahkan: {text}\nDiingatkan setiap hari jam {t} ({tz_label(u['tz'])})"
    hari = "besok" if due > today else "hari ini"
    rem = f"\n⏰ Pengingat {hari} jam {t}" if t else (f"\n📅 Untuk besok" if besok else "")
    return f"📌 Tugas #{tid} ditambahkan: {text}{rem}"


def daftar_tugas(uid, judul="📋 *Tugas kamu*", hanya_hari_ini=False):
    u = user(uid)
    today = local_now(u["tz"]).date().isoformat()
    rows = run(
        "SELECT * FROM tasks WHERE uid=? AND (repeat=1 OR done_date IS NULL) "
        "ORDER BY repeat DESC, due_date, remind_time IS NULL, remind_time, id", (uid,), True)
    lines = []
    for r in rows:
        if r["repeat"]:
            ok = " ✅" if r["done_date"] == today else ""
            lines.append(f"🔁 #{r['id']} {r['text']} ⏰{r['remind_time']}{ok}")
            continue
        if hanya_hari_ini and r["due_date"] > today:
            continue
        extra = f" ⏰{r['remind_time']}" if r["remind_time"] else ""
        if r["due_date"] > today:
            extra += " (besok)" if r["due_date"] == (local_now(u["tz"]).date() + timedelta(days=1)).isoformat() \
                else f" ({r['due_date']})"
        elif r["due_date"] < today:
            extra += " (terlewat)"
        lines.append(f"☐ #{r['id']} {r['text']}{extra}")
    if not lines:
        return None if hanya_hari_ini else "Belum ada tugas 🎉\nTambah: *tugas kirim laporan @14:00*"
    footer = "" if hanya_hari_ini else "\n\nSelesai: *selesai <nomor>* • Hapus: *tugas hapus <nomor>*"
    return judul + "\n" + "\n".join(lines) + footer


def _tugas_id(uid, arg):
    arg = arg.strip().lstrip("#")
    if not arg.isdigit():
        return None, None
    rows = run("SELECT * FROM tasks WHERE id=? AND uid=?", (int(arg), uid), True)
    return (int(arg), rows[0]) if rows else (int(arg), None)


def tugas_selesai(uid, arg):
    tid, t = _tugas_id(uid, arg)
    if tid is None:
        return "Contoh: *selesai 3* (nomor lihat di *tugas*)"
    if not t:
        return "Tugas tidak ditemukan."
    today = local_now(user(uid)["tz"]).date().isoformat()
    run("UPDATE tasks SET done_date=? WHERE id=? AND uid=?", (today, tid, uid))
    extra = " (muncul lagi besok)" if t["repeat"] else ""
    return f"✅ Tugas #{tid} selesai: {t['text']}{extra}"


def tugas_hapus(uid, arg):
    tid, t = _tugas_id(uid, arg)
    if tid is None:
        return "Contoh: *tugas hapus 3*"
    if not t:
        return "Tugas tidak ditemukan."
    run("DELETE FROM tasks WHERE id=? AND uid=?", (tid, uid))
    return f"Tugas #{tid} dihapus 🗑"


def cmd_tugas(uid, arg):
    a = arg.strip()
    if not a:
        return daftar_tugas(uid)
    first, _, rest = a.partition(" ")
    f = first.lower()
    if f in ("selesai", "done"):
        return tugas_selesai(uid, rest)
    if f in ("hapus", "del", "delete"):
        return tugas_hapus(uid, rest)
    if f == "harian":
        return tugas_tambah(uid, rest, repeat=True)
    if f in ("tambah", "baru", "add"):
        return tugas_tambah(uid, rest)
    return tugas_tambah(uid, a)


def _f_tugas(u):
    uid = u["uid"]
    now = local_now(u["tz"])
    today = now.date().isoformat()
    now_naive = now.replace(tzinfo=None)
    out = []
    for t in run("SELECT * FROM tasks WHERE uid=? AND remind_time IS NOT NULL", (uid,), True):
        if t["done_date"] == today or (not t["repeat"] and t["done_date"]):
            continue
        if t["repeat"]:
            fire = in_window(now, t["remind_time"], 60) and once(f"{uid}|task|{t['id']}|{today}")
        else:
            due = datetime.fromisoformat(f"{t['due_date']}T{t['remind_time']}")
            fire = now_naive >= due and once(f"{uid}|task|{t['id']}|once")
        if fire:
            out.append((uid, f"📌 *Pengingat tugas:* {t['text']}\nSudah selesai? Balas *selesai {t['id']}*"))
    return out


def _f_agenda(u):
    if not u["agenda_time"]:
        return []
    uid = u["uid"]
    now = local_now(u["tz"])
    if not in_window(now, u["agenda_time"]) or not once(f"{uid}|agenda|{now.date().isoformat()}"):
        return []
    teks = daftar_tugas(uid, "📋 *Agenda hari ini*", hanya_hari_ini=True)
    return [(uid, teks)] if teks else []


# ---------- Pengingat harian: tidur, alarm, cuaca, agenda ----------
DAILY = {
    "tidur": ("tidur_time", "Pengingat tidur", "22:00"),
    "alarm": ("alarm_time", "Alarm bangun pagi", "04:30"),
    "cuaca": ("cuaca_time", "Pengingat cuaca harian", "06:30"),
    "agenda": ("agenda_time", "Ringkasan tugas pagi", "07:00"),
}


def cmd_daily(uid, key, arg):
    field, label, default = DAILY[key]
    u = user(uid)
    a = arg.strip().lower()
    if not a:
        cur = u[field]
        return (f"{label}: " + (f"aktif setiap hari jam {cur} ({tz_label(u['tz'])})" if cur else "nonaktif")
                + f"\nAtur: *{key} on {default}* • Matikan: *{key} off*")
    if re.match(r"^(off|mati|matikan|stop)\b", a):
        set_user(uid, **{field: None})
        if key == "alarm":
            set_user(uid, ring_left=0, ring_next=None)
        return f"{label} dimatikan 🔕"
    rest = re.sub(r"^on\b", "", a).strip()
    if rest:
        t, _ = extract_time(rest)
        if not t:
            return f"Format jam tidak dikenali. Contoh: *{key} on {default}*"
    else:
        t = default
    set_user(uid, **{field: t})
    extra = "\nBalas *bangun* untuk mematikan alarm saat berbunyi." if key == "alarm" else ""
    return f"{label} aktif ✅ setiap hari jam {t} ({tz_label(u['tz'])}){extra}"


def _f_tidur(u):
    if not u["tidur_time"]:
        return []
    uid = u["uid"]
    now = local_now(u["tz"])
    if not in_window(now, u["tidur_time"]) or not once(f"{uid}|tidur|{now.date().isoformat()}"):
        return []
    msg = "😴 *Waktunya tidur!* Simpan HP, redupkan lampu, dan istirahat."
    if u["alarm_time"]:
        mins = (to_min(u["alarm_time"]) - (now.hour * 60 + now.minute)) % 1440
        h, m = divmod(mins, 60)
        msg += f"\nAlarm bangun jam {u['alarm_time']}: kamu punya ±{h} jam {m} menit untuk tidur."
        if mins < 6 * 60:
            msg += " Agak kurang, usahakan segera tidur 🙏"
    return [(uid, msg)]


def _f_alarm(u):
    if not u["alarm_time"] and not u["ring_left"]:
        return []
    uid = u["uid"]
    now = local_now(u["tz"])
    today = now.date().isoformat()
    if u["alarm_time"] and in_window(now, u["alarm_time"]) and once(f"{uid}|alarm|{today}"):
        nxt = utcnow() + timedelta(minutes=ALARM_INTERVAL)
        set_user(uid, ring_left=ALARM_REPEAT, ring_next=nxt.isoformat())
        return [(uid, f"⏰ *ALARM BANGUN PAGI!* Sudah jam {u['alarm_time']}. Ayo bangun! ☀️\n"
                      f"Balas *bangun* untuk mematikan alarm.")]
    if u["ring_left"] > 0 and u["ring_next"] and utcnow() >= datetime.fromisoformat(u["ring_next"]):
        left = u["ring_left"] - 1
        nxt = (utcnow() + timedelta(minutes=ALARM_INTERVAL)).isoformat() if left > 0 else None
        set_user(uid, ring_left=left, ring_next=nxt)
        return [(uid, f"⏰ *BANGUN!* (ulangan {ALARM_REPEAT - left}/{ALARM_REPEAT}) "
                      f"Balas *bangun* untuk mematikan alarm.")]
    return []


def cmd_bangun(uid):
    u = user(uid)
    ringing = (u["ring_left"] or 0) > 0
    set_user(uid, ring_left=0, ring_next=None)
    parts = ["☀️ *Selamat pagi!* Alarm dimatikan." if ringing else "☀️ *Selamat pagi!*"]
    try:
        timings, _ = get_prayer_times(u["city"], u["tz"])
        parts.append(f"🕌 Sholat berikutnya: {info_berikutnya(timings, local_now(u['tz']))}")
    except Exception:
        logger.exception("ringkasan pagi: sholat gagal")
    try:
        cuaca = cuaca_text(u["city"])
        if cuaca:
            parts.append(cuaca)
    except Exception:
        logger.exception("ringkasan pagi: cuaca gagal")
    tugas = daftar_tugas(uid, "📋 *Agenda hari ini*", hanya_hari_ini=True)
    if tugas:
        parts.append(tugas)
    return "\n\n".join(parts)


# ---------- Status & bantuan ----------
def cmd_status(uid):
    u = user(uid)
    def jam(v):
        return f"jam {v}" if v else "nonaktif"
    sholat = "aktif" if u["sholat_on"] else "nonaktif"
    if u["sholat_on"] and u["sholat_lead"]:
        sholat += f" (+ingatkan {u['sholat_lead']} menit sebelumnya)"
    return "\n".join([
        "⚙️ *Pengaturan kamu*",
        f"📍 Kota: {u['city']} ({tz_label(u['tz'])})",
        f"🕌 Pengingat sholat: {sholat}",
        f"🌤 Kabar cuaca harian: {jam(u['cuaca_time'])}",
        f"📋 Ringkasan tugas pagi: {jam(u['agenda_time'])}",
        f"😴 Pengingat tidur: {jam(u['tidur_time'])}",
        f"⏰ Alarm bangun: {jam(u['alarm_time'])}",
    ])


HELP = (
    "Halo! Aku asisten harianmu 👋\n\n"
    "🕌 *Sholat*\n"
    "• sholat — jadwal + sholat berikutnya\n"
    "• sholat on / off — pengingat tiap waktu sholat\n"
    "• sholat menit 15 — ingatkan 15 menit sebelum (0 = mati)\n"
    "• kota <nama> — ganti kota (default Yogyakarta)\n\n"
    "🌤 *Cuaca*\n"
    "• cuaca / cuaca <kota> — kondisi + penilaian baik/buruk\n"
    "• cuaca on 06:30 — kabar cuaca tiap pagi (cuaca off)\n\n"
    "💰 *Keuangan* (tercatat otomatis)\n"
    "• 25rb makan siang — pengeluaran\n"
    "• +5jt gaji — pemasukan\n"
    "• masuk / keluar <jumlah> <ket>\n"
    "• saldo • rekap • riwayat • hapus <nomor>\n\n"
    "📋 *Tugas*\n"
    "• tugas kirim laporan @14:00\n"
    "• tugas harian minum obat @08:00\n"
    "• tugas — daftar • selesai <nomor> • tugas hapus <nomor>\n"
    "• agenda on 07:00 — ringkasan tugas tiap pagi\n\n"
    "😴 *Tidur & bangun*\n"
    "• tidur on 22:00 (tidur off)\n"
    "• alarm on 04:30 (alarm off)\n"
    "• bangun — matikan alarm\n\n"
    "⚙️ status — lihat semua pengaturan"
)


# ---------- Router perintah ----------
def handle(uid, text):
    text = (text or "").strip()
    if not text:
        return HELP
    if text[0] == "+":
        return tambah_trx(uid, "in", text[1:])
    if text[0] == "-" and len(text) > 1 and text[1].isdigit():
        return tambah_trx(uid, "out", text[1:])
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().lstrip("/!").split("@")[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("menu", "help", "bantuan", "start", "halo", "hai", "hi", "p"):
        user(uid)
        return HELP
    if cmd in ("status", "pengaturan"):
        return cmd_status(uid)
    if cmd in ("sholat", "shalat", "jadwal"):
        return cmd_sholat(uid, arg)
    if cmd == "pengingat":  # kompatibel dengan versi lama: 'pengingat on'
        return cmd_sholat(uid, arg)
    if cmd == "kota":
        return cmd_kota(uid, arg)
    if cmd == "cuaca":
        if re.match(r"^(on|off)\b", arg, re.I) or re.match(r"^[@\s]*\d{1,2}[:.]\d{2}\s*$", arg):
            return cmd_daily(uid, "cuaca", arg)
        return cmd_cuaca(uid, arg)
    if cmd in ("masuk", "pemasukan", "income", "terima"):
        return tambah_trx(uid, "in", arg)
    if cmd in ("keluar", "catat", "pengeluaran", "bayar"):
        return tambah_trx(uid, "out", arg)
    if cmd == "saldo":
        return cmd_saldo(uid)
    if cmd == "rekap":
        return cmd_rekap(uid)
    if cmd == "riwayat":
        return cmd_riwayat(uid)
    if cmd == "hapus":
        return cmd_hapus(uid, arg)
    if cmd == "tugas":
        return cmd_tugas(uid, arg)
    if cmd in ("selesai", "done"):
        return tugas_selesai(uid, arg)
    if cmd == "agenda":
        return cmd_daily(uid, "agenda", arg) if arg else (daftar_tugas(uid) or "Belum ada tugas 🎉")
    if cmd in ("tidur", "alarm"):
        return cmd_daily(uid, cmd, arg)
    if cmd in ("bangun", "matikan"):
        return cmd_bangun(uid)
    if parse_amount(text):  # chat biasa diawali angka = pengeluaran
        return tambah_trx(uid, "out", text)
    return "Aku belum paham 😅 Ketik *menu* untuk lihat semua perintah."


# ---------- Penjadwal (dipanggil tiap menit oleh adapter) ----------
def tick():
    out = []
    run("DELETE FROM sent WHERE ts < ?", ((utcnow() - timedelta(days=3)).isoformat(),))
    for u in run("SELECT * FROM users", fetch=True):
        for fn in (_f_sholat, _f_tidur, _f_agenda, _f_cuaca, _f_tugas, _f_alarm):
            try:
                out.extend(fn(u))
            except Exception:
                logger.exception("tick %s gagal untuk %s", fn.__name__, u["uid"])
    return out
