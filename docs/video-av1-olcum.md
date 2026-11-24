# Video: H.264 · MP4 vs AV1 · MKV (22 October 2026)

ytd gained an "AV1 · MKV" video option, and after these measurements it became the default. It puts
YouTube's AV1 video (or VP9 if there is no AV1) together with Opus audio into an MKV, without re-encoding.
The "H.264 · MP4" option stays for QuickTime compatibility (`ytd.py --h264`).

## Method

- **Video:** TEDxIstanbul, "Korku, Kaygı ve Endişe" (14:38; YouTube offers up to 2160p).
- 720p, 1080p, 1440p and 2160p were requested with both options.
- **Picture quality:** VMAF (Netflix's metric, 0–100; above ~93 is hard to tell apart by eye).
  - Reference: YouTube's highest-bitrate stream, 2160p VP9 (~3.6 Mbps).
  - A 60 s clip starting at 2:00 was taken from each file, scaled to 1080p and compared with the
    reference. So the result answers "how does it look on a 1080p screen".
- **QuickTime:** whether macOS's AVFoundation considers the file playable.
- **Transcript:** text was extracted with Whisper (`whisper-large-v3-turbo`) directly from the 1080p video
  files. Accuracy was measured as word error rate (WER) against the uploader's hand-written Turkish subtitles.
- Measurement script: `opus_test/olcum_video.py` in the session scratchpad (not added to the project).

## Results

| Requested | Option | Received | Size | Download | VMAF | QuickTime |
|---|---|---|---|---|---|---|
| 720p | H.264 · MP4 | H.264 + AAC, 720p | 58.5 MB | 10.7 s | 87.2 | opens |
| 720p | AV1 · MKV | AV1 + Opus, 720p | **29.9 MB** | 8.4 s | 87.6 | doesn't open |
| 1080p | H.264 · MP4 | H.264 + AAC, 1080p | 95.9 MB | 17.1 s | 92.4 | opens |
| 1080p | AV1 · MKV | AV1 + Opus, 1080p | **40.8 MB** | 9.6 s | 92.8 | doesn't open |
| 1440p | H.264 · MP4 | **H.264, 1080p** (no 1440p) | 95.9 MB | — | 92.4 | opens |
| 1440p | AV1 · MKV | AV1 + Opus, 1440p | 83.4 MB | 17.8 s | 95.7 | doesn't open |
| 2160p | H.264 · MP4 | **H.264, 1080p** (no 2160p) | 95.9 MB | 22.9 s | 92.4 | opens |
| 2160p | AV1 · MKV | AV1 + Opus, 2160p | 144.0 MB | 31.5 s | 96.5 | doesn't open |

(The download time for the H.264 1440p row came out at 83.6 s; other downloads of the same file took
17–23 s, so it was network fluctuation. It is left out of the table.)

| Transcript (from 1080p video) | Whisper time | WER |
|---|---|---|
| H.264 · MP4 (AAC audio) | 119.6 s | 6.5 % |
| AV1 · MKV (Opus audio) | 130.3 s | 6.9 % |
| For comparison: audio only, Opus (previous measurement) | 95.0 s | 6.9 % |
| For comparison: audio only, M4A (previous measurement) | 110.0 s | 6.5 % |

## Takeaways

- **AV1 is half the size at the same quality:** 49% smaller at 720p, 57% smaller at 1080p. VMAF is equal,
  even slightly higher (87.6 / 87.2 and 92.8 / 92.4). It also downloads faster.
- **Above 1080p only with AV1:** YouTube serves H.264 only up to 1080p, so the "H.264 · MP4" option
  downloads 1080p even when 1440p or 2160p is requested. AV1 gives real 1440p (VMAF 95.7) and 2160p
  (96.5); even 2160p AV1 is only 50% larger than 1080p H.264.
- **The one downside: QuickTime.** QuickTime does not open AV1 · MKV files. VLC and Chrome do
  (both installed on this Mac); the M4 chip decodes AV1 in hardware, so playback should be smooth.
  If you need QuickTime, use `--h264` / "H.264 · MP4 (QuickTime)" in the UI.
- **Fit for transcripts:** transcribing directly from the video file works fine. Accuracy is identical to
  the audio-only download of the same audio (AV1 → Opus audio → 6.9%; MP4 → AAC audio → 6.5%). Whisper
  takes about 10–35 s longer on the video file: ffmpeg extracts the audio from the large file
  (the times are not from the same measurement run, so part of this may be fluctuation).
  - If you only need the text, **Ses › Opus** (Audio › Opus) or **Transkript** (Transcript) mode is
    faster and much smaller (12 MB / 41–96 MB).
  - If you need both video and text, video + "Transkript de çıkar" (also make a transcript) is enough;
    no separate audio download needed.
- **Language:** in this measurement "tr" was passed to Whisper as the language: since YouTube gave no
  language information, ytd's new fallback found it from the Turkish letters in the title.
