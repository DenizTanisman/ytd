# Video: H.264 · MP4 ile AV1 · MKV karşılaştırması (22 Ekim 2026)

ytd'ye "AV1 · MKV" video seçeneği eklendi ve ölçüm sonucunda varsayılan yapıldı. Bu seçenek
YouTube'un AV1 görüntüsünü (yoksa VP9) Opus sesle birlikte, yeniden kodlamadan MKV'ye koyar.
"H.264 · MP4" seçeneği QuickTime uyumluluğu için duruyor (`ytd.py --h264`).

## Yöntem

- **Video:** TEDxIstanbul, "Korku, Kaygı ve Endişe" (14:38; YouTube 2160p'ye kadar sunuyor).
- Her iki seçenekle 720p, 1080p, 1440p ve 2160p istendi.
- **Görüntü kalitesi:** VMAF (Netflix'in ölçüsü, 0–100; ~93 üstü gözle ayırt etmesi zor).
  - Referans: YouTube'un en yüksek bit hızlı akışı olan 2160p VP9 (~3,6 Mbps).
  - Her dosyadan 2:00'dan başlayan 60 sn'lik kesit alındı, 1080p'ye ölçeklendi ve referansla
    karşılaştırıldı. Yani sonuç "1080p ekranda nasıl görünüyor" sorusunun cevabı.
- **QuickTime:** macOS'in AVFoundation'ı dosyayı oynatılabilir buluyor mu.
- **Transkript:** 1080p video dosyalarından doğrudan Whisper (`whisper-large-v3-turbo`) ile metin
  çıkarıldı. Doğruluk, yükleyicinin elle yazdığı Türkçe altyazıya göre kelime hata oranıyla (WER) ölçüldü.
- Ölçüm betiği: oturumun scratchpad'inde `opus_test/olcum_video.py` (projeye alınmadı).

## Sonuçlar

| İstenen | Seçenek | Gelen | Boyut | İndirme | VMAF | QuickTime |
|---|---|---|---|---|---|---|
| 720p | H.264 · MP4 | H.264 + AAC, 720p | 58,5 MB | 10,7 sn | 87,2 | açar |
| 720p | AV1 · MKV | AV1 + Opus, 720p | **29,9 MB** | 8,4 sn | 87,6 | açmaz |
| 1080p | H.264 · MP4 | H.264 + AAC, 1080p | 95,9 MB | 17,1 sn | 92,4 | açar |
| 1080p | AV1 · MKV | AV1 + Opus, 1080p | **40,8 MB** | 9,6 sn | 92,8 | açmaz |
| 1440p | H.264 · MP4 | **H.264, 1080p** (1440p yok) | 95,9 MB | — | 92,4 | açar |
| 1440p | AV1 · MKV | AV1 + Opus, 1440p | 83,4 MB | 17,8 sn | 95,7 | açmaz |
| 2160p | H.264 · MP4 | **H.264, 1080p** (2160p yok) | 95,9 MB | 22,9 sn | 92,4 | açar |
| 2160p | AV1 · MKV | AV1 + Opus, 2160p | 144,0 MB | 31,5 sn | 96,5 | açmaz |

(H.264 1440p satırının indirme süresi 83,6 sn çıktı; aynı dosyanın diğer indirmeleri 17–23 sn,
yani ağ dalgalanması. Tabloda verilmedi.)

| Transkript (1080p videodan) | Whisper süresi | WER |
|---|---|---|
| H.264 · MP4 (AAC ses) | 119,6 sn | 6,5 % |
| AV1 · MKV (Opus ses) | 130,3 sn | 6,9 % |
| Karşılaştırma: sadece ses, Opus (önceki ölçüm) | 95,0 sn | 6,9 % |
| Karşılaştırma: sadece ses, M4A (önceki ölçüm) | 110,0 sn | 6,5 % |

## Çıkarımlar

- **AV1 aynı kalitede yarı boyut:** 720p'de %49, 1080p'de %57 daha küçük. VMAF değeri eşit, hatta
  biraz daha yüksek (87,6 / 87,2 ve 92,8 / 92,4). İndirmesi de daha hızlı.
- **1080p üstü yalnız AV1 ile:** YouTube H.264'ü en fazla 1080p veriyor, bu yüzden "H.264 · MP4"
  seçeneği 1440p ya da 2160p istense de 1080p indiriyor. AV1 gerçek 1440p (VMAF 95,7) ve 2160p
  (96,5) veriyor; 2160p AV1 bile 1080p H.264'ten sadece %50 büyük.
- **Tek eksi: QuickTime.** AV1 · MKV dosyalarını QuickTime açmıyor. VLC ve Chrome açıyor
  (ikisi de bu Mac'te kurulu); M4 çipte AV1 donanımla çözüldüğü için oynatma akıcı olmalı.
  QuickTime gerekiyorsa `--h264` / arayüzde "H.264 · MP4 (QuickTime)".
- **Transkripte uygunluk:** Video dosyasından doğrudan transkript sorunsuz. Doğruluk, aynı sesin
  sadece-ses indirmesiyle birebir aynı (AV1 → Opus ses → %6,9; MP4 → AAC ses → %6,5). Whisper
  süresi video dosyasında yaklaşık 10–35 sn daha uzun: ffmpeg sesi büyük dosyanın içinden ayıklıyor
  (süreler aynı ölçüm turunda değil, bir kısmı dalgalanma olabilir).
  - Sadece metin lazımsa **Ses › Opus** ya da **Transkript** modu daha hızlı ve çok daha küçük
    (12 MB / 41–96 MB).
  - Hem video hem metin lazımsa video + "Transkript de çıkar" yeterli; ayrıca ses indirmeye gerek yok.
- **Dil:** Bu ölçümde Whisper'a dil olarak "tr" verildi: YouTube dil bilgisi vermediği için ytd'nin
  yeni yedek yöntemi başlıktaki Türkçe harflerden buldu.
