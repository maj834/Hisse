"""Günlük yüksek ihtimalli sinyaller + canlı isabet takibi -> data/secim.json

İki liste üretir (az ama öz):
- "zayif": Piyasadan (BIST 100) zayıf kalması beklenenler. Yalnızca geçmiş veri testinde (backtest.py)
  eğitim ve test döneminde tutmuş kurallara uyan hisseler. Oran ölçülmüştür.
- "guclu": Piyasadan güçlü gitmesi beklenenler. Geçmiş testte doğrulanmış bir AL kuralı henüz yok; bu yüzden
  yalnızca yapay zekâ kararları + yükselen trend + haber + teknik aynı yönü gösterdiğinde, en çok 3 hisse.
  Son elemeyi Cerebras yapar (günde 1 çağrı, toplam bütçe sınırlı). "Ölçüm sürüyor" etiketiyle gösterilir.

Her seçim o günün kapanış fiyatı ve BIST 100 ile kaydedilir; vade dolunca (1 ay = 21 işlem günü)
"piyasadan iyi/kötü gitti mi" diye puanlanır. Tutmayanlar da sayılır, hiçbir şey silinmez.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

KOK = Path(__file__).resolve().parent.parent
VERI = KOK / "data"
CIKTI = VERI / "secim.json"
TSI = timezone(timedelta(hours=3))

VADE_GUN = 21                 # 1 ay (işlem günü)
EN_COK_ZAYIF = 5
EN_COK_GUCLU = 3
MIN_LIKIT = 50e6              # günlük ~50 mn TL altı hisse seçilmez (sığ hissede sinyal güvenilmez)

# Cerebras: 5 $ kredi (30 Ocak 2027'ye kadar). Güvenlik payı bırak.
CEREBRAS_BUTCE = float(os.environ.get("CEREBRAS_BUTCE_USD", "4.0"))
CEREBRAS_FIYAT = (0.35 / 1e6, 0.75 / 1e6)   # $/token (girdi, çıktı) — tutucu tahmin
CEREBRAS_SON_GUN = "2027-01-30"


def log(*a):
    print(*a, flush=True)


def oku(ad, varsayilan=None):
    try:
        return json.loads((VERI / ad).read_text(encoding="utf-8"))
    except Exception:
        return {} if varsayilan is None else varsayilan


# ---------------------------------------------------------------- canlı takip
def puanla(gecmis: list, fiyat: dict) -> None:
    """Vadesi dolan seçimleri gerçek fiyatlarla puanla."""
    xu = fiyat.get("XU100", {})
    for s in gecmis:
        if s.get("sonuc") or s["tur"] == "fon":
            continue
        seri = fiyat.get(s["kod"])
        if not seri or not xu:
            continue
        gunler = sorted(g for g in seri if g in xu)
        if s["tarih"] not in gunler:
            continue
        i = gunler.index(s["tarih"])
        if len(gunler) - 1 - i < s.get("vade_gun", VADE_GUN):
            # ara durum (henüz sonuç değil)
            son = gunler[-1]
            s["ara"] = round(((seri[son] / seri[s["tarih"]]) - (xu[son] / xu[s["tarih"]])) * 100, 2)
            s["ara_gun"] = len(gunler) - 1 - i
            continue
        j = gunler[i + s.get("vade_gun", VADE_GUN)]
        getiri = seri[j] / seri[s["tarih"]] - 1
        xg = xu[j] / xu[s["tarih"]] - 1
        fark = getiri - xg
        dogru = fark < 0 if s["tur"] == "zayif" else fark > 0
        s["sonuc"] = {"tarih": j, "getiri": round(getiri * 100, 2), "bist100": round(xg * 100, 2),
                      "fark": round(fark * 100, 2), "dogru": bool(dogru)}
        s.pop("ara", None)
        s.pop("ara_gun", None)


def ozet(gecmis: list) -> dict:
    o = {}
    for tur in ("zayif", "guclu", "fon"):
        biten = [s for s in gecmis if s["tur"] == tur and s.get("sonuc") and "dogru" in s["sonuc"]]
        son = biten[-60:]
        o[tur] = {"n": len(biten), "dogru": sum(s["sonuc"]["dogru"] for s in biten),
                  "son60_n": len(son), "son60_dogru": sum(s["sonuc"]["dogru"] for s in son),
                  "bekleyen": sum(1 for s in gecmis if s["tur"] == tur and not s.get("sonuc"))}
    return o


# ---------------------------------------------------------------- yapay zekâ oyları
def yas_saat(v) -> float:
    try:
        if isinstance(v, (int, float)):
            return (datetime.now(timezone.utc).timestamp() - v) / 3600
        return (datetime.now(timezone.utc) - datetime.fromisoformat(v)).total_seconds() / 3600
    except Exception:
        return 1e9


def ai_oylari(kod: str, ai: dict, genis: dict, outlook: dict) -> list[dict]:
    """Yalnızca taze (3 günden yeni) yapay zekâ kararları sayılır."""
    oy = []
    k1 = ai.get("hisseler", {}).get(kod) if yas_saat(ai.get("updatedAt")) < 72 else None
    k2 = genis.get("hisseler", {}).get(kod)
    if isinstance(k2, dict) and yas_saat(k2.get("ts")) >= 72:
        k2 = None
    for kaynak in (k1, k2):
        if not isinstance(kaynak, dict):
            continue
        for mid, v in kaynak.items():
            if isinstance(v, dict) and v.get("karar1a"):
                oy.append({"ad": mid, "karar": v["karar1a"], "guven": v.get("guven") or 50, "neden": v.get("neden", "")})
    kd = (outlook.get("kararlar") or {}).get(kod) if isinstance(outlook.get("kararlar"), dict) and yas_saat(outlook.get("updatedAt")) < 36 else None
    if isinstance(kd, dict) and (kd.get("karar1a") or kd.get("karar")):
        oy.append({"ad": "claude", "karar": kd.get("karar1a") or kd.get("karar"), "guven": kd.get("guven") or 50, "neden": kd.get("neden", "")})
    return oy


def haber_etkisi(kod: str, sektor: str, gundem: dict, outlook: dict) -> str:
    h = (gundem.get("haber_ai") or {})
    e = (h.get("hisseler") or {}).get(kod)
    if e and (e.get("onem") or 0) >= 2:
        return e.get("etki", "")
    for s in h.get("sektorler") or []:
        if s.get("sektor") == sektor and (s.get("onem") or 0) >= 2:
            return s.get("etki", "")
    if any(x.get("sembol") == kod for x in outlook.get("dusus") or []):
        return "olumsuz"
    if any(x.get("sembol") == kod for x in outlook.get("yukselis") or []):
        return "olumlu"
    return ""


def durum(g: dict) -> dict:
    """Hisse detayında gösterilecek genel durum (yüzdeler)."""
    f = lambda v, c=100: None if v is None else round(v * c, 1)
    return {"r20": f(g.get("r20")), "r60": f(g.get("r60")), "rs60": f(g.get("rs60")), "s200": f(g.get("s200")),
            "zirve": f(g.get("zirve")), "rsi": None if g.get("rsi") is None else round(g["rsi"], 1),
            "trend": g.get("trend"), "hacim_mn": None if not g.get("likit") else round(g["likit"] / 1e6)}


# ---------------------------------------------------------------- risk (soruşturma, temerrüt, tasfiye ...)
RISK_KELIME = re.compile(r"soruşturma|gözaltı|yakalama kararı|tutukla|yurt ?dışı (çıkış )?yasağı|tedbir|iflas|konkordato|temerrüt|"
                         r"tasfiye|el kon|kayyum|tmsf|dolandırıcılık|manipülasyon|işlem yasağı|kara para", re.I)
GENEL_KELIME = {"TÜRKİYE", "TÜRK", "BORSA", "İSTANBUL", "ANADOLU", "GLOBAL", "YATIRIM", "HOLDİNG", "ENERJİ", "PORTFÖY", "GRUP", "DOĞU", "BATI"}


def fon_kurum(ad: str) -> str:
    return (ad or "").split(" ")[0].upper()


def risk_listesi(market: dict, haberler: list[str]) -> dict:
    """Elle doğrulanmış liste (data/riskli.json) + haberlerden otomatik tespit. Dönüş: {"kurum": {AD: neden}, "hisse": {KOD: neden}}."""
    r = oku("riskli.json")
    kurum = {k["ad"].upper(): k["neden"] for k in r.get("kurumlar", [])}
    hisse = {h["kod"]: h["neden"] for h in r.get("hisseler", [])}
    ilk_ad = {}
    for kod, m in market.items():
        w = fon_kurum(m.get("ad", ""))
        if len(w) >= 4 and w not in GENEL_KELIME:
            ilk_ad.setdefault(w, kod)
    for b in haberler:
        if not b or not RISK_KELIME.search(b):
            continue
        B = b.upper().replace("İ", "İ")
        for kod in market:
            if re.search(rf"\b{re.escape(kod)}\b", B) and kod not in hisse:
                hisse[kod] = "Haber: " + b[:160]
        for w, kod in ilk_ad.items():
            if re.search(rf"\b{re.escape(w)}\b", B) and kod not in hisse:
                hisse[kod] = "Haber: " + b[:160]
        m = re.search(r"([A-ZÇĞİÖŞÜ]{3,})\s+PORTFÖY", B)
        if m and m.group(1) not in kurum:
            kurum[m.group(1)] = "Haber: " + b[:160]
    return {"kurum": kurum, "hisse": hisse}


# ---------------------------------------------------------------- fonlar
PASIF_TUR = ("para piyasası", "kısa vadeli", "kira sertifika", "katılım para")


def fon_sec(fon_ai: dict, fonlar: dict, riskli_kurum: dict | None = None) -> list[dict]:
    """Öne çıkan fonlar: yapay zekâların hepsi 1 aylık AL + kendi türünde getiri sırası üstte + tek günlük sıçrama yok."""
    if yas_saat(fon_ai.get("updatedAt")) > 48 or not fonlar:
        return []
    tum = fonlar.get("tum") or []
    tur_grup: dict = {}
    for f in tum:
        tur_grup.setdefault(f.get("tur") or "?", []).append(f)

    def sira(f, alan):
        l = sorted([x for x in tur_grup.get(f.get("tur") or "?", []) if x.get(alan) is not None], key=lambda x: x[alan])
        if len(l) < 5 or f.get(alan) is None:
            return None
        return l.index(f) / (len(l) - 1)

    modeller = [m["id"] for m in fon_ai.get("modeller", []) if m.get("durum") != "hata"]
    tmap = {f["k"]: f for f in tum}
    aday = []
    for k, v in (fon_ai.get("fonlar") or {}).items():
        f = tmap.get(k)
        if not f or any(s in (f.get("tur") or "").lower() for s in PASIF_TUR):
            continue
        if fon_kurum(f.get("ad", "")) in (riskli_kurum or {}):
            continue   # soruşturma/temerrüt/tasfiye yaşayan kurumun fonu önerilmez
        detay = (fonlar.get("fonlar") or {}).get(k) or {}
        if f.get("g1a") == 0 or (detay.get("tarih") and (datetime.now(TSI).date() - datetime.fromisoformat(detay["tarih"]).date()).days > 5):
            continue   # fiyatı donmuş ya da güncellenmeyen fon
        oy = [v[m] for m in modeller if isinstance(v.get(m), dict) and v[m].get("karar1a")]
        if len(oy) < 2 or any(o["karar1a"] != "AL" for o in oy):
            continue
        s1, s3 = sira(f, "g1a"), sira(f, "g3a")
        if s1 is None or s3 is None or s1 < 0.6 or s3 < 0.5 or (f.get("g1a") or 0) > 25:
            continue
        g = ((fonlar.get("fonlar") or {}).get(k) or {}).get("g") or []
        c = [x[1] for x in g[-23:]]
        sicrama = max([abs(c[i] / c[i - 1] - 1) * 100 for i in range(1, len(c)) if c[i - 1]] or [0])
        if sicrama >= 5 and sicrama >= abs(f.get("g1a") or 0) * 0.5:
            continue
        guven = sum(o.get("guven") or 50 for o in oy) / len(oy)
        aday.append({"kod": k, "ad": f.get("ad", ""), "kategori": f.get("tur", ""), "g1a": f.get("g1a"), "g3a": f.get("g3a"),
                     "sira1a": round(s1 * 100), "sira3a": round(s3 * 100), "oy": f"{len(oy)}/{len(oy)}",
                     "neden": " · ".join((o.get("neden") or "")[:140] for o in oy)[:300],
                     "_puan": guven + (s1 + s3) * 20})
    aday.sort(key=lambda a: -a["_puan"])
    return [{k2: v2 for k2, v2 in a.items() if k2 != "_puan"} for a in aday[:3]]


def fon_puanla(gecmis: list, fonlar: dict) -> None:
    """Fon seçimi 1 ay sonra: fonun 1 aylık getirisi kendi türündeki fonların ortancasından iyi mi?"""
    tum = fonlar.get("tum") or []
    if not tum:
        return
    tmap = {f["k"]: f for f in tum}
    bugun = datetime.now(TSI).date()
    for s in gecmis:
        if s["tur"] != "fon" or s.get("sonuc"):
            continue
        gun = (bugun - datetime.fromisoformat(s["tarih"]).date()).days
        f = tmap.get(s["kod"])
        if gun < 30 or not f or f.get("g1a") is None:
            s["ara_gun"] = gun
            continue
        if gun > 36:   # ölçüm penceresi kaçtı: dürüstçe "ölçülemedi"
            s["sonuc"] = {"olculemedi": True}
            continue
        grup = sorted(x["g1a"] for x in tum if x.get("tur") == f.get("tur") and x.get("g1a") is not None)
        orta = grup[len(grup) // 2] if grup else 0
        s["sonuc"] = {"tarih": bugun.isoformat(), "getiri": f["g1a"], "tur_ortanca": orta,
                      "fark": round(f["g1a"] - orta, 2), "dogru": bool(f["g1a"] > orta)}
        s.pop("ara_gun", None)


# ---------------------------------------------------------------- Cerebras (son eleme)
def cerebras_sec(adaylar: list[dict], durum: dict, bugun: str) -> tuple[list[dict] | None, str]:
    anahtar = os.environ.get("CEREBRAS_API_KEY")
    if not anahtar:
        return None, "anahtar yok"
    if bugun > CEREBRAS_SON_GUN:
        return None, "kredi süresi doldu"
    if durum.get("harcanan_usd", 0) >= CEREBRAS_BUTCE:
        return None, "bütçe doldu"
    if durum.get("son_gun") == bugun:
        return None, "bugün zaten kullanıldı"
    istem = (
        "Sen temkinli bir Borsa İstanbul analistisin. Aşağıdaki adaylar arasından ÖNÜMÜZDEKİ 1 AYDA BIST 100 endeksinden "
        f"DAHA İYİ performans gösterme ihtimali en yüksek EN ÇOK {EN_COK_GUCLU} hisse seç. Emin değilsen daha az seç, hiç seçmemek de doğru bir cevaptır. "
        "Yalnızca verilen sayılara dayan, bilgi uydurma. Çok yükselmiş (RSI>68 ya da 1 ayda %25+) hisselerde geri çekilme riskini, "
        "zayıf trendi ve olumsuz haberi eleme sebebi say. Her seçim için gerekçede en az iki somut rakam kullan.\n"
        'Yalnızca şu JSON\'u döndür: {"secilen":[{"kod":"XXXXX","neden":"en çok 200 karakter"}],"elenen_not":"kısa"}\n\n'
        "Adaylar:\n" + json.dumps(adaylar, ensure_ascii=False)
    )
    govde = {"model": os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b"), "temperature": 0.1, "max_tokens": 2500,
             "messages": [{"role": "user", "content": istem}], "response_format": {"type": "json_object"}}
    if "gpt-oss" in govde["model"]:
        govde["reasoning_effort"] = "medium"
    try:
        r = requests.post("https://api.cerebras.ai/v1/chat/completions", json=govde, timeout=90,
                          headers={"Authorization": f"Bearer {anahtar}"})
        if r.status_code == 400:  # model bazı ayarları desteklemiyorsa sade istekle bir kez daha (400 ücretlendirilmez)
            govde.pop("reasoning_effort", None)
            govde.pop("response_format", None)
            r = requests.post("https://api.cerebras.ai/v1/chat/completions", json=govde, timeout=90,
                              headers={"Authorization": f"Bearer {anahtar}"})
        durum["son_gun"] = bugun
        durum["cagri"] = durum.get("cagri", 0) + 1
        if r.status_code != 200:
            return None, f"hata {r.status_code}: {r.text[:120]}"
        d = r.json()
        u = d.get("usage") or {}
        maliyet = (u.get("prompt_tokens", 0) * CEREBRAS_FIYAT[0] + u.get("completion_tokens", 0) * CEREBRAS_FIYAT[1])
        durum["harcanan_usd"] = round(durum.get("harcanan_usd", 0) + maliyet, 5)
        durum["token"] = durum.get("token", 0) + u.get("total_tokens", 0)
        metin = d["choices"][0]["message"]["content"] or ""
        m = re.search(r"\{.*\}", metin, re.S)
        j = json.loads(m.group(0) if m else metin)
        kodlar = {a["kod"] for a in adaylar}
        secilen = [s for s in j.get("secilen", []) if s.get("kod") in kodlar][:EN_COK_GUCLU]
        return secilen, "ok"
    except Exception as e:
        return None, f"hata: {str(e)[:120]}"


# ---------------------------------------------------------------- ana akış
def main():
    bt = oku("backtest.json")
    gecici = json.loads(Path(os.environ.get("BT_GECICI", "/tmp/bt_gecici.json")).read_text(encoding="utf-8"))
    fiyat, gunluk = gecici["fiyat"], gecici["gunluk"]
    eski = oku("secim.json")
    gecmis = eski.get("gecmis", [])
    cer = eski.get("cerebras", {})
    ai, genis, gundem, outlook = oku("ai.json"), oku("ai_genis.json"), oku("gundem.json"), oku("outlook.json")
    market = {}
    try:
        md = requests.get("https://raw.githubusercontent.com/maj834/Hisse/data/market.json", timeout=30).json()
        s = md["sutun"]
        market = {x[s.index("k")]: {"ad": x[s.index("ad")], "sektor": x[s.index("sektor")]} for x in md["hisseler"]}
    except Exception as e:
        log("market.json:", e)

    xu = fiyat.get("XU100", {})
    tarih = max(xu) if xu else datetime.now(TSI).strftime("%Y-%m-%d")
    haberler = []
    try:
        nw = requests.get("https://raw.githubusercontent.com/maj834/Hisse/data/news.json", timeout=30).json()
        haberler += [n.get("title", "") for n in nw.get("items", [])]
    except Exception as e:
        log("news.json:", e)
    haberler += [f'{o.get("baslik", "")} {o.get("detay", "")}' for o in outlook.get("olaylar", [])]
    for sk in (gundem.get("sirket") or {}).values():
        haberler += [h.get("baslik", "") for h in sk.get("haberler", [])]
    risk = risk_listesi(market, haberler)
    log("Riskli:", risk)
    bugun = datetime.now(TSI).strftime("%Y-%m-%d")

    puanla(gecmis, fiyat)
    try:
        fonlar = requests.get("https://raw.githubusercontent.com/maj834/Hisse/data/funds.json", timeout=60).json()
    except Exception as e:
        log("funds.json:", e)
        fonlar = {}
    fon_puanla(gecmis, fonlar)
    fon = fon_sec(oku("fon_ai.json"), fonlar, risk["kurum"])
    log("Fon seçimi:", [f["kod"] for f in fon])

    # ---- zayıf kalacaklar (ölçülmüş kurallar)
    uyum = (bt.get("uyum") or {}).get("SAT") or []
    # eğitim döneminde en iyi olan "en az kaç kural" eşiği (test dönemine bakmadan seçilir), yeterli örnek şartıyla
    esik = 1
    iyi = [u for u in uyum if u["egitim"][1] >= 150]
    if iyi:
        esik = max(iyi, key=lambda u: u["egitim"][0])["en_az"]
    esik_bilgi = next((u for u in uyum if u["en_az"] == esik), None)
    zayif = []
    for b in bt.get("bugun", []):
        if b["karar"] != "SAT":
            continue
        g = gunluk.get(b["kod"], {})
        if (g.get("likit") or 0) < MIN_LIKIT:
            continue
        if b["kural_sayisi"] < esik:
            continue
        sek = market.get(b["kod"], {}).get("sektor", "")
        if haber_etkisi(b["kod"], sek, gundem, outlook) == "olumlu":
            continue   # haber tersini söylüyorsa gösterme
        grup = next((u for u in uyum if u["en_az"] == min(b["kural_sayisi"], len(uyum))), None)
        zayif.append({**b, "ad": market.get(b["kod"], {}).get("ad", ""),
                      "grup_isabet": grup["test"][0] if grup else None, "grup_n": grup["test"][1] if grup else None,
                      "durum": durum(g)})
    zayif.sort(key=lambda x: (-x["kural_sayisi"], -x["isabet"], (x.get("rsi") or 50)))
    zayif = zayif[:EN_COK_ZAYIF]

    # ---- güçlü gidecekler (ölçüm sürüyor)
    sat_kodlari = {b["kod"] for b in bt.get("bugun", []) if b["karar"] == "SAT"}
    adaylar = []
    for kod, g in gunluk.items():
        if kod in sat_kodlari or kod in risk["hisse"] or (g.get("likit") or 0) < MIN_LIKIT:
            continue
        if g.get("trend") != 1 or (g.get("rsi") or 0) >= 70 or (g.get("rsi") or 0) < 45:
            continue
        if (g.get("rs60_sira") or 0) < 0.5 or (g.get("r20") or 0) > 0.25:
            continue
        oy = ai_oylari(kod, ai, genis, outlook)
        al = [o for o in oy if o["karar"] == "AL" and (o["guven"] or 0) >= 50]
        sat = [o for o in oy if o["karar"] == "SAT"]
        if sat or not al or (len(oy) >= 2 and len(al) < 2):
            continue
        sek = market.get(kod, {}).get("sektor", "")
        haber = haber_etkisi(kod, sek, gundem, outlook)
        if haber == "olumsuz":
            continue
        adaylar.append({"kod": kod, "ad": market.get(kod, {}).get("ad", ""), "sektor": sek,
                        "rsi": g.get("rsi"), "getiri_1ay_%": round((g.get("r20") or 0) * 100, 1),
                        "getiri_3ay_%": round((g.get("r60") or 0) * 100, 1),
                        "bist100e_gore_3ay_%": round((g.get("rs60") or 0) * 100, 1),
                        "200gun_ort_uzaklik_%": round((g.get("s200") or 0) * 100, 1),
                        "zirveye_uzaklik_%": round((g.get("zirve") or 0) * 100, 1),
                        "gunluk_islem_mn_tl": round((g.get("likit") or 0) / 1e6),
                        "haber": haber or "yok",
                        "ai_oylari": [{"karar": o["karar"], "guven": o["guven"], "neden": o["neden"][:160]} for o in oy],
                        "_puan": sum(o["guven"] for o in al) / max(1, len(oy)) + (5 if haber == "olumlu" else 0)})
    adaylar.sort(key=lambda a: -a["_puan"])
    adaylar = adaylar[:10]
    log(f"Güçlü aday: {len(adaylar)}")

    guclu, cer_not = [], ""
    if adaylar:
        temiz = [{k: v for k, v in a.items() if k != "_puan"} for a in adaylar]
        secilen, cer_not = cerebras_sec(temiz, cer, bugun)
        log("Cerebras:", cer_not, secilen)
        bugunku = [s for s in gecmis if s["tur"] == "guclu" and s["tarih"] == tarih]
        if secilen is None and bugunku:
            # aynı gün yeniden çalıştı: bugün kaydedilen seçimi koru (Cerebras kredisini tekrar harcama)
            amap0 = {a["kod"] for a in adaylar}
            secilen = [{"kod": s["kod"], "neden": s.get("neden", "")} for s in bugunku if s["kod"] in amap0]
            cer_not = "ok"
        if secilen is None:
            # yedek: yalnızca en az 2 kaynağın AL dediği adaylar, en çok 2 tane
            secilen = [{"kod": a["kod"], "neden": "; ".join(o["neden"] for o in a["ai_oylari"] if o["karar"] == "AL")[:220]}
                       for a in adaylar if sum(o["karar"] == "AL" for o in a["ai_oylari"]) >= 2][:2]
        amap = {a["kod"]: a for a in adaylar}
        for s in secilen:
            a = amap[s["kod"]]
            guclu.append({"kod": a["kod"], "ad": a["ad"], "neden": s.get("neden", ""), "rsi": round(a["rsi"] or 0, 1),
                          "getiri_1ay": a["getiri_1ay_%"], "bist100e_gore_3ay": a["bist100e_gore_3ay_%"],
                          "oy": f'{sum(o["karar"] == "AL" for o in a["ai_oylari"])}/{len(a["ai_oylari"])}',
                          "durum": durum(gunluk.get(a["kod"], {}))})

    # ---- riskli çıkan açık öneriler geri çekilir ve YANLIŞ sayılır (hatayı gizlemeyiz)
    fon_ad = {f["k"]: f.get("ad", "") for f in (fonlar.get("tum") or [])}
    for s in gecmis:
        if s.get("sonuc") or s["tur"] == "zayif":
            continue
        neden = risk["kurum"].get(fon_kurum(fon_ad.get(s["kod"], ""))) if s["tur"] == "fon" else risk["hisse"].get(s["kod"])
        if neden:
            s["sonuc"] = {"tarih": bugun, "dogru": False, "geri_cekildi": True, "neden": neden[:200]}

    # ---- bugünkü seçimleri kaydet (aynı hisse vadesi dolmadan tekrar sayılmaz)
    acik = {(s["kod"], s["tur"]) for s in gecmis if not s.get("sonuc")}
    for tur, liste in (("zayif", zayif), ("guclu", guclu), ("fon", fon)):
        for x in liste:
            if (x["kod"], tur) in acik:
                continue
            if tur == "fon":
                gecmis.append({"tarih": bugun, "kod": x["kod"], "tur": "fon", "kategori": x.get("kategori", ""), "neden": (x.get("neden") or "")[:240]})
                continue
            if x["kod"] not in fiyat or tarih not in fiyat[x["kod"]]:
                continue
            gecmis.append({"tarih": tarih, "kod": x["kod"], "tur": tur, "vade_gun": VADE_GUN, "neden": (x.get("neden") or "")[:240],
                           "fiyat": fiyat[x["kod"]][tarih], "bist100": xu.get(tarih)})
    gecmis = gecmis[-600:]

    sonuc = {
        "updatedAt": datetime.now(TSI).isoformat(timespec="seconds"),
        "tarih": tarih,
        "zayif": zayif,
        "zayif_olcum": {"esik_kural": esik, "test_isabet": esik_bilgi["test"][0] if esik_bilgi else None,
                        "test_n": esik_bilgi["test"][1] if esik_bilgi else None,
                        "taban": next((k["test"]["taban"] for k in bt.get("kurallar", []) if k["karar"] == "SAT"), None),
                        "donem": bt.get("donem")},
        "guclu": guclu,
        "guclu_not": cer_not,
        "fon": fon,
        "riskli": risk,
        "canli": ozet(gecmis),
        "gecmis": gecmis,
        "cerebras": cer,
    }
    CIKTI.write_text(json.dumps(sonuc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(json.dumps({k: v for k, v in sonuc.items() if k != "gecmis"}, ensure_ascii=False)[:3000])


if __name__ == "__main__":
    main()
