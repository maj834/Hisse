"""Hisse Radar veri güncelleyici.

GitHub Actions üzerinde çalışır. BIST 30 fiyatlarını (Yahoo Finance, ~15 dk gecikmeli),
haber başlıklarını (Google Haberler RSS) ve fon fiyatlarını (TEFAS) çeker; JSON dosyalarını
`data` dalına tek commit olarak (force push) yazar. Uygulama bu dosyaları 5 saniyede bir kontrol eder.

Ortam değişkenleri:
  TEK_SEFER=1   -> bir kez güncelle ve çık (borsa kapalıyken)
  CIKIS_UTC     -> döngünün biteceği saat, "HH:MM" (UTC). Verilmezse otomatik seçilir.
  CIKTI_DIZIN   -> JSON'ların yazılacağı klasör (varsayılan: out)
  GIT_PUSH=0    -> git'e gönderme (yerel deneme için)
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Europe/Istanbul")
UA = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36",
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}

HISSELER = {
    "AEFES": ("Anadolu Efes", "İçecek"),
    "AKBNK": ("Akbank", "Bankacılık"),
    "ASELS": ("Aselsan", "Savunma"),
    "ASTOR": ("Astor Enerji", "Enerji Ekipmanları"),
    "BIMAS": ("BİM Mağazalar", "Perakende"),
    "DSTKF": ("Destek Finans Faktoring", "Finans"),
    "EKGYO": ("Emlak Konut GYO", "Gayrimenkul"),
    "ENKAI": ("Enka İnşaat", "İnşaat"),
    "EREGL": ("Ereğli Demir Çelik", "Demir-Çelik"),
    "FROTO": ("Ford Otosan", "Otomotiv"),
    "GARAN": ("Garanti BBVA", "Bankacılık"),
    "GUBRF": ("Gübre Fabrikaları", "Kimya / Gübre"),
    "ISCTR": ("İş Bankası (C)", "Bankacılık"),
    "KCHOL": ("Koç Holding", "Holding"),
    "KRDMD": ("Kardemir (D)", "Demir-Çelik"),
    "MGROS": ("Migros", "Perakende"),
    "PETKM": ("Petkim", "Petrokimya"),
    "PGSUS": ("Pegasus", "Ulaştırma"),
    "SAHOL": ("Sabancı Holding", "Holding"),
    "SASA": ("SASA Polyester", "Kimya / Tekstil"),
    "SISE": ("Şişecam", "Cam"),
    "TAVHL": ("TAV Havalimanları", "Ulaştırma"),
    "TCELL": ("Turkcell", "Telekom"),
    "THYAO": ("Türk Hava Yolları", "Ulaştırma"),
    "TOASO": ("Tofaş", "Otomotiv"),
    "TRALT": ("Türk Altın İşletmeleri", "Madencilik"),
    "TTKOM": ("Türk Telekom", "Telekom"),
    "TUPRS": ("Tüpraş", "Enerji / Rafineri"),
    "VAKBN": ("VakıfBank", "Bankacılık"),
    "YKBNK": ("Yapı Kredi", "Bankacılık"),
}
# Yahoo'da farklı kodla duran hisseler (ilk bulunan kullanılır)
YAHOO_ALT = {"TRALT": ["TRALT.IS", "KOZAL.IS"]}

HABER_SORGULARI = [
    ("Borsa", "borsa istanbul hisse"),
    ("Borsa", "BIST 100 bugün"),
    ("Dünya", "Trump piyasalar petrol"),
    ("Dünya", "küresel piyasalar Fed faiz"),
    ("Ekonomi", "Merkez Bankası faiz enflasyon"),
]

AY = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]
AY_UZUN = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]

KOK = Path(__file__).resolve().parent.parent
CIKTI = Path(os.environ.get("CIKTI_DIZIN", "out"))
oturum = requests.Session()
oturum.headers.update(UA)

# Yahoo, sunuculardan gelen düz istekleri 429 ile reddediyor; tarayıcı taklidi yapan curl_cffi kullan.
try:
    from curl_cffi import requests as creq  # type: ignore

    yahoo_oturum = creq.Session(impersonate="chrome")
except Exception:  # kütüphane yoksa normal oturum
    yahoo_oturum = requests.Session()
    yahoo_oturum.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"})
_yahoo_hazir = False


def yahoo_isit() -> None:
    """Çerez almak için Yahoo ana sayfasına bir kez uğra."""
    global _yahoo_hazir
    if _yahoo_hazir:
        return
    _yahoo_hazir = True
    for u in ("https://fc.yahoo.com", "https://finance.yahoo.com/quote/ASELS.IS/"):
        try:
            yahoo_oturum.get(u, timeout=15)
        except Exception:
            pass


HATALAR: list[str] = []


def log(*a):
    if a and str(a[0]).startswith(("hata", "haber alınamadı", "fon alınamadı", "günlük alınamadı", "push")):
        HATALAR.append(" ".join(str(x) for x in a)[:300])
    print(dt.datetime.now(IST).strftime("%H:%M:%S"), *a, flush=True)


def simdi() -> dt.datetime:
    return dt.datetime.now(IST)


def kisa_tarih(d: dt.datetime) -> str:
    return f"{d.day} {AY[d.month - 1]}"


def oku_json(ad: str, varsayilan):
    for yer in (CIKTI / ad, KOK / "data" / ad):
        try:
            return json.loads(yer.read_text(encoding="utf-8"))
        except Exception:
            continue
    return varsayilan


def yaz_json(ad: str, veri) -> None:
    CIKTI.mkdir(parents=True, exist_ok=True)
    (CIKTI / ad).write_text(json.dumps(veri, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# ---------------------------------------------------------------- fiyatlar
def yahoo_chart(sembol: str, aralik: str, arasi: str):
    yahoo_isit()
    hatalar = []
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{sembol}?range={aralik}&interval={arasi}&includePrePost=false"
        try:
            r = yahoo_oturum.get(url, timeout=15)
            if r.status_code == 429:
                time.sleep(2)
                r = yahoo_oturum.get(url, timeout=15)
            if r.status_code == 200:
                res = r.json()["chart"]["result"]
                if res:
                    return res[0]
            hatalar.append(f"{host}:{r.status_code}")
        except Exception as e:  # ağ hatası
            hatalar.append(f"{host}:{type(e).__name__}")
    raise RuntimeError(f"{sembol} {aralik}/{arasi}: " + ", ".join(hatalar))


def yahoo_sembol(kod: str) -> list[str]:
    return YAHOO_ALT.get(kod, [f"{kod}.IS"])


def chart_dene(kod: str, aralik: str, arasi: str):
    son = None
    for s in yahoo_sembol(kod):
        try:
            return yahoo_chart(s, aralik, arasi)
        except Exception as e:
            son = e
    raise son  # type: ignore[misc]


def gunluk_satirlar(res) -> list[list]:
    ts = res.get("timestamp") or []
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    satirlar = []
    for i, t in enumerate(ts):
        try:
            o, h, l, c, v = (q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i] or 0)
        except (KeyError, IndexError):
            continue
        if None in (o, h, l, c):
            continue
        d = dt.datetime.fromtimestamp(t, IST).strftime("%Y-%m-%d")
        satirlar.append([d, round(o, 4), round(max(h, o, c), 4), round(min(l, o, c), 4), round(c, 4), int(v)])
    # aynı tarihten birden fazla varsa sonuncusu kalsın
    tekil = {}
    for s in satirlar:
        tekil[s[0]] = s
    return [tekil[k] for k in sorted(tekil)]


def hisse_guncelle(kod: str, eski: dict | None, gunluk_de: bool) -> dict:
    ad, sektor = HISSELER[kod]
    kayit = dict(eski or {})
    kayit.update({"ad": ad, "sektor": sektor})

    gun = chart_dene(kod, "1d", "5m")
    meta = gun.get("meta", {})
    last = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if last is None or prev is None:
        raise RuntimeError(f"{kod}: fiyat yok")
    zaman = dt.datetime.fromtimestamp(meta.get("regularMarketTime") or time.time(), IST)
    gun_id = zaman.strftime("%Y-%m-%d")

    noktalar = []
    ts = gun.get("timestamp") or []
    kapan = ((gun.get("indicators", {}).get("quote") or [{}])[0]).get("close") or []
    for t, c in zip(ts, kapan):
        if c is None:
            continue
        noktalar.append([dt.datetime.fromtimestamp(t, IST).strftime("%H:%M"), round(c, 4)])
    if not noktalar or noktalar[-1][1] != round(last, 4):
        noktalar.append([zaman.strftime("%H:%M"), round(last, 4)])

    hi = meta.get("regularMarketDayHigh") or max([p[1] for p in noktalar] + [last])
    lo = meta.get("regularMarketDayLow") or min([p[1] for p in noktalar] + [last])

    kayit.update({
        "last": round(last, 4), "prev": round(prev, 4), "hi": round(hi, 4), "lo": round(lo, 4),
        "t": f"{kisa_tarih(zaman)} {zaman.strftime('%H:%M')}", "ts": int(zaman.timestamp()),
        "i": noktalar[-100:], "id": gun_id,
    })

    g = kayit.get("g") or []
    if gunluk_de or not g:
        try:
            g = gunluk_satirlar(chart_dene(kod, "3mo", "1d"))
        except Exception as e:
            log("günlük alınamadı", kod, e)
    # bugünün mumu: gün içi veriden güncelle (Yahoo'nun günlük satırı yoksa ya da eksikse)
    hacim = sum(v or 0 for v in (((gun.get("indicators", {}).get("quote") or [{}])[0]).get("volume") or []))
    acilis = noktalar[0][1] if noktalar else prev
    bugun = [gun_id, round(acilis, 4), round(max(hi, last), 4), round(min(lo, last), 4), round(last, 4), int(hacim)]
    g = [s for s in g if s[0] != gun_id] + [bugun]
    kayit["g"] = sorted(g, key=lambda s: s[0])[-60:]
    return kayit


# ---------------------------------------------------------------- haberler
def haberleri_cek(eski: dict) -> dict:
    items = {it["title"]: it for it in eski.get("items", [])}
    for kat, sorgu in HABER_SORGULARI:
        url = f"https://news.google.com/rss/search?q={quote_plus(sorgu + ' when:2d')}&hl=tr&gl=TR&ceid=TR:tr"
        try:
            r = oturum.get(url, timeout=15)
            r.raise_for_status()
            kok = ET.fromstring(r.content)
        except Exception as e:
            log("haber alınamadı", sorgu, e)
            continue
        for it in list(kok.iter("item"))[:12]:
            baslik = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            kaynak = (it.findtext("source") or "").strip()
            if kaynak and baslik.endswith(" - " + kaynak):
                baslik = baslik[: -len(kaynak) - 3].strip()
            try:
                t = email.utils.parsedate_to_datetime(it.findtext("pubDate")).astimezone(IST)
            except Exception:
                t = simdi()
            if not baslik or not link:
                continue
            if baslik not in items:
                items[baslik] = {"title": baslik, "url": link, "kaynak": kaynak, "kat": kat}
            items[baslik]["ts"] = int(t.timestamp())
            items[baslik]["t"] = kisa_tarih(t)
    liste = sorted(items.values(), key=lambda x: x.get("ts", 0), reverse=True)[:40]
    return {"updatedAt": simdi().isoformat(timespec="seconds"), "kaynak": "Google Haberler", "items": liste}


# ---------------------------------------------------------------- fonlar
def fon_listesi() -> list[str]:
    try:
        satirlar = (KOK / "fonlar.txt").read_text(encoding="utf-8").split()
        return [s.strip().upper() for s in satirlar if s.strip() and not s.startswith("#")]
    except Exception:
        return ["AES"]


def fonlari_cek(eski: dict) -> dict:
    fonlar = dict(eski.get("fonlar", {}))
    bit = simdi()
    bas = bit - dt.timedelta(days=10)
    try:
        yahoo_oturum.get("https://www.tefas.gov.tr/TarihselVeriler.aspx", timeout=20)
    except Exception as e:
        log("fon alınamadı", "tefas giriş", e)
    for kod in fon_listesi():
        try:
            r = yahoo_oturum.post(
                "https://www.tefas.gov.tr/api/DB/BindHistoryInfo",
                data={"fontip": "YAT", "sfontur": "", "fonkod": kod, "fongrup": "", "bastarih": bas.strftime("%d.%m.%Y"),
                      "bittarih": bit.strftime("%d.%m.%Y"), "fonturkod": "", "fonunvantip": ""},
                headers={"X-Requested-With": "XMLHttpRequest", "Origin": "https://www.tefas.gov.tr",
                         "Referer": "https://www.tefas.gov.tr/TarihselVeriler.aspx"},
                timeout=20,
            )
            if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
                log("fon alınamadı", kod, r.status_code, r.text[:120].replace("\n", " "))
                continue
            veri = sorted(r.json().get("data") or [], key=lambda x: int(x.get("TARIH", 0)))
            if not veri:
                log("fon alınamadı", kod, "boş yanıt")
        except Exception as e:
            log("fon alınamadı", kod, e)
            continue
        if not veri:
            continue
        son = veri[-1]
        onceki = veri[-2] if len(veri) > 1 else None
        tarih = dt.datetime.fromtimestamp(int(son["TARIH"]) / 1000, IST)
        f = dict(fonlar.get(kod, {}))
        f.update({
            "ad": son.get("FONUNVAN") or f.get("ad") or kod,
            "fiyat": float(son["FIYAT"]),
            "buyukluk": float(son.get("PORTFOYBUYUKLUK") or f.get("buyukluk") or 0),
            "yatirimci": int(son.get("KISISAYISI") or f.get("yatirimci") or 0),
            "tarih": f"{tarih.day} {AY[tarih.month - 1]} {tarih.year}",
        })
        if onceki and float(onceki["FIYAT"]):
            f["gunluk"] = round((float(son["FIYAT"]) / float(onceki["FIYAT"]) - 1) * 100, 3)
        f.setdefault("kategori", "TEFAS yatırım fonu")
        fonlar[kod] = f
    return {"updatedAt": simdi().isoformat(timespec="seconds"), "fonlar": fonlar}


# ---------------------------------------------------------------- git
def git(*args, check=True):
    return subprocess.run(["git", "-C", str(CIKTI), *args], check=check, capture_output=True, text=True)


def gonder(mesaj: str) -> None:
    if os.environ.get("GIT_PUSH", "1") == "0":
        return
    git("add", "-A")
    git("commit", "--amend", "--quiet", "--allow-empty", "-m", mesaj, check=False)
    r = git("push", "--force", "--quiet", "origin", "HEAD:data", check=False)
    if r.returncode != 0:
        log("push hatası", r.stderr.strip()[:300])


# ---------------------------------------------------------------- ana döngü
def bir_tur(sayac: int, zorla_hepsi: bool) -> tuple[int, int]:
    snap = oku_json("snapshot.json", {"hisseler": {}})
    hisseler = snap.get("hisseler", {})
    gunluk_de = zorla_hepsi or sayac % 30 == 0
    basari = hata = 0
    for kod in HISSELER:
        try:
            hisseler[kod] = hisse_guncelle(kod, hisseler.get(kod), gunluk_de)
            basari += 1
        except Exception as e:
            hata += 1
            log("hata", kod, e)
        time.sleep(0.4)
    for eski_kod in [k for k in hisseler if k not in HISSELER]:
        hisseler.pop(eski_kod)
    if basari:
        en_yeni = max((h.get("ts", 0) for h in hisseler.values()), default=0)
        z = dt.datetime.fromtimestamp(en_yeni, IST) if en_yeni else simdi()
        snap.update({
            "updatedAt": simdi().isoformat(timespec="seconds"),
            "guncelleme": f"{z.day} {AY_UZUN[z.month - 1]} {z.year}, {z.strftime('%H:%M')}",
            "kaynak": "Yahoo Finance (BIST verisi yaklaşık 15 dk gecikmeli)",
            "hisseler": hisseler,
        })
        yaz_json("snapshot.json", snap)
    if zorla_hepsi or sayac % 5 == 0:
        yaz_json("news.json", haberleri_cek(oku_json("news.json", {})))
    if zorla_hepsi or sayac % 30 == 0:
        yaz_json("funds.json", fonlari_cek(oku_json("funds.json", {})))
    yaz_json("durum.json", {"zaman": simdi().isoformat(timespec="seconds"), "basarili": basari, "hatali": hata,
                            "hatalar": HATALAR[-40:]})
    HATALAR.clear()
    return basari, hata


def cikis_zamani() -> dt.datetime:
    su = dt.datetime.now(dt.timezone.utc)
    if os.environ.get("CIKIS_UTC"):
        h, m = map(int, os.environ["CIKIS_UTC"].split(":"))
        c = su.replace(hour=h, minute=m, second=0, microsecond=0)
    else:
        # Borsa İstanbul 10:00-18:10 (UTC 07:00-15:10). İki parça halinde çalışır.
        c = su.replace(hour=12, minute=0, second=0, microsecond=0)
        if su >= c - dt.timedelta(minutes=5):
            c = su.replace(hour=15, minute=25, second=0, microsecond=0)
    return min(c, su + dt.timedelta(minutes=345))


def main() -> int:
    tek = os.environ.get("TEK_SEFER") == "1"
    if tek:
        b, h = bir_tur(0, True)
        log(f"tek sefer: {b} hisse güncellendi, {h} hata")
        gonder(f"veri {simdi():%d.%m %H:%M}")
        return 0
    bitis = cikis_zamani()
    log("döngü bitişi (UTC):", bitis.strftime("%H:%M"))
    sayac = 0
    toplam_basari = 0
    while dt.datetime.now(dt.timezone.utc) < bitis:
        bas = time.time()
        b, h = bir_tur(sayac, sayac == 0)
        toplam_basari += b
        log(f"tur {sayac}: {b} hisse, {h} hata")
        gonder(f"veri {simdi():%d.%m %H:%M}")
        sayac += 1
        time.sleep(max(5, 60 - (time.time() - bas)))
    return 0 if toplam_basari else 1


if __name__ == "__main__":
    sys.exit(main())
