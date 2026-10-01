"""BIST endeks üyelikleri (BIST 30 / 50 / 100) -> data/endeksler.json. Günde bir kez yeterli (üyelikler 3 ayda bir değişir).
Kaynak: TradingView tarayıcısı, endeks grubu filtresi. Sayı tutarsızsa eski liste korunur."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guncelle  # noqa: E402  (tarayıcı taklitli oturum)

KOK = Path(__file__).resolve().parent.parent
CIKTI = KOK / "data" / "endeksler.json"
TSI = timezone(timedelta(hours=3))
ENDEKSLER = {"XU030": ("BIST 30", 28, 32), "XU050": ("BIST 50", 47, 53), "XU100": ("BIST 100", 95, 105)}


def uyeler(endeks: str) -> list[str]:
    govde = {"filter": [{"left": "type", "operation": "equal", "right": "stock"}],
             "symbols": {"query": {"types": []}, "tickers": [], "groups": [{"type": "index", "values": [f"BIST:{endeks}"]}]},
             "markets": ["turkey"], "columns": ["name", "market_cap_basic"], "range": [0, 300],
             "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"}}
    r = guncelle.yahoo_oturum.post("https://scanner.tradingview.com/turkey/scan", json=govde, timeout=30,
                                   headers={"Content-Type": "application/json", "Origin": "https://www.tradingview.com",
                                            "Referer": "https://www.tradingview.com/"})
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} {r.text[:120]}")
    return [str(x["d"][0]).upper() for x in r.json().get("data", []) if x.get("d")]


def main() -> int:
    try:
        eski = json.loads(CIKTI.read_text(encoding="utf-8"))
    except Exception:
        eski = {}
    sonuc = {"updatedAt": datetime.now(TSI).isoformat(timespec="seconds"), "kaynak": "TradingView", "endeksler": {}}
    for kod, (ad, en_az, en_cok) in ENDEKSLER.items():
        try:
            u = uyeler(kod)
            if not en_az <= len(u) <= en_cok:
                raise RuntimeError(f"{len(u)} hisse geldi, beklenen {en_az}-{en_cok}")
            sonuc["endeksler"][kod] = {"ad": ad, "hisseler": sorted(u)}
            print(ad, len(u), "hisse")
        except Exception as e:
            print(ad, "alınamadı:", e)
            if kod in (eski.get("endeksler") or {}):
                sonuc["endeksler"][kod] = eski["endeksler"][kod]
    if not sonuc["endeksler"]:
        return 1
    CIKTI.write_text(json.dumps(sonuc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
