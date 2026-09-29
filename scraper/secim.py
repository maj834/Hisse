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
        if s.get("sonuc"):
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
    for tur in ("zayif", "guclu"):
        biten = [s for s in gecmis if s["tur"] == tur and s.get("sonuc")]
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
    bugun = datetime.now(TSI).strftime("%Y-%m-%d")

    puanla(gecmis, fiyat)

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
                      "r20": g.get("r20"), "zirve": g.get("zirve")})
    zayif.sort(key=lambda x: (-x["kural_sayisi"], -x["isabet"], (x.get("rsi") or 50)))
    zayif = zayif[:EN_COK_ZAYIF]

    # ---- güçlü gidecekler (ölçüm sürüyor)
    sat_kodlari = {b["kod"] for b in bt.get("bugun", []) if b["karar"] == "SAT"}
    adaylar = []
    for kod, g in gunluk.items():
        if kod in sat_kodlari or (g.get("likit") or 0) < MIN_LIKIT:
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
        if secilen is None and eski.get("tarih") == tarih and eski.get("guclu_not") == "ok":
            # aynı gün yeniden çalıştı: Cerebras'ın bugünkü seçimini koru (krediyi tekrar harcama)
            secilen = [{"kod": x["kod"], "neden": x.get("neden", "")} for x in eski.get("guclu", []) if x["kod"] in {a["kod"] for a in adaylar}]
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
                          "oy": f'{sum(o["karar"] == "AL" for o in a["ai_oylari"])}/{len(a["ai_oylari"])}'})

    # ---- bugünkü seçimleri kaydet (aynı hisse vadesi dolmadan tekrar sayılmaz)
    acik = {(s["kod"], s["tur"]) for s in gecmis if not s.get("sonuc")}
    for tur, liste in (("zayif", zayif), ("guclu", guclu)):
        for x in liste:
            if (x["kod"], tur) in acik or x["kod"] not in fiyat or tarih not in fiyat[x["kod"]]:
                continue
            gecmis.append({"tarih": tarih, "kod": x["kod"], "tur": tur, "vade_gun": VADE_GUN,
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
        "canli": ozet(gecmis),
        "gecmis": gecmis,
        "cerebras": cer,
    }
    CIKTI.write_text(json.dumps(sonuc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(json.dumps({k: v for k, v in sonuc.items() if k != "gecmis"}, ensure_ascii=False)[:3000])


if __name__ == "__main__":
    main()
