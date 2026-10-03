# Hisse Radar

BIST 30 hisseleri için telefondan açılan borsa uygulaması: dokunmatik fiyat grafikleri, gün içi takip, otomatik al/sat seviyeleri, haberler, fonlar ve haberlere dayalı "yükselmesi / düşmesi beklenen" listeleri.

## Nasıl çalışır

| Parça | Ne yapar | Ne sıklıkla |
|---|---|---|
| `scraper/guncelle.py` (GitHub Actions) | Yahoo Finance'ten BIST 30 fiyatlarını, Google Haberler'den başlıkları, TEFAS'tan fon fiyatlarını çeker; `data` dalına yazar | Borsa açıkken dakikada bir, kapalıyken 2 saatte bir |
| `index.html` (GitHub Pages) | Uygulamanın kendisi; `data` dalındaki dosyaları kontrol eder, değişince ekranı günceller | 5 saniyede bir |
| `data/outlook.json` | Claude'un haber ve fiyatlara bakarak yazdığı beklenti analizi, kararları ve 1 haftalık hedefleri | Borsa açıkken saatte bir |
| `scraper/ai_degerlendir.py` | Llama (Groq), OpenRouter ve isteğe bağlı Grok, Mistral, GPT, DeepSeek'e 30 hisse için AL/TUT/SAT ve 1 haftalık hedef sorar; `data/ai.json` | Borsa açıkken saatte bir |
| `scraper/trader.py` | Sanal trader: kurallarla (trend, piyasaya göre güç, zarar-kes, iz süren stop, %1 risk) gerçek para olmadan al-sat yapar; aynı kuralları 2022'den bugüne geçmişte de dener ve BIST 100 ile karşılaştırır; her hisse için AL/TUT/SAT, zarar-kes ve hedef yazar (`data/trader.json`). Uygulamadaki **Portföy** sekmesi bununla kullanıcının kendi hisselerini değerlendirir | Her işlem günü 18:40 TSİ |
| `android/` | Uygulamanın APK'sı; açılışta güncel sayfayı yükler, internet yoksa içindeki kopyayı açar | Her değişiklikte derlenir |

## APK

İndirme linki: https://github.com/maj834/Hisse/releases/latest/download/HisseRadar.apk

## Yapay zekâ anahtarları

Settings → Secrets and variables → Actions → New repository secret:

| Ad | Nereden | Ücret |
|---|---|---|
| `GROQ_API_KEY` | console.groq.com → API Keys | Ücretsiz katman |
| `XAI_API_KEY` | console.x.ai → API Keys | Ücretli |
| `OPENROUTER_API_KEY` | openrouter.ai → Keys (`:free` modeller) | Ücretsiz katman |
| `MISTRAL_API_KEY` | console.mistral.ai → API Keys | Ücretsiz katman |
| `GH_MODELS_TOKEN` | GitHub → Developer settings → Fine-grained token, "Models: Read" | Ücretsiz (GPT, DeepSeek) |

Anahtarlar hiçbir zaman koda ya da uygulamaya yazılmaz; yalnızca GitHub Actions içinde kullanılır.

Fiyatlar kaynağında yaklaşık 15 dakika gecikmelidir. Uygulamadaki sinyaller ve beklentiler yatırım tavsiyesi değildir.

## Ayarlar

- Takip edilen fonlar: `fonlar.txt` (her satıra bir TEFAS kodu).
- Elle güncelleme: Actions → "Veri güncelle" → Run workflow (`tek` veya `dongu`).
- Tasarım: `app/app.html` düzenlenir, `python3 tools/build_index.py` ile `index.html` üretilir.

## Gereksinim

Depo **herkese açık (public)** olmalı: GitHub Pages ücretsiz planda yalnızca açık depolarda çalışır, Actions dakikaları da açık depolarda sınırsızdır.


## Telif hakkı ve lisans
© 2026 Cem Ulaş Eren. **Tüm hakları saklıdır.** Bu depo yalnızca görüntülemeye açıktır; kod, uygulama, APK, tasarım,
analiz kuralları, yapay zekâ istemleri ve veriler izinsiz kopyalanamaz, değiştirilemez, yayınlanamaz ve kullanılamaz.
Ayrıntılar: [LICENSE](LICENSE). Uygulama ekranı yalnızca resmi APK'da ve https://maj834.github.io/Hisse/ adresinde çalışır.
