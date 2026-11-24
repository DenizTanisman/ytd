# Opus audio + Whisper transcripts: measurements (22 October 2026)

ytd gained an "Opus" audio option (`ytd.py -a --opus URL`, or Ses › Opus (Audio › Opus) in the UI). YouTube's
Opus stream (itag 251) is saved to an `.opus` file without re-encoding. This note compares the effect of Opus
on downloads and Whisper transcripts against M4A.

## Method

- **Videos:** two talks with subtitles written by hand by the uploader
  - English: TED-Ed, "How to manage your emotions" (4:51)
  - Turkish: TEDxIstanbul, "Korku, Kaygı ve Endişe" (14:38)
- For each video, Opus and M4A (high) audio was downloaded and both were transcribed with ytd's Whisper
  (`mlx-community/whisper-large-v3-turbo`, on this Mac, offline).
- **Accuracy:** the Whisper text was compared word by word with the uploader's hand-written subtitles.
  - **WER:** word error rate (substituted + missing + extra words / words in the subtitles)
  - **CER:** character error rate
  - Before comparing, both texts were lowercased; punctuation, apostrophes and notes such as
    [Müzik] (music) were removed. For Turkish, I→ı and İ→i were mapped.
- Measurement script: `opus_test/olcum.py` in the session scratchpad (not added to the project).

## Results

| Video | Format | Duration | Bitrate | Size | Download | Whisper | Speed | WER | CER |
|---|---|---|---|---|---|---|---|---|---|
| English (4:51) | Opus | 290.8 s | 151 kbps | 5.48 MB | 2.6 s | 28.0 s | 10.4× | 8.6 % | 7.9 % |
| English (4:51) | M4A | 290.9 s | 138 kbps | 5.02 MB | 4.1 s | 26.4 s | 11.0× | 8.6 % | 7.9 % |
| Turkish (14:38) | Opus | 877.7 s | 109 kbps | 11.96 MB | 3.5 s | 95.0 s | 9.2× | 6.9 % | 3.4 % |
| Turkish (14:38) | M4A | 877.7 s | 132 kbps | 14.43 MB | 3.6 s | 110.0 s | 8.0× | 6.5 % | 3.4 % |

"Speed": audio duration / Whisper time. 10× means 10 minutes of audio is transcribed in about 1 minute.

## Takeaways

- **Opus is as accurate as M4A:** identical in English; in Turkish the difference is 0.4 points (about
  7 words out of 1,718), with the same character error rate. The difference is at noise level.
- **Opus can be smaller:** 17% smaller file for the Turkish talk (109 / 132 kbps). For English, YouTube's
  Opus stream had a slightly higher bitrate (151 / 138 kbps). Size depends on the stream YouTube serves
  for that video.
- **Time:** downloads take a few seconds. Whisper runs at about 8–11× real time on this Mac: a 15-minute
  talk takes 1.5–2 minutes, a 1-hour stream 6–8 minutes. The time difference between Opus and M4A is
  within measurement noise.
- **The real error rate is lower:** hand-written subtitles don't transcribe speech verbatim (filler words,
  repetitions and contractions are dropped). Whisper writing 45 extra words in English is a sign of this.
  Some of the remaining differences are spelling variants (realize/realise). The measured WER is therefore
  an upper bound.
- **Turkish language detection:** YouTube gave no language information for this Turkish video, so no
  language could be passed to Whisper and it was auto-detected. That was fine on this short, clean talk
  (lines drifting into English: 0%). But on long live streams auto-detection can turn part of the text
  into English (seen in the Haluk Tatar series). So when there is no language information, ytd now checks,
  in order: a single "-orig" marker in the automatic captions (the video's original language), then
  Turkish-specific letters in the title (ğ, ş, ı, İ), which mean Turkish. This test video is now recognised
  as Turkish from its title.

## A bug fixed in the first measurement

The first measurement gave an English WER of 12.2%. The cause was the measurement code, not Whisper: the
typographic apostrophe in the subtitles (they’re) split the word in two. Once apostrophes were removed from
both texts, WER dropped to 8.6%.
