"""Uji otomatis bot (tanpa internet, tanpa token). Jalankan:  python test_bot.py

Cara kerja: jam disimulasikan (tiap menit core.tick() dipanggil) dan API Aladhan/Open-Meteo/Meta
diganti respons palsu yang bentuknya sama dengan respons asli.
"""
import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import requests

_tmp = tempfile.mkdtemp()
os.environ.update(
    BOT_DB=os.path.join(_tmp, "test.db"), WA_TOKEN="tok", WA_PHONE_ID="111",
    WA_VERIFY_TOKEN="rahasia", WA_APP_SECRET="s3cret", WA_TEMPLATE_NAME="pengingat_bot",
)
import core  # noqa: E402
import wa_bot  # noqa: E402

try:
    import bot as tg_bot  # noqa: E402
    HAS_TG = True
except ImportError:
    HAS_TG = False

WIB = "Asia/Jakarta"


# ---------- Jam palsu ----------
class CLOCK:
    now = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)


core.utcnow = lambda: CLOCK.now


def at(y, mo, d, h, mi, tz=WIB):
    CLOCK.now = datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def hm(dt):
    return dt.strftime("%H:%M")


# ---------- API palsu (bentuk sama dengan respons asli) ----------
class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status
        self.text = json.dumps(data)

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


CALLS, WX = [], {}
PRAYER = {
    "default": ({"Fajr": "04:22", "Dhuhr": "11:46", "Asr": "15:03", "Maghrib": "17:52", "Isha": "19:03"}, WIB),
    "makassar": ({"Fajr": "04:40", "Dhuhr": "12:00", "Asr": "15:15", "Maghrib": "18:05", "Isha": "19:15"}, "Asia/Makassar"),
}


def fake_get(url, params=None, timeout=None):
    CALLS.append(url)
    if "aladhan.com" in url:
        if WX.get("aladhan_fail"):
            raise requests.ConnectionError("aladhan down")
        city = params["city"].lower()
        if city == "kotangawur":
            return Resp({"code": 400, "status": "BAD_REQUEST", "data": "Please specify a valid city and country."}, 400)
        t, tz = PRAYER.get(city, PRAYER["default"])
        extra = {"Sunrise": "05:36", "Sunset": t["Maghrib"], "Imsak": "04:12", "Midnight": "23:37"}
        return Resp({"code": 200, "status": "OK", "data": {"timings": {**t, **extra}, "meta": {"timezone": tz}}})
    if "geocoding" in url:
        if params["name"].lower() == "nowhere":
            return Resp({"generationtime_ms": 0.4})  # tanpa kunci "results" seperti API asli
        return Resp({"results": [{"name": params["name"].title(), "latitude": -7.8, "longitude": 110.36,
                                  "admin1": "Daerah Istimewa Yogyakarta", "country_code": "ID"}]})
    if "api.open-meteo.com" in url:
        if WX.get("weather_fail"):
            raise requests.ConnectionError("open-meteo down")
        return Resp({
            "current": {"temperature_2m": WX["temp"], "apparent_temperature": WX["feels"],
                        "relative_humidity_2m": WX["hum"], "precipitation": WX["precip"],
                        "weather_code": WX["code"], "wind_speed_10m": WX["wind"]},
            "daily": {"precipitation_probability_max": [WX["prob"]]},
        })
    raise AssertionError("URL tak terduga: " + url)


class Base(unittest.TestCase):
    def setUp(self):
        if os.path.exists(core.DB_PATH):
            os.remove(core.DB_PATH)
        core.init_db()
        for c in (core._prayer_cache, core._geo_cache, core._weather_cache):
            c.clear()
        CALLS.clear()
        WX.clear()
        WX.update(code=1, temp=30.0, feels=33.0, hum=70, wind=8.0, precip=0.0, prob=20)
        at(2026, 9, 20, 6, 0)
        p = mock.patch("core.requests.get", fake_get)
        p.start()
        self.addCleanup(p.stop)

    def sim(self, y, mo, d, h, mi, minutes, tz=WIB):
        """Jalankan tick tiap menit -> [(waktu_lokal, uid, teks)]"""
        start = datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
        out = []
        for i in range(minutes):
            CLOCK.now = start + timedelta(minutes=i)
            for uid, text in core.tick():
                out.append((CLOCK.now.astimezone(ZoneInfo(tz)), uid, text))
        return out

    def aladhan_calls(self):
        return [c for c in CALLS if "aladhan" in c]


# ================= Parser =================
class TestParser(Base):
    def test_amount(self):
        ok = {
            "25000 makan siang": (25000, "makan siang"), "25rb bensin": (25000, "bensin"),
            "1,5jt kos": (1500000, "kos"), "Rp 12.500 kopi": (12500, "kopi"), "25.000": (25000, "Lainnya"),
            "10k parkir": (10000, "parkir"), "2 juta gaji": (2000000, "gaji"), "25 kopi susu": (25, "kopi susu"),
        }
        for text, exp in ok.items():
            self.assertEqual(core.parse_amount(text), exp, text)
        for bad in ["halo", "0 x", "", "rp", "..."]:
            self.assertIsNone(core.parse_amount(bad), bad)

    def test_extract_time(self):
        self.assertEqual(core.extract_time("kirim laporan @14:00", strict=True), ("14:00", "kirim laporan"))
        self.assertEqual(core.extract_time("rapat jam 8", strict=True), ("08:00", "rapat"))
        self.assertEqual(core.extract_time("rapat pukul 9.30 ya", strict=True), ("09:30", "rapat ya"))
        self.assertEqual(core.extract_time("beli beras 2.5 kg", strict=True)[0], None)  # tanpa awalan = bukan jam
        self.assertEqual(core.extract_time("on 04:30")[0], "04:30")
        self.assertEqual(core.extract_time("4.15")[0], "04:15")
        for bad in ["25:00", "24:00", "12:75", "pagi"]:
            self.assertIsNone(core.extract_time(bad)[0], bad)

    def test_category(self):
        self.assertEqual(core.kategori("bensin motor", "out"), "Transport")
        self.assertEqual(core.kategori("bayar kos", "out"), "Tagihan")
        self.assertEqual(core.kategori("bersih rumah", "out"), "Lainnya")  # 'rs' tidak salah cocok
        self.assertEqual(core.kategori("gaji september", "in"), "Gaji")


# ================= Keuangan =================
class TestFinance(Base):
    def test_full_flow(self):
        h = core.handle
        r = h("A", "masuk 5jt gaji")
        self.assertIn("Rp5.000.000", r); self.assertIn("(Gaji)", r); self.assertIn("Saldo sekarang: Rp5.000.000", r)
        r = h("A", "25rb makan siang")   # chat biasa -> pengeluaran
        self.assertIn("(Makan & Minum)", r); self.assertIn("Saldo sekarang: Rp4.975.000", r)
        self.assertIn("(Bonus)", h("A", "+150k bonus"))
        self.assertIn("(Transport)", h("A", "-10k parkir"))
        self.assertIn("(Tagihan)", h("A", "keluar 1,5jt bayar kos"))
        self.assertIn("Rp3.615.000", h("A", "saldo"))
        rekap = h("A", "rekap")
        self.assertIn("Bulan ini: masuk Rp5.150.000 • keluar Rp1.535.000", rekap)
        self.assertIn("selisih +Rp3.615.000", rekap)
        self.assertLess(rekap.index("Tagihan: Rp1.500.000"), rekap.index("Makan & Minum: Rp25.000"))
        self.assertIn("#4", h("A", "riwayat"))
        self.assertIn("Saldo sekarang: Rp3.625.000", h("A", "hapus 4"))
        self.assertIn("tidak ditemukan", h("A", "hapus 4"))

    def test_users_are_isolated_and_negative_balance(self):
        core.handle("A", "masuk 1jt gaji")
        self.assertIn("Saldo sekarang: -Rp10.000", core.handle("B", "keluar 10rb kopi"))
        self.assertIn("Rp1.000.000", core.handle("A", "saldo"))
        self.assertIn("tidak ditemukan", core.handle("B", "hapus 1"))  # tidak bisa hapus milik A
        self.assertIn("Rp1.000.000", core.handle("A", "saldo"))

    def test_day_and_month_rollover(self):
        core.handle("A", "masuk 5jt gaji")
        core.handle("A", "keluar 20rb makan")
        at(2026, 9, 21, 9, 0)
        r = core.handle("A", "rekap")
        self.assertIn("Hari ini: masuk Rp0 • keluar Rp0", r); self.assertIn("Bulan ini: masuk Rp5.000.000", r)
        at(2026, 10, 1, 9, 0)
        r = core.handle("A", "rekap")
        self.assertIn("Bulan ini: masuk Rp0 • keluar Rp0", r); self.assertIn("Saldo total: Rp4.980.000", r)

    def test_bad_input(self):
        self.assertIn("Format", core.handle("A", "masuk"))
        self.assertIn("Format", core.handle("A", "keluar abc"))
        self.assertIn("Belum ada catatan", core.handle("A", "riwayat"))


# ================= Sholat =================
class TestSholat(Base):
    def test_schedule_and_next(self):
        at(2026, 9, 20, 10, 0)
        r = core.handle("A", "sholat")
        self.assertIn("Jadwal sholat Yogyakarta", r); self.assertIn("▶ Dzuhur: 11:46", r)
        self.assertIn("Selanjutnya: Dzuhur 11:46 (1 jam 46 menit lagi)", r)
        at(2026, 9, 20, 20, 0)
        r = core.handle("A", "SHOLAT")
        self.assertIn("▶ Subuh: 04:22", r); self.assertIn("Subuh besok 04:22 (8 jam 22 menit lagi)", r)

    def test_uses_local_date_not_utc(self):
        at(2026, 9, 20, 0, 30)  # 17:30 UTC tanggal 19
        core.handle("A", "sholat")
        self.assertTrue(self.aladhan_calls()[0].endswith("/20-09-2026"), self.aladhan_calls())

    def test_reminders_full_day(self):
        core.handle("A", "sholat on")
        msgs = self.sim(2026, 9, 20, 0, 0, 1440)
        got = [(hm(t), txt) for t, _, txt in msgs]
        self.assertEqual([g[0] for g in got],
                         ["04:07", "04:22", "11:31", "11:46", "14:48", "15:03", "17:37", "17:52", "18:48", "19:03"])
        self.assertIn("15 menit lagi masuk waktu Dzuhur (11:46)", got[2][1])
        self.assertIn("Waktunya sholat Subuh (04:22)", got[1][1])
        self.assertIn("Selanjutnya: Dzuhur 11:46 (7 jam 24 menit lagi)", got[1][1])
        self.assertIn("Selanjutnya: Ashar 15:03 (3 jam 17 menit lagi)", got[3][1])
        self.assertIn("Selanjutnya: Subuh besok 04:22 (9 jam 19 menit lagi)", got[9][1])
        self.assertEqual(len(self.aladhan_calls()), 1)  # jadwal di-cache seharian
        again = self.sim(2026, 9, 21, 0, 0, 300)         # hari berikutnya: jadwal diambil ulang
        self.assertEqual([hm(t) for t, _, _ in again], ["04:07", "04:22"])
        self.assertEqual(len(self.aladhan_calls()), 2)

    def test_no_duplicate_after_restart_and_lateness(self):
        core.handle("A", "sholat on")
        core.handle("A", "sholat menit 0")
        at(2026, 9, 20, 11, 46)
        self.assertEqual(len(core.tick()), 1)
        self.assertEqual(core.tick(), [])                      # tick ganda di menit yang sama
        core._prayer_cache.clear()                             # simulasi bot restart
        at(2026, 9, 20, 11, 48)
        self.assertEqual(core.tick(), [])                      # tidak kirim ulang
        core.handle("B", "sholat on"); core.handle("B", "sholat menit 0")
        at(2026, 9, 20, 15, 6)                                 # bot baru menyala 3 menit setelah Ashar
        self.assertEqual([u for u, _ in core.tick()], ["A", "B"])   # masih dalam toleransi -> tetap terkirim
        at(2026, 9, 20, 17, 59)                                # terlambat > 5 menit -> dilewati
        self.assertEqual(core.tick(), [])

    def test_lead_settings(self):
        core.handle("A", "sholat on")
        self.assertIn("Isi 0", core.handle("A", "sholat menit 90"))
        self.assertIn("Contoh", core.handle("A", "sholat menit abc"))
        self.assertIn("10 menit", core.handle("A", "sholat menit 10"))
        self.assertEqual(hm(self.sim(2026, 9, 20, 11, 0, 60)[0][0]), "11:36")
        core.handle("A", "sholat menit 0")
        self.assertEqual([hm(t) for t, _, _ in self.sim(2026, 9, 21, 11, 0, 60)], ["11:46"])
        core.handle("A", "pengingat off")                      # alias versi lama
        self.assertEqual(self.sim(2026, 9, 22, 11, 0, 60), [])

    def test_city_and_timezone(self):
        r = core.handle("M", "kota Makassar")
        self.assertIn("WITA", r); self.assertEqual(core.user("M")["tz"], "Asia/Makassar")
        self.assertIn("tidak ditemukan", core.handle("M", "kota kotangawur"))
        self.assertEqual(core.user("M")["city"], "Makassar")   # tidak berubah karena kota salah
        core.handle("M", "sholat on"); core.handle("M", "sholat menit 0")
        got = self.sim(2026, 9, 20, 0, 0, 1440, tz="Asia/Makassar")
        self.assertEqual([hm(t) for t, _, _ in got], ["04:40", "12:00", "15:15", "18:05", "19:15"])

    def test_timezone_alarm_wita_vs_wib(self):
        core.handle("M", "kota Makassar"); core.handle("M", "alarm on 05:00")
        core.handle("J", "alarm on 05:00")
        got = self.sim(2026, 9, 19, 20, 55, 200, tz="UTC")
        first = {u: hm(t) for t, u, txt in got if "ALARM BANGUN PAGI" in txt}
        self.assertEqual(first, {"M": "21:00", "J": "22:00"})  # 05:00 WITA = 21:00 UTC, 05:00 WIB = 22:00 UTC


# ================= Cuaca =================
class TestCuaca(Base):
    def verdict(self, **wx):
        WX.update(wx)
        core._weather_cache.clear()
        return core.handle("A", "cuaca")

    def test_good_and_advice(self):
        r = self.verdict()
        self.assertIn("✅ *Cuaca baik*", r); self.assertIn("Aman untuk beraktivitas", r); self.assertIn("Cerah berawan", r)
        r = self.verdict(prob=70)
        self.assertIn("✅ *Cuaca baik*", r); self.assertIn("peluang hujan hari ini 70%", r)

    def test_bad_conditions(self):
        cases = [
            (dict(code=53), "⚠️ *Cuaca kurang baik*", "payung"),
            (dict(code=45), "⚠️ *Cuaca kurang baik*", "Jarak pandang"),
            (dict(code=1, wind=35.0), "⚠️ *Cuaca kurang baik*", "Angin cukup kencang"),
            (dict(code=1, feels=39.0), "⚠️ *Cuaca kurang baik*", "Terik"),
            (dict(code=1, precip=0.4), "⚠️ *Cuaca kurang baik*", "Sedang turun hujan"),
            (dict(code=95), "❌ *Cuaca buruk*", "Badai petir"),
            (dict(code=65), "❌ *Cuaca buruk*", "Hujan lebat"),
            (dict(code=1, wind=55.0), "❌ *Cuaca buruk*", "sangat kencang"),
            (dict(code=1, feels=43.0), "❌ *Cuaca buruk*", "Panas ekstrem"),
        ]
        for wx, label, tip in cases:
            base = dict(code=1, feels=33.0, wind=8.0, precip=0.0, prob=20)
            base.update(wx)
            r = self.verdict(**base)
            self.assertIn(label, r, wx); self.assertIn(tip, r, wx)

    def test_unknown_city_other_city_and_errors(self):
        self.assertIn("tidak ditemukan", core.handle("A", "cuaca nowhere"))
        self.assertIn("Bandung", core.handle("A", "cuaca Bandung"))
        WX["weather_fail"] = True; core._weather_cache.clear()
        with self.assertLogs("core", level="ERROR"):
            self.assertIn("Gagal", core.handle("A", "cuaca"))

    def test_cache_15_minutes(self):
        forecast = lambda: len([c for c in CALLS if "api.open-meteo.com/v1/forecast" in c])
        geocode = lambda: len([c for c in CALLS if "geocoding-api.open-meteo.com" in c])
        core.handle("A", "cuaca"); core.handle("A", "cuaca")
        self.assertEqual((forecast(), geocode()), (1, 1))      # panggilan kedua dari cache
        CLOCK.now += timedelta(minutes=16)
        core.handle("A", "cuaca")
        self.assertEqual((forecast(), geocode()), (2, 1))      # cuaca diperbarui, geocoding tetap cache

    def test_daily_reminder(self):
        self.assertIn("aktif ✅ setiap hari jam 06:30 (WIB)", core.handle("A", "cuaca on 06:30"))
        WX.update(code=95)
        got = self.sim(2026, 9, 20, 6, 0, 120)
        self.assertEqual([hm(t) for t, _, _ in got], ["06:30"])
        self.assertIn("Pengingat cuaca", got[0][2]); self.assertIn("❌ *Cuaca buruk*", got[0][2])
        self.assertEqual([hm(t) for t, _, _ in self.sim(2026, 9, 21, 6, 0, 120)], ["06:30"])  # setiap hari
        core.handle("A", "cuaca off")
        self.assertEqual(self.sim(2026, 9, 22, 6, 0, 120), [])
        self.assertIn("jam 07:15", core.handle("A", "cuaca 07:15"))  # bentuk singkat

    def test_daily_reminder_retries_when_api_down(self):
        core.handle("A", "cuaca on 06:30")
        WX["weather_fail"] = True
        with self.assertLogs("core", level="ERROR"):
            self.assertEqual(self.sim(2026, 9, 20, 6, 29, 3), [])   # 06:29, 06:30, 06:31 gagal
        WX["weather_fail"] = False
        got = self.sim(2026, 9, 20, 6, 32, 5)                        # pulih -> terkirim sekali
        self.assertEqual([hm(t) for t, _, _ in got], ["06:32"])


# ================= Tugas =================
class TestTugas(Base):
    def test_one_off_reminder_and_done(self):
        at(2026, 9, 20, 9, 0)
        self.assertIn("hari ini jam 14:00", core.handle("A", "tugas kirim laporan @14:00"))
        self.assertIn("Tugas #2", core.handle("A", "tugas beli beras"))
        self.assertIn("besok jam 08:00", core.handle("A", "tugas lapor bos @08:00"))   # jam sudah lewat
        daftar = core.handle("A", "tugas")
        self.assertIn("☐ #1 kirim laporan ⏰14:00", daftar); self.assertIn("☐ #2 beli beras", daftar)
        self.assertIn("(besok)", daftar)
        got = self.sim(2026, 9, 20, 9, 0, 900)
        self.assertEqual([(hm(t), "kirim laporan" in x) for t, _, x in got], [("14:00", True)])
        self.assertIn("Balas *selesai 1*", got[0][2])
        self.assertIn("selesai", core.handle("A", "selesai 1"))
        self.assertNotIn("kirim laporan", core.handle("A", "tugas"))
        got = self.sim(2026, 9, 21, 0, 0, 600)
        self.assertEqual([hm(t) for t, _, _ in got], ["08:00"])                      # tugas 'besok' berbunyi

    def test_done_before_reminder_suppresses_it(self):
        at(2026, 9, 20, 9, 0)
        core.handle("A", "tugas telpon ibu jam 14")
        core.handle("A", "selesai 1")
        self.assertEqual(self.sim(2026, 9, 20, 9, 0, 900), [])

    def test_daily_repeating_task(self):
        at(2026, 9, 20, 9, 0)
        self.assertIn("butuh jam", core.handle("A", "tugas harian minum obat"))
        self.assertIn("setiap hari jam 08:00", core.handle("A", "tugas harian minum obat @08:00"))
        self.assertEqual(self.sim(2026, 9, 20, 9, 0, 300), [])            # hari ini sudah lewat
        got = self.sim(2026, 9, 21, 7, 0, 180)
        self.assertEqual([hm(t) for t, _, _ in got], ["08:00"])
        self.assertIn("muncul lagi besok", core.handle("A", "selesai 1"))
        self.assertIn("✅", core.handle("A", "tugas"))
        self.assertEqual(self.sim(2026, 9, 21, 8, 30, 60), [])
        self.assertEqual(len(self.sim(2026, 9, 22, 7, 0, 180)), 1)        # besoknya berbunyi lagi
        self.assertEqual(len(self.sim(2026, 9, 23, 7, 0, 180)), 1)

    def test_delete_and_ownership(self):
        core.handle("A", "tugas rapat @15:00")
        self.assertIn("tidak ditemukan", core.handle("B", "tugas hapus 1"))
        self.assertIn("tidak ditemukan", core.handle("B", "selesai 1"))
        self.assertIn("dihapus", core.handle("A", "tugas hapus 1"))
        self.assertIn("Belum ada tugas", core.handle("A", "tugas"))
        self.assertEqual(self.sim(2026, 9, 20, 6, 0, 600), [])

    def test_overdue_marker(self):
        at(2026, 9, 20, 9, 0)
        core.handle("A", "tugas cuci motor")
        at(2026, 9, 21, 9, 0)
        self.assertIn("(terlewat)", core.handle("A", "tugas"))

    def test_agenda_digest(self):
        at(2026, 9, 20, 6, 0)
        core.handle("A", "agenda on 07:00")
        self.assertEqual(self.sim(2026, 9, 20, 6, 50, 30), [])              # tanpa tugas -> tidak kirim apa-apa
        core.handle("A", "tugas kirim laporan")
        core.handle("A", "tugas harian minum obat @08:00")
        core.handle("A", "tugas ide besok")
        got = self.sim(2026, 9, 21, 6, 50, 30)
        self.assertEqual([hm(t) for t, _, _ in got], ["07:00"])
        txt = got[0][2]
        self.assertIn("Agenda hari ini", txt); self.assertIn("minum obat", txt)


# ================= Tidur & alarm =================
class TestTidurAlarm(Base):
    def test_sleep_reminder_with_duration(self):
        core.handle("A", "alarm on 04:30"); core.handle("A", "tidur on 22:00")
        got = [x for x in self.sim(2026, 9, 20, 21, 55, 10) if "tidur" in x[2].lower()]
        self.assertEqual([hm(t) for t, _, _ in got], ["22:00"])
        self.assertIn("±6 jam 30 menit", got[0][2]); self.assertNotIn("Agak kurang", got[0][2])
        core.handle("A", "tidur on 23:30")
        late = [x for x in self.sim(2026, 9, 21, 23, 25, 10) if "tidur" in x[2].lower()]
        self.assertIn("±5 jam 0 menit", late[0][2]); self.assertIn("Agak kurang", late[0][2])

    def test_alarm_repeats_then_stops(self):
        core.handle("A", "alarm on 04:30")
        got = self.sim(2026, 9, 20, 4, 0, 90)
        self.assertEqual([hm(t) for t, _, _ in got], ["04:30", "04:33", "04:36", "04:39"])
        self.assertIn("ALARM BANGUN PAGI", got[0][2]); self.assertIn("ulangan 3/3", got[3][2])
        self.assertEqual(len(self.sim(2026, 9, 21, 4, 0, 90)), 4)              # besok berbunyi lagi

    def test_bangun_stops_alarm_and_summarises(self):
        core.handle("A", "alarm on 04:30"); core.handle("A", "tugas harian minum obat @08:00")
        self.assertEqual([hm(t) for t, _, _ in self.sim(2026, 9, 20, 4, 29, 3)], ["04:30"])
        r = core.handle("A", "bangun")
        self.assertIn("Alarm dimatikan", r); self.assertIn("Sholat berikutnya: Dzuhur 11:46", r)
        self.assertIn("Cuaca", r); self.assertIn("minum obat", r)
        self.assertEqual(self.sim(2026, 9, 20, 4, 33, 30), [])                   # tidak ada ulangan lagi
        self.assertNotIn("Alarm dimatikan", core.handle("A", "bangun"))          # tidak sedang berbunyi

    def test_alarm_off_while_ringing_and_config(self):
        core.handle("A", "alarm on 04:30")
        self.sim(2026, 9, 20, 4, 30, 1)
        self.assertIn("dimatikan", core.handle("A", "alarm off"))
        self.assertEqual(self.sim(2026, 9, 20, 4, 31, 30), [])
        self.assertIn("nonaktif", core.handle("A", "alarm"))
        self.assertIn("jam 04:30", core.handle("A", "alarm on"))                 # default
        self.assertIn("jam 04:15", core.handle("A", "alarm on 4.15"))
        self.assertIn("jam 05:00", core.handle("A", "alarm 5:00"))
        for bad in ("alarm on 25:00", "alarm on pagi"):
            self.assertIn("tidak dikenali", core.handle("A", bad))
        self.assertEqual(core.user("A")["alarm_time"], "05:00")


# ================= Umum =================
class TestGeneral(Base):
    def test_router_and_status(self):
        self.assertIn("asisten harianmu", core.handle("A", "menu"))
        self.assertIn("asisten harianmu", core.handle("A", ""))
        self.assertIn("asisten harianmu", core.handle("A", "/start"))
        self.assertIn("belum paham", core.handle("A", "ngawur banget"))
        self.assertIn("Jadwal sholat", core.handle("A", "/sholat@NamaBotku"))
        core.handle("A", "sholat on"); core.handle("A", "tidur on 22:00"); core.handle("A", "alarm on 04:30")
        core.handle("A", "cuaca on 06:30"); core.handle("A", "agenda on 07:00")
        s = core.handle("A", "status")
        for frag in ("Yogyakarta (WIB)", "aktif (+ingatkan 15 menit", "jam 06:30", "jam 07:00", "jam 22:00", "jam 04:30"):
            self.assertIn(frag, s)

    def test_failure_isolation(self):
        core.handle("A", "sholat on"); core.handle("A", "tidur on 22:00")
        core.handle("B", "tidur on 22:00")
        WX["aladhan_fail"] = True
        with self.assertLogs("core", level="ERROR"):
            got = self.sim(2026, 9, 20, 21, 58, 5)
        self.assertEqual(sorted(u for _, u, _ in got), ["A", "B"])              # sholat error tak menghalangi tidur

    def test_users_dont_leak(self):
        core.handle("A", "alarm on 04:30"); core.handle("B", "tidur on 22:00")
        got = self.sim(2026, 9, 20, 4, 25, 10)
        self.assertEqual({u for _, u, _ in got}, {"A"})


class TestSoak(Base):
    def test_three_days_all_features_no_duplicates_no_misses(self):
        for cmd in ("sholat on", "tidur on 22:00", "alarm on 04:30", "cuaca on 06:30", "agenda on 07:00",
                    "tugas harian minum obat @08:00"):
            core.handle("A", cmd)
        msgs = self.sim(2026, 9, 21, 0, 0, 3 * 1440)
        by_day = {}
        for t, uid, text in msgs:
            by_day.setdefault(t.date().isoformat(), []).append((hm(t), text))
        self.assertEqual(sorted(by_day), ["2026-09-21", "2026-09-22", "2026-09-23"])
        for day, items in by_day.items():
            kinds = {
                "sholat": sum("Waktunya sholat" in x for _, x in items),
                "sebelum sholat": sum("menit lagi masuk waktu" in x for _, x in items),
                "tidur": sum("Waktunya tidur" in x for _, x in items),
                "alarm": sum("BANGUN" in x for _, x in items),
                "cuaca": sum("Pengingat cuaca" in x for _, x in items),
                "agenda": sum("Agenda hari ini" in x for _, x in items),
                "tugas": sum("Pengingat tugas" in x for _, x in items),
            }
            self.assertEqual(kinds, {"sholat": 5, "sebelum sholat": 5, "tidur": 1, "alarm": 4,
                                     "cuaca": 1, "agenda": 1, "tugas": 1}, day)
            self.assertEqual(len(items), 18, day)
            self.assertEqual(len(set(items)), 18, "ada pesan kembar pada " + day)


# ================= Adapter WhatsApp =================
def signed(body):
    return {"X-Hub-Signature-256": "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest(),
            "Content-Type": "application/json"}


def wa_payload(msg_id, text=None, mtype="text", frm="628123"):
    msg = {"from": frm, "id": msg_id, "type": mtype}
    if text is not None:
        msg["text"] = {"body": text}
    return json.dumps({"entry": [{"changes": [{"value": {"messages": [msg]}}]}]}).encode()


class SyncThread:
    def __init__(self, target, args=(), daemon=None):
        self.t, self.a = target, args

    def start(self):
        self.t(*self.a)


class TestWhatsApp(Base):
    def setUp(self):
        super().setUp()
        wa_bot._seen.clear()
        self.posts = []
        self.post_results = []

        def fake_post(url, headers=None, json=None, timeout=None):
            self.posts.append((url, headers, json))
            return self.post_results.pop(0) if self.post_results else Resp({"messages": [{"id": "x"}]})

        for p in (mock.patch("wa_bot.requests.post", fake_post), mock.patch("wa_bot.threading.Thread", SyncThread)):
            p.start(); self.addCleanup(p.stop)
        self.client = wa_bot.app.test_client()

    def test_webhook_verification(self):
        ok = self.client.get("/webhook?hub.mode=subscribe&hub.verify_token=rahasia&hub.challenge=12345")
        self.assertEqual((ok.status_code, ok.data), (200, b"12345"))
        self.assertEqual(self.client.get("/webhook?hub.mode=subscribe&hub.verify_token=salah&hub.challenge=1").status_code, 403)
        self.assertEqual(self.client.get("/webhook").status_code, 403)

    def test_signature_required(self):
        body = wa_payload("m1", "saldo")
        self.assertEqual(self.client.post("/webhook", data=body, headers={"Content-Type": "application/json"}).status_code, 403)
        bad = {"X-Hub-Signature-256": "sha256=deadbeef", "Content-Type": "application/json"}
        self.assertEqual(self.client.post("/webhook", data=body, headers=bad).status_code, 403)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.client.post("/webhook", data=body, headers=signed(body)).status_code, 200)
        self.assertEqual(len(self.posts), 1)

    def test_message_reply_dedupe_and_types(self):
        body = wa_payload("m1", "25rb makan siang")
        self.client.post("/webhook", data=body, headers=signed(body))
        url, headers, payload = self.posts[0]
        self.assertTrue(url.endswith("/v23.0/111/messages")); self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertEqual((payload["to"], payload["type"], payload["messaging_product"]), ("628123", "text", "whatsapp"))
        self.assertIn("Pengeluaran tercatat", payload["text"]["body"])
        self.client.post("/webhook", data=body, headers=signed(body))            # kiriman ulang dari Meta
        self.assertEqual(len(self.posts), 1)
        status = json.dumps({"entry": [{"changes": [{"value": {"statuses": [{"id": "s", "status": "read"}]}}]}]}).encode()
        self.assertEqual(self.client.post("/webhook", data=status, headers=signed(status)).status_code, 200)
        self.assertEqual(len(self.posts), 1)                                     # status delivery diabaikan
        img = wa_payload("m2", mtype="image")
        self.client.post("/webhook", data=img, headers=signed(img))
        self.assertIn("pesan teks", self.posts[-1][2]["text"]["body"])
        self.assertIn("Rp25.000", core.handle("628123", "riwayat"))  # tercatat atas nama pengirim WA

    def test_template_fallback_after_24h_window(self):
        err = Resp({"error": {"code": 131047, "message": "Re-engagement message"}}, 400)
        self.post_results = [err]
        self.assertTrue(wa_bot.kirim("628123", "⏰ *Bangun!*\nBalas *bangun*"))
        self.assertEqual(len(self.posts), 2)
        tpl = self.posts[1][2]
        self.assertEqual(tpl["type"], "template"); self.assertEqual(tpl["template"]["name"], "pengingat_bot")
        self.assertEqual(tpl["template"]["language"]["code"], "id")
        param = tpl["template"]["components"][0]["parameters"][0]["text"]
        self.assertNotIn("\n", param); self.assertIn("Bangun", param)

    def test_no_fallback_for_other_errors_or_without_template(self):
        with self.assertLogs("wa_bot", level="ERROR"):
            self.post_results = [Resp({"error": {"code": 131026}}, 400)]
            self.assertFalse(wa_bot.kirim("628123", "halo")); self.assertEqual(len(self.posts), 1)
            self.posts.clear()
            self.post_results = [Resp({"error": {"code": 131047}}, 400)]
            with mock.patch.object(wa_bot, "TEMPLATE_NAME", None):
                self.assertFalse(wa_bot.kirim("628123", "halo"))
        self.assertEqual(len(self.posts), 1)

    def test_scheduler_sends_reminders_via_whatsapp(self):
        core.handle("628123", "tidur on 22:00")
        at(2026, 9, 20, 22, 0)
        wa_bot.kirim_pengingat()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0][2]["to"], "628123"); self.assertIn("Waktunya tidur", self.posts[0][2]["text"]["body"])
        wa_bot.kirim_pengingat()                                                 # menit yang sama: tidak dobel
        self.assertEqual(len(self.posts), 1)


# ================= Adapter Telegram =================
@unittest.skipUnless(HAS_TG, "python-telegram-bot belum terpasang (lewati uji Telegram)")
class TestTelegram(Base):
    def test_to_html(self):
        self.assertEqual(tg_bot.to_html("*Halo* <a> & b"), "<b>Halo</b> &lt;a&gt; &amp; b")

    def test_on_text_and_job(self):
        sent = []

        async def reply_text(text, parse_mode=None):
            sent.append((text, parse_mode))

        upd = SimpleNamespace(effective_chat=SimpleNamespace(id=777),
                              message=SimpleNamespace(text="/saldo", reply_text=reply_text))
        asyncio.run(tg_bot.on_text(upd, None))
        self.assertIn("<b>Saldo kamu: Rp0</b>", sent[0][0]); self.assertEqual(sent[0][1], "HTML")

        core.handle("777", "tidur on 22:00")
        at(2026, 9, 20, 22, 0)
        out = []

        async def send_message(chat_id, text, parse_mode=None):
            out.append((chat_id, text))

        ctx = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
        asyncio.run(tg_bot.job_tick(ctx))
        self.assertEqual(out[0][0], 777); self.assertIn("<b>Waktunya tidur!</b>", out[0][1])

    def test_build_app(self):
        app = tg_bot.build_app("123456:TEST-TOKEN")
        self.assertEqual(len(app.handlers[0]), 1)
        self.assertEqual(len(app.job_queue.jobs()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
