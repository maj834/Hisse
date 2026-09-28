# Hisse Radar

BIST 30 hisseleri için telefondan açılan borsa uygulaması: dokunmatik fiyat grafikleri, gün içi takip, otomatik al/sat seviyeleri, haberler, fonlar ve haberlere dayalı "yükselmesi / düşmesi beklenen" listeleri.

## Nasıl çalışır

| Parça | Ne yapar | Ne sıklıkla |
|---|---|---|
| `scraper/guncelle.py` (GitHub Actions) | Yahoo Finance'ten BIST 30 fiyatlarını, Google Haberler'den başlıkları, TEFAS'tan fon fiyatlarını çeker; `data` dalına yazar | Borsa açıkken dakikada bir, kapalıyken 2 saatte bir |
| `index.html` (GitHub Pages) | Uygulamanın kendisi; `data` dalındaki dosyaları kontrol eder, değişince ekranı günceller | 5 saniyede bir |
| `data/outlook.json` | Claude'un haber ve fiyatlara bakarak yazdığı beklenti analizi | Borsa açıkken saatte bir |

Fiyatlar kaynağında yaklaşık 15 dakika gecikmelidir. Uygulamadaki sinyaller ve beklentiler yatırım tavsiyesi değildir.

## Ayarlar

- Takip edilen fonlar: `fonlar.txt` (her satıra bir TEFAS kodu).
- Elle güncelleme: Actions → "Veri güncelle" → Run workflow (`tek` veya `dongu`).

## Gereksinim

Depo **herkese açık (public)** olmalı: GitHub Pages ücretsiz planda yalnızca açık depolarda çalışır, Actions dakikaları da açık depolarda sınırsızdır.
