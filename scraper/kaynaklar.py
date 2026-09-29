"""Hisse Radar - ek bilgi kaynakları (Tavily, Alpha Vantage, Marketaux).

Anahtarlar yalnızca GitHub Secrets'tan ortam değişkeni olarak gelir (koda asla yazılmaz):
  TAVILY_API_KEY, ALPHAVANTAGE_API_KEY, MARKETAUX_API_KEY
Anahtar yoksa ilgili kaynak sessizce atlanır.

Ücretsiz kotalar dar olduğu için her kaynağın bütçesi kodda sınırlanır ve kullanım bir dosyada sayılır:
  Tavily       : ayda 1000 kredi (temel arama = 1 kredi). ai.yml ayda en çok 300, istek üzerine analiz ayda en çok 600.
  Alpha Vantage: günde 25 istek. Günde en çok 8 (makro veriler günde bir kez yenilenir).
  Marketaux    : günde 100 istek, istek başına 3 haber. ai.yml günde en çok 45, istek üzerine analiz günde en çok 45.
"""
from __future__ import annotations

import datetime as dt
import os
import time
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Europe/Istanbul")
ZA = 25


def _log(*a):
    print(dt.datetime.now(IST).strftime("%H:%M:%S"), *a, flush=True)


class Butce:
    """Kaynak başına günlük/aylık sayaç. Durum sözlüğü çağıranın JSON dosyasında saklanır."""

    def __init__(self, durum: dict, limitler: dict):
        self.d = durum
        self.lim = limitler  # {"tavily": ("ay", 300), "marketaux": ("gun", 45), ...}

    def _anahtar(self, tur):
        z = dt.datetime.now(IST)
        return z.strftime("%Y-%m") if tur == "ay" else z.strftime("%Y-%m-%d")

    def kalan(self, kaynak: str) -> int:
        if kaynak not in self.lim:
            return 0
        tur, limit = self.lim[kaynak]
        k = self.d.get(kaynak) or {}
        return limit - (k.get("n", 0) if k.get("donem") == self._anahtar(tur) else 0)

    def harca(self, kaynak: str, n: int = 1) -> bool:
        if self.kalan(kaynak) < n:
            return False
        tur, _ = self.lim[kaynak]
        donem = self._anahtar(tur)
        k = self.d.get(kaynak) or {}
        if k.get("donem") != donem:
            k = {"donem": donem, "n": 0}
        k["n"] += n
        self.d[kaynak] = k
        return True


# ------------------------------------------------------------ Tavily (web + haber araması, özet cevaplı)
def tavily(sorgu: str, butce: Butce, gun: int = 2, adet: int = 5) -> dict | None:
    anahtar = os.environ.get("TAVILY_API_KEY", "").strip()
    if not anahtar or not butce.harca("tavily"):
        return None
    try:
        r = requests.post("https://api.tavily.com/search", timeout=ZA,
                          headers={"Authorization": f"Bearer {anahtar}", "Content-Type": "application/json"},
                          json={"query": sorgu, "topic": "news", "search_depth": "basic", "days": gun,
                                "max_results": adet, "include_answer": True})
        if not r.ok:
            _log("Tavily", r.status_code, r.text[:120].replace(anahtar, "***"))
            return None
        d = r.json()
        return {"ozet": (d.get("answer") or "")[:700],
                "sonuclar": [{"baslik": (x.get("title") or "")[:160], "url": x.get("url", ""),
                              "icerik": (x.get("content") or "")[:300], "tarih": x.get("published_date", "")}
                             for x in (d.get("results") or [])[:adet]]}
    except Exception as e:
        _log("Tavily hata:", str(e)[:120])
        return None


# ------------------------------------------------------------ Alpha Vantage (makro: petrol, kur, ABD faizi, küresel haber duyarlılığı)
def _av(params: dict, butce: Butce) -> dict | None:
    anahtar = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
    if not anahtar or not butce.harca("alphavantage"):
        return None
    try:
        r = requests.get("https://www.alphavantage.co/query", params={**params, "apikey": anahtar}, timeout=ZA)
        d = r.json()
        if not isinstance(d, dict) or d.get("Information") or d.get("Note") or d.get("Error Message"):
            _log("Alpha Vantage:", str(d.get("Information") or d.get("Note") or d.get("Error Message"))[:120])
            return None
        time.sleep(1.5)
        return d
    except Exception as e:
        _log("Alpha Vantage hata:", str(e)[:120])
        return None


def _seri_ozet(veri: list, ad: str, birim: str = "") -> dict | None:
    s = [(x.get("date"), float(x["value"])) for x in veri if x.get("value") not in (None, ".", "")]
    if len(s) < 6:
        return None
    s.sort()
    son = s[-1][1]
    d5 = (son / s[-6][1] - 1) * 100
    d20 = (son / s[-21][1] - 1) * 100 if len(s) > 21 else None
    return {"ad": ad, "son": round(son, 3), "tarih": s[-1][0], "d5": round(d5, 2), "d20": round(d20, 2) if d20 is not None else None, "birim": birim}


def av_makro(butce: Butce) -> dict:
    """Günde bir kez: Brent, USD/TRY, ABD 10 yıllık faiz ve küresel piyasa haber duyarlılığı (4 istek)."""
    m: dict = {"zaman": dt.datetime.now(IST).isoformat(timespec="seconds"), "seriler": [], "haberler": []}
    d = _av({"function": "BRENT", "interval": "daily"}, butce)
    if d and (x := _seri_ozet(d.get("data") or [], "Brent petrol", "$")):
        m["seriler"].append(x)
    d = _av({"function": "FX_DAILY", "from_symbol": "USD", "to_symbol": "TRY"}, butce)
    if d:
        ts = d.get("Time Series FX (Daily)") or {}
        veri = [{"date": k, "value": v.get("4. close")} for k, v in ts.items()]
        if x := _seri_ozet(veri, "Dolar/TL", "TL"):
            m["seriler"].append(x)
    d = _av({"function": "TREASURY_YIELD", "interval": "daily", "maturity": "10year"}, butce)
    if d and (x := _seri_ozet(d.get("data") or [], "ABD 10 yıllık faiz", "%")):
        m["seriler"].append(x)
    d = _av({"function": "NEWS_SENTIMENT", "topics": "financial_markets,economy_macro,economy_monetary,energy_transportation",
             "sort": "LATEST", "limit": "50"}, butce)
    if d:
        puan = []
        for f in (d.get("feed") or [])[:50]:
            try:
                puan.append(float(f.get("overall_sentiment_score")))
            except (TypeError, ValueError):
                pass
            if len(m["haberler"]) < 8:
                m["haberler"].append({"baslik": (f.get("title") or "")[:160], "duygu": f.get("overall_sentiment_label", ""),
                                      "kaynak": f.get("source", ""), "url": f.get("url", ""), "zaman": f.get("time_published", "")})
        if puan:
            ort = sum(puan) / len(puan)
            m["kuresel_duygu"] = {"puan": round(ort, 3), "etiket": "olumlu" if ort >= 0.15 else "olumsuz" if ort <= -0.15 else "nötr",
                                  "haber_sayisi": len(puan)}
    return m


# ------------------------------------------------------------ Marketaux (haber + varlık bazlı duyarlılık)
def marketaux(butce: Butce, **params) -> list:
    anahtar = os.environ.get("MARKETAUX_API_KEY", "").strip()
    if not anahtar or not butce.harca("marketaux"):
        return []
    try:
        p = {"api_token": anahtar, "limit": "3", "language": "tr,en",
             "published_after": (dt.datetime.utcnow() - dt.timedelta(days=4)).strftime("%Y-%m-%dT%H:%M"), **params}
        r = requests.get("https://api.marketaux.com/v1/news/all", params=p, timeout=ZA)
        if not r.ok:
            _log("Marketaux", r.status_code, r.text[:120].replace(anahtar, "***"))
            return []
        out = []
        for x in (r.json().get("data") or [])[:3]:
            duygu = [e.get("sentiment_score") for e in x.get("entities") or [] if isinstance(e.get("sentiment_score"), (int, float))]
            out.append({"baslik": (x.get("title") or "")[:160], "ozet": (x.get("description") or x.get("snippet") or "")[:240],
                        "url": x.get("url", ""), "kaynak": x.get("source", ""), "zaman": x.get("published_at", ""),
                        "duygu": round(sum(duygu) / len(duygu), 2) if duygu else None})
        return out
    except Exception as e:
        _log("Marketaux hata:", str(e)[:120])
        return []


def sirket_haberleri(kod: str, ad: str, butce: Butce, tavily_de: bool = True) -> list:
    """Bir şirketin son günlerdeki haberleri: önce Marketaux (ucuz), yetmezse Tavily."""
    temiz_ad = " ".join(w for w in (ad or kod).replace(",", " ").split() if w.upper() not in ("A.Ş.", "AŞ", "A.S.", "SAN.", "VE", "TİC.", "TIC."))[:60]
    haber = marketaux(butce, search=f'"{temiz_ad}"') if temiz_ad else []
    if len(haber) < 2 and tavily_de:
        t = tavily(f"{temiz_ad} {kod} hisse haber", butce, gun=5, adet=4)
        if t:
            for x in t["sonuclar"]:
                haber.append({"baslik": x["baslik"], "ozet": x["icerik"][:240], "url": x["url"], "kaynak": "web", "zaman": x["tarih"]})
    return haber[:5]


def haber_satiri(haberler: list, n: int = 3) -> str:
    parca = []
    for h in haberler[:n]:
        d = h.get("duygu")
        parca.append(h["baslik"] + (f" (duygu {d:+.2f})" if isinstance(d, (int, float)) else ""))
    return " / ".join(parca)


def makro_satiri(makro: dict, piyasa: dict | None) -> str:
    satir = []
    bugun = dt.date.today()
    for s in (makro or {}).get("seriler", []):
        try:  # Alpha Vantage bazı serileri günlerce geç günceller; eski veri yanıltmasın
            if (bugun - dt.date.fromisoformat(s.get("tarih", "")[:10])).days > 4:
                continue
        except ValueError:
            continue
        deger = f"%{s['son']:g}" if s["birim"] == "%" else f"{s['son']:g} {s['birim']}"
        satir.append(f"{s['ad']} {deger} (5 gün {s['d5']:+.1f}%"
                     + (f", 20 gün {s['d20']:+.1f}%" if s.get("d20") is not None else "") + ")")
    kd = (makro or {}).get("kuresel_duygu")
    if kd:
        satir.append(f"küresel piyasa haberlerinin genel duygusu {kd['etiket']} ({kd['puan']:+.2f}, {kd['haber_sayisi']} haber)")
    metin = ""
    if satir:
        metin += "\nMakro veriler (Alpha Vantage): " + "; ".join(satir) + "."
    if piyasa and piyasa.get("ozet"):
        metin += "\nGünün piyasa özeti (web taraması): " + piyasa["ozet"]
    if piyasa and piyasa.get("basliklar"):
        metin += "\nTürkiye piyasa haberleri: " + " / ".join(piyasa["basliklar"][:6])
    return metin + ("\n" if metin else "")
