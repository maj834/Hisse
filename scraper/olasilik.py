"""Geçmiş benzer durumlar -> data/olasilik.json

Soru: "Bu hisse bugünkü durumdayken geçmişte 1 hafta / 1 ay / 3 ay sonra ne oldu?"
- Tüm BIST hisseleri için Yahoo'dan 5 yıllık günlük (düzeltilmiş) kapanış çekilir.
- Durum = son 20 günlük getirinin hissenin kendi geçmişindeki dilimi (5 dilim) x 200 günlük ortalamanın üstü/altı.
- Son 3 yılda aynı durumdaki günlerden sonra: yükselme oranı ve getirinin %25 / %50 / %75 değerleri.
  Benzer gün azsa koşulsuz (tüm günler) değerlerle harmanlanır.
- Doğrulama: eski dönemde hesaplanan oranlar yeni dönemde tutuyor mu? Koşullu oranın koşulsuzdan iyi olup
  olmadığı Brier puanıyla ölçülür; iyi değilse uygulama koşulsuz değerleri kullanır.
Not: TL enflasyonu yüzünden fiyatların yükselmesi zaten sıktır; oranlar bunu da içerir.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guncelle  # noqa: E402

KOK = Path(__file__).resolve().parent.parent
CIKTI = KOK / "data" / "olasilik.json"
TSI = timezone(timedelta(hours=3))
VADELER = {"1h": 5, "1a": 21, "3a": 63}
PENCERE = 756          # son 3 yıl (işlem günü)
MIN_GUN = 300          # bundan kısa geçmişi olan hisse atlanır
HARMAN = 40            # benzer gün sayısı bunun altındaysa koşulsuza doğru çekilir
DILIM = 5


def log(*a):
    print(*a, flush=True)


def evren() -> list[str]:
    import requests
    try:
        d = requests.get("https://raw.githubusercontent.com/maj834/Hisse/data/market.json", timeout=30).json()
        ik = d["sutun"].index("k")
        return [x[ik] for x in d["hisseler"] if x[ik]]
    except Exception as e:
        log("market.json okunamadı:", e)
        snap = json.loads((KOK / "data" / "snapshot.json").read_text(encoding="utf-8"))
        return list(snap.get("hisseler", {}))


def kapanis(kod: str) -> pd.Series | None:
    for s in guncelle.yahoo_sembol(kod):
        for deneme in range(2):
            try:
                res = guncelle.yahoo_chart(s, "5y", "1d")
                if not res:
                    break
                adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")
                c = pd.Series(adj or res["indicators"]["quote"][0].get("close"),
                              index=pd.to_datetime(res.get("timestamp") or [], unit="s").normalize(), dtype="float64")
                c = c[~c.index.duplicated(keep="last")].dropna()
                c = c[c > 0]
                return c if len(c) >= MIN_GUN else None
            except Exception as e:
                log(f"  {s}: {str(e)[:80]}")
                time.sleep(2)
    return None


def durumlar(c: pd.Series) -> tuple[pd.Series, pd.Series]:
    """20 günlük getiri ve 200 günlük ortalamanın üstünde mi (1/0)."""
    r20 = c / c.shift(20) - 1
    ust = (c > c.rolling(200, min_periods=120).mean()).astype(int)
    return r20, ust


def kodla(r20: pd.Series, ust: pd.Series, sinir: list[float]) -> pd.Series:
    d = np.searchsorted(sinir, r20.values, side="right")
    s = pd.Series(d * 2 + ust.values, index=r20.index)
    return s.where(r20.notna())


def istatistik(ileri: pd.Series, durum: pd.Series, simdi: float | None):
    ok = ileri.notna() & durum.notna()
    tum = ileri[ok]
    if len(tum) < 60:
        return None
    pu = float((tum > 0).mean())
    if simdi is None or np.isnan(simdi):
        ben = tum.iloc[:0]
    else:
        ben = tum[durum[ok] == simdi]
    n = len(ben)
    w = n / (n + HARMAN)
    p = w * float((ben > 0).mean()) + (1 - w) * pu if n else pu
    kaynak = ben if n >= HARMAN else tum
    q = np.percentile(kaynak.values, [25, 50, 75]) * 100
    qu = np.percentile(tum.values, [25, 50, 75]) * 100
    return {"p": round(p, 3), "pu": round(pu, 3), "q": [round(float(x), 2) for x in q],
            "qu": [round(float(x), 2) for x in qu], "n": int(n), "N": int(len(tum))}


def brier(c: pd.Series, n: int):
    """Eski 2/3'te hesaplanan oranla yeni 1/3'ü tahmin et (haftada bir örnek). (koşullu, koşulsuz, örnek) döner."""
    c = c.iloc[-(PENCERE + n):]
    r20, ust = durumlar(c)
    ileri = c.shift(-n) / c - 1
    kes = int(len(c) * 2 / 3)
    eski_r = r20.iloc[:kes].dropna()
    if len(eski_r) < 150:
        return None
    sinir = list(np.percentile(eski_r.values, [20, 40, 60, 80]))
    d = kodla(r20, ust, sinir)
    egt = slice(0, kes - n)          # eğitim etiketleri test dönemine taşmasın
    ei, ed = ileri.iloc[egt], d.iloc[egt]
    ok = ei.notna() & ed.notna()
    ei, ed = ei[ok], ed[ok]
    if len(ei) < 100:
        return None
    pu = float((ei > 0).mean())
    tablo = {}
    for k, g in ei.groupby(ed):
        m = len(g)
        w = m / (m + HARMAN)
        tablo[k] = w * float((g > 0).mean()) + (1 - w) * pu
    ti, td = ileri.iloc[kes::5], d.iloc[kes::5]
    ok = ti.notna() & td.notna()
    ti, td = ti[ok], td[ok]
    if not len(ti):
        return None
    y = (ti > 0).astype(float).values
    pk = np.array([tablo.get(k, pu) for k in td.values])
    return float(((pk - y) ** 2).sum()), float(((pu - y) ** 2).sum()), len(y)


def main():
    kodlar = evren()
    log(f"Evren: {len(kodlar)} hisse")
    cikti, eksik = {}, []
    dog = {v: [0.0, 0.0, 0] for v in VADELER}
    son_tarih = None
    for i, kod in enumerate(kodlar):
        c = kapanis(kod)
        if c is None:
            eksik.append(kod)
            continue
        if (pd.Timestamp.now().normalize() - c.index[-1]).days > 10:
            eksik.append(kod)
            continue
        son_tarih = max(son_tarih or c.index[-1], c.index[-1])
        cc = c.iloc[-(PENCERE + 63 + 20):]
        r20, ust = durumlar(cc)
        sinir = list(np.percentile(r20.dropna().values, [20, 40, 60, 80]))
        d = kodla(r20, ust, sinir)
        simdi = d.iloc[-1]
        dilim = int(np.searchsorted(sinir, r20.iloc[-1], side="right")) if pd.notna(r20.iloc[-1]) else None
        h = {"r20": round(float(r20.iloc[-1]) * 100, 1) if pd.notna(r20.iloc[-1]) else None,
             "dilim": dilim, "ust200": int(ust.iloc[-1])}
        for v, n in VADELER.items():
            ileri = cc.shift(-n) / cc - 1
            st = istatistik(ileri.iloc[-PENCERE - n:], d.iloc[-PENCERE - n:], simdi)
            if st:
                h[v] = st
            b = brier(c, n)
            if b:
                dog[v][0] += b[0]
                dog[v][1] += b[1]
                dog[v][2] += b[2]
        if any(v in h for v in VADELER):
            cikti[kod] = h
        if i % 50 == 0:
            log(f"  {i}/{len(kodlar)}")
        time.sleep(0.25)
    if len(cikti) < 20:
        log(f"çok az hisse ({len(cikti)}), dosya yazılmadı")
        sys.exit(1)
    dogrulama = {}
    for v, (bk, bu, m) in dog.items():
        if m:
            dogrulama[v] = {"brier_kosullu": round(bk / m, 4), "brier_kosulsuz": round(bu / m, 4), "n": m,
                            "kosul_ise_yariyor": bk < bu * 0.995}
    CIKTI.write_text(json.dumps({
        "updatedAt": datetime.now(TSI).isoformat(timespec="seconds"),
        "son": son_tarih.strftime("%Y-%m-%d") if son_tarih is not None else None,
        "pencere_gun": PENCERE,
        "dogrulama": dogrulama,
        "eksik": eksik,
        "hisseler": cikti,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"Yazıldı: {len(cikti)} hisse, eksik {len(eksik)}, doğrulama {dogrulama}")


if __name__ == "__main__":
    main()
