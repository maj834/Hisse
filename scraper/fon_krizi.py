"""Fon krizi sayfası -> data/fon_krizi.json (saatte bir, GitHub Actions)

- Google News'ten fon krizi haberlerini toplar (Tera, Pusula, fon soruşturması, tasfiye, iade kısıtı, SPK...).
- Ücretsiz haber yapay zekâsıyla (Cloudflare; yoksa diğerleri) sade Türkçe "son durum" özeti yazar.
- Etkilenen kurumları (data/riskli.json) ve bu kurumların fonlarını (funds.json) listeler.
- Kriz sürüyor mu? Son 5 günde en az 3 kriz haberi varsa "aktif". Haber kesilince sayfa uygulamada kendiliğinden gizlenir.
  data/riskli.json -> "fon_krizi": {"aktif": true|false} ile elle de açılıp kapatılabilir ("oto" = haberlere göre).
"""
from __future__ import annotations

import email.utils
import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

KOK = Path(__file__).resolve().parent.parent
CIKTI = KOK / "data" / "fon_krizi.json"
TSI = timezone(timedelta(hours=3))

SORGULAR = ["fon soruşturması", "Tera Portföy", "Pusula Portföy", "fon tasfiye", "serbest fon iade talimatı",
            "SPK fon tedbir", "fon krizi yatırımcı", "portföy yönetim şirketi soruşturma"]
ILGILI = re.compile(r"fon|portföy|spk|tasfiye|soruşturma|temerrüt|iade|gözaltı|yakalama|tedbir|yatırımcı", re.I)
COP = re.compile(r"hava durumu|maç|burç|dizi|hangi kanalda|konser|tarif", re.I)
EN_ESKI_GUN = 21
# borsada toparlanma / yükseliş haberleri (krizin piyasaya etkisi geçiyor mu?)
YUKSELIS_SORGULARI = ["BIST 100 yükseldi", "borsa toparlandı", "borsa güne yükselişle başladı", "Borsa İstanbul yükseliş"]
YUKSELIS = re.compile(r"yüksel|toparlan|pozitif|artı|rekor|tepki alım|kazandır|yeşil", re.I)
DUSUS = re.compile(r"düş|geriled|kayıp|sert satış|çöktü|\beksi\b|ekside", re.I)
BORSA_TR = re.compile(r"BIST|Borsa İstanbul|\bborsa", re.I)
YABANCI = re.compile(r"New York|Nasdaq|\bDow\b|S&P|Wall Street|Avrupa|\bAsya|Japon|\bÇin\b|Almanya|Londra|Tokyo|\bDAX\b|Nikkei|futbol|Milli Takım|\bmaç|\blig\b|Süper Lig|altın|gümüş|güven endeksi|enflasyon|dolar|euro|kripto|bitcoin", re.I)
GEMINI_EN_SIK_DK = 30   # Gemini en çok yarım saatte bir (ücretsiz kota)
GEMINI_HATA = ""


def log(*a):
    print(datetime.now(TSI).strftime("%H:%M:%S"), *a, flush=True)


def oku(yol, varsayilan):
    try:
        return json.loads(Path(yol).read_text(encoding="utf-8"))
    except Exception:
        return varsayilan


def rss(sorgu: str, gun: int, adet: int = 15) -> list:
    oturum = requests.Session()
    oturum.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0 Safari/537.36"})
    url = f"https://news.google.com/rss/search?q={quote_plus(sorgu + f' when:{gun}d')}&hl=tr&gl=TR&ceid=TR:tr"
    out = []
    try:
        r = oturum.get(url, timeout=20)
        r.raise_for_status()
        kok = ET.fromstring(r.content)
    except Exception as e:
        log("alınamadı:", sorgu, str(e)[:80])
        return out
    for it in list(kok.iter("item"))[:adet]:
        baslik = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        kaynak = (it.findtext("source") or "").strip()
        if kaynak and baslik.endswith(" - " + kaynak):
            baslik = baslik[: -len(kaynak) - 3].strip()
        try:
            t = email.utils.parsedate_to_datetime(it.findtext("pubDate")).astimezone(TSI)
        except Exception:
            t = datetime.now(TSI)
        if baslik and link and not COP.search(baslik):
            out.append({"baslik": baslik[:200], "url": link, "kaynak": kaynak, "ts": int(t.timestamp())})
    return out


def yukselis_haberleri(eski: list) -> list:
    # eski kayıtlar da yeni filtreden geçsin
    kayit = {h["baslik"]: h for h in eski if BORSA_TR.search(h["baslik"]) and not YABANCI.search(h["baslik"]) and not DUSUS.search(h["baslik"])}
    for s in YUKSELIS_SORGULARI:
        for h in rss(s, 2, 12):
            b = h["baslik"]
            if YUKSELIS.search(b) and BORSA_TR.search(b) and not DUSUS.search(b) and not YABANCI.search(b):
                kayit.setdefault(h["baslik"], h)
        time.sleep(1)
    liste = [h for h in kayit.values() if h["ts"] >= time.time() - 3 * 86400]
    return sorted(liste, key=lambda x: -x["ts"])[:12]


def gemini(istem: str) -> tuple[str, str, list]:
    """Gemini + Google araması: en güncel gelişmeleri web'den bulur. Dönüş: (model, metin, kaynaklar).
    Ücretsiz kotası dolu/kapalı modelde (429) sıradakini dener; hiçbiri aramayla olmazsa aramasız dener."""
    anahtar = os.environ.get("GEMINI_API_KEY", "").strip()
    if not anahtar:
        raise RuntimeError("GEMINI_API_KEY yok")
    taban = "https://generativelanguage.googleapis.com/v1beta"
    bas = {"x-goog-api-key": anahtar}
    try:
        r = requests.get(f"{taban}/models", params={"pageSize": 200}, headers=bas, timeout=30)
        r.raise_for_status()
        adlar = [m["name"] for m in r.json().get("models", []) if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    except Exception as e:
        raise RuntimeError(f"model listesi alınamadı: {str(e)[:120]}")
    tercih = ["models/gemini-2.5-flash", "models/gemini-flash-latest", "models/gemini-2.5-flash-lite",
              "models/gemini-flash-lite-latest", "models/gemini-2.0-flash"]
    if os.environ.get("GEMINI_MODEL"):
        tercih.insert(0, "models/" + os.environ["GEMINI_MODEL"].replace("models/", ""))
    adaylar = [a for a in tercih if a in adlar] or [a for a in adlar if "flash" in a][:3]
    hatalar = []
    for arama in (True, False):
        for model in adaylar:
            govde = {"contents": [{"role": "user", "parts": [{"text": istem}]}],
                     "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2500}}
            if arama:
                govde["tools"] = [{"google_search": {}}]
            r = requests.post(f"{taban}/{model}:generateContent", headers=bas, json=govde, timeout=90)
            if not r.ok:
                try:
                    msj = r.json().get("error", {}).get("message", "")
                except Exception:
                    msj = r.text
                hatalar.append(f"{model.replace('models/', '')}{'+arama' if arama else ''} {r.status_code}: {msj[:140]}")
                if r.status_code in (429, 400, 403, 404):
                    continue
                break
            c = (r.json().get("candidates") or [{}])[0]
            metin = "".join(p_.get("text", "") for p_ in (c.get("content") or {}).get("parts", []))
            kaynaklar = []
            for ch in ((c.get("groundingMetadata") or {}).get("groundingChunks") or [])[:10]:
                w = ch.get("web") or {}
                if w.get("uri"):
                    kaynaklar.append({"baslik": w.get("title", ""), "url": w["uri"]})
            if hatalar:
                log("Gemini denemeleri:", " | ".join(hatalar))
            return model.replace("models/", "") + ("" if arama else " (aramasız)"), metin, kaynaklar
    raise RuntimeError(" | ".join(hatalar)[:900] or "uygun Gemini modeli yok")


def haberleri_topla(eski: list) -> list:
    oturum = requests.Session()
    oturum.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0 Safari/537.36"})
    kayit = {h["baslik"]: h for h in eski}
    for sorgu in SORGULAR:
        url = f"https://news.google.com/rss/search?q={quote_plus(sorgu + ' when:7d')}&hl=tr&gl=TR&ceid=TR:tr"
        try:
            r = oturum.get(url, timeout=20)
            r.raise_for_status()
            kok = ET.fromstring(r.content)
        except Exception as e:
            log("alınamadı:", sorgu, str(e)[:80])
            continue
        for it in list(kok.iter("item"))[:15]:
            baslik = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            kaynak = (it.findtext("source") or "").strip()
            if kaynak and baslik.endswith(" - " + kaynak):
                baslik = baslik[: -len(kaynak) - 3].strip()
            if not baslik or not link or COP.search(baslik) or not ILGILI.search(baslik):
                continue
            try:
                t = email.utils.parsedate_to_datetime(it.findtext("pubDate")).astimezone(TSI)
            except Exception:
                t = datetime.now(TSI)
            if baslik not in kayit:
                kayit[baslik] = {"baslik": baslik[:200], "url": link, "kaynak": kaynak, "ts": int(t.timestamp())}
        time.sleep(1)
    sinir = time.time() - EN_ESKI_GUN * 86400
    liste = [h for h in kayit.values() if h["ts"] >= sinir]
    # aynı olayın farklı sitelerdeki neredeyse aynı başlıklarını ayıkla
    gorulen, temiz = set(), []
    for h in sorted(liste, key=lambda x: -x["ts"]):
        anahtar = re.sub(r"[^a-zçğıöşü0-9]", "", h["baslik"].lower())[:60]
        if anahtar in gorulen:
            continue
        gorulen.add(anahtar)
        temiz.append(h)
    return temiz[:80]


def ozet_yaz(haberler: list, eski: dict) -> dict:
    """Ücretsiz haber yapay zekâsıyla sade Türkçe özet. Başlıklar değişmediyse eski özeti korur."""
    son = haberler[:30]
    imza = hashlib.md5("|".join(h["baslik"] for h in son).encode()).hexdigest()
    satirlar = "\n".join(f"- {datetime.fromtimestamp(h['ts'], TSI):%d.%m %H:%M} [{h['kaynak']}] {h['baslik']}" for h in son)
    istem = (
        "Türkiye'deki fon krizi (portföy yönetim şirketleri, fon soruşturması, tasfiye, iade kısıtları) hakkındaki son haber başlıkları aşağıda.\n"
        "YALNIZCA bu başlıklardaki bilgilere dayan; başlıkta olmayan rakam, isim ya da olay uydurma. 18 yaşındaki sıradan bir yatırımcının "
        "anlayacağı sade Türkçe yaz.\n"
        'Şu JSON\'u döndür: {"ozet":"3-4 cümle: krizde son durum ne, en son ne oldu","yatirimci":["fon yatırımcısı için 2-3 kısa, somut not"]}\n\n'
        f"Başlıklar:\n{satirlar}"
    )
    # 1) Gemini + Google araması (en güncel, web'den); yarım saatte bir en fazla
    son_g = eski.get("gemini_ts", 0)
    if os.environ.get("GEMINI_API_KEY") and time.time() - son_g >= GEMINI_EN_SIK_DK * 60:
        g_istem = (
            f"Bugün {datetime.now(TSI):%d.%m.%Y %H:%M} (Türkiye saati). Google'da ara: Türkiye'deki fon krizi (Tera Portföy, Pusula Portföy, "
            "fon soruşturması, fon tasfiyeleri, iade kısıtları, SPK kararları) ile ilgili SON 48 SAATTEKİ en güncel gelişmeler neler? "
            "Ayrıca Borsa İstanbul'un bugünkü seyri (kriz etkisi, toparlanma var mı).\n"
            "Yalnızca bulduğun kaynaklara dayan, uydurma. 18 yaşındaki sıradan bir yatırımcının anlayacağı sade Türkçe yaz.\n"
            'Sadece şu JSON\'u döndür: {"ozet":"3-4 cümle son durum","yatirimci":["fon yatırımcısı için 2-3 kısa, somut not"]}\n\n'
            f"Bildiğimiz son başlıklar:\n{satirlar}"
        )
        try:
            model, metin, kaynaklar = gemini(g_istem)
            m = re.search(r"\{.*\}", metin, re.S)
            j = json.loads(m.group(0) if m else metin)
            return {"ozet": str(j.get("ozet", ""))[:900], "yatirimci": [str(x)[:220] for x in (j.get("yatirimci") or [])][:3],
                    "imza": imza, "model": model, "model_ad": "Gemini + Google araması" if kaynaklar else "Gemini", "kaynaklar": kaynaklar,
                    "gemini_ts": int(time.time()), "ozet_zaman": datetime.now(TSI).isoformat(timespec="seconds")}
        except Exception as e:
            global GEMINI_HATA
            GEMINI_HATA = str(e)[:900]
            log("Gemini olmadı:", GEMINI_HATA)
    if eski.get("model_ad", "").startswith("Gemini") and time.time() - son_g < 3 * 3600:
        return {k: eski[k] for k in ("ozet", "yatirimci", "imza", "model", "model_ad", "kaynaklar", "gemini_ts", "ozet_zaman") if k in eski}
    # 2) yedek: ücretsiz haber yapay zekâsı (yalnızca başlıklardan)
    if eski.get("imza") == imza and eski.get("ozet"):
        return {k: eski[k] for k in ("ozet", "yatirimci", "imza", "model", "model_ad", "kaynaklar", "gemini_ts", "ozet_zaman") if k in eski}
    try:
        import ai_degerlendir
        model, metin = ai_degerlendir.haber_modeli(istem)
        m = re.search(r"\{.*\}", metin, re.S)
        j = json.loads(m.group(0) if m else metin)
        return {"ozet": str(j.get("ozet", ""))[:900], "yatirimci": [str(x)[:220] for x in (j.get("yatirimci") or [])][:3],
                "imza": imza, "model": model, "model_ad": "Haber yapay zekâsı", "ozet_zaman": datetime.now(TSI).isoformat(timespec="seconds")}
    except Exception as e:
        log("özet yazılamadı:", str(e)[:120])
        return {k: eski[k] for k in ("ozet", "yatirimci", "imza", "model", "model_ad", "kaynaklar", "gemini_ts", "ozet_zaman") if k in eski}


def main() -> int:
    eski = oku(CIKTI, {})
    riskli = oku(KOK / "data" / "riskli.json", {})
    funds = oku(os.environ.get("FUNDS", KOK / "data" / "funds.json"), {})
    secim = oku(KOK / "data" / "secim.json", {})

    haberler = haberleri_topla(eski.get("haberler") or [])
    log("haber:", len(haberler))

    # etkilenen kurumlar: elle doğrulanmış + haberlerden otomatik tespit edilenler
    kurumlar = [{"ad": k["ad"], "neden": k["neden"], "kaynak": k.get("kaynak", ""), "dogrulandi": True} for k in riskli.get("kurumlar", [])]
    elle = {k["ad"].upper() for k in kurumlar}
    for ad, neden in ((secim.get("riskli") or {}).get("kurum") or {}).items():
        if ad.upper() not in elle:
            kurumlar.append({"ad": ad, "neden": neden, "kaynak": "", "dogrulandi": False})
    adlar = {k["ad"].upper() for k in kurumlar}
    fonlar = []
    for f in funds.get("tum") or []:
        if (f.get("ad") or "").split(" ")[0].upper() in adlar:
            donmus = f.get("g1a") in (0, 0.0, None) and f.get("g3a") in (0, 0.0, None)
            fonlar.append({"kod": f["k"], "ad": f.get("ad", ""), "tur": f.get("tur", ""), "g1a": f.get("g1a"), "g3a": f.get("g3a"),
                           "donmus": bool(donmus)})
    fonlar.sort(key=lambda x: (x["ad"].split(" ")[0], x["g1a"] if x["g1a"] is not None else 0))

    # kriz sürüyor mu?
    ayar = (riskli.get("fon_krizi") or {}).get("aktif", "oto")
    son5 = sum(1 for h in haberler if h["ts"] >= time.time() - 5 * 86400)
    aktif = ayar if isinstance(ayar, bool) else son5 >= 3

    sonuc = {
        "updatedAt": datetime.now(TSI).isoformat(timespec="seconds"),
        "aktif": aktif,
        "son5gun_haber": son5,
        "son24saat_haber": sum(1 for h in haberler if h["ts"] >= time.time() - 86400),
        **ozet_yaz(haberler, eski),
        "kurumlar": kurumlar,
        "fonlar": fonlar,
        "haberler": haberler,
        "yukselis": yukselis_haberleri(eski.get("yukselis") or []),
        "gemini_hata": GEMINI_HATA,
    }
    CIKTI.write_text(json.dumps(sonuc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log("yazıldı: aktif", aktif, "| kurum", len(kurumlar), "| fon", len(fonlar), "| haber", len(haberler))
    return 0


if __name__ == "__main__":
    sys.exit(main())
