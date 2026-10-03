"""Sanal trader -> data/trader.json

Kurallarla çalışan, gerçek para kullanmayan bir al-sat botu. Her işlem günü kapanıştan sonra:
- En likit ~100 hisse + BIST 30 + BIST 100 için Yahoo'dan 5 yıllık günlük fiyat çeker.
- Elindeki hisselerde zarar-kes / iz süren stop / trend bozulması var mı bakar, varsa SATAR.
- Boş yeri varsa yükselen trenddeki, piyasadan güçlü ve zirvesine yakın hisseleri AL'ır.
  Yapay zekâ çoğunluğu SAT diyorsa ya da hisse "riskli" listesindeyse almaz.
- Her işlemde komisyon ve kayma düşülür. Pozisyon büyüklüğü riske göre ayarlanır (işlem başına
  sermayenin %1'i riske edilir, tek hisse en çok %20).

Aynı kurallar 2022'den bugüne geçmiş veride de çalıştırılır (sim) ve BIST 100 ile karşılaştırılır.
Kurallar geçmiş veriye göre ayarlanmadı (parametre taraması yok); sonuçlar olduğu gibi yazılır.
"canli" bölümü botun ilk çalıştığı günden itibaren gerçek zamanlı kayıttır; hiçbir işlem silinmez.

Ayrıca evrendeki her hisse için bugünkü bot görüşü (AL/TUT/SAT, stop, hedef) "analiz" altında yazılır;
uygulamadaki "Portföyüm" ekranı kullanıcının hisselerini bununla değerlendirir.
"""
from __future__ import annotations

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

KOK = Path(__file__).resolve().parent.parent
VERI = KOK / "data"
CIKTI = VERI / "trader.json"
TSI = timezone(timedelta(hours=3))

# ---------------------------------------------------------------- kurallar (bilerek basit ve sabit)
BASLANGIC = 100_000.0       # sanal sermaye (TL)
KOMISYON = 0.0008           # alış ve satışta ayrı ayrı (komisyon + BSMV yaklaşık)
KAYMA = 0.0015              # kapanıştan kötü fiyatla dolma payı
EN_COK_POZ = 6
RISK = 0.01                 # işlem başına riske edilen sermaye oranı
EN_COK_AGIRLIK = 0.20
STOP_ATR = 2.5              # ilk zarar-kes: giriş - 2.5 ATR
IZ_ATR = 3.0                # iz süren stop: girişten beri en yüksek kapanış - 3 ATR
KAR_KILIT_R = 3.0           # 3R kâra ulaşınca iz süren stop 2 ATR'ye sıkılır
MIN_LIKIT = 50e6            # günlük işlem (TL, 20 gün medyan)
SIM_BASI = pd.Timestamp(os.environ.get("TR_SIM_BASI", "2022-01-01"))
TEST_BASI = pd.Timestamp("2025-01-01")
EVREN = int(os.environ.get("TR_EVREN", "100"))


def log(*a):
    print(*a, flush=True)


def r2(v, n=2):
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else round(float(v), n)


# ---------------------------------------------------------------- veri
def veri_cek() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    if os.environ.get("TR_SAHTE") == "1":
        return sahte_veri()
    import backtest
    import guncelle
    kodlar = list(dict.fromkeys(backtest.evren_sec()[:EVREN] + list(guncelle.HISSELER)))
    log(f"Evren: {len(kodlar)} hisse")
    xu = backtest.seri("XU100", "XU100.IS")
    if xu is None:
        log("BIST 100 verisi alınamadı")
        sys.exit(1)
    seriler = {}
    for i, kod in enumerate(kodlar):
        df = backtest.seri(kod)
        if df is not None:
            seriler[kod] = df
        if i % 20 == 0:
            log(f"  {i}/{len(kodlar)}")
        time.sleep(0.3)
    return seriler, xu


def sahte_veri():
    """Ağ olmadan deneme için rastgele yürüyüş (TR_SAHTE=1)."""
    rng = np.random.default_rng(7)
    gun = pd.bdate_range("2021-01-04", datetime.now(TSI).date())
    piyasa = rng.normal(0.0012, 0.015, len(gun))
    xu = pd.DataFrame({"c": 1000 * np.exp(np.cumsum(piyasa))}, index=gun)
    xu["h"], xu["l"], xu["v"] = xu["c"] * 1.01, xu["c"] * 0.99, 1e9
    seriler = {}
    for j in range(40):
        ozel = rng.normal(rng.normal(0, 0.0008), 0.02, len(gun))
        c = 50 * np.exp(np.cumsum(piyasa + ozel))
        df = pd.DataFrame({"c": c, "h": c * (1 + abs(rng.normal(0, .01, len(gun)))),
                           "l": c * (1 - abs(rng.normal(0, .01, len(gun)))), "v": 5e6}, index=gun)
        seriler[f"HS{j:02d}"] = df
    return seriler, xu


def kapanmis(df: pd.DataFrame) -> pd.DataFrame:
    """Gün içinde elle çalıştırılırsa bugünün yarım mumunu at (karar kapanışla verilir)."""
    simdi = datetime.now(TSI)
    if len(df) and df.index[-1].date() == simdi.date() and (simdi.hour, simdi.minute) < (18, 20):
        return df.iloc[:-1]
    return df


# ---------------------------------------------------------------- göstergeler
def rsi(c: pd.Series, n=14) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def gostergeler(df: pd.DataFrame, xc: pd.Series) -> pd.DataFrame:
    c = df["c"]
    x = xc.reindex(c.index).ffill()
    onceki = c.shift(1)
    tr = pd.concat([df["h"] - df["l"], (df["h"] - onceki).abs(), (df["l"] - onceki).abs()], axis=1).max(axis=1)
    o = pd.DataFrame(index=c.index)
    o["c"] = c
    o["sma20"] = c.rolling(20).mean()
    o["sma50"] = c.rolling(50).mean()
    o["sma200"] = c.rolling(200).mean()
    o["atr"] = tr.rolling(14).mean()
    o["rsi"] = rsi(c)
    o["rs60"] = (c / c.shift(60)) - (x / x.shift(60))
    o["zirve60"] = c / c.rolling(60).max() - 1
    o["likit"] = (c * df["v"]).rolling(20).median()
    return o


def panel_kur(seriler: dict[str, pd.DataFrame], xu: pd.DataFrame):
    xc = xu["c"]
    xt = pd.DataFrame({"c": xc, "sma200": xc.rolling(200).mean()})
    g = {k: gostergeler(kapanmis(df), xc) for k, df in seriler.items()}
    return g, kapanmis(xt)


# ---------------------------------------------------------------- kural motoru
def giris_uygun(s) -> tuple[bool, list[str]]:
    """Bugün AL şartları. Döner: (uygun mu, gerekçeler/eksikler)."""
    if any(pd.isna(s.get(a)) for a in ("sma200", "atr", "rsi", "rs60", "likit")):
        return False, ["yeterli geçmiş yok"]
    neden, eksik = [], []
    (neden if s["c"] > s["sma50"] > s["sma200"] else eksik).append("yükselen trend (fiyat > 50g > 200g ort.)")
    (neden if s["c"] > s["sma20"] else eksik).append("kısa vade ortalamanın üstünde")
    (neden if 50 <= s["rsi"] <= 72 else eksik).append(f"RSI {s['rsi']:.0f} (50-72 arası)")
    (neden if s["rs60"] > 0 else eksik).append(f"3 ayda BIST 100'den %{s['rs60'] * 100:+.0f} iyi")
    (neden if s["zirve60"] >= -0.05 else eksik).append(f"3 aylık zirveye %{-s['zirve60'] * 100:.0f} uzaklıkta")
    (neden if s["likit"] >= MIN_LIKIT else eksik).append("yeterli işlem hacmi")
    return not eksik, (neden if not eksik else eksik)


def cikis_nedeni(p: dict, s) -> str | None:
    if s["c"] <= p["stop"]:
        return "iz süren stop" if p["stop"] > p["ilk_stop"] else "zarar-kes"
    if not pd.isna(s["sma50"]) and s["c"] < s["sma50"]:
        return "trend bozuldu (50 günlük ort. altı)"
    return None


def stop_guncelle(p: dict, s):
    p["zirve"] = max(p["zirve"], float(s["c"]))
    r = p["giris"] - p["ilk_stop"]
    carpan = 2.0 if r > 0 and p["zirve"] - p["giris"] >= KAR_KILIT_R * r else IZ_ATR
    p["stop"] = max(p["stop"], p["zirve"] - carpan * float(s["atr"]))


def deger(d: dict, fiyat: dict[str, float]) -> float:
    return d["nakit"] + sum(p["adet"] * fiyat.get(k, p["son"]) for k, p in d["poz"].items())


def gun_isle(d: dict, tarih: pd.Timestamp, gunluk: dict[str, pd.Series], piyasa_ok: bool, veto: set[str]) -> list[dict]:
    """Tek işlem günü: önce çıkışlar, sonra girişler. Kapanış fiyatından (kayma ve komisyonla) işlem yapar."""
    ts = tarih.strftime("%Y-%m-%d")
    islemler = []
    for kod in list(d["poz"]):
        p, s = d["poz"][kod], gunluk.get(kod)
        if s is None or pd.isna(s["c"]):
            continue
        p["son"] = float(s["c"])
        p["son_tarih"] = ts
        neden = cikis_nedeni(p, s)
        if neden:
            f = s["c"] * (1 - KAYMA)
            gelir = p["adet"] * f * (1 - KOMISYON)
            maliyet = p["adet"] * p["giris"] * (1 + KOMISYON)
            d["nakit"] += gelir
            islemler.append({"tarih": ts, "kod": kod, "tur": "SAT", "fiyat": r2(f), "adet": p["adet"], "neden": neden,
                             "kz": r2(gelir - maliyet, 0), "kz_yuzde": r2((gelir / maliyet - 1) * 100, 1),
                             "gun": int(np.busday_count(p["tarih"], ts))})
            del d["poz"][kod]
        else:
            stop_guncelle(p, s)

    if piyasa_ok and len(d["poz"]) < EN_COK_POZ:
        fiyat = {k: float(s["c"]) for k, s in gunluk.items()}
        toplam = deger(d, fiyat)
        adaylar = []
        for kod, s in gunluk.items():
            if kod in d["poz"] or kod in veto:
                continue
            ok, neden = giris_uygun(s)
            if ok:
                adaylar.append((float(s["rs60"]), kod, s, neden))
        for _, kod, s, neden in sorted(adaylar, key=lambda x: -x[0]):
            if len(d["poz"]) >= EN_COK_POZ:
                break
            f = float(s["c"]) * (1 + KAYMA)
            stop = f - STOP_ATR * float(s["atr"])
            if stop <= 0:
                continue
            adet = int(min(toplam * RISK / (f - stop), toplam * EN_COK_AGIRLIK / f, d["nakit"] / (f * (1 + KOMISYON))))
            if adet < 1 or adet * f < toplam * 0.03:      # çok küçük pozisyon açma
                continue
            d["nakit"] -= adet * f * (1 + KOMISYON)
            d["poz"][kod] = {"adet": adet, "giris": f, "tarih": ts, "ilk_stop": stop, "stop": stop, "zirve": f,
                             "son": float(s["c"]), "son_tarih": ts}
            islemler.append({"tarih": ts, "kod": kod, "tur": "AL", "fiyat": r2(f), "adet": adet,
                             "neden": "; ".join(neden), "stop": r2(stop)})
    return islemler


def gunluk_tablo(g: dict[str, pd.DataFrame], tarih) -> dict[str, pd.Series]:
    out = {}
    for k, o in g.items():
        if tarih in o.index:
            out[k] = o.loc[tarih]
    return out


def piyasa_durumu(xt: pd.DataFrame, tarih) -> bool:
    if tarih not in xt.index:
        return False
    s = xt.loc[tarih]
    return bool(not pd.isna(s["sma200"]) and s["c"] > s["sma200"])


# ---------------------------------------------------------------- geçmiş simülasyon
def istatistik(egri: pd.Series, xu: pd.Series, islemler: list[dict]) -> dict:
    if len(egri) < 2:
        return {}
    yil = max((egri.index[-1] - egri.index[0]).days / 365.25, 1e-9)
    tepe = egri.cummax()
    sat = [i for i in islemler if i["tur"] == "SAT"]
    kaz = [i["kz_yuzde"] for i in sat if i["kz"] > 0]
    kay = [i["kz_yuzde"] for i in sat if i["kz"] <= 0]
    brut_k, brut_z = sum(i["kz"] for i in sat if i["kz"] > 0), -sum(i["kz"] for i in sat if i["kz"] <= 0)
    xr = xu.reindex(egri.index).ffill()
    return {
        "bas": str(egri.index[0].date()), "son": str(egri.index[-1].date()),
        "getiri": r2((egri.iloc[-1] / egri.iloc[0] - 1) * 100, 1),
        "xu_getiri": r2((xr.iloc[-1] / xr.iloc[0] - 1) * 100, 1),
        "yillik": r2(((egri.iloc[-1] / egri.iloc[0]) ** (1 / yil) - 1) * 100, 1),
        "max_dusus": r2((egri / tepe - 1).min() * 100, 1),
        "xu_max_dusus": r2((xr / xr.cummax() - 1).min() * 100, 1),
        "islem": len(sat),
        "isabet": r2(len(kaz) / len(sat) * 100, 0) if sat else None,
        "ort_kazanc": r2(np.mean(kaz), 1) if kaz else None,
        "ort_kayip": r2(np.mean(kay), 1) if kay else None,
        "kar_faktoru": r2(brut_k / brut_z, 2) if brut_z > 0 else None,
        "ort_gun": r2(np.mean([i["gun"] for i in sat]), 0) if sat else None,
    }


def simulasyon(g, xt) -> dict:
    tarihler = [t for t in xt.index if t >= SIM_BASI]
    d = {"nakit": BASLANGIC, "poz": {}}
    egri, islemler = {}, []
    for t in tarihler:
        gl = gunluk_tablo(g, t)
        islemler += gun_isle(d, t, gl, piyasa_durumu(xt, t), set())
        egri[t] = deger(d, {k: float(s["c"]) for k, s in gl.items()})
    e = pd.Series(egri)
    tum = istatistik(e, xt["c"], islemler)
    et = e[e.index >= TEST_BASI]
    test = istatistik(et / et.iloc[0] * BASLANGIC, xt["c"], [i for i in islemler if i["tarih"] >= str(TEST_BASI.date())]) if len(et) > 1 else {}
    haftalik = e.resample("W-FRI").last().dropna()
    xr = xt["c"].reindex(haftalik.index, method="ffill")
    return {
        **tum, "test": test,
        "egri": [[str(t.date()), r2(v / BASLANGIC * 100, 1), r2(x / xr.iloc[0] * 100, 1)] for t, v, x in zip(haftalik.index, haftalik, xr)],
        "son_islemler": islemler[-15:],
        "not": "Bugünkü hisse listesiyle geçmişe gidildiği için batan/endeksten çıkan hisseler yok (hayatta kalan yanlılığı); "
               "gerçek sonuç bundan kötü olabilir. Temettüler fiyat düzeltmesiyle dahil.",
    }


# ---------------------------------------------------------------- canlı (ileriye dönük) hesap
def ai_veto() -> tuple[set[str], dict[str, str]]:
    """Yapay zekâ çoğunluğu SAT ya da riskli listede olan hisseler alınmaz."""
    veto, neden = set(), {}
    try:
        ai = json.loads((VERI / "ai.json").read_text(encoding="utf-8"))
        for kod, m in (ai.get("hisseler") or {}).items():
            k = [v.get("karar1a") or v.get("karar") for v in m.values() if isinstance(v, dict)]
            if k and k.count("SAT") > len(k) / 2:
                veto.add(kod)
                neden[kod] = f"yapay zekâ çoğunluğu SAT diyor ({k.count('SAT')}/{len(k)})"
    except Exception:
        pass
    try:  # secim.py elle doğrulanmış (riskli.json) ve haberden tespit edilen riskleri birleştirir
        rk = json.loads((VERI / "secim.json").read_text(encoding="utf-8")).get("riskli") or {}
        for kod, n in (rk.get("hisse") or {}).items():
            veto.add(kod)
            neden[kod] = "riskli listesinde: " + str(n)[:100]
    except Exception:
        pass
    return veto, neden


def bolunme_duzelt(d: dict, g: dict[str, pd.DataFrame]):
    """Bedelsiz/temettü sonrası Yahoo geçmiş fiyatları yeniden ölçekler; açık pozisyonları da aynı oranla ölçekle."""
    for kod, p in d["poz"].items():
        o = g.get(kod)
        t = pd.Timestamp(p.get("son_tarih") or p["tarih"])
        if o is None or t not in o.index or not p.get("son"):
            continue
        oran = float(o.loc[t, "c"]) / p["son"]
        if abs(oran - 1) > 0.01:
            log(f"  {kod}: fiyat düzeltmesi x{oran:.4f}")
            p["adet"] = p["adet"] / oran
            for a in ("giris", "ilk_stop", "stop", "zirve", "son"):
                p[a] *= oran


def canli(g, xt, eski: dict | None) -> dict:
    tarihler = list(xt.index)
    c = (eski or {}).get("canli")
    if not c or "durum" not in c:
        t0 = tarihler[-1]
        c = {"baslangic_tarih": str(t0.date()), "baslangic": BASLANGIC, "xu_baslangic": float(xt["c"].iloc[-1]),
             "son_tarih": None, "durum": {"nakit": BASLANGIC, "poz": {}}, "islemler": [], "egri": []}
    d = c["durum"]
    bolunme_duzelt(d, g)
    veto, _ = ai_veto()
    son = pd.Timestamp(c["son_tarih"]) if c["son_tarih"] else None
    bugun_islem = []
    for t in tarihler:
        if (son is not None and t <= son) or t < pd.Timestamp(c["baslangic_tarih"]):
            continue
        gl = gunluk_tablo(g, t)
        isl = gun_isle(d, t, gl, piyasa_durumu(xt, t), veto if t == tarihler[-1] else set())
        c["islemler"] += isl
        bugun_islem = isl
        fiyat = {k: float(s["c"]) for k, s in gl.items()}
        c["egri"].append([str(t.date()), r2(deger(d, fiyat), 0), r2(float(xt.loc[t, "c"]), 2)])
        c["son_tarih"] = str(t.date())
    toplam = deger(d, {})
    xu_son = float(xt["c"].iloc[-1])
    c["deger"] = r2(toplam, 0)
    c["getiri"] = r2((toplam / c["baslangic"] - 1) * 100, 2)
    c["xu_getiri"] = r2((xu_son / c["xu_baslangic"] - 1) * 100, 2)
    c["nakit"] = r2(d["nakit"], 0)
    c["pozisyonlar"] = sorted([{
        "kod": k, "adet": r2(p["adet"], 2), "giris": r2(p["giris"]), "tarih": p["tarih"], "son": r2(p["son"]),
        "stop": r2(p["stop"]), "deger": r2(p["adet"] * p["son"], 0),
        "kz": r2(p["adet"] * (p["son"] - p["giris"]), 0), "kz_yuzde": r2((p["son"] / p["giris"] - 1) * 100, 1),
        "stopa_uzaklik": r2((p["stop"] / p["son"] - 1) * 100, 1),
    } for k, p in d["poz"].items()], key=lambda x: -x["deger"])
    sat = [i for i in c["islemler"] if i["tur"] == "SAT"]
    c["kapanan"] = len(sat)
    c["kazanan"] = sum(1 for i in sat if i["kz"] > 0)
    c["gerceklesen_kz"] = r2(sum(i["kz"] for i in sat), 0)
    c["bugun"] = bugun_islem
    return c


def analiz(g, xt) -> dict:
    """Her hisse için bugünkü bot görüşü (Portföyüm ekranı kullanır)."""
    veto, veto_neden = ai_veto()
    piyasa_ok = piyasa_durumu(xt, xt.index[-1])
    out = {}
    for kod, o in g.items():
        if not len(o) or (xt.index[-1] - o.index[-1]).days > 7:
            continue
        s = o.iloc[-1]
        if pd.isna(s["atr"]) or pd.isna(s["sma50"]):
            continue
        c_, atr = float(s["c"]), float(s["atr"])
        ok, neden = giris_uygun(s)
        trend_bozuk = c_ < s["sma50"] or (not pd.isna(s["sma200"]) and s["sma50"] < s["sma200"] and c_ < s["sma200"])
        if trend_bozuk:
            karar, gerekce = "SAT", ["fiyat 50 günlük ortalamanın altında" if c_ < s["sma50"] else "düşen trend (200 günlük ort. altı)"]
        elif ok and kod not in veto:
            karar, gerekce = "AL", neden + ([] if piyasa_ok else ["dikkat: BIST 100 200 günlük ortalamanın altında, bot şu an yeni alım yapmıyor"])
        else:
            karar, gerekce = "TUT", (["trend sürüyor, alım şartları tam değil: " + ", ".join(neden[:3])] if not ok else [])
        if kod in veto:
            gerekce.append(veto_neden[kod])
        stop = c_ - STOP_ATR * atr
        out[kod] = {
            "f": r2(c_), "karar": karar, "neden": gerekce[:5],
            "stop": r2(stop), "hedef": r2(c_ + 2 * STOP_ATR * atr), "iz": r2(IZ_ATR * atr),
            "atr_yuzde": r2(atr / c_ * 100, 1), "rsi": r2(s["rsi"], 0), "rs60": r2(s["rs60"] * 100, 1),
            "sma50": r2(s["sma50"]), "sma200": r2(s["sma200"]), "zirve60": r2(s["zirve60"] * 100, 1),
        }
    return out


def main():
    eski = None
    try:
        eski = json.loads(CIKTI.read_text(encoding="utf-8"))
    except Exception:
        pass
    seriler, xu = veri_cek()
    g, xt = panel_kur(seriler, xu)
    log(f"Veri: {len(g)} hisse, son gün {xt.index[-1].date()}")
    sim = simulasyon(g, xt)
    log("Sim:", json.dumps({k: v for k, v in sim.items() if k not in ("egri", "son_islemler")}, ensure_ascii=False))
    c = canli(g, xt, eski)
    log("Canlı:", c["getiri"], "BIST100:", c["xu_getiri"], "pozisyon:", [p["kod"] for p in c["pozisyonlar"]],
        "bugün:", [(i["tur"], i["kod"]) for i in c["bugun"]])
    sonuc = {
        "updatedAt": datetime.now(TSI).isoformat(timespec="seconds"),
        "tarih": str(xt.index[-1].date()),
        "piyasa_ok": piyasa_durumu(xt, xt.index[-1]),
        "kurallar": {"baslangic": BASLANGIC, "komisyon": KOMISYON, "kayma": KAYMA, "en_cok_poz": EN_COK_POZ,
                     "risk": RISK, "en_cok_agirlik": EN_COK_AGIRLIK, "stop_atr": STOP_ATR, "iz_atr": IZ_ATR},
        "canli": c,
        "sim": sim,
        "analiz": analiz(g, xt),
    }
    CIKTI.write_text(json.dumps(sonuc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
