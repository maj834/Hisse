"""Hisse Radar - yapay zekâ değerlendirmesi.

BIST 30 hisselerini birkaç yapay zekâya (Groq/Llama, OpenRouter, isteğe bağlı xAI Grok, Mistral, GitHub Models)
değerlendirtir: her hisse için AL / TUT / SAT kararı, güven puanı, 1 haftalık hedef fiyat ve kısa gerekçe.
Sonuç `data/ai.json` dosyasına yazılır; uygulama bu dosyayı okur.

API anahtarları GitHub Secrets'tan ortam değişkeni olarak gelir (koda asla yazılmaz):
  GROQ_API_KEY, OPENROUTER_API_KEY, XAI_API_KEY, MISTRAL_API_KEY, GH_MODELS_TOKEN
İsteğe bağlı model seçimi: GEMINI_MODEL, GROQ_MODEL, XAI_MODEL, OPENROUTER_MODEL
Girdiler: SNAPSHOT (varsayılan data/snapshot.json), NEWS (data/news.json), OUTLOOK (data/outlook.json)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Europe/Istanbul")
KOK = Path(__file__).resolve().parent.parent
CIKTI = Path(os.environ.get("AI_CIKTI", KOK / "data" / "ai.json"))
ZAMAN_ASIMI = 150


def log(*a):
    print(dt.datetime.now(IST).strftime("%H:%M:%S"), *a, flush=True)


def oku(yol, varsayilan):
    try:
        return json.loads(Path(yol).read_text(encoding="utf-8"))
    except Exception:
        return varsayilan


# ------------------------------------------------------------ göstergeler
def sma(a, n):
    a = a[-n:]
    return sum(a) / len(a) if a else 0.0


def rsi(c, n=14):
    if len(c) < n + 1:
        return 50.0
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    g /= n
    l /= n
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def atr(g, n=14):
    tr = [max(r[2] - r[3], abs(r[2] - g[i][4]), abs(r[3] - g[i][4])) for i, r in enumerate(g[1:])]
    return sma(tr, n) if tr else 0.0


def ozet_satiri(kod, h) -> str:
    g = h.get("g") or []
    c = [r[4] for r in g]
    last, prev = h["last"], h["prev"]
    deg = (last / prev - 1) * 100 if prev else 0
    d5 = (last / c[-6] - 1) * 100 if len(c) > 6 else 0
    d20 = (last / c[-21] - 1) * 100 if len(c) > 21 else 0
    hi20 = max((r[2] for r in g[-20:]), default=last)
    lo20 = min((r[3] for r in g[-20:]), default=last)
    hacim = [r[5] for r in g if r[5]]
    hac_oran = (hacim[-1] / sma(hacim[:-1], 20)) if len(hacim) > 5 and sma(hacim[:-1], 20) else 0
    hi60 = max((r[2] for r in g[-60:]), default=last)
    lo60 = min((r[3] for r in g[-60:]), default=last)
    ema12 = ema26 = c[0] if c else last
    for v in c:
        ema12 = ema12 + (v - ema12) * 2 / 13
        ema26 = ema26 + (v - ema26) * 2 / 27
    return (f"{kod} ({h.get('ad','')}, {h.get('sektor','')}): son {last:g} TL, bugün {deg:+.2f}%, 5 gün {d5:+.1f}%, "
            f"20 gün {d20:+.1f}%, SMA10 {sma(c,10):.2f}, SMA20 {sma(c,20):.2f}, RSI14 {rsi(c):.0f}, "
            f"MACD {'pozitif' if ema12 > ema26 else 'negatif'}, ATR {atr(g):.2f}, 20g düşük/yüksek {lo20:g}/{hi20:g}, "
            f"3 ay düşük/yüksek {lo60:g}/{hi60:g}, hacim ortalamanın {hac_oran:.1f} katı")


ALIAS = {"AEFES": ["EFES"], "AKBNK": ["AKBANK"], "ASELS": ["ASELSAN", "SAVUNMA"], "BIMAS": ["BİM"], "EKGYO": ["EMLAK KONUT"],
         "ENKAI": ["ENKA"], "EREGL": ["ERDEMİR", "EREĞLİ", "ÇELİK"], "FROTO": ["FORD OTOSAN", "OTOMOTİV"], "GARAN": ["GARANTİ"],
         "GUBRF": ["GÜBRETAŞ", "GÜBRE"], "ISCTR": ["İŞ BANKASI", "İŞBANK"], "KCHOL": ["KOÇ"], "TRALT": ["KOZA", "ALTIN"],
         "KRDMD": ["KARDEMİR", "ÇELİK"], "MGROS": ["MİGROS"], "PETKM": ["PETKİM"], "SAHOL": ["SABANCI"], "SASA": ["SASA"],
         "SISE": ["ŞİŞECAM"], "TAVHL": ["TAV", "HAVALİMANI"], "TCELL": ["TURKCELL"], "THYAO": ["THY", "TÜRK HAVA YOLLARI"],
         "TOASO": ["TOFAŞ", "OTOMOTİV"], "TTKOM": ["TÜRK TELEKOM"], "TUPRS": ["TÜPRAŞ", "PETROL", "BRENT"],
         "VAKBN": ["VAKIFBANK"], "YKBNK": ["YAPI KREDİ"], "PGSUS": ["PEGASUS"], "ASTOR": ["ASTOR"], "DSTKF": ["FAKTORİNG"]}


def ilgili_haber(kod, news, n=2):
    sonuc = []
    for it in news.get("items") or []:
        b = " " + re.sub(r"[^A-ZÇĞİÖŞÜ0-9]+", " ", it.get("title", "").replace("i", "İ").upper()) + " "
        if it.get("sym") == kod or any(f" {w} " in b for w in [kod] + ALIAS.get(kod, [])):
            sonuc.append(it.get("title", "")[:110])
        if len(sonuc) >= n:
            break
    return sonuc


def temel_ek(kod, temel) -> str:
    x = (temel.get("hisseler") or {}).get(kod)
    if not x:
        return ""
    s = {r["ad"]: r for r in x.get("satirlar", [])}
    o = x.get("oranlar", {})
    parca = []
    for ad in ("Hasılat", "Net kâr"):
        if ad in s and s[ad].get("yillik") is not None:
            parca.append(f"{ad.lower()} yıllık {s[ad]['yillik']:+.0f}%")
    if o.get("fk"):
        parca.append(f"F/K {o['fk']:.1f}")
    if o.get("pddd"):
        parca.append(f"PD/DD {o['pddd']:.1f}")
    if o.get("roe") is not None:
        parca.append(f"ROE %{o['roe']:.0f}")
    return f" | Temel ({x.get('ceyrek','')}): " + ", ".join(parca) if parca else ""


def istem_olustur(snap, news, outlook, temel=None) -> str:
    temel = temel or {}
    hisseler = snap.get("hisseler", {})
    def satir(k, h):
        s = ozet_satiri(k, h) + temel_ek(k, temel)
        hb = ilgili_haber(k, news)
        return s + (" | Haber: " + " / ".join(hb) if hb else "")
    satirlar = "\n".join(satir(k, h) for k, h in sorted(hisseler.items()) if h.get("g"))
    basliklar = "\n".join(f"- {n.get('t','')}: {n.get('title','')}" for n in (news.get("items") or [])[:25])
    gundem = ""
    if outlook.get("olaylar"):
        gundem = "\nGünün önemli olayları:\n" + "\n".join(f"- {o.get('baslik','')}: {o.get('detay','')}" for o in outlook["olaylar"][:5])
    return f"""Sen Borsa İstanbul'u takip eden deneyimli bir analistsin. Bugün {dt.datetime.now(IST):%d.%m.%Y %H:%M}.
Aşağıda BIST 30 hisselerinin güncel teknik verileri, son çeyrek finansalları (varsa) ve son haber başlıkları var. Teknik görünümü, şirketin finansal durumunu ve haberleri birlikte değerlendir. Her hisse için 1 haftalık vadede
karar ver: "AL", "TUT" ya da "SAT". Gerçekçi ol: her hisseye AL deme, zayıf olanlara SAT de.
Karar verirken şunları birlikte tart: trend (fiyatın ortalamalara göre yeri, MACD), momentum (RSI; 30 altı aşırı satım, 70 üstü aşırı alım),
destek/direnç (20 gün ve 3 ay aralığı), hacim teyidi, şirketin son çeyrek finansalları ve hisseye özel haberler ile genel gündem.
Tek bir göstergeye dayanma; sinyaller çelişiyorsa TUT de ve güveni düşük tut.
Her hisse için 1 hafta içinde ulaşabileceği gerçekçi bir hedef fiyat ver (genellikle son fiyatın ±%8'i içinde, günlük oynaklığı (ATR) dikkate al;
AL için hedef son fiyatın üstünde, SAT için altında olsun).
Gerekçe en fazla 18 kelime, Türkçe; somut bir veri ya da haberi ansın.

Hisseler:
{satirlar}

Son haberler:
{basliklar}{gundem}

Yalnızca şu biçimde geçerli JSON döndür, başka hiçbir metin yazma:
{{"hisseler":[{{"k":"ASELS","karar":"AL","guven":65,"hedef":380.5,"neden":"..."}}]}}
Listede yukarıdaki hisselerin hepsi olsun."""


# ------------------------------------------------------------ sağlayıcılar
def json_ayikla(metin: str):
    metin = metin.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", metin, re.S)
    if m:
        metin = m.group(1)
    bas = min([i for i in (metin.find("{"), metin.find("[")) if i >= 0], default=-1)
    son = max(metin.rfind("}"), metin.rfind("]"))
    if bas < 0 or son < 0:
        raise ValueError("JSON bulunamadı")
    veri = json.loads(metin[bas: son + 1])
    if isinstance(veri, dict):
        veri = veri.get("hisseler") or veri.get("stocks") or next((v for v in veri.values() if isinstance(v, list)), [])
    return veri


def tercih_et(adaylar: list[str], tercihler: list[str], filtre=None) -> str | None:
    for t in tercihler:
        for a in adaylar:
            if a == t or a.endswith("/" + t):
                return a
    uygun = [a for a in adaylar if filtre is None or filtre(a)]
    return sorted(uygun, reverse=True)[0] if uygun else None


def _surum(ad: str) -> float:
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", ad)
    return float(m.group(1)) if m else 0.0


def gemini(istem: str, anahtar: str):
    adaylar = [os.environ["GEMINI_MODEL"]] if os.environ.get("GEMINI_MODEL") else []
    try:
        r = requests.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={anahtar}&pageSize=200", timeout=30)
        r.raise_for_status()
        yasak = ("lite", "image", "tts", "live", "audio", "embedding", "exp", "vision", "learnlm", "gemma", "thinking", "robotics", "computer")
        adlar = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        flash = [a for a in adlar if "flash" in a and not any(y in a for y in yasak)]
        kararli = sorted([a for a in flash if "preview" not in a], key=_surum, reverse=True)
        onizleme = sorted([a for a in flash if "preview" in a], key=_surum, reverse=True)
        adaylar += kararli + onizleme
    except Exception as e:
        log("Gemini model listesi alınamadı:", str(e).replace(anahtar, "***")[:120])
    adaylar += ["gemini-flash-latest", "gemini-2.5-flash"]
    son = None
    for model in list(dict.fromkeys(adaylar))[:8]:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={anahtar}",
            json={"contents": [{"parts": [{"text": istem}]}],
                  "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"}},
            timeout=ZAMAN_ASIMI,
        )
        if r.status_code in (400, 402, 403, 404):  # model yok ya da ücretsiz planda kapalı: sıradakini dene
            son = f"{model}: {r.status_code} {r.text[:100]}"
            continue
        if r.status_code == 429:
            time.sleep(30)
            son = f"{model}: 429"
            continue
        r.raise_for_status()
        parcalar = r.json()["candidates"][0]["content"]["parts"]
        return model, "".join(p.get("text", "") for p in parcalar)
    raise RuntimeError(f"Gemini hiçbir modelle çalışmadı ({son})")


def openai_uyumlu(taban: str, anahtar: str, model: str | None, tercihler, filtre, istem: str, json_modu=True):
    bas = {"Authorization": f"Bearer {anahtar}"}
    if not model:
        r = requests.get(f"{taban}/models", headers=bas, timeout=30)
        r.raise_for_status()
        adlar = [m["id"] for m in r.json().get("data", [])]
        model = tercih_et(adlar, tercihler, filtre)
        if not model:
            raise RuntimeError("uygun model yok")
    govde = {"model": model, "temperature": 0.3,
             "messages": [{"role": "system", "content": "Yalnızca geçerli JSON döndür."},
                          {"role": "user", "content": istem}]}
    if json_modu:
        govde["response_format"] = {"type": "json_object"}
    r = requests.post(f"{taban}/chat/completions", headers=bas, json=govde, timeout=ZAMAN_ASIMI)
    for _ in range(3):
        if r.status_code != 429:
            break
        bekle = min(65, float(r.headers.get("retry-after") or 30) + 2)
        time.sleep(bekle)
        r = requests.post(f"{taban}/chat/completions", headers=bas, json=govde, timeout=ZAMAN_ASIMI)
    if r.status_code == 400 and json_modu:  # bazı modeller json modunu desteklemez
        govde.pop("response_format")
        r = requests.post(f"{taban}/chat/completions", headers=bas, json=govde, timeout=ZAMAN_ASIMI)
    r.raise_for_status()
    return model, r.json()["choices"][0]["message"]["content"]


def groq(istem, anahtar):
    return openai_uyumlu("https://api.groq.com/openai/v1", anahtar, os.environ.get("GROQ_MODEL"),
                         ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "moonshotai/kimi-k2-instruct"],
                         lambda a: ("70b" in a or "120b" in a) and "guard" not in a, istem)


def grok(istem, anahtar):
    return openai_uyumlu("https://api.x.ai/v1", anahtar, os.environ.get("XAI_MODEL"),
                         ["grok-4-fast-non-reasoning", "grok-4-fast", "grok-4", "grok-3-mini", "grok-3"],
                         lambda a: a.startswith("grok") and "image" not in a and "vision" not in a, istem)


def openrouter(istem, anahtar):
    return openai_uyumlu("https://openrouter.ai/api/v1", anahtar, os.environ.get("OPENROUTER_MODEL"),
                         ["qwen/qwen3-235b-a22b:free", "meta-llama/llama-3.3-70b-instruct:free",
                          "deepseek/deepseek-chat-v3.1:free"],
                         lambda a: a.endswith(":free") and ("70b" in a or "deepseek" in a or "qwen3" in a), istem,
                         json_modu=False)


def github_models(istem, anahtar, tercihler, filtre):
    """GitHub Models: GitHub Actions'ın kendi GITHUB_TOKEN'ı ile ücretsiz (anahtar gerekmez)."""
    model = None
    try:
        r = requests.get("https://models.github.ai/catalog/models", headers={"Authorization": f"Bearer {anahtar}", "Accept": "application/json"}, timeout=30)
        if r.ok:
            adlar = [m.get("id") for m in r.json() if m.get("id")]
            model = tercih_et(adlar, tercihler, filtre)
    except Exception:
        pass
    model = model or tercihler[0]
    bas = {"Authorization": f"Bearer {anahtar}", "Accept": "application/json", "Content-Type": "application/json",
           "X-GitHub-Api-Version": "2022-11-28"}
    govde = {"model": model, "temperature": 0.3, "max_tokens": 4000,
             "messages": [{"role": "system", "content": "Yalnızca geçerli JSON döndür."}, {"role": "user", "content": istem}]}
    def gonder(g):
        url = "https://models.github.ai/inference/chat/completions"
        for _ in range(4):  # yönlendirmelerde POST gövdesini koru
            r = requests.post(url, headers=bas, json=g, timeout=ZAMAN_ASIMI, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = requests.compat.urljoin(url, r.headers["location"])
                continue
            return r
        return r

    r = gonder({**govde, "response_format": {"type": "json_object"}})
    if r.status_code == 400:
        r = gonder(govde)
    if "json" not in r.headers.get("content-type", ""):
        raise RuntimeError("GitHub Models yanıt vermedi; GH_MODELS_TOKEN anahtarında 'Models: Read' izni olmalı")
    if not r.ok:
        raise RuntimeError(f"{model} {r.status_code} {r.text[:150]}")
    try:
        return model, r.json()["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError(f"{model} {r.status_code} yanıt okunamadı: {r.headers.get('content-type','')} {r.text[:150]!r} url={r.url}")


def gpt(istem, anahtar):
    return github_models(istem, os.environ.get("GH_MODELS_TOKEN") or anahtar,
                         [os.environ.get("GPT_MODEL") or "openai/gpt-4.1", "openai/gpt-4.1-mini", "openai/gpt-4o"],
                         lambda a: a.startswith("openai/gpt-4"))


def deepseek_gh(istem, anahtar):
    return github_models(istem, os.environ.get("GH_MODELS_TOKEN") or anahtar,
                         [os.environ.get("DEEPSEEK_MODEL") or "deepseek/DeepSeek-V3-0324", "deepseek/deepseek-v3-0324"],
                         lambda a: a.lower().startswith("deepseek/") and "r1" not in a.lower())


def mistral(istem, anahtar):
    return openai_uyumlu("https://api.mistral.ai/v1", anahtar, os.environ.get("MISTRAL_MODEL"),
                         ["mistral-large-latest", "mistral-medium-latest", "mistral-small-latest"],
                         lambda a: a.endswith("-latest") and ("large" in a or "medium" in a), istem)


# Ücretsiz katmanda dakikalık metin sınırı düşük olanlara hisseler küçük gruplar hâlinde gönderilir
PARCA = {"groq": 8, "mistral": 15}
# Günlük ücretsiz kotası dar olanlar her saat değil, N saatte bir çalışır (arada önceki analiz gösterilir)
PERIYOT = {"groq": 2}

SAGLAYICILAR = [
    ("groq", "Llama (Groq)", "GROQ_API_KEY", groq),
    ("grok", "Grok", "XAI_API_KEY", grok),
    ("openrouter", "OpenRouter", "OPENROUTER_API_KEY", openrouter),
    ("gpt", "GPT (OpenAI)", "GH_MODELS_TOKEN", gpt),
    ("deepseek", "DeepSeek", "GH_MODELS_TOKEN", deepseek_gh),
    ("mistral", "Mistral", "MISTRAL_API_KEY", mistral),
]


def temizle(liste, hisseler) -> dict:
    sonuc = {}
    for x in liste or []:
        if not isinstance(x, dict):
            continue
        kod = str(x.get("k") or x.get("kod") or x.get("sembol") or x.get("symbol") or "").upper().replace(".IS", "").replace("BIST:", "")
        if kod not in hisseler:
            continue
        karar = str(x.get("karar") or x.get("decision") or "").upper()
        karar = {"BUY": "AL", "HOLD": "TUT", "SELL": "SAT", "AL": "AL", "TUT": "TUT", "SAT": "SAT"}.get(karar)
        if not karar:
            continue
        last = float(hisseler[kod]["last"])
        try:
            hedef = float(str(x.get("hedef") or x.get("target") or "").replace(",", "."))
        except ValueError:
            hedef = None
        if hedef is not None and not (last * 0.7 <= hedef <= last * 1.3):
            hedef = None  # gerçek dışı hedefleri at
        try:
            guven = max(0, min(100, int(float(x.get("guven") or x.get("confidence") or 50))))
        except ValueError:
            guven = 50
        sonuc[kod] = {"karar": karar, "guven": guven, "hedef": round(hedef, 2) if hedef else None,
                      "neden": str(x.get("neden") or x.get("reason") or "")[:200]}
    return sonuc


def main() -> int:
    snap = oku(os.environ.get("SNAPSHOT", KOK / "data" / "snapshot.json"), {})
    news = oku(os.environ.get("NEWS", KOK / "data" / "news.json"), {})
    outlook = oku(os.environ.get("OUTLOOK", KOK / "data" / "outlook.json"), {})
    hisseler = snap.get("hisseler") or {}
    if not hisseler:
        log("snapshot boş")
        return 1
    temel = oku(os.environ.get("TEMEL", KOK / "data" / "temel.json"), {})
    istem = istem_olustur(snap, news, outlook, temel)
    eski = oku(CIKTI, {})
    cikti = {"updatedAt": dt.datetime.now(IST).isoformat(timespec="seconds"),
             "veri": snap.get("guncelleme", ""), "modeller": [], "hisseler": {}}
    eski_hisse = eski.get("hisseler", {})
    calisan = 0
    for kimlik, ad, env, fn in SAGLAYICILAR:
        anahtar = os.environ.get(env, "").strip()
        if not anahtar:
            continue
        onceki_ok = next((m for m in eski.get("modeller", []) if m.get("id") == kimlik and m.get("durum") in ("ok", "eski")), None)
        if (onceki_ok and os.environ.get("GITHUB_EVENT_NAME") == "schedule"
                and dt.datetime.now(IST).hour % PERIYOT.get(kimlik, 1) != 0):
            for kod, h in eski_hisse.items():
                if kimlik in h:
                    cikti["hisseler"].setdefault(kod, {})[kimlik] = h[kimlik]
            cikti["modeller"].append(onceki_ok)
            continue
        bas = time.time()
        try:
            kodlar = sorted(hisseler)
            boy = PARCA.get(kimlik, len(kodlar))
            kararlar, model = {}, ""
            for i in range(0, len(kodlar), boy):
                alt = {**snap, "hisseler": {k: hisseler[k] for k in kodlar[i:i + boy]}}
                parca_istem = istem if boy >= len(kodlar) else istem_olustur(alt, news, outlook, temel)
                model, metin = fn(parca_istem, anahtar)
                try:
                    ham = json_ayikla(metin)
                except Exception as e:
                    raise RuntimeError(f"JSON okunamadı ({e}); yanıt başı: {metin[:120]!r}")
                kararlar.update(temizle(ham, hisseler))
                if boy < len(kodlar) and i + boy < len(kodlar):
                    time.sleep(20)
            if len(kararlar) < len(hisseler) * 0.5:
                raise RuntimeError(f"eksik yanıt ({len(kararlar)} hisse)")
            for kod, k in kararlar.items():
                cikti["hisseler"].setdefault(kod, {})[kimlik] = k
            cikti["modeller"].append({"id": kimlik, "ad": ad, "model": model, "durum": "ok", "sayi": len(kararlar),
                                      "sure": round(time.time() - bas, 1)})
            calisan += 1
            log(ad, model, len(kararlar), "hisse")
        except Exception as e:
            mesaj = str(e)
            if anahtar in mesaj:
                mesaj = mesaj.replace(anahtar, "***")
            log(ad, "hata:", mesaj[:200])
            # önceki başarılı sonucu koru
            onceki = next((m for m in eski.get("modeller", []) if m.get("id") == kimlik and m.get("durum") == "ok"), None)
            if onceki:
                for kod, h in eski_hisse.items():
                    if kimlik in h:
                        cikti["hisseler"].setdefault(kod, {})[kimlik] = h[kimlik]
                cikti["modeller"].append({**onceki, "durum": "eski", "hata": mesaj[:120]})
            else:
                cikti["modeller"].append({"id": kimlik, "ad": ad, "durum": "hata", "hata": mesaj[:120]})
    CIKTI.parent.mkdir(parents=True, exist_ok=True)
    CIKTI.write_text(json.dumps(cikti, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log("yazıldı:", CIKTI, "çalışan model:", calisan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
