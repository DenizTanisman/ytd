# Opus ses + Whisper transkript: ölçüm (22 Ekim 2026)

ytd'ye "Opus" ses seçeneği eklendi (`ytd.py -a --opus URL` ya da arayüzde Ses › Opus). YouTube'un
Opus akışı (itag 251) yeniden kodlanmadan `.opus` dosyasına alınır. Bu notta Opus'un indirme ve
Whisper transkripti üzerindeki etkisi M4A ile karşılaştırıldı.

## Yöntem

- **Videolar:** yükleyicinin elle yazdığı altyazısı olan iki konuşma
  - İngilizce: TED-Ed, "How to manage your emotions" (4:51)
  - Türkçe: TEDxIstanbul, "Korku, Kaygı ve Endişe" (14:38)
- Her video için Opus ve M4A (yüksek) ses indirildi, ikisi de ytd'nin Whisper'ıyla
  (`mlx-community/whisper-large-v3-turbo`, bu Mac'te, internetsiz) yazıya döküldü.
- **Doğruluk:** Whisper metni, yükleyicinin elle yazdığı altyazıyla kelime kelime karşılaştırıldı.
  - **WER:** kelime hata oranı (değişen + eksik + fazla kelime / altyazıdaki kelime)
  - **CER:** harf hata oranı
  - Karşılaştırmadan önce iki metin de küçük harfe çevrildi; noktalama, kesme işareti ve
    [Müzik] gibi notlar atıldı. Türkçede I→ı, İ→i dönüşümü yapıldı.
- Ölçüm betiği: oturumun scratchpad'inde `opus_test/olcum.py` (projeye alınmadı).

## Sonuçlar

| Video | Biçim | Süre | Bit hızı | Boyut | İndirme | Whisper | Hız | WER | CER |
|---|---|---|---|---|---|---|---|---|---|
| İngilizce (4:51) | Opus | 290,8 sn | 151 kbps | 5,48 MB | 2,6 sn | 28,0 sn | 10,4× | 8,6 % | 7,9 % |
| İngilizce (4:51) | M4A | 290,9 sn | 138 kbps | 5,02 MB | 4,1 sn | 26,4 sn | 11,0× | 8,6 % | 7,9 % |
| Türkçe (14:38) | Opus | 877,7 sn | 109 kbps | 11,96 MB | 3,5 sn | 95,0 sn | 9,2× | 6,9 % | 3,4 % |
| Türkçe (14:38) | M4A | 877,7 sn | 132 kbps | 14,43 MB | 3,6 sn | 110,0 sn | 8,0× | 6,5 % | 3,4 % |

"Hız": ses süresi / Whisper süresi. 10× = 10 dakikalık ses yaklaşık 1 dakikada yazıya döker.

## Çıkarımlar

- **Opus, M4A kadar doğru:** İngilizcede birebir aynı, Türkçede fark 0,4 puan (1.718 kelimede yaklaşık
  7 kelime), harf hata oranı aynı. Fark gürültü düzeyinde.
- **Opus daha küçük olabilir:** Türkçe konuşmada %17 daha küçük dosya (109 / 132 kbps). İngilizcede
  YouTube'un Opus akışı biraz daha yüksek bit hızlıydı (151 / 138 kbps). Boyut, YouTube'un o video için
  sunduğu akışa bağlı.
- **Süre:** İndirme birkaç saniye. Whisper bu Mac'te yaklaşık 8–11× gerçek zamanlı: 15 dakikalık
  konuşma 1,5–2 dakika, 1 saatlik yayın 6–8 dakika. Opus ile M4A arasındaki süre farkı ölçüm
  dalgalanması düzeyinde.
- **Hata oranı gerçekte daha düşük:** Elle yazılmış altyazı konuşmayı birebir yazmaz (dolgu sözcükleri,
  tekrarlar, kısaltmalar atılır). İngilizcede Whisper'ın 45 kelime fazla yazması bunun işareti.
  Kalan farkların bir kısmı yazım farkı (realize/realise gibi). Ölçülen WER bu yüzden üst sınırdır.
- **Türkçe dil bilgisi:** YouTube bu Türkçe video için dil bilgisi vermedi, bu yüzden Whisper'a dil
  verilemedi ve dil otomatik algılandı. Bu kısa, temiz konuşmada sorun olmadı (İngilizceye kayan satır:
  %0). Ama uzun canlı yayınlarda otomatik algılama yazının bir kısmını İngilizceye çevirebiliyor
  (Haluk Tatar serisinde görüldü). Bu yüzden ytd artık dil bilgisi yoksa sırayla şunlara bakar:
  otomatik altyazılardaki tek "-orig" işareti (videonun asıl dili), sonra başlıkta Türkçeye özgü
  harfler (ğ, ş, ı, İ) varsa Türkçe. Bu test videosu artık başlığından Türkçe olarak tanınır.

## İlk ölçümde düzeltilen bir hata

İlk ölçümde İngilizce WER %12,2 çıktı. Sebep ölçüm koduydu, Whisper değil: altyazıdaki tipografik kesme
işareti (they’re) kelimeyi ikiye bölüyordu. Kesme işaretleri iki metinden de atılınca WER %8,6'ya indi.
