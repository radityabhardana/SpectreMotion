# SpectreMotion

Aplikasi otomatisasi untuk memotong video horizontal panjang menjadi klip vertikal 9:16 (siap untuk TikTok, Instagram Reels, dan YouTube Shorts). Seluruh pipeline berjalan secara mandiri (self-hosted) dengan transkripsi kata demi kata (Whisper), analisis momen viral (LLM), pelacakan wajah (OpenCV), subtitle karaoke dinamis, dan banner hook pembuka.

<!-- [TEMPAT GAMBAR: BANNER / HERO PREVIEW] -->
<!-- Panduan: Letakkan tangkapan layar antarmuka utama di: docs/assets/hero-preview.png -->
<!-- Contoh: Tampilan web saat pengguna memasukkan link video YouTube atau preview hasil render -->

<img width="1535" height="897" alt="image" src="https://github.com/user-attachments/assets/7f33b9ab-dddf-404a-9716-97a865fb7aea" />


---

## Daftar Isi
- [Gambaran Umum](#gambaran-umum)
- [Fitur Utama](#fitur-utama)
- [Alur Kerja Pipeline](#alur-kerja-pipeline)
- [Persyaratan Sistem](#persyaratan-sistem)
- [Instalasi dan Menjalankan](#instalasi-dan-menjalankan)
  - [Persiapan Lingkungan](#persiapan-lingkungan)
  - [Menjalankan Web Dashboard](#menjalankan-web-dashboard)
  - [Menjalankan Bot Telegram](#menjalankan-bot-telegram)
  - [Menjalankan Worker Terpisah](#menjalankan-worker-terpisah)
- [Editor Klip dan Pratinjau Interaktif](#editor-klip-dan-pratinjau-interaktif)
- [Hook Studio](#hook-studio)
- [Konfigurasi Environment (.env)](#konfigurasi-environment-env)
- [Sistem Campaign](#sistem-campaign)
- [Deployment](#deployment)
- [Pengujian](#pengujian)
- [Struktur Repositori](#struktur-repositori)

---

## Gambaran Umum

SpectreMotion dirancang sebagai alternatif self-hosted untuk layanan seperti 2short.ai atau Opus Clip. Aplikasi memproses input video dari URL YouTube atau berkas lokal, mengekstrak audio, mendeteksi topik dan momen berpotensi viral menggunakan LLM, lalu merender video 9:16 lengkap dengan subtitle karaoke dan banner judul pancingan (hook).

---

## Fitur Utama

- **Transkripsi Kata demi Kata (Whisper)**: Ekstraksi ucapan dengan timestamp presisi tinggi per kata untuk kebutuhan subtitle animasi karaoke. Dilengkapi sistem caching agar video yang sama tidak perlu ditranskripsi berulang kali.
- **Deteksi Momen Viral (LLM)**: Menilai dan memilih bagian video terbaik berdasarkan 5 indikator virality (hook strength, emotional intensity, shareability, clarity, payoff). Menyertakan pembuatan caption dan hashtag siap pakai.
- **Live Phone Preview**: Tampilan kanvas 9:16 interaktif untuk mengecek posisi video mentah, animasi subtitle karaoke, dan banner hook secara real-time sebelum melakukan render final.
- **Hook Studio & Overlay Pembuka**: Pengaturan banner judul hook di 3 detik pertama video dengan dukungan kotak bubble, outline, dan highlight kata khusus.
- **Face Tracking 9:16**: Pelacakan posisi wajah pembicara menggunakan OpenCV agar subjek selalu berada di area tengah layar pada video rasio vertikal.
- **Preset Subtitle Siap Pakai**: Mendukung berbagai gaya populer (Hormozi, Super Simple, Simple Boxy, They Are Spoken, Subtitles, dll) serta kustomisasi font, ukuran, warna sorotan, dan posisi drag-and-drop.
- **Manajemen Sesi dan Riwayat**: Dashboard pencarian, pemutaran klip, pengunduhan file MP4, serta fitur hapus sesi massal (batch delete) untuk menghemat ruang disk.
- **Antarmuka Ganda**: Tersedia dashboard web modern dan integrasi bot Telegram untuk memproses video langsung lewat aplikasi pesan.

---

## Alur Kerja Pipeline

```
Sumber Video (YouTube / Upload Lokal)
        │
        ▼ (app/ingest/)
Pemeriksaan Metadata & Download Video
        │
        ▼ (app/media/transcriber.py)
Transkripsi Whisper (Word Timestamps) ──► Cache Transkrip (.json)
        │
        ▼ (app/media/analyzer.py)
Analisis AI & Scoring Momen Viral ◄── Aturan Campaign (Opsional)
        │
        ├─────────────────────────────────────────┐
        ▼ (Mode Kurasi / Edit)                    ▼ (Mode Render)
Katalog Klip & Live Editor                   Worker Pipeline
  - Pratinjau HP 9:16 (Subtitle & Hook)        - Face Tracking (OpenCV)
  - Kustomisasi font, warna, posisi            - Reframe & Dynamic Zoom (FFmpeg)
  - Edit caption & copywriting                 - Burn Subtitle (ASS) & Hook (Pillow)
        │                                         │
        └─────────────────► File Final (.mp4) ◄───┘
```

---

## Persyaratan Sistem

- **Sistem Operasi**: Windows 10/11, Linux (Ubuntu/Debian), atau macOS.
- **Python**: Versi 3.10 atau lebih baru.
- **FFmpeg & FFprobe**: Wajib terpasang dan dapat diakses dari terminal (PATH).
  - Windows: `winget install --id Gyan.FFmpeg -e`
  - Linux: `sudo apt update && sudo apt install -y ffmpeg`
  - macOS: `brew install ffmpeg`
- **Penyedia LLM**: Server lokal atau remote yang kompatibel dengan format OpenAI API (Ollama, vLLM, 9Router, atau API komersial).

---

## Instalasi dan Menjalankan

### Persiapan Lingkungan

```bash
# 1. Masuk ke direktori repositori
cd SpectreMotion

# 2. Buat virtual environment
python -m venv .venv

# 3. Aktifkan virtual environment
# Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

# 4. Pasang paket dependensi
pip install --upgrade pip
pip install -r requirements.txt

# 5. Siapkan berkas konfigurasi
cp .env.example .env
```

Sesuaikan nilai di dalam file `.env`, terutama alamat `LLM_BASE_URL` dan `LLM_MODEL`.

### Menjalankan Web Dashboard

**Windows (Instan):**
Cukup klik dua kali file `JALANKAN_WEB.bat`. Skrip ini akan memeriksa dependensi, memastikan port bersih, dan membuka browser secara otomatis.

**Terminal / CLI:**
```bash
uvicorn app.web:app --host 127.0.0.1 --port 8080 --reload
```
Buka peramban di: `http://127.0.0.1:8080`

<!-- [TEMPAT GAMBAR: WEB GENERATOR] -->
<!-- Panduan: Letakkan tangkapan layar form input video / halaman depan di: docs/assets/generator-page.png -->
<img width="1535" height="894" alt="image" src="https://github.com/user-attachments/assets/ef2de5c0-37bd-459c-b9ae-b691c0c28b0d" />

### Menjalankan Bot Telegram

Jika ingin menggunakan bot Telegram untuk memproses video:
```bash
# Windows (Instan): Klik JALANKAN_BOT.bat
# Atau via terminal:
python -m app.bot
```
Pastikan `TELEGRAM_BOT_TOKEN` telah diisi pada berkas `.env`.

### Menjalankan Worker Terpisah

Jika Anda menjalankan aplikasi di VPS dan ingin memisahkan proses web server dari worker rendering:
```bash
# Memproses antrean secara berkelanjutan:
python -m app.worker

# Memproses satu pekerjaan lalu berhenti:
python -m app.worker --once
```

---

## Editor Klip dan Pratinjau Interaktif

Ketika video selesai dianalisis, katalog klip akan menampilkan daftar potongan video terbaik. Klik **Atur & render** pada salah satu klip untuk membuka editor gaya:

<!-- [TEMPAT GAMBAR: EDITOR MODAL & PHONE PREVIEW] -->
<!-- Panduan: Letakkan tangkapan layar modal editor klip (frame HP dengan teks hook dan subtitle) di: docs/assets/editor-preview.png -->
<img width="1534" height="896" alt="image" src="https://github.com/user-attachments/assets/ab9f28d9-ec07-4d0a-8ec8-f8d6ef4394be" />
<img width="1535" height="885" alt="image" src="https://github.com/user-attachments/assets/f770820e-2aba-488e-a2b2-f32043403a32" />


1. **Kanvas HP Real-Time**: Memutar video klip 480p instan tanpa proses render ulang.
2. **Pratinjau Hook**: Menampilkan banner judul pancingan di bagian atas frame (otomatis tampil pada 0s-3s pemutaran). Anda dapat mengedit teks hook langsung dari form editor.
3. **Pratinjau Subtitle**: Menampilkan kata yang sedang aktif dengan highlight warna dan animasi bounce/slide.
4. **Posisi Drag-and-Drop**: Geser subtitle langsung di layar untuk menentukan penempatan atas, tengah, atau bawah.
5. **Kontrol Suara**: Tombol speaker untuk mendengarkan audio saat menyetel teks.
6. **Kirim Render**: Tombol untuk memproses klip final 1080x1920 ke disk.

---

## Hook Studio

Menu **Hook Studio** di navigasi atas digunakan untuk mengatur template global tampilan hook/judul pancingan:

<!-- [TEMPAT GAMBAR: HOOK STUDIO] -->
<!-- Panduan: Letakkan tangkapan layar halaman Hook Studio di: docs/assets/hook-studio.png -->
![Hook Studio](docs/assets/hook-studio.png)

- Mengatur ukuran font, radius sudut bubble, dan ketebalan outline teks.
- Mengatur warna teks utama dan warna highlight (kata yang diapit tanda bintang `*kata*`).
- Pratinjau statis 9:16 untuk melihat perbandingan tata letak sebelum disimpan.
- Template yang disimpan akan menjadi nilai default bagi seluruh klip baru yang diproses.

---

## Konfigurasi Environment (.env)

| Variabel | Default | Deskripsi |
| :--- | :--- | :--- |
| `LLM_BASE_URL` | `http://127.0.0.1:20128/v1` | URL endpoint AI (kompatibel OpenAI format). |
| `LLM_MODEL` | `gemini-2.5-flash` | Model AI untuk analisis dan penilaian momen viral. |
| `LLM_API_KEY` | `sk-dummy` | Kunci API LLM jika diperlukan oleh penyedia. |
| `WHISPER_MODEL` | `small` | Ukuran model Faster-Whisper (`tiny`, `base`, `small`, `medium`, `large-v3`). |
| `WHISPER_COMPUTE_TYPE` | `int8` | Tipe komputasi (`int8`, `float16`, `float32`). |
| `APP_TOKEN` | *(kosong)* | Kunci token jika web dipasang proteksi autentikasi. |
| `MAX_PROPOSED_CLIPS` | `25` | Jumlah maksimal momen yang diusulkan oleh AI per video. |
| `OUTPUT_DIR` | `data/output` | Direktori penyimpanan berkas video MP4 hasil render. |
| `TARGET_WIDTH` / `HEIGHT` | `1080` / `1920` | Resolusi output kanvas vertikal (9:16). |

---

## Sistem Campaign

Fitur Campaign digunakan jika Anda ingin menyaring klip berdasarkan kriteria khusus (misalnya pedoman konten atau aturan sponsor). Berkas aturan disimpan dalam format JSON di folder `data/campaigns/<id>.json`:

```json
{
  "id": "edukasi",
  "name": "Konten Edukasi Bersih",
  "duration_s": { "min": 25, "max": 60 },
  "banned_exact": ["kata_terlarang", "promosi_ilegal"],
  "caption_required": { "all_of": ["#edukasi", "#shorts"] },
  "curation_note": "Fokus pada tips praktis yang aplikatif"
}
```

---

## Deployment

### Docker Compose
```bash
cp .env.example .env
docker compose up -d --build
```
Container akan menjalankan instance web server dan background worker secara terpisah.

### Linux VPS (systemd)
Contoh unit service systemd tersedia di direktori `deploy/`:
```bash
sudo cp deploy/spectremotion-web.service /etc/systemd/system/
sudo cp deploy/spectremotion-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spectremotion-web spectremotion-worker
```

---

## Pengujian

Proyek ini memiliki rangkaian unit test dan integration test mandiri tanpa ketergantungan koneksi jaringan luar:

```bash
# Menjalankan seluruh test suite:
pytest -q -p no:warnings
```

---

## Struktur Repositori

```
SpectreMotion/
├── app/
│   ├── adapters/          # Implementasi antrean SQLite, LLM, dan storage
│   ├── db/                # Skrip migrasi skema database SQLite
│   ├── ingest/            # Downloader YouTube (yt-dlp) dan parser file lokal
│   ├── media/             # Modul Whisper, Analyzer LLM, Overlay, dan Renderer FFmpeg
│   ├── web/               # Controller FastAPI, template Jinja2, dan static CSS/JS
│   ├── bot.py             # Layanan bot Telegram
│   ├── config.py          # Parser konfigurasi .env
│   ├── hookstudio.py      # Endpoint dan logika preview Hook Studio
│   ├── pipeline.py        # Orkestrasi alur pemrosesan dan render klip
│   ├── presets.py         # Preset gaya subtitle dan loader template
│   ├── storage.py         # Lapisan akses database SQLite
│   └── worker.py          # Background worker pemroses antrean render
├── data/                  # Direktori database lokal, video upload, dan hasil render
├── deploy/                # Berkas konfigurasi deployment systemd dan reverse proxy
├── docs/                  # Dokumentasi pendukung dan aset gambar
│   └── assets/            # Folder peletakan screenshot dan visual dokumentasi
├── templates/             # Template konfigurasi hook default (JSON)
├── tests/                 # Rangkaian automated test suite
├── JALANKAN_WEB.bat       # Launcher instan web untuk Windows
├── JALANKAN_BOT.bat       # Launcher instan bot Telegram untuk Windows
└── requirements.txt       # Daftar pustaka Python yang dibutuhkan
```
