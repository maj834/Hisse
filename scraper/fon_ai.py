"""Hisse Radar - fonlar için yapay zekâ değerlendirmesi.

Takip listesindeki fonları (fonlar.txt) ve grafiği tutulan fonların en çok yatırımcısı olanlarını
Groq (Llama) ve OpenRouter'a değerlendirtir: 1 hafta / 1 ay / 3 ay için AL / TUT / SAT, beklenen getiri (%)
ve kısa gerekçe. Sonuç `data/fon_ai.json` dosyasına yazılır.

Kalite kuralları: model yalnızca verilen sayılara dayanır; yönüyle çelişen ya da gerçek dışı
getiri tahminleri otomatik atılır; para piyasası gibi fonlara SAT denmez (bu fonlarda zarar beklentisi gerçek dışıdır).

Anahtarlar GitHub Secrets'tan gelir: GROQ_API_KEY, OPENROUTER_API_KEY. Koda asla yazılmaz.
Girdiler: FUNDS (funds.json), NEWS (news.json). Çıktı: FON_AI_CIKTI (varsayılan data/fon_ai.json)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_degerlendir  # noqa: E402
import kaynaklar  # noqa: E402
from ai_degerlendir import IST, KARAR, groq_yedekli, json_ayikla, log, oku, openai_uyumlu, openrouter  # noqa: E402

KOK = Path(__file__).resolve().parent.parent
CIKTI = Path(os.environ.get("FON_AI_CIKTI", KOK / "data" / "fon_ai.json"))
EN_FAZLA = int(os.environ.get("FON_SAYI", "60"))
PARCA = 10

# vade: (en büyük |getiri| %, TUT için en büyük |getiri| %)
SINIR = {"": (6.0, 3.0), "1a": (12.0, 6.0), "3a": (25.0, 12.0)}
SAT_OLMAZ = re.compile(r"para piyasası|kısa vadeli borçlanma|kira sertifika", re.I)

KONU = [(r"PETROL|ENERJİ", ["PETROL", "BRENT", "OPEC", "ENERJİ"]), (r"ALTIN|KIYMETLİ MADEN", ["ALTIN", "ONS"]),
        (r"GÜMÜŞ|KIYMETLİ MADEN", ["GÜMÜŞ"]), (r"TEKNOLOJİ|BİLİŞİM", ["TEKNOLOJİ", "YAPAY ZEKA", "NASDAQ", "ÇİP"]),
        (r"YABANCI|ABD|GLOBAL|DÜNYA", ["NASDAQ", "S&P", "ABD", "FED", "WALL STREET"]),
        (r"HİSSE|BIST|ENDEKS", ["BORSA İSTANBUL", "BIST", "BORSA"]),
        (r"PARA PİYASASI|BORÇLANMA|KİRA SERTİFİKA|KATILIM|TAHVİL|BONO", ["FAİZ", "TCMB", "MERKEZ BANKASI", "ENFLASYON"]),
        (r"EUROBOND|DÖVİZ|DOLAR|EURO", ["DOLAR", "EURO", "KUR"]), (r"BANKA", ["BANKA"]), (r"SAVUNMA", ["SAVUNMA", "ASELSAN"])]


def ust(s: str) -> str:
    return " " + re.sub(r"[^A-ZÇĞİÖŞÜ0-9&]+", " ", str(s or "").replace("i", "İ").upper()) + " "


def konular(f: dict) -> list[str]:
    s = ust(f.get("ad", "") + " " + f.get("tur", "") + " " + f.get("kategori", ""))
    w: list[str] = []
    for desen, ks in KONU:
        if re.search(desen, s):
            w += [k for k in ks if k not in w]
    return w


def haberler(f: dict, news: dict, n=2) -> list[str]:
    ws = konular(f)
    out = []
    for it in news.get("items") or []:
        t = ust(it.get("title", ""))
        if f" {f['k']} " in t or any(f" {w} " in t for w in ws):
            out.append(it.get("title", "")[:100])
        if len(out) >= n:
            break
    return out


def secilen_fonlar(funds: dict) -> list[str]:
    try:
        takip = [s.split()[0].upper() for s in (KOK / "fonlar.txt").read_text(encoding="utf-8").splitlines()
                 if s.strip() and not s.startswith("#")]
    except Exception:
        takip = []
    detay = funds.get("fonlar") or {}
    liste = [k for k in takip if k in detay]
    for k, f in sorted(detay.items(), key=lambda kv: -(kv[1].get("yatirimci") or 0)):
        if len(liste) >= EN_FAZLA:
            break
        if k not in liste and len(f.get("g") or []) >= 25:
            liste.append(k)
    return liste


def kategori_medyan(tum: list) -> dict:
    gr: dict[str, dict[str, list]] = {}
    for f in tum:
        for m in ("g1a", "g3a", "g6a"):
            if f.get(m) is not None:
                gr.setdefault(f.get("tur") or "?", {}).setdefault(m, []).append(f[m])
    return {t: {m: round(statistics.median(v), 1) for m, v in d.items()} for t, d in gr.items()}


def fon_satiri(k: str, funds: dict, med: dict, news: dict) -> str:
    d = (funds.get("fonlar") or {}).get(k, {})
    t = next((x for x in funds.get("tum") or [] if x["k"] == k), {})
    g = d.get("getiri") or {m: t.get(m) for m in ("g1a", "g3a", "g6a", "gyb", "g1y")}
    tur = d.get("tur") or t.get("tur") or ""
    parca = [f"{k} ({d.get('ad') or t.get('ad', '')}; tür: {tur}; kategori: {d.get('kategori', '')}; risk {d.get('r') or t.get('r') or '-'}/7)"]
    parca.append("getiri " + ", ".join(f"{a} %{g[m]:+.1f}" for a, m in (("1 ay", "g1a"), ("3 ay", "g3a"), ("6 ay", "g6a"), ("1 yıl", "g1y"))
                                       if g.get(m) is not None))
    m = med.get(tur) or {}
    if m:
        parca.append("aynı türdeki fonların medyanı " + ", ".join(f"{a} %{m[x]:+.1f}" for a, x in (("1 ay", "g1a"), ("3 ay", "g3a")) if x in m))
    s = [r[1] for r in d.get("g") or []]
    if len(s) >= 25:
        son = s[-1]
        sma20 = sum(s[-20:]) / 20
        d5 = (son / s[-6] - 1) * 100
        zip_ = max(abs(s[i] / s[i - 1] - 1) * 100 for i in range(len(s) - 22, len(s)))
        parca.append(f"grafik: fiyat 20 günlük ortalamanın {'üstünde' if son > sma20 else 'altında'}, son 5 iş günü %{d5:+.1f}, "
                     f"son 1 ayda en büyük günlük hareket %{zip_:.1f}" + (" (tek günlük sıçrama, kalıcı eğilim sayma)" if zip_ >= 5 else ""))
    if d.get("yatirimci"):
        parca.append(f"{d['yatirimci']} yatırımcı")
    hb = haberler({"k": k, "ad": d.get("ad") or t.get("ad", ""), "tur": tur, "kategori": d.get("kategori", "")}, news)
    if hb:
        parca.append("ilgili haber: " + " / ".join(hb))
    return " | ".join(parca)


def _gundem_metni() -> str:
    g = oku(KOK / "data" / "gundem.json", {})
    ai_degerlendir.GUNDEM = g
    return kaynaklar.makro_satiri(g.get("makro") or {}, g.get("piyasa")) + ai_degerlendir.haber_ai_genel()


def istem(kodlar, funds, med, news) -> str:
    satirlar = "\n".join(fon_satiri(k, funds, med, news) for k in kodlar)
    return f"""Sen Türkiye'deki yatırım fonlarını (TEFAS) değerlendiren temkinli ve dürüst bir analistsin. Bugün {dt.datetime.now(IST):%d.%m.%Y}.
Bu kararları sıradan yatırımcılar görecek; yanlış yönlendirmemek en önemli kural.
KURALLAR:
1) YALNIZCA aşağıdaki sayılara ve haberlere dayan; veride olmayan bilgi uydurma.
2) Her fon için üç vade: 1 hafta (karar/getiri), 1 ay (karar1a/getiri1a), 3 ay (karar3a/getiri3a). "getiri" beklenen yüzde getiridir (ör. 1.5 = +%1,5).
3) Fonu kendi türündeki fonların medyanıyla karşılaştır. Geçmişte çok kazandı diye AL deme; tek günlük sıçramaları eğilim sayma.
4) Emin değilsen ya da sinyaller çelişiyorsa TUT de. Para piyasası, kısa vadeli borçlanma ve kira sertifikası fonlarına SAT deme (düşük riskli, faiz getirisi sağlar); bunlarda genelde TUT uygundur.
5) Getiri tahminleri gerçekçi olsun: 1 hafta en çok ±%6, 1 ay ±%12, 3 ay ±%25. AL ise getiri pozitif, SAT ise negatif olmalı. Kurala uymayan yanıt otomatik silinir.
6) "guven" 0-100; karışık sinyalde 50 altı.
7) Gerekçe en fazla 18 kelime, Türkçe, verideki somut bir sayıyı ansın.
{_gundem_metni()}
Petrol, altın, dolar, faiz ve borsa hakkında YALNIZCA yukarıdaki makro verilere ve haber özetine dayan; yönünü bilmediğin bir piyasa hareketinden bahsetme.

Fonlar:
{satirlar}

Yalnızca şu biçimde geçerli JSON döndür:
{{"fonlar":[{{"k":"AES","karar":"TUT","guven":55,"getiri":0.8,"karar1a":"AL","getiri1a":3.5,"karar3a":"TUT","getiri3a":6,"neden":"..."}}]}}
Listede yukarıdaki fonların hepsi olsun."""


def _getiri(x, alan, vade, karar):
    ham = x.get(alan)
    if ham in (None, ""):
        return None, True
    try:
        v = float(str(ham).replace(",", ".").replace("%", ""))
    except ValueError:
        return None, True
    buyuk, tut = SINIR[vade]
    if abs(v) > buyuk or (karar == "AL" and v <= 0) or (karar == "SAT" and v >= 0) or (karar == "TUT" and abs(v) > tut):
        return None, False
    return round(v, 2), True


def temizle(liste, turler: dict) -> tuple[dict, int]:
    sonuc, atilan = {}, 0
    for x in liste or []:
        if not isinstance(x, dict):
            continue
        k = str(x.get("k") or x.get("kod") or "").upper().strip()
        if k not in turler:
            continue
        karar = KARAR.get(str(x.get("karar") or "").upper())
        if not karar:
            continue
        sat_olmaz = bool(SAT_OLMAZ.search(turler[k] or ""))
        if karar == "SAT" and sat_olmaz:
            atilan += 1
            continue
        g, ok = _getiri(x, "getiri", "", karar)
        if not ok:
            atilan += 1
            continue
        try:
            guven = max(0, min(100, int(float(x.get("guven") or 50))))
        except ValueError:
            guven = 50
        k_ = {"karar": karar, "guven": guven, "getiri": g, "neden": str(x.get("neden") or "")[:200]}
        for vade in ("1a", "3a"):
            kv = KARAR.get(str(x.get("karar" + vade) or "").upper())
            if kv and not (kv == "SAT" and sat_olmaz):
                gv, ok = _getiri(x, "getiri" + vade, vade, kv)
                if ok:
                    k_["karar" + vade] = kv
                    k_["getiri" + vade] = gv
                    continue
            if kv:
                atilan += 1
        sonuc[k] = k_
    return sonuc, atilan


def groq_fon(istem_, anahtar):
    """Hisse değerlendirmesinden ayrı bir Groq modeli (her modelin kendi günlük kotası var)."""
    tercih = [os.environ["GROQ_FON_MODEL"]] if os.environ.get("GROQ_FON_MODEL") else []
    # hisse analiziyle aynı modelin günlük kotasını paylaşmamak için önce farklı model
    tercih += ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    return groq_yedekli(istem_, anahtar, tercih, en_cok=3000)


SAGLAYICILAR = [
    ("groq", "Groq", "GROQ_API_KEY", groq_fon),
    ("cohere", "Cohere", "COHERE_API_KEY", lambda i, a: openai_uyumlu("https://api.cohere.ai/compatibility/v1", a, "command-a-03-2025", [], None, i, en_cok=3000)),
    ("mistral", "Mistral", "MISTRAL_API_KEY", lambda i, a: openai_uyumlu("https://api.mistral.ai/v1", a, os.environ.get("MISTRAL_MODEL"),
                                                                         ["mistral-large-latest", "mistral-medium-latest"], None, i, en_cok=3000)),
]


def main() -> int:
    funds = oku(os.environ.get("FUNDS", KOK / "data" / "funds.json"), {})
    news = oku(os.environ.get("NEWS", KOK / "data" / "news.json"), {})
    if not funds.get("fonlar"):
        log("funds.json boş")
        return 1
    kodlar = secilen_fonlar(funds)
    turler = {k: " ".join(filter(None, [(funds["fonlar"].get(k) or {}).get("tur"), (funds["fonlar"].get(k) or {}).get("kategori")])) for k in kodlar}
    med = kategori_medyan(funds.get("tum") or [])
    eski = oku(CIKTI, {})
    cikti = {"updatedAt": dt.datetime.now(IST).isoformat(timespec="seconds"), "modeller": [], "fonlar": {}}
    for kimlik, ad, env, fn in SAGLAYICILAR:
        anahtar = os.environ.get(env, "").strip()
        if not anahtar:
            continue
        bas = time.time()
        try:
            kararlar, model, atilan, son_hata = {}, "", 0, ""
            for i in range(0, len(kodlar), PARCA):
                parca = kodlar[i:i + PARCA]
                try:
                    model, metin = fn(istem(parca, funds, med, news), anahtar)
                    sonuc, at = temizle(json_ayikla(metin), turler)
                    kararlar.update(sonuc)
                    atilan += at
                except Exception as e:  # tek parça başarısızsa diğerlerine devam et
                    son_hata = str(e).replace(anahtar, "***")[:160]
                    log(ad, "parça hatası:", son_hata)
                    if "per day" in str(e) or "kota" in str(e):
                        break
                if i + PARCA < len(kodlar):
                    time.sleep(25)
            if len(kararlar) < len(kodlar) * 0.4:
                raise RuntimeError(f"eksik ya da tutarsız yanıt ({len(kararlar)} geçerli fon, {atilan} tutarsız kayıt) {son_hata}")
            for k, v in kararlar.items():
                cikti["fonlar"].setdefault(k, {})[kimlik] = v
            cikti["modeller"].append({"id": kimlik, "ad": ad, "model": model, "durum": "ok", "sayi": len(kararlar),
                                      "atilan": atilan, "sure": round(time.time() - bas, 1)})
            log(ad, model, len(kararlar), "fon,", atilan, "tutarsız kayıt atıldı")
        except Exception as e:
            mesaj = str(e).replace(anahtar, "***")
            log(ad, "hata:", mesaj[:200])
            onceki = next((m for m in eski.get("modeller", []) if m.get("id") == kimlik and m.get("durum") in ("ok", "eski")), None)
            if onceki:
                for k, h in (eski.get("fonlar") or {}).items():
                    if kimlik in h:
                        cikti["fonlar"].setdefault(k, {})[kimlik] = h[kimlik]
                cikti["modeller"].append({**onceki, "durum": "eski", "hata": mesaj[:120]})
            else:
                cikti["modeller"].append({"id": kimlik, "ad": ad, "durum": "hata", "hata": mesaj[:120]})
    CIKTI.parent.mkdir(parents=True, exist_ok=True)
    CIKTI.write_text(json.dumps(cikti, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log("yazıldı:", CIKTI, len(cikti["fonlar"]), "fon")
    return 0


if __name__ == "__main__":
    sys.exit(main())
