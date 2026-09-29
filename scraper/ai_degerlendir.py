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


HISSELER_BIST30 = set(ALIAS)


def ilgili_haber(kod, news, n=2):
    sonuc = []
    for it in news.get("items") or []:
        b = " " + re.sub(r"[^A-ZÇĞİÖŞÜ0-9]+", " ", it.get("title", "").replace("i", "İ").upper()) + " "
        if it.get("sym") == kod or any(f" {w} " in b for w in [kod] + ALIAS.get(kod, [])):
            sonuc.append(it.get("title", "")[:110])
        if len(sonuc) >= n:
            break
    return sonuc


def degisim_yazi(r) -> str:
    """Yıllık değişimi yanıltmayacak biçimde yaz: işaret değişimi ve çok büyük oranlar ayrıca belirtilir."""
    son, gy, y = r.get("son"), r.get("gecenYil"), r.get("yillik")
    if son is None or gy is None or y is None:
        return "yıllık karşılaştırma yok"
    if gy < 0 <= son:
        return "zarardan kâra geçti (geçen yılın aynı çeyreğine göre)"
    if gy >= 0 > son:
        return "kârdan zarara geçti (geçen yılın aynı çeyreğine göre)"
    if gy < 0 and son < 0:
        return "zarar " + ("azaldı" if son > gy else "büyüdü")
    if y > 200:
        return "yıllık %200'den fazla arttı (düşük bazdan, temkinli yorumla)"
    return f"yıllık {y:+.0f}%"


def temel_ek(kod, temel) -> str:
    x = (temel.get("hisseler") or {}).get(kod)
    if not x:
        return ""
    s = {r["ad"]: r for r in x.get("satirlar", [])}
    o = x.get("oranlar", {})
    parca = []
    for ad in ("Hasılat", "Net kâr"):
        if ad in s:
            parca.append(f"{ad.lower()} {degisim_yazi(s[ad])}")
    ttm = o.get("netKarTTM")
    if ttm is not None and ttm < 0:
        parca.append("son 4 çeyrek toplamı zarar (F/K anlamsız)")
    elif o.get("fk") and x.get("para", "TRY") == "TRY":
        parca.append(f"F/K {o['fk']:.1f}")
    if o.get("pddd") and x.get("para", "TRY") == "TRY":
        parca.append(f"PD/DD {o['pddd']:.1f}")
    if o.get("roe") is not None:
        parca.append(f"ROE %{o['roe']:.0f}")
    return f" | Temel ({x.get('ceyrek','')}): " + ", ".join(parca) if parca else ""


def endeks_satiri(snap) -> str:
    x = (snap.get("endeksler") or {}).get("XU100")
    if not x or not x.get("g"):
        return ""
    c = [r[4] for r in x["g"]]
    last = x["last"]
    d5 = (last / c[-6] - 1) * 100 if len(c) > 6 else 0
    d20 = (last / c[-21] - 1) * 100 if len(c) > 21 else 0
    return (f"\nGenel piyasa: BIST 100 {last:,.0f}, bugün {(last / x['prev'] - 1) * 100:+.2f}%, 5 gün {d5:+.1f}%, 20 gün {d20:+.1f}%, "
            f"20 günlük ortalamanın {'üstünde' if last > sma(c, 20) else 'altında'}, RSI14 {rsi(c):.0f}.\n")


def goreli_guc(h, snap) -> str:
    x = (snap.get("endeksler") or {}).get("XU100")
    g = h.get("g") or []
    if not x or len(x.get("g") or []) < 22 or len(g) < 22:
        return ""
    xi = (x["last"] / x["g"][-22][4] - 1) * 100
    hi = (h["last"] / g[-22][4] - 1) * 100
    return f", BIST 100'e göre 20 günde {hi - xi:+.1f} puan"


def istem_olustur(snap, news, outlook, temel=None) -> str:
    temel = temel or {}
    hisseler = snap.get("hisseler", {})
    def satir(k, h):
        s = ozet_satiri(k, h) + goreli_guc(h, snap) + temel_ek(k, temel)
        hb = ilgili_haber(k, news)
        return s + (" | Haber: " + " / ".join(hb) if hb else "")
    satirlar = "\n".join(satir(k, h) for k, h in sorted(hisseler.items()) if h.get("g"))
    cop = re.compile(r"hava durumu|hangi kanalda|\bdizi\b|\bmaç|burç|canlı grafik|stock price today|hisse senedi canlı|resmî gazete|resmi gazete", re.I)
    basliklar = "\n".join(f"- {n.get('t','')}: {n.get('title','')}" for n in [x for x in (news.get("items") or []) if not cop.search(x.get("title", ""))][:25])
    gundem = ""
    if outlook.get("olaylar"):
        gundem = "\nGünün önemli olayları:\n" + "\n".join(f"- {o.get('baslik','')}: {o.get('detay','')}" for o in outlook["olaylar"][:5])
    return f"""Sen Borsa İstanbul'u takip eden temkinli ve dürüst bir analistsin. Bugün {dt.datetime.now(IST):%d.%m.%Y %H:%M}.
Bu kararları sıradan yatırımcılar görecek; yanlış yönlendirmemek en önemli kural.
Aşağıda BIST 30 hisselerinin güncel teknik verileri, son çeyrek finansalları (varsa) ve son haber başlıkları var.
KURALLAR:
1) YALNIZCA aşağıda verilen sayılara ve haber başlıklarına dayan. Veride olmayan rakam, haber, hedef ya da olay uydurma.
2) Her hisse için ÜÇ vadeyi ayrı değerlendir: 1 hafta (karar/hedef), 1 ay (karar1a/hedef1a), 3 ay (karar3a/hedef3a).
   Kısa vadede teknik görünüm ve haberler, uzun vadede finansallar ve genel trend ağır bassın.
3) Sinyaller çelişiyorsa ya da emin değilsen TUT de. AL/SAT yalnızca birden fazla gösterge aynı yönü gösteriyorsa.
   Genel piyasa yönünü de hesaba kat; her hisseye aynı kararı verme.
4) Sert düşüşten sonra RSI 30'un altındaysa kısa vadede SAT demek geç kalmış olabilir; sert yükselişten sonra RSI 70'in üstündeyse AL demek riskli.
5) Hedefler gerçekçi olsun ve günlük oynaklığı (ATR) aşmasın: 1 hafta en çok ±%8, 1 ay ±%15, 3 ay ±%30.
   AL ise hedef son fiyatın ÜSTÜNDE, SAT ise ALTINDA, TUT ise son fiyata yakın olmalı. Bu kurala uymayan yanıtlar otomatik silinir.
6) "guven" 0-100: sinyaller net ve birbirini destekliyorsa 65 üstü, karışıksa 50 altı ver. Aşırı güvenme.
7) Gerekçe en fazla 18 kelime, Türkçe; mutlaka yukarıdaki veriden somut bir sayı ya da haberi ansın.
{endeks_satiri(snap)}
Hisseler:
{satirlar}

Son haberler:
{basliklar}{gundem}

Yalnızca şu biçimde geçerli JSON döndür, başka hiçbir metin yazma:
{{"hisseler":[{{"k":"ASELS","karar":"AL","guven":65,"hedef":380.5,"karar1a":"AL","hedef1a":395,"karar3a":"TUT","hedef3a":400,"neden":"..."}}]}}
("karar/hedef" 1 hafta, "karar1a/hedef1a" 1 ay, "karar3a/hedef3a" 3 ay içindir.)
Listede yukarıdaki hisselerin hepsi olsun."""


# ------------------------------------------------------------ sağlayıcılar
def json_ayikla(metin: str):
    metin = re.sub(r"<think>.*?</think>", "", metin or "", flags=re.S).strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", metin, re.S)
    if m:
        metin = m.group(1)
    bas = min([i for i in (metin.find("{"), metin.find("[")) if i >= 0], default=-1)
    son = max(metin.rfind("}"), metin.rfind("]"))
    if bas < 0:
        raise ValueError("JSON bulunamadı")
    if son < bas:
        son = len(metin) - 1
    try:
        veri = json.loads(metin[bas: son + 1])
    except ValueError:
        # yanıt yarıda kesildiyse tamamlanmış kayıtları kurtar
        parca = []
        for m in re.finditer(r"\{[^{}]*\}", metin):
            try:
                o = json.loads(m.group(0))
            except ValueError:
                continue
            if isinstance(o, dict) and (o.get("k") or o.get("kod")):
                parca.append(o)
        if not parca:
            raise
        veri = parca
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


def openai_uyumlu(taban: str, anahtar: str, model: str | None, tercihler, filtre, istem: str, json_modu=True, en_cok=8000, tekrar=3):
    bas = {"Authorization": f"Bearer {anahtar}"}
    if not model:
        r = requests.get(f"{taban}/models", headers=bas, timeout=30)
        r.raise_for_status()
        adlar = [m["id"] for m in r.json().get("data", [])]
        model = tercih_et(adlar, tercihler, filtre)
        if not model:
            raise RuntimeError("uygun model yok")
    govde = {"model": model, "temperature": 0.2, "max_tokens": en_cok,
             "messages": [{"role": "system", "content": "Yalnızca geçerli JSON döndür."},
                          {"role": "user", "content": istem}]}
    if "gpt-oss" in model:  # düşünme payı yanıtı yarıda kesmesin
        govde["reasoning_effort"] = "low"
    elif "qwen3" in model and "groq" in taban:
        govde["reasoning_format"] = "hidden"
    if json_modu:
        govde["response_format"] = {"type": "json_object"}
    r = requests.post(f"{taban}/chat/completions", headers=bas, json=govde, timeout=ZAMAN_ASIMI)
    for _ in range(tekrar):
        if r.status_code != 429:
            break
        bekle = min(65, float(r.headers.get("retry-after") or 30) + 2)
        time.sleep(bekle)
        r = requests.post(f"{taban}/chat/completions", headers=bas, json=govde, timeout=ZAMAN_ASIMI)
    if r.status_code == 400 and json_modu:  # bazı modeller json modunu desteklemez
        govde.pop("response_format")
        r = requests.post(f"{taban}/chat/completions", headers=bas, json=govde, timeout=ZAMAN_ASIMI)
    if r.status_code == 400 and ("reasoning_effort" in govde or "reasoning_format" in govde):
        govde.pop("reasoning_effort", None)
        govde.pop("reasoning_format", None)
        r = requests.post(f"{taban}/chat/completions", headers=bas, json=govde, timeout=ZAMAN_ASIMI)
    if not r.ok:
        raise RuntimeError(f"{model} {r.status_code}: {r.text[:160]}")
    ch = r.json()["choices"][0]
    if ch.get("finish_reason") == "length":
        log(model, "uyarı: yanıt uzunluk sınırında kesildi, tamamlanan kayıtlar kurtarılacak")
    return model, ch["message"]["content"] or ""


def groq(istem, anahtar):
    return openai_uyumlu("https://api.groq.com/openai/v1", anahtar, os.environ.get("GROQ_MODEL"),
                         ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "moonshotai/kimi-k2-instruct"],
                         lambda a: ("70b" in a or "120b" in a) and "guard" not in a, istem)


def grok(istem, anahtar):
    return openai_uyumlu("https://api.x.ai/v1", anahtar, os.environ.get("XAI_MODEL"),
                         ["grok-4-fast-non-reasoning", "grok-4-fast", "grok-4", "grok-3-mini", "grok-3"],
                         lambda a: a.startswith("grok") and "image" not in a and "vision" not in a, istem)


_OR_ADAYLAR: list[str] = []


def openrouter(istem, anahtar, en_cok=8000):
    """Ücretsiz OpenRouter modelleri sık sık 429 veriyor; sırayla birkaç ücretsiz modeli dene."""
    global _OR_ADAYLAR
    if os.environ.get("OPENROUTER_MODEL"):
        adaylar = [os.environ["OPENROUTER_MODEL"]]
    else:
        if not _OR_ADAYLAR:
            tercih = ["qwen/qwen3-235b-a22b:free", "deepseek/deepseek-chat-v3.1:free", "meta-llama/llama-3.3-70b-instruct:free",
                      "moonshotai/kimi-k2:free", "openai/gpt-oss-120b:free", "z-ai/glm-4.5-air:free"]
            try:
                r = requests.get("https://openrouter.ai/api/v1/models", timeout=30)
                adlar = [m["id"] for m in r.json().get("data", [])]
            except Exception:
                adlar = tercih
            ucretsiz = [a for a in adlar if a.endswith(":free")]
            buyuk = [a for a in ucretsiz if any(x in a for x in ("70b", "deepseek", "qwen3", "kimi", "gpt-oss-120b", "glm", "235b"))]
            _OR_ADAYLAR = list(dict.fromkeys([t for t in tercih if t in ucretsiz] + sorted(buyuk, reverse=True)))[:5] or tercih[:3]
        adaylar = _OR_ADAYLAR
    son = None
    for m in adaylar:
        try:
            return openai_uyumlu("https://openrouter.ai/api/v1", anahtar, m, [], None, istem, json_modu=False, en_cok=en_cok, tekrar=1)
        except Exception as e:
            son = e
            if not any(x in str(e) for x in ("429", "404", "400", "402", "503")):
                raise
            log("OpenRouter", m, "olmadı, sıradaki model deneniyor:", str(e)[:80])
    raise RuntimeError(f"OpenRouter: hiçbir ücretsiz model yanıt vermedi ({son})")


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
PARCA = {"groq": 6, "openrouter": 15, "mistral": 15}
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


KARAR = {"BUY": "AL", "HOLD": "TUT", "SELL": "SAT", "AL": "AL", "TUT": "TUT", "SAT": "SAT"}


# Vadeye göre izin verilen en büyük hedef sapması ve TUT için en büyük sapma
SINIR = {"": (0.10, 0.04), "1a": (0.20, 0.08), "3a": (0.35, 0.15)}


def _hedef(x, alan, last, vade, karar):
    """Hedef fiyatı doğrula. Dönüş: (hedef | None, geçerli_mi). Yön ya da büyüklük tutarsızsa kayıt geçersizdir."""
    ham = x.get(alan)
    if ham in (None, ""):
        return None, True
    try:
        v = float(str(ham).replace(",", "."))
    except ValueError:
        return None, True
    r = v / last - 1
    buyuk, tut = SINIR[vade]
    if abs(r) > buyuk:
        return None, False
    if (karar == "AL" and r <= 0) or (karar == "SAT" and r >= 0) or (karar == "TUT" and abs(r) > tut):
        return None, False
    return round(v, 2), True


GECERSIZ: dict[str, int] = {}


def temizle(liste, hisseler, kimlik="") -> dict:
    sonuc = {}
    simdi_ts = time.time()
    for x in liste or []:
        if not isinstance(x, dict):
            continue
        kod = str(x.get("k") or x.get("kod") or x.get("sembol") or x.get("symbol") or "").upper().replace(".IS", "").replace("BIST:", "")
        if kod not in hisseler:
            continue
        karar = KARAR.get(str(x.get("karar") or x.get("decision") or "").upper())
        if not karar:
            continue
        h = hisseler[kod]
        if simdi_ts - (h.get("ts") or simdi_ts) > 5 * 86400:  # fiyatı eski olana karar verilmez
            continue
        last = float(h["last"])
        try:
            guven = max(0, min(100, int(float(x.get("guven") or x.get("confidence") or 50))))
        except ValueError:
            guven = 50
        hedef, ok = _hedef(x, "hedef", last, "", karar)
        if not ok:  # yönü ya da büyüklüğü tutarsız kararı atla
            GECERSIZ[kimlik] = GECERSIZ.get(kimlik, 0) + 1
            continue
        k = {"karar": karar, "guven": guven, "hedef": hedef,
             "neden": str(x.get("neden") or x.get("reason") or "")[:200]}
        for vade in ("1a", "3a"):
            kv = KARAR.get(str(x.get("karar" + vade) or "").upper())
            if kv:
                hv, ok = _hedef(x, "hedef" + vade, last, vade, kv)
                if ok:
                    k["karar" + vade] = kv
                    k["hedef" + vade] = hv
                else:
                    GECERSIZ[kimlik] = GECERSIZ.get(kimlik, 0) + 1
        sonuc[kod] = k
    return sonuc


# ------------------------------------------------------------ isabet takibi
GECMIS = Path(os.environ.get("KARAR_GECMISI", KOK / "data" / "karar_gecmisi.json"))
VADE_GUN = {"1h": 5, "1a": 21, "3a": 63}
TUT_ESIK = {"1h": 3.0, "1a": 6.0, "3a": 10.0}


def isabet_guncelle(cikti: dict, snap: dict, outlook: dict) -> dict:
    """Her günün kararlarını kaydeder; vadesi dolan kararları gerçekleşen fiyatla karşılaştırıp son 30 günün isabetini hesaplar."""
    gecmis = oku(GECMIS, {"gunler": {}})
    gunler = gecmis.setdefault("gunler", {})
    hisseler = snap.get("hisseler") or {}
    bugun = dt.datetime.now(IST).strftime("%Y-%m-%d")
    kay = {"fiyat": {k: h["last"] for k, h in hisseler.items() if h.get("last")}, "k": {}}
    kaynaklar = {m: {k: h[m] for k, h in cikti["hisseler"].items() if m in h}
                 for m in {x["id"] for x in cikti["modeller"] if x.get("durum") == "ok"}}
    kaynaklar["claude"] = outlook.get("kararlar") or {}
    for m, kk in kaynaklar.items():
        kay["k"][m] = {k: [v.get("karar"), v.get("karar1a"), v.get("karar3a")] for k, v in kk.items() if v.get("karar")}
    if dt.datetime.now(IST).weekday() < 5:
        gunler[bugun] = kay
    for d in sorted(gunler)[:-130]:
        gunler.pop(d)
    # fiyat geçmişi: kayıtlardaki fiyatlar + snapshot günlük kapanışları
    kapanis: dict[str, dict[str, float]] = {}
    for d, g in gunler.items():
        for k, v in g.get("fiyat", {}).items():
            kapanis.setdefault(k, {})[d] = v
    for k, h in hisseler.items():
        for r in h.get("g") or []:
            kapanis.setdefault(k, {})[r[0]] = r[4]
    sinir = (dt.datetime.now(IST) - dt.timedelta(days=45)).strftime("%Y-%m-%d")
    sonuc: dict[str, dict] = {}
    for d0, g in gunler.items():
        for m, kk in g.get("k", {}).items():
            for kod, kararlar in kk.items():
                seri = kapanis.get(kod, {})
                tarihler = sorted(t for t in seri if t > d0)
                p0 = g.get("fiyat", {}).get(kod) or seri.get(d0)
                for i, vade in enumerate(("1h", "1a", "3a")):
                    karar = kararlar[i] if i < len(kararlar) else None
                    n = VADE_GUN[vade]
                    if not karar or not p0 or len(tarihler) < n:
                        continue
                    bitis = tarihler[n - 1]
                    if bitis < sinir:  # son 30 iş günü civarında vadesi dolanlar
                        continue
                    ret = (seri[bitis] / p0 - 1) * 100
                    dogru = ret > 0 if karar == "AL" else ret < 0 if karar == "SAT" else abs(ret) < TUT_ESIK[vade]
                    x = sonuc.setdefault(m, {}).setdefault(vade, {"n": 0, "d": 0})
                    x["n"] += 1
                    x["d"] += int(dogru)
    for m in sonuc.values():
        for vade, x in m.items():
            m[vade] = {"n": x["n"], "oran": round(x["d"] / x["n"] * 100, 1)}
    GECMIS.write_text(json.dumps(gecmis, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
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
                kararlar.update(temizle(ham, hisseler, kimlik))
                if boy < len(kodlar) and i + boy < len(kodlar):
                    time.sleep(20)
            if len(kararlar) < len(hisseler) * 0.5:
                raise RuntimeError(f"eksik ya da tutarsız yanıt ({len(kararlar)} geçerli hisse, {GECERSIZ.get(kimlik, 0)} tutarsız kayıt)")
            if GECERSIZ.get(kimlik):
                log(ad, GECERSIZ[kimlik], "tutarsız kayıt atıldı")
            for kod, k in kararlar.items():
                cikti["hisseler"].setdefault(kod, {})[kimlik] = k
            cikti["modeller"].append({"id": kimlik, "ad": ad, "model": model, "durum": "ok", "sayi": len(kararlar),
                                      "atilan": GECERSIZ.get(kimlik, 0), "sure": round(time.time() - bas, 1)})
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
    try:
        cikti["isabet"] = isabet_guncelle(cikti, snap, outlook)
    except Exception as e:
        log("isabet hesaplanamadı:", e)
    CIKTI.write_text(json.dumps(cikti, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log("yazıldı:", CIKTI, "çalışan model:", calisan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
