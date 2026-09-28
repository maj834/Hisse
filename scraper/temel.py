"""Hisse Radar - temel analiz (şirket finansalları).

BIST 30 şirketlerinin çeyreklik gelir tablosu kalemlerini ve bilanço özetini Yahoo Finance
fundamentals-timeseries uç noktasından çeker; finans eklentisindeki gelir tablosu yöntemine göre
dönem karşılaştırması (önceki çeyrek ve geçen yılın aynı çeyreği), marjlar, önemli sapmalar ve
değerleme oranları hesaplar. Sonuç data/temel.json dosyasına yazılır.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Europe/Istanbul")
KOK = Path(__file__).resolve().parent.parent
CIKTI = Path(os.environ.get("TEMEL_CIKTI", KOK / "data" / "temel.json"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from guncelle import HISSELER, YAHOO_ALT, yahoo_isit, yahoo_oturum  # noqa: E402

CEYREK = ["TotalRevenue", "GrossProfit", "OperatingIncome", "EBITDA", "NetIncome", "NetIncomeCommonStockholders",
          "TotalDebt", "StockholdersEquity", "CashAndCashEquivalents", "OperatingCashFlow", "FreeCashFlow"]
DEGERLEME = ["MarketCap", "PeRatio", "PbRatio", "EnterprisesValueEBITDARatio"]
HATALAR: list[str] = []


def log(*a):
    print(dt.datetime.now(IST).strftime("%H:%M:%S"), *a, flush=True)


def seri_cek(sembol: str) -> dict:
    tipler = [f"quarterly{t}" for t in CEYREK] + [f"trailing{t}" for t in DEGERLEME]
    simdi = int(time.time())
    url = (f"https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{sembol}"
           f"?symbol={sembol}&type={','.join(tipler)}&period1={simdi - 3 * 365 * 86400}&period2={simdi}")
    r = yahoo_oturum.get(url, timeout=25)
    if r.status_code == 429:
        time.sleep(3)
        r = yahoo_oturum.get(url, timeout=25)
    r.raise_for_status()
    sonuc = {}
    for s in r.json().get("timeseries", {}).get("result", []) or []:
        tip = (s.get("meta", {}).get("type") or [None])[0]
        if not tip or tip not in s:
            continue
        noktalar = []
        for p in s[tip] or []:
            if not p:
                continue
            v = (p.get("reportedValue") or {}).get("raw")
            if v is None:
                continue
            noktalar.append((p.get("asOfDate"), float(v)))
        noktalar.sort()
        sonuc[tip] = noktalar
    return sonuc


def son(seri, n=1):
    return seri[-n] if seri and len(seri) >= n else (None, None)


def degisim(yeni, eski):
    if yeni is None or eski in (None, 0):
        return None
    return round((yeni - eski) / abs(eski) * 100, 1)


def marj(pay, payda):
    if pay is None or not payda:
        return None
    return round(pay / payda * 100, 1)


def ceyrek_adi(tarih: str | None) -> str:
    if not tarih:
        return ""
    y, m, _ = tarih.split("-")
    return f"{(int(m) - 1) // 3 + 1}Ç{y[2:]}"


def analiz(kod: str) -> dict:
    veri = None
    for s in YAHOO_ALT.get(kod, [f"{kod}.IS"]):
        try:
            veri = seri_cek(s)
            if veri:
                break
        except Exception as e:
            HATALAR.append(f"{kod} {s}: {e}"[:200])
    if not veri:
        raise RuntimeError("veri yok")
    q = lambda t: veri.get(f"quarterly{t}", [])  # noqa: E731
    gelir = q("TotalRevenue")
    net = q("NetIncomeCommonStockholders") or q("NetIncome")
    tarih, g0 = son(gelir)
    if tarih is None:
        tarih, _ = son(net)
    if tarih is None:
        raise RuntimeError("çeyrek verisi yok")

    def deger(seri, hedef_tarih):
        for t, v in seri:
            if t == hedef_tarih:
                return v
        return None

    tarihler = sorted({t for t, _ in gelir} | {t for t, _ in net})
    i = tarihler.index(tarih)
    onceki = tarihler[i - 1] if i >= 1 else None
    gecen_yil = next((t for t in tarihler if t[:4] == str(int(tarih[:4]) - 1) and t[5:7] == tarih[5:7]), None)

    satirlar = []
    for ad, seri in [("Hasılat", gelir), ("Brüt kâr", q("GrossProfit")), ("Esas faaliyet kârı", q("OperatingIncome")),
                     ("FAVÖK", q("EBITDA")), ("Net kâr", net)]:
        cur = deger(seri, tarih)
        if cur is None:
            continue
        pq = deger(seri, onceki) if onceki else None
        py = deger(seri, gecen_yil) if gecen_yil else None
        satirlar.append({"ad": ad, "son": cur, "onceki": pq, "gecenYil": py,
                         "ceyreklik": degisim(cur, pq), "yillik": degisim(cur, py)})

    g_cur = deger(gelir, tarih)
    g_py = deger(gelir, gecen_yil) if gecen_yil else None
    marjlar = []
    for ad, seri in [("Brüt marj", q("GrossProfit")), ("Faaliyet marjı", q("OperatingIncome")),
                     ("FAVÖK marjı", q("EBITDA")), ("Net marj", net)]:
        m1 = marj(deger(seri, tarih), g_cur)
        m0 = marj(deger(seri, gecen_yil), g_py) if gecen_yil else None
        if m1 is not None:
            marjlar.append({"ad": ad, "son": m1, "gecenYil": m0, "degisimPuan": round(m1 - m0, 1) if m0 is not None else None})

    # önemli sapmalar: finans eklentisindeki eşik mantığı (%15 ve üzeri yıllık değişim)
    sapmalar = []
    for s in satirlar:
        y = s["yillik"]
        if y is not None and abs(y) >= 15:
            sapmalar.append({"ad": s["ad"], "yillik": y, "yon": "olumlu" if y > 0 else "olumsuz"})
    for m in marjlar:
        d = m["degisimPuan"]
        if d is not None and abs(d) >= 3:
            sapmalar.append({"ad": m["ad"], "puan": d, "yon": "olumlu" if d > 0 else "olumsuz"})

    # son 4 çeyrek toplamı (yıllıklandırılmış)
    def ttm(seri):
        v = [x for _, x in seri[-4:]]
        return sum(v) if len(v) == 4 else None

    net_ttm, gelir_ttm = ttm(net), ttm(gelir)
    borc = son(q("TotalDebt"))[1]
    oz = son(q("StockholdersEquity"))[1]
    nakit = son(q("CashAndCashEquivalents"))[1]
    t = lambda n: son(veri.get(f"trailing{n}", []))[1]  # noqa: E731
    pd_ = t("MarketCap")
    fk = t("PeRatio") or (round(pd_ / net_ttm, 2) if pd_ and net_ttm and net_ttm > 0 else None)
    pddd = t("PbRatio") or (round(pd_ / oz, 2) if pd_ and oz and oz > 0 else None)
    roe = round(net_ttm / oz * 100, 1) if net_ttm is not None and oz and oz > 0 else None

    # temel puan: -3..+3
    puan = 0
    neden = []
    gy = next((s["yillik"] for s in satirlar if s["ad"] == "Hasılat"), None)
    ny = next((s["yillik"] for s in satirlar if s["ad"] == "Net kâr"), None)
    if gy is not None:
        if gy >= 30:
            puan += 1; neden.append(f"hasılat yıllık %{gy:.0f} arttı")
        elif gy < 0:
            puan -= 1; neden.append(f"hasılat yıllık %{abs(gy):.0f} azaldı")
    if ny is not None:
        if ny >= 20:
            puan += 1; neden.append(f"net kâr yıllık %{ny:.0f} arttı")
        elif ny <= -20:
            puan -= 1; neden.append(f"net kâr yıllık %{abs(ny):.0f} azaldı")
    son_net = deger(net, tarih)
    if son_net is not None and son_net < 0:
        puan -= 1; neden.append("son çeyrekte zarar")
    if roe is not None:
        if roe >= 25:
            puan += 1; neden.append(f"özsermaye kârlılığı %{roe:.0f}")
        elif roe < 5:
            puan -= 1; neden.append(f"özsermaye kârlılığı düşük (%{roe:.0f})")
    puan = max(-3, min(3, puan))

    return {
        "ceyrek": ceyrek_adi(tarih), "tarih": tarih,
        "oncekiCeyrek": ceyrek_adi(onceki), "gecenYilCeyrek": ceyrek_adi(gecen_yil),
        "satirlar": satirlar, "marjlar": marjlar, "sapmalar": sapmalar,
        "oranlar": {"fk": fk, "pddd": pddd, "roe": roe, "piyasaDegeri": pd_,
                    "borcOzsermaye": round(borc / oz, 2) if borc is not None and oz and oz > 0 else None,
                    "netBorc": (borc - nakit) if borc is not None and nakit is not None else None,
                    "netKarTTM": net_ttm, "hasilatTTM": gelir_ttm},
        "puan": puan, "ozet": ", ".join(neden),
    }


def main() -> int:
    yahoo_isit()
    eski = {}
    try:
        eski = json.loads(CIKTI.read_text(encoding="utf-8")).get("hisseler", {})
    except Exception:
        pass
    sonuc = {}
    for kod in HISSELER:
        try:
            sonuc[kod] = analiz(kod)
            log(kod, sonuc[kod]["ceyrek"], "puan", sonuc[kod]["puan"])
        except Exception as e:
            HATALAR.append(f"{kod}: {e}"[:200])
            if kod in eski:
                sonuc[kod] = eski[kod]
        time.sleep(0.6)
    CIKTI.parent.mkdir(parents=True, exist_ok=True)
    CIKTI.write_text(json.dumps({"updatedAt": dt.datetime.now(IST).isoformat(timespec="seconds"),
                                 "kaynak": "Yahoo Finance (şirket finansal tabloları)", "hisseler": sonuc,
                                 "hatalar": HATALAR[-30:]}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log("yazıldı", len(sonuc), "hisse,", len(HATALAR), "hata")
    return 0


if __name__ == "__main__":
    sys.exit(main())
