"""app/app.html şablonuna data/ klasöründeki başlangıç verilerini gömer ve index.html üretir."""
from pathlib import Path

KOK = Path(__file__).resolve().parent.parent
t = (KOK / "app" / "app.html").read_text(encoding="utf-8")
for yer, dosya in [("__SNAP__", "snapshot"), ("__NEWS__", "news"), ("__OUTLOOK__", "outlook"), ("__FUNDS__", "funds"), ("__AI__", "ai"), ("__TEMEL__", "temel")]:
    yol = KOK / "data" / f"{dosya}.json"
    veri = yol.read_text(encoding="utf-8") if yol.exists() else "{}"
    t = t.replace(yer, veri.replace("</", "<\\/"))
(KOK / "index.html").write_text(t, encoding="utf-8")
print("index.html", len(t), "bayt")
