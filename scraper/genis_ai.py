"""Hisse Radar - BIST 30 dışındaki hisseler için yapay zekâ değerlendirmesi.

İki çalışma biçimi (MOD):
  populer : en popüler POPULER_N hisseyi (piyasa değeri + işlem hacmi) sırayla, en eski analizden başlayarak yeniler.
  talep   : kullanıcıların uygulamadaki "Yapay zekâya analiz ettir" düğmesiyle istediği hisseleri değerlendirir.
            İstekler anahtarsız ntfy.sh konusundan okunur (TALEP_KONU); yalnızca geçerli hisse kodları alınır,
            bir çalıştırmada en çok TALEP_EN_COK hisse, son 3 saatte analiz edilmiş hisse tekrar edilmez.
Böylece kimsenin bakmadığı hisseler boşuna analiz edilmez, ücretsiz kota korunur.

Kalite kuralları ai_degerlendir.py ile aynıdır: model yalnızca verilen sayılara dayanır, yönü ya da büyüklüğü
tutarsız hedefler atılır, fiyatı eski hisseye karar verilmez. Hacmi çok düşük hisselerde güven düşürülür.

Girdiler: MARKET (market.json), GECMIS (gecmis.json), EK (ek.json, isteğe bağlı), SNAPSHOT (BIST 100 için), NEWS
Çıktı: GENIS_CIKTI (varsayılan data/ai_genis.json). Anahtar: GROQ_API_KEY (GitHub Secrets). OPENROUTER_API_KEY isteğe bağlı.
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

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_degerlendir  # noqa: E402
import kaynaklar  # noqa: E402
from ai_degerlendir import (IST, HISSELER_BIST30, endeks_satiri, json_ayikla, log, oku, openai_uyumlu, openrouter,  # noqa: E402
                            rsi, sma, temizle, GECERSIZ)

KOK = Path(__file__).resolve().parent.parent
CIKTI = Path(os.environ.get("GENIS_CIKTI", KOK / "data" / "ai_genis.json"))
MOD = os.environ.get("MOD", "talep")
GRUP = int(os.environ.get("GENIS_GRUP", "12"))       # bir istekteki hisse sayısı
ISTEK = int(os.environ.get("GENIS_ISTEK", "2"))      # bir çalıştırmadaki en çok istek sayısı
POPULER_N = int(os.environ.get("POPULER_N", "100"))
TALEP_KONU = os.environ.get("TALEP_KONU", "hisseradar-analiz-istek-7k3q")
TALEP_TAZE_SAAT = 3
BEKLE = int(os.environ.get("GENIS_BEKLE", "65"))     # istekler arası bekleme (dakikalık token sınırı için)
DUSUK_HACIM_TL = 20_000_000                          # günlük işlem hacmi bunun altındaysa sığ hisse

# Groq'ta her modelin ayrı günlük kotası var; BIST 30 değerlendirmesi gpt-oss-120b kullandığı için burada önce başkaları denenir
# En kaliteli model önce; kota dolarsa küçük modele düşer
GROQ_TERCIH = ["openai/gpt-oss-120b", "moonshotai/kimi-k2-instruct-0905", "llama-3.3-70b-versatile", "openai/gpt-oss-20b"]


def talepleri_oku(son_ts: int, gecerli: set) -> tuple[list[str], int]:
    """ntfy.sh konusundaki istekleri oku. Dönüş: (istenen kodlar, en yeni istek zamanı)."""
    since = str(son_ts + 1) if son_ts else "12h"
    r = requests.get(f"https://ntfy.sh/{TALEP_KONU}/json", params={"poll": "1", "since": since}, timeout=30)
    r.raise_for_status()
    kodlar, en_yeni = [], son_ts
    for satir_ in r.text.splitlines():
        try:
            m = json.loads(satir_)
        except ValueError:
            continue
        if m.get("event") != "message":
            continue
        en_yeni = max(en_yeni, int(m.get("time") or 0))
        kod = re.sub(r"[^A-Z0-9]", "", str(m.get("message") or "").upper())[:8]
        if kod in gecerli and kod not in kodlar:
            kodlar.append(kod)
    return kodlar, en_yeni


def populerlik(satirlar: list) -> list[str]:
    """Piyasa değeri sırası ile TL işlem hacmi sırasının ortalamasına göre (en popüler başta)."""
    pd_sira = {r[0]: i for i, r in enumerate(sorted(satirlar, key=lambda r: -(r[6] or 0)))}
    hc_sira = {r[0]: i for i, r in enumerate(sorted(satirlar, key=lambda r: -((r[4] or 0) * (r[2] or 0))))}
    return sorted((r[0] for r in satirlar), key=lambda k: (pd_sira[k] + hc_sira[k]) / 2)


def sira_sec(sirali: list[str], eski: dict, adet: int) -> list[str]:
    simdi = time.time()
    def oncelik(i_k):
        i, k = i_k
        agirlik = 4 if i < 100 else 2 if i < 300 else 1
        t = (eski.get(k) or {}).get("ts")
        if not t:
            return (1, -i)  # hiç değerlendirilmemiş: popüler olan önce
        yas = (simdi - t) / 3600
        if yas < 6:
            return (-1, 0)  # 6 saatten yeni analizi tekrar etme
        return (0, yas * agirlik)
    aday = sorted(enumerate(sirali), key=oncelik, reverse=True)
    return [k for i, k in aday if oncelik((i, k))[0] >= 0][:adet]


HABER: dict = {}   # istek üzerine analizde şirket haberleri (Marketaux / Tavily)
GUNDEM: dict = {}  # BIST 30 değerlendirmesinin topladığı makro veriler ve piyasa özeti (data/gundem.json)


def satir(r: list, gecmis: dict, ek: dict, sektor_med: dict, xu: dict | None, sira: int) -> str:
    k, ad, fiyat, deg, hacim, sektor, pd_, tv, rsi_tv, h1, a1, a3, yuk, dus, yb = (r + [None] * 15)[:15]
    parca = [f"{k} ({ad}, {sektor or '-'}; popülerlik sırası {sira + 1}): son {fiyat:g} TL, bugün {deg or 0:+.2f}%"]
    if h1 is not None:
        parca.append(f"1 hafta {h1:+.1f}%")
    if a1 is not None:
        parca.append(f"1 ay {a1:+.1f}%" + (f" (sektör medyanı {sektor_med[sektor]:+.1f}%)" if sektor in sektor_med else ""))
    if a3 is not None:
        parca.append(f"3 ay {a3:+.1f}%")
    if yb is not None:
        parca.append(f"yılbaşından {yb:+.1f}%")
    if xu and a1 is not None:
        parca.append(f"BIST 100'e göre 1 ay {a1 - xu['a1']:+.1f} puan")
    c = [x[1] for x in gecmis.get(k) or []]
    if len(c) >= 21:
        c = c + [fiyat]
        parca.append(f"SMA20 {sma(c, 20):.4g} (fiyat {'üstünde' if fiyat > sma(c, 20) else 'altında'}), RSI14 {rsi(c):.0f}, "
                     f"3 ay düşük/yüksek {min(c):g}/{max(c):g}")
        gunluk = [abs(c[i] / c[i - 1] - 1) * 100 for i in range(1, len(c))]
        parca.append(f"ortalama günlük hareket %{statistics.mean(gunluk[-20:]):.1f}")
    elif rsi_tv is not None:
        parca.append(f"RSI14 {rsi_tv:.0f}")
    if tv is not None:
        parca.append(f"TradingView teknik özet {tv:+.2f} (-1 güçlü sat, +1 güçlü al)")
    e = ek.get(k) or {}
    for alan, yazi in (("fk", "F/K"), ("pddd", "PD/DD"), ("roe", "özsermaye kârlılığı %"), ("borc", "borç/özsermaye")):
        if e.get(alan) is not None and not (alan == "fk" and e[alan] <= 0):
            parca.append(f"{yazi} {e[alan]:.1f}")
    if e.get("sma200"):
        parca.append(f"200 günlük ortalamanın {'üstünde' if fiyat > e['sma200'] else 'altında'}")
    tl_hacim = (hacim or 0) * (fiyat or 0)
    parca.append(f"günlük işlem hacmi {tl_hacim / 1e6:.0f} milyon TL" + (" (SIĞ HİSSE)" if tl_hacim < DUSUK_HACIM_TL else ""))
    if pd_:
        parca.append(f"piyasa değeri {pd_ / 1e9:.1f} milyar TL")
    hb = HABER.get(k)
    return ", ".join(parca) + ai_degerlendir.haber_ai_satiri(k, sektor or "") + (" | Şirket haberleri: " + kaynaklar.haber_satiri(hb, 3) if hb else "")


def istem(satirlar: str, snap: dict, basliklar: str) -> str:
    return f"""Sen Borsa İstanbul'u takip eden temkinli ve dürüst bir analistsin. Bugün {dt.datetime.now(IST):%d.%m.%Y %H:%M}.
Bu kararları sıradan yatırımcılar görecek; yanlış yönlendirmemek en önemli kural.
KURALLAR:
1) YALNIZCA aşağıda verilen sayılara ve haber başlıklarına dayan. Veride olmayan rakam, haber ya da olay uydurma.
2) Her hisse için üç vade: 1 hafta (karar/hedef), 1 ay (karar1a/hedef1a), 3 ay (karar3a/hedef3a).
3) Sinyaller çelişiyorsa ya da emin değilsen TUT de. AL/SAT yalnızca birden fazla gösterge (trend, momentum, sektöre ve endekse göre güç, değerleme) aynı yönü gösteriyorsa.
4) Sert düşüş sonrası RSI 30 altındaysa kısa vadede SAT deme; sert yükseliş sonrası RSI 70 üstündeyse AL deme.
5) "SIĞ HİSSE" yazanlarda fiyat kolay oynatılabilir: güveni en çok 45 ver, AL demekte çok temkinli ol.
6) Hedefler gerçekçi olsun, ortalama günlük hareketi dikkate al: 1 hafta en çok ±%8, 1 ay ±%15, 3 ay ±%30.
   AL ise hedef son fiyatın ÜSTÜNDE, SAT ise ALTINDA, TUT ise son fiyata yakın. Uymayan yanıt otomatik silinir.
7) "guven" 0-100; karışık sinyalde 50 altı. Gerekçe ("neden") en fazla 18 kelime, verideki somut bir sayıyı ansın.
8) "aciklama": en fazla 40 kelime, sade Türkçe. 1 aylık kararı neden verdiğini VE neden diğer iki kararı vermediğini
   verideki sayılarla anlat. Örnek (TUT için): "Neden AL değil: ... Neden SAT değil: ...". Uydurma bilgi yazma.
{endeks_satiri(snap)}{kaynaklar.makro_satiri(GUNDEM.get("makro") or {}, GUNDEM.get("piyasa"))}{ai_degerlendir.haber_ai_genel()}
Hisseler:
{satirlar}

Son haber başlıkları:
{basliklar}

Yalnızca şu biçimde geçerli JSON döndür, başka metin yazma:
{{"hisseler":[{{"k":"KOD","karar":"TUT","guven":50,"hedef":10.2,"karar1a":"TUT","hedef1a":10.4,"karar3a":"AL","hedef3a":11.5,"neden":"...","aciklama":"Neden AL değil: ... Neden SAT değil: ..."}}]}}
Listede yukarıdaki hisselerin hepsi olsun."""


def groq_genis(i: str, anahtar: str):
    """Tercih sırasındaki Groq modellerini dener; kota dolduysa ya da model yoksa sıradakine geçer."""
    try:
        r = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {anahtar}"}, timeout=30)
        mevcut = [m["id"] for m in r.json().get("data", [])]
    except Exception:
        mevcut = []
    MODELLER["groq"] = mevcut
    adaylar = ([os.environ["GROQ_GENIS_MODEL"]] if os.environ.get("GROQ_GENIS_MODEL") else []) + \
        [m for m in GROQ_TERCIH if not mevcut or m in mevcut]
    son = None
    for m in adaylar:
        try:
            return openai_uyumlu("https://api.groq.com/openai/v1", anahtar, m, [], None, i, en_cok=3000, tekrar=1)
        except Exception as e:
            son = e
            if not any(x in str(e) for x in ("429", "413", "404", "400", "503")):
                raise
            log("Groq", m, "olmadı:", str(e)[:100])
    raise RuntimeError(f"Groq: hiçbir model yanıt vermedi ({son})")


MODELLER: dict[str, list] = {}
SAGLAYICILAR = [("groq", "Groq", "GROQ_API_KEY", groq_genis),
                ("cerebras", "Cerebras", "CEREBRAS_API_KEY", lambda i, a: ai_degerlendir.cerebras(i, a, en_cok=3000)),
                ("mistral", "Mistral", "MISTRAL_API_KEY", ai_degerlendir.mistral),
                ("cohere", "Cohere", "COHERE_API_KEY", lambda i, a: ai_degerlendir.cohere(i, a, en_cok=3000))]
# OpenRouter'ın ücretsiz modelleri şu an sürekli 429 veriyor; düzelince GENIS_OPENROUTER=1 ile eklenebilir
if os.environ.get("GENIS_OPENROUTER") == "1":
    SAGLAYICILAR.append(("openrouter", "OpenRouter", "OPENROUTER_API_KEY", lambda i, a: openrouter(i, a, en_cok=3000)))


def main() -> int:
    market = oku(os.environ.get("MARKET", KOK / "data" / "market.json"), {})
    gecmis = (oku(os.environ.get("GECMIS", KOK / "data" / "gecmis.json"), {}) or {}).get("g", {})
    ek_ham = oku(os.environ.get("EK", KOK / "data" / "ek.json"), {}) or {}
    snap = oku(os.environ.get("SNAPSHOT", KOK / "data" / "snapshot.json"), {})
    news = oku(os.environ.get("NEWS", KOK / "data" / "news.json"), {})
    satirlar = [r for r in market.get("hisseler") or [] if r[0] not in HISSELER_BIST30 and isinstance(r[2], (int, float))]
    if not satirlar:
        log("market.json boş")
        return 1
    ek = {k: dict(zip(ek_ham.get("sutun", []), v)) for k, v in (ek_ham.get("ek") or {}).items()}
    sirali = populerlik(satirlar)
    sira_no = {k: i for i, k in enumerate(sirali)}
    by = {r[0]: r for r in satirlar}
    # sektör medyanları ve BIST 100'ün 1 aylık getirisi (göreli güç için)
    sek: dict[str, list] = {}
    for r in satirlar:
        if r[10] is not None and r[5]:
            sek.setdefault(r[5], []).append(r[10])
    sektor_med = {s: statistics.median(v) for s, v in sek.items() if len(v) >= 5}
    xu = None
    x = (snap.get("endeksler") or {}).get("XU100")
    if x and len(x.get("g") or []) > 21:
        xu = {"a1": (x["last"] / x["g"][-22][4] - 1) * 100}
    cop = re.compile(r"hava durumu|hangi kanalda|\bmaç|canlı grafik|stock price today|resm[iî] gazete", re.I)
    basliklar = "\n".join(f"- {n.get('t', '')}: {n.get('title', '')}" for n in
                          [n for n in news.get("items") or [] if not cop.search(n.get("title", ""))][:20])

    eski = oku(CIKTI, {})
    hisseler = dict(eski.get("hisseler") or {})
    # borsadan çıkan hisseleri at
    for k in [k for k in hisseler if k not in by]:
        hisseler.pop(k)
    ts_market = dt.datetime.fromisoformat(market["updatedAt"]).timestamp() if market.get("updatedAt") else time.time()
    fiyatlar = {k: {"last": r[2], "ts": ts_market} for k, r in by.items()}
    talep_son = int(eski.get("talepSon") or 0)
    baslangic = int(time.time())
    if MOD == "talep":
        try:
            istenen, talep_son = talepleri_oku(talep_son, set(by))
        except Exception as e:
            log("istekler okunamadı:", e)
            return 0
        simdi = time.time()
        # Başarısız analizler kaybolmasın: her istek "bekleyen" listesine girer, sonuç gelince çıkar; en çok 3 deneme, denemeler arası 4 dk
        bekleyen = {k: v for k, v in (eski.get("bekleyen") or {}).items() if k in by and v.get("n", 0) < 3}
        sonuclar = {k: v for k, v in (eski.get("sonuclar") or {}).items() if simdi - v.get("ts", 0) < 86400}
        eski["sonuclar"] = sonuclar
        for k in istenen:
            if simdi - ((hisseler.get(k) or {}).get("ts") or 0) > TALEP_TAZE_SAAT * 3600:
                bekleyen[k] = {"n": 0, "ts": 0}
            else:  # zaten taze analiz var: kullanıcıya "yapıldı" de
                sonuclar[k] = {"ts": int(simdi), "durum": "ok"}
        secilen = [k for k, v in bekleyen.items() if simdi - v.get("ts", 0) >= 240][:GRUP * ISTEK]
        log("istenen:", istenen, "bekleyen:", list(bekleyen), "değerlendirilecek:", secilen)
        if not secilen:
            if talep_son != int(eski.get("talepSon") or 0) or bekleyen != (eski.get("bekleyen") or {}) or istenen:
                eski["talepSon"] = talep_son
                eski["bekleyen"] = bekleyen
                CIKTI.write_text(json.dumps(eski, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            return 0
        eski["bekleyen"] = bekleyen
    else:
        # popüler taramada saatte tek istek (12 hisse): Groq'un günlük kotası BIST 30 ve kullanıcı istekleri için korunur
        secilen = sira_sec(sirali[:POPULER_N], hisseler, GRUP)
    log("değerlendirilecek:", len(secilen), "hisse:", ", ".join(secilen[:12]), "...")
    global GUNDEM
    GUNDEM = oku(KOK / "data" / "gundem.json", {})
    ai_degerlendir.GUNDEM = GUNDEM
    # istek üzerine analizde şirketin güncel haberleri de toplanır (kota sınırlı)
    butce = kaynaklar.Butce(eski.setdefault("kullanim", {}), {"tavily": ("ay", 600), "marketaux": ("gun", 45), "cohere": ("ay", 300)})
    ai_degerlendir.BUTCE = butce
    if MOD == "talep":
        for k in secilen[:6]:
            try:
                HABER[k] = kaynaklar.sirket_haberleri(k, by[k][1], butce, tavily_de=butce.kalan("tavily") > 0)
            except Exception as e:
                log("haber alınamadı:", k, e)
    modeller = {m["id"]: m for m in eski.get("modeller") or []}
    hatalar_bu_tur: list[str] = []
    for kimlik, ad, env, fn in SAGLAYICILAR:
        anahtar = os.environ.get(env, "").strip()
        if not anahtar or not secilen:
            continue
        # popüler taramayı Mistral/Cerebras yapar; Groq'un günlük kotası BIST 30 ve kullanıcı istekleri için kalır
        if MOD == "populer" and kimlik == "groq" and (os.environ.get("MISTRAL_API_KEY") or os.environ.get("CEREBRAS_API_KEY")):
            continue
        if MOD == "populer" and kimlik == "cohere":  # Cohere'nin aylık kotası yalnızca kullanıcı isteklerine
            continue
        basari, atilan, model, son_hata = 0, 0, "", ""
        for i in range(0, len(secilen), GRUP):
            parca = secilen[i:i + GRUP]
            metin_satir = "\n".join(satir(by[k], gecmis, ek, sektor_med, xu, sira_no[k]) for k in parca)
            try:
                GECERSIZ.pop(kimlik, None)
                model, metin = fn(istem(metin_satir, snap, basliklar), anahtar)
                sonuc = temizle(json_ayikla(metin), fiyatlar, kimlik)
                atilan += GECERSIZ.get(kimlik, 0)
                for k, v in sonuc.items():
                    if not re.search(r"\d", v.get("neden", "")):  # somut veriye dayanmayan gerekçe: güveni düşür
                        v["guven"] = min(v.get("guven", 50), 40)
                    if (by[k][4] or 0) * (by[k][2] or 0) < DUSUK_HACIM_TL:
                        v["guven"] = min(v.get("guven", 50), 45)
                        v["sig"] = True
                    kayit = dict(hisseler.get(k) or {})
                    kayit[kimlik] = {**v, "model": model}
                    kayit["ts"] = int(time.time())
                    if HABER.get(k):
                        kayit["haber"] = [{"baslik": h["baslik"], "url": h["url"], "kaynak": h.get("kaynak", ""), "duygu": h.get("duygu")}
                                          for h in HABER[k][:4]]
                    hisseler[k] = kayit
                basari += len(sonuc)
            except Exception as e:
                son_hata = str(e).replace(anahtar, "***")[:160]
                hatalar_bu_tur.append(son_hata)
                log(ad, "parça hatası:", son_hata)
                if "per day" in str(e) or "kota" in str(e):
                    break
            if i + GRUP < len(secilen):
                time.sleep(BEKLE)
        onceki = modeller.get(kimlik, {})
        modeller[kimlik] = {"id": kimlik, "ad": ad, "model": model or onceki.get("model", ""),
                            "durum": "ok" if basari else ("eski" if onceki.get("durum") in ("ok", "eski") else "hata"),
                            "son": basari, "atilan": atilan, "hata": son_hata if not basari else "",
                            "zaman": dt.datetime.now(IST).isoformat(timespec="seconds")}
        log(ad, model, basari, "hisse,", atilan, "tutarsız kayıt atıldı")
    if MOD == "talep":  # sonucu gelenleri bekleyenlerden çıkar, gelmeyenlerin deneme sayısını artır
        bek = eski.get("bekleyen") or {}
        sonuclar = eski.setdefault("sonuclar", {})
        # tüm sağlayıcılar limit/kota yüzünden yanıt vermediyse tekrar denemek boşuna: kullanıcıya hemen söyle
        limit = bool(hatalar_bu_tur) and all(re.search(r"429|rate|limit|kota|quota|per day|402|credit", h, re.I) for h in hatalar_bu_tur)
        for k in secilen:
            if (hisseler.get(k) or {}).get("ts", 0) >= baslangic:
                bek.pop(k, None)
                sonuclar[k] = {"ts": int(time.time()), "durum": "ok"}
            elif k in bek:
                bek[k] = {"n": bek[k].get("n", 0) + 1, "ts": int(time.time())}
                if limit or bek[k]["n"] >= 3:
                    bek.pop(k, None)
                    sonuclar[k] = {"ts": int(time.time()), "durum": "hata", "neden": "limit" if limit else "yanit_yok"}
        eski["bekleyen"] = {k: v for k, v in bek.items() if v.get("n", 0) < 3}
    yapilan = sum(1 for k in sirali if k in hisseler)
    cikti = {"updatedAt": dt.datetime.now(IST).isoformat(timespec="seconds"), "talepSon": talep_son, "bekleyen": eski.get("bekleyen", {}),
             "sonuclar": eski.get("sonuclar", {}), "kullanim": eski.get("kullanim", {}),
             "ilerleme": {"yapilan": yapilan, "toplam": len(sirali), "populer": POPULER_N},
             "modeller": list(modeller.values()), "groqModelleri": MODELLER.get("groq", [])[:40], "hisseler": hisseler}
    CIKTI.parent.mkdir(parents=True, exist_ok=True)
    CIKTI.write_text(json.dumps(cikti, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log("yazıldı:", CIKTI, f"{yapilan}/{len(sirali)} hisse değerlendirildi")
    return 0


if __name__ == "__main__":
    sys.exit(main())
