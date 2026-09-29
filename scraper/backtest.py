"""Geçmiş veri testi (backtest) -> data/backtest.json

Amaç: "Hangi koşullarda tahmin gerçekten tutuyor?" sorusunu sayılarla cevaplamak.
- En likit ~100 hisse + BIST 100 için Yahoo'dan 5 yıllık günlük fiyat çekilir.
- Her hisse için her hafta yalnızca O GÜNE KADARKİ veriyle göstergeler hesaplanır (geleceği görmek yok).
- Koşullar (ör. "RSI<30 ve 200 günlük ortalamanın üstünde") için 1 hafta ve 1 ay sonrası ölçülür:
    * "piyasa": hisse BIST 100'den iyi mi gitti (asıl ölçü; enflasyon ve genel piyasa etkisini ayıklar)
    * "yon": fiyat yükseldi mi (TL enflasyonu yüzünden yükseliş zaten sık; taban oranla birlikte okunmalı)
- Koşullar ESKİ dönemde (eğitim) seçilir, YENİ dönemde (test) doğrulanır. Yalnızca iki dönemde de tutanlar kalır.
- Son olarak bugün bu doğrulanmış koşullara uyan hisseler listelenir (Beklenti bölümü bunu kullanır).
"""
from __future__ import annotations

import itertools
import json
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guncelle  # noqa: E402  (Yahoo oturumu ve sembol eşlemesi)

KOK = Path(__file__).resolve().parent.parent
CIKTI = KOK / "data" / "backtest.json"
TSI = timezone(timedelta(hours=3))

EVREN = int(os.environ.get("BT_EVREN", "100"))
TEST_BASI = pd.Timestamp(os.environ.get("BT_TEST_BASI", "2025-01-01"))
VADELER = {"1h": 5, "1a": 21}           # işlem günü
ADIM = 5                                 # her hisseden haftada bir örnek (örnekler birbirinin kopyası olmasın)

# Seçim eşikleri (bilerek sıkı): az ama sağlam kural
EGT_MIN_ISABET, EGT_MIN_N, EGT_MIN_HAFTA = 0.60, 250, 40
TST_MIN_ISABET, TST_MIN_N, TST_MIN_HAFTA = 0.60, 80, 20


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- veri
def evren_sec() -> list[str]:
    """Günlük işlem hacmi (TL) en yüksek hisseler."""
    import requests
    kodlar: list[str] = []
    try:
        r = requests.get("https://raw.githubusercontent.com/maj834/Hisse/data/market.json", timeout=30)
        d = r.json()
        s = d["sutun"]
        ik, ifi, ih = s.index("k"), s.index("fiyat"), s.index("hacim")
        satir = sorted(d["hisseler"], key=lambda x: -((x[ifi] or 0) * (x[ih] or 0)))
        kodlar = [x[ik] for x in satir[:EVREN]]
    except Exception as e:
        log("market.json okunamadı:", e)
    if not kodlar:
        snap = json.loads((KOK / "data" / "snapshot.json").read_text(encoding="utf-8"))
        kodlar = list(snap.get("hisseler", {}))
    return kodlar


def seri(kod: str, sembol: str | None = None) -> pd.DataFrame | None:
    semboller = [sembol] if sembol else guncelle.yahoo_sembol(kod)
    for s in semboller:
        for deneme in range(3):
            try:
                res = guncelle.yahoo_chart(s, "5y", "1d")
                if not res:
                    break
                ts = res.get("timestamp") or []
                q = res["indicators"]["quote"][0]
                adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")
                df = pd.DataFrame({
                    "c": adj or q.get("close"),
                    "h": q.get("high"), "l": q.get("low"), "v": q.get("volume"),
                    "ham": q.get("close"),
                }, index=pd.to_datetime(ts, unit="s").normalize())
                df = df[~df.index.duplicated(keep="last")].dropna(subset=["c"])
                if len(df) > 260:
                    # yükseklik/düşüklük düzeltilmemiş olabilir: düzeltme oranıyla ölçekle
                    oran = (df["c"] / df["ham"]).replace([np.inf, -np.inf], np.nan).fillna(1)
                    df["h"] = df["h"] * oran
                    df["l"] = df["l"] * oran
                    return df
                break
            except Exception as e:
                log(f"  {s} deneme {deneme + 1}: {str(e)[:80]}")
                time.sleep(2 + deneme * 3)
    return None


# ---------------------------------------------------------------- göstergeler (yalnızca geçmiş veri)
def rsi(c: pd.Series, n=14) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def ozellikler(df: pd.DataFrame, xu: pd.Series) -> pd.DataFrame:
    c = df["c"]
    x = xu.reindex(c.index).ffill()
    o = pd.DataFrame(index=c.index)
    o["rsi"] = rsi(c)
    o["r20"] = c / c.shift(20) - 1
    o["r60"] = c / c.shift(60) - 1
    o["rs20"] = o["r20"] - (x / x.shift(20) - 1)          # BIST 100'e göre 1 aylık güç
    o["rs60"] = o["r60"] - (x / x.shift(60) - 1)
    sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
    o["s200"] = c / sma200 - 1
    o["trend"] = ((c > sma200) & (sma50 > sma200)).astype(float)
    o.loc[sma200.isna(), "trend"] = np.nan
    o["zirve"] = c / c.rolling(250).max() - 1              # 52 haftalık zirveye uzaklık
    tl = (df["c"] * df["v"]).rolling(20).median()
    o["hacim"] = (df["v"].rolling(5).mean() / df["v"].rolling(60).mean())
    o["oyn"] = np.log(c).diff().rolling(20).std() * math.sqrt(252)
    o["likit"] = tl
    for ad, g in VADELER.items():
        ileri = c.shift(-g) / c - 1
        xi = x.shift(-g) / x - 1
        o[f"y_{ad}"] = ileri
        o[f"p_{ad}"] = ileri - xi
    return o


# ---------------------------------------------------------------- koşullar
def kosul_tablosu(t: pd.DataFrame) -> dict[str, pd.Series]:
    """Her koşul bir True/False sütunu. Sıralama koşulları o günkü tüm hisselere göre (5'te 1'lik dilimler)."""
    k: dict[str, pd.Series] = {}
    k["RSI<30"] = t["rsi"] < 30
    k["RSI 30-45"] = t["rsi"].between(30, 45, inclusive="left")
    k["RSI 55-70"] = t["rsi"].between(55, 70, inclusive="left")
    k["RSI>70"] = t["rsi"] >= 70
    k["Yükselen trend"] = t["trend"] == 1
    k["Düşen trend"] = t["trend"] == 0
    k["Zirveye yakın (<%5)"] = t["zirve"] > -0.05
    k["Zirveden %30+ aşağı"] = t["zirve"] < -0.30
    k["Hacim artışı (1.5x)"] = t["hacim"] > 1.5
    k["Piyasa yükselişte"] = t["xu_trend"] == 1
    k["Piyasa düşüşte"] = t["xu_trend"] == 0
    for ad, ac in [("rs20", "1 aylık piyasaya göre güç"), ("rs60", "3 aylık piyasaya göre güç"),
                   ("r20", "1 aylık getiri"), ("s200", "200 günlük ort. uzaklık"), ("oyn", "oynaklık")]:
        rk = t.groupby("tarih")[ad].rank(pct=True)
        k[f"{ac}: en yüksek %20"] = rk > 0.8
        k[f"{ac}: en düşük %20"] = rk <= 0.2
    return {a: s.fillna(False) for a, s in k.items()}


def say(maske: pd.Series, hedef: pd.Series, yon: int, tarih: pd.Series):
    m = maske & hedef.notna()
    n = int(m.sum())
    if n == 0:
        return 0, 0.0, 0, 0.0
    h = hedef[m]
    isabet = float(((h > 0) if yon > 0 else (h < 0)).mean())
    hafta = int(tarih[m].nunique())
    return n, isabet, hafta, float(h.mean())


def main():
    kodlar = evren_sec()
    log(f"Evren: {len(kodlar)} hisse")
    xu = seri("XU100", "XU100.IS")
    if xu is None:
        log("BIST 100 verisi alınamadı")
        sys.exit(1)
    xc = xu["c"]
    xu_trend = ((xc > xc.rolling(200).mean()) & (xc.rolling(50).mean() > xc.rolling(200).mean())).astype(float)

    parcalar, bugun_ozellik, eksik = [], [], []
    for i, kod in enumerate(kodlar):
        df = seri(kod)
        if df is None:
            eksik.append(kod)
            continue
        o = ozellikler(df, xc)
        o["xu_trend"] = xu_trend.reindex(o.index).ffill()
        o["kod"] = kod
        o["tarih"] = o.index
        son = o.iloc[-1:].copy()
        if (pd.Timestamp.now().normalize() - son.index[-1]).days <= 6:
            bugun_ozellik.append(son)
        parcalar.append(o.iloc[::-1].iloc[::ADIM].iloc[::-1])   # sondan geriye haftada bir
        if i % 20 == 0:
            log(f"  {i}/{len(kodlar)}")
        time.sleep(0.3)
    if not parcalar:
        log("veri yok")
        sys.exit(1)

    t = pd.concat(parcalar, ignore_index=True)
    # sığ hisse günlerini at (günlük ~20 mn TL altı)
    t = t[t["likit"] > 20e6].reset_index(drop=True)
    t = t.dropna(subset=["rsi", "r60", "s200"]).reset_index(drop=True)
    kos = kosul_tablosu(t)
    egitim = t["tarih"] < TEST_BASI
    test = ~egitim
    log(f"Örnek: {len(t)} (eğitim {int(egitim.sum())}, test {int(test.sum())})")

    # taban oranlar: hiçbir koşul olmadan "yükselir" ya da "piyasayı yener" demek ne kadar tutar
    taban = {}
    for vade in VADELER:
        for hedef in ("p", "y"):
            h = t[f"{hedef}_{vade}"]
            taban[f"{hedef}_{vade}"] = {
                "egitim_yukari": round(float((h[egitim].dropna() > 0).mean()), 3),
                "test_yukari": round(float((h[test].dropna() > 0).mean()), 3),
            }

    tekler = list(kos)
    adaylar = [(a,) for a in tekler] + list(itertools.combinations(tekler, 2))
    kurallar = []
    for ad in adaylar:
        m = kos[ad[0]] if len(ad) == 1 else (kos[ad[0]] & kos[ad[1]])
        if m.sum() < EGT_MIN_N:
            continue
        for vade in VADELER:
            for hedef in ("p", "y"):
                h = t[f"{hedef}_{vade}"]
                for yon in (1, -1):
                    n, isb, hf, ort = say(m & egitim, h, yon, t["tarih"])
                    tb = taban[f"{hedef}_{vade}"]["egitim_yukari"]
                    tb = tb if yon > 0 else 1 - tb
                    # taban orandan en az 5 puan iyi olmalı (aksi hâlde "hep AL de" ile aynı)
                    if n < EGT_MIN_N or hf < EGT_MIN_HAFTA or isb < max(EGT_MIN_ISABET, tb + 0.05):
                        continue
                    n2, isb2, hf2, ort2 = say(m & test, h, yon, t["tarih"])
                    tb2 = taban[f"{hedef}_{vade}"]["test_yukari"]
                    tb2 = tb2 if yon > 0 else 1 - tb2
                    gecti = n2 >= TST_MIN_N and hf2 >= TST_MIN_HAFTA and isb2 >= max(TST_MIN_ISABET, tb2 + 0.05)
                    kurallar.append({
                        "kosul": list(ad), "vade": vade,
                        "hedef": "piyasa" if hedef == "p" else "yon",
                        "karar": "AL" if yon > 0 else "SAT",
                        "egitim": {"isabet": round(isb, 3), "n": n, "hafta": hf, "ort": round(ort * 100, 2), "taban": round(tb, 3)},
                        "test": {"isabet": round(isb2, 3), "n": n2, "hafta": hf2, "ort": round(ort2 * 100, 2), "taban": round(tb2, 3)},
                        "gecti": bool(gecti),
                    })
    gecen = sorted([k for k in kurallar if k["gecti"]], key=lambda k: (-k["test"]["isabet"], -k["test"]["n"]))
    log(f"Eğitimde tutan: {len(kurallar)}, testte de tutan: {len(gecen)}")

    # bugün bu kurallara uyan hisseler
    bugun = []
    if bugun_ozellik and gecen:
        b = pd.concat(bugun_ozellik, ignore_index=True)
        b["tarih"] = b["tarih"].max()
        b = b[b["likit"] > 20e6].reset_index(drop=True)
        kb = kosul_tablosu(b)
        for _, satir in b.iterrows():
            eslesen = []
            for k in gecen:
                if all(bool(kb[a].iloc[_]) for a in k["kosul"]):
                    eslesen.append(k)
            if not eslesen:
                continue
            en_iyi = max(eslesen, key=lambda k: (k["test"]["isabet"], k["test"]["n"]))
            yonler = {k["karar"] for k in eslesen}
            if len(yonler) > 1:      # çelişen kurallar: hisseyi gösterme
                continue
            bugun.append({
                "kod": satir["kod"], "karar": en_iyi["karar"], "vade": en_iyi["vade"], "hedef": en_iyi["hedef"],
                "kosul": en_iyi["kosul"], "isabet": en_iyi["test"]["isabet"], "n": en_iyi["test"]["n"],
                "kural_sayisi": len(eslesen), "rsi": round(float(satir["rsi"]), 1),
            })
        bugun.sort(key=lambda x: (-x["isabet"], -x["kural_sayisi"]))

    sonuc = {
        "updatedAt": datetime.now(TSI).isoformat(timespec="seconds"),
        "evren": len(kodlar) - len(eksik), "eksik": eksik,
        "donem": {"bas": str(t["tarih"].min().date()), "test_basi": str(TEST_BASI.date()), "son": str(t["tarih"].max().date())},
        "ornek": {"egitim": int(egitim.sum()), "test": int(test.sum())},
        "esik": {"egitim": [EGT_MIN_ISABET, EGT_MIN_N, EGT_MIN_HAFTA], "test": [TST_MIN_ISABET, TST_MIN_N, TST_MIN_HAFTA]},
        "taban": taban,
        "aday_sayisi": len(adaylar),
        "kurallar": gecen[:60],
        "egitimde_tutup_testte_tutmayan": len(kurallar) - len(gecen),
        "yakin": sorted([k for k in kurallar if not k["gecti"]], key=lambda k: -k["test"]["isabet"])[:20],
        "bugun": bugun[:25],
    }
    CIKTI.write_text(json.dumps(sonuc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(json.dumps({k: v for k, v in sonuc.items() if k not in ("kurallar", "bugun")}, ensure_ascii=False))
    for k in gecen[:15]:
        log(" ", k["karar"], k["vade"], k["hedef"], " + ".join(k["kosul"]), "| eğitim", k["egitim"]["isabet"], k["egitim"]["n"],
            "| test", k["test"]["isabet"], k["test"]["n"], "taban", k["test"]["taban"])
    log("Bugün:", [(x["kod"], x["karar"], x["vade"]) for x in bugun[:25]])


if __name__ == "__main__":
    main()
