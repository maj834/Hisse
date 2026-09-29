"""app/app.html şablonuna data/ klasöründeki başlangıç verilerini gömer ve index.html üretir."""
import json
from pathlib import Path

KOK = Path(__file__).resolve().parent.parent
t = (KOK / "app" / "app.html").read_text(encoding="utf-8")
for yer, dosya in [("__SNAP__", "snapshot"), ("__NEWS__", "news"), ("__OUTLOOK__", "outlook"), ("__FUNDS__", "funds"),
                   ("__AI__", "ai"), ("__TEMEL__", "temel"), ("__FAI__", "fon_ai")]:
    yol = KOK / "data" / f"{dosya}.json"
    veri = yol.read_text(encoding="utf-8") if yol.exists() else "{}"
    if dosya == "funds":  # 2000+ fonluk liste uygulama açılınca ayrıca indirilir; başlangıç dosyasını küçük tut
        try:
            d = json.loads(veri)
            d.pop("tum", None)
            d["updatedAt"] = ""
            veri = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            pass
    t = t.replace(yer, veri.replace("</", "<\\/"))


def md_html(md: str) -> str:
    """gizlilik.md için basit Markdown → HTML (başlık, madde, kalın, paragraf)."""
    import html, re
    out, liste = [], False
    for satir in md.splitlines():
        s = html.escape(satir.strip())
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        if s.startswith("- "):
            if not liste:
                out.append("<ul>")
                liste = True
            out.append(f"<li>{s[2:]}</li>")
            continue
        if liste:
            out.append("</ul>")
            liste = False
        if s.startswith("## "):
            out.append(f"<h3>{s[3:]}</h3>")
        elif s.startswith("# "):
            out.append(f"<h2>{s[2:]}</h2>")
        elif s:
            out.append(f"<p>{s}</p>")
    if liste:
        out.append("</ul>")
    return "".join(out)


t = t.replace("__YASAL__", md_html((KOK / "gizlilik.md").read_text(encoding="utf-8")))


def karistir(html: str) -> str:
    """Ana <script> bloğunu okunamaz hâle getirir (javascript-obfuscator, tools/karistir.js).
    Başarısız olursa HATA verir: karıştırılmamış ekran asla yayınlanmaz.
    Yalnızca yerel deneme için KARISTIRMA=0 ile atlanır (o çıktı commit edilmez; GitHub 'Ekranı derle' yeniden üretir)."""
    import os, re, subprocess, tempfile
    if os.environ.get("KARISTIRMA") == "0":
        print("UYARI: karıştırma atlandı (yalnızca yerel deneme)")
        return html
    m = list(re.finditer(r"<script>(.*?)</script>", html, re.S))
    if len(m) != 1:
        raise SystemExit(f"ana script bulunamadı ({len(m)})")
    araclar = KOK / "tools"
    if not (araclar / "node_modules" / "javascript-obfuscator").exists():
        subprocess.run(["npm", "install", "--no-audit", "--no-fund", "--silent"], cwd=araclar, check=True)
    with tempfile.TemporaryDirectory() as d:
        g, c = Path(d) / "g.js", Path(d) / "c.js"
        g.write_text(m[0].group(1), encoding="utf-8")
        subprocess.run(["node", str(araclar / "karistir.js"), str(g), str(c)], check=True)
        kod = c.read_text(encoding="utf-8")
    if len(kod) < 1000 or "secimKart" in kod or "renderOutlook" in kod:  # iç fonksiyon adları görünmemeli
        raise SystemExit("karıştırma doğrulanamadı")
    return html[:m[0].start(1)] + kod + html[m[0].end(1):]


t = karistir(t)
(KOK / "index.html").write_text(t, encoding="utf-8")
print("index.html", len(t), "bayt")
