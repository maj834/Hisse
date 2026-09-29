"""Yapay zekâ sağlayıcılarını küçük bir istekle dener; sonucu (anahtarsız) data/tani.json'a yazar. Elle çalıştırılır."""
import datetime as dt
import json
import os
from pathlib import Path

import requests

KOK = Path(__file__).resolve().parent.parent
MESAJ = [{"role": "user", "content": "Sadece TAMAM yaz."}]


def dene(ad, yontem, url, anahtar, **kw):
    try:
        r = requests.request(yontem, url, timeout=60, allow_redirects=False, **kw)
        govde = r.text[:400].replace(anahtar, "***") if anahtar else r.text[:400]
        return {"ad": ad, "url": url, "durum": r.status_code, "tur": r.headers.get("content-type", ""),
                "yonlendirme": r.headers.get("location", ""), "govde": govde,
                "limit": {k: v for k, v in r.headers.items() if "ratelimit" in k.lower() or "retry" in k.lower()}}
    except Exception as e:
        return {"ad": ad, "url": url, "hata": str(e)[:200]}


def main():
    out = {"zaman": dt.datetime.now().isoformat(timespec="seconds"), "sonuc": []}
    k = os.environ.get("MISTRAL_API_KEY", "").strip()
    if k:
        h = {"Authorization": f"Bearer {k}", "Content-Type": "application/json"}
        out["sonuc"].append(dene("mistral modeller", "GET", "https://api.mistral.ai/v1/models", k, headers=h))
        for m in ("mistral-small-latest", "mistral-large-latest"):
            out["sonuc"].append(dene(f"mistral {m}", "POST", "https://api.mistral.ai/v1/chat/completions", k, headers=h,
                                     json={"model": m, "messages": MESAJ, "max_tokens": 10}))
    k = os.environ.get("SAMBANOVA_API_KEY", "").strip()
    if k:
        h = {"Authorization": f"Bearer {k}", "Content-Type": "application/json"}
        out["sonuc"].append(dene("sambanova modeller", "GET", "https://api.sambanova.ai/v1/models", k, headers=h))
        for m in ("Meta-Llama-3.1-8B-Instruct", "Meta-Llama-3.3-70B-Instruct"):
            out["sonuc"].append(dene(f"sambanova {m}", "POST", "https://api.sambanova.ai/v1/chat/completions", k, headers=h,
                                     json={"model": m, "messages": MESAJ, "max_tokens": 10}))
    k = os.environ.get("GH_MODELS_TOKEN", "").strip()
    out["gh_token_uzunluk"] = len(k)
    out["gh_token_bas"] = k[:11]
    if k:
        h = {"Authorization": f"Bearer {k}", "Content-Type": "application/json", "Accept": "application/json"}
        out["sonuc"].append(dene("github katalog", "GET", "https://models.github.ai/catalog/models", k, headers=h))
        out["sonuc"].append(dene("github gpt-4.1", "POST", "https://models.github.ai/inference/chat/completions", k, headers=h,
                                 json={"model": "openai/gpt-4.1", "messages": MESAJ, "max_tokens": 10}))
        out["sonuc"].append(dene("github gpt-4.1-mini", "POST", "https://models.github.ai/inference/chat/completions", k, headers=h,
                                 json={"model": "openai/gpt-4.1-mini", "messages": MESAJ, "max_tokens": 10}))
        out["sonuc"].append(dene("github eski uç", "POST", "https://models.inference.ai.azure.com/chat/completions", k, headers=h,
                                 json={"model": "gpt-4.1-mini", "messages": MESAJ, "max_tokens": 10}))
        out["sonuc"].append(dene("github kullanıcı", "GET", "https://api.github.com/user", k, headers=h))
    for s in out["sonuc"]:
        for x in ("govde",):
            if s.get(x) and len(s[x]) > 400:
                s[x] = s[x][:400]
    (KOK / "data" / "tani.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1)[:4000])


main()
