<div align="center">

<img src="docs/assets/under-construction.svg" alt="SpectreMotion — Under Construction" width="100%" />

# 🚧 SpectreMotion

**Next-Gen Local AI Video Short & Clip Repurposing Engine**

Ubah video panjang (YouTube, Google Drive, atau Upload Lokal) menjadi klip vertikal **9:16** secara otomatis — didukung transkripsi kata demi kata (*word-level Whisper*), deteksi momen viral (*LLM scoring*), *face-tracking*, dan subtitle animasi (*karaoke highlight*).

![status](https://img.shields.io/badge/status-under%20construction-f5b301?style=for-the-badge&labelColor=0d1117)
![python](https://img.shields.io/badge/python-3.10%2B-blue?style=for-the-badge&logo=python&logoColor=white)
![ffmpeg](https://img.shields.io/badge/ffmpeg-required-green?style=for-the-badge&logo=ffmpeg&logoColor=white)
![fastapi](https://img.shields.io/badge/FastAPI-005571?style=for-the-badge&logo=fastapi)
![tests](https://img.shields.io/badge/tests-144%20passed-brightgreen?style=for-the-badge)

</div>

---

## 📌 Daftar Isi
- [Tentang SpectreMotion](#-tentang-spectremotion)
- [Fitur Utama](#-fitur-utama)
- [Arsitektur & Alur Pipeline](#-arsitektur--alur-pipeline)
- [Persyaratan Sistem](#-persyaratan-sistem)
- [Panduan Instalasi & Menjalankan](#-panduan-instalasi--menjalankan)
  - [1. Menjalankan Web Dashboard (Rekomendasi)](#1-menjalankan-web-dashboard-rekomendasi)
  - [2. Menjalankan Mode Bot Telegram](#2-menjalankan-mode-bot-telegram)
  - [3. Menjalankan Worker di Background](#3-menjalankan-worker-di-background)
- [Real-Time Style Editor (Ala 2short.ai)](#-real-time-style-editor-ala-2shortai)
- [Konfigurasi `.env`](#-konfigurasi-env)
- [Sistem Campaign (Aturan Brand & S&K)](#-sistem-campaign-aturan-brand--sk)
- [Deployment (Docker & VPS)](#-deployment-docker--vps)
- [Pengujian (Testing)](#-pengujian-testing)
- [Struktur Repositori](#-struktur-repositori)

---

## 💡 Tentang SpectreMotion

**SpectreMotion** adalah alternatif lokal (*self-hosted*) untuk layanan SaaS video repurposing seperti **2short.ai** dan **Opus Clip**. 

Aplikasi ini memproses video horizontal panjang menjadi beberapa klip pendek vertikal siap unggah ke **TikTok, Instagram Reels, dan YouTube Shorts**. Semua proses berat—mulai dari ekstraksi audio, deteksi wajah, perataan framing, hingga burning subtitle—dapat berjalan 100% di mesin lokal tanpa biaya langganan API cloud.

---

## ⚡ Fitur Utama

- 📱 **Real-Time Interactive Editor (2short.ai Style)**:
  - Tonton pratinjau klip instan 480p tanpa harus menunggu proses *full render* FFmpeg.
  - Subtitle kata-demi-kata ditampilkan langsung di browser menggunakan layer overlay CSS.
  - Kendali audio penuh: tombol speaker `🔇`/`🔊` untuk menyalakan suara pratinjau.
  - Ganti font, warna sorotan (*highlight*), ukuran teks, dan posisi secara *live*.
- 🧠 **AI Viral Moment Scoring & Analysis**:
  - Skor potensi viralitas otomatis (0–100) berdasarkan transkrip.
  - Pendeteksi kalimat pembuka (*hook detection*) yang memancing rasa penasaran audiens.
  - Auto-generate teks *copywriting* / caption lengkap dengan tagar, siap disalin dengan tombol **Copy**.
- 🎙️ **Transkripsi Suara Presisi (Whisper)**:
  - Deteksi waktu per kata (*word-level timestamps*) untuk sinkronisasi karaoke subtitle yang akurat.
  - Caching transkrip otomatis—video yang sama tidak perlu ditranskripsi ulang saat berganti gaya klip.
- 👤 **Face Tracking & Smart Framing 9:16**:
  - Deteksi wajah dengan OpenCV untuk menjaga pembicara tetap berada di tengah layar (*center framing*).
  - Dilengkapi fitur *Dynamic Zoom* halus untuk meningkatkan retensi penonton.
- 🎨 **Koleksi Preset Subtitle**:
  - **Hormozi**: Teks kuning tebal dengan outline kontras tinggi (2 kata per layar).
  - **CapCut**: Aksen hijau energik (3 kata per layar).
  - **Neon**: Cyan modern bergaya futuristik.
  - **Cinematic**: Warna emas elegan dengan posisi tengah.
  - **Minimal**: Putih bersih tanpa outline tebal untuk kesan editorial.
- 🛡️ **Kualitas Kode Teruji & Terisolasi**:
  - Dilengkapi 144 unit test (*hermetic*) tanpa koneksi luar.
  - Database terisolasi dan aman dari polusi data saat pengujian.

---

## 📐 Arsitektur & Alur Pipeline

```
[Sumber Video] 
(YouTube / Google Drive / Upload Berkas)
        │
        ▼ (app/ingest/)
[Ingestion & Probe Video] 
        │
        ▼ (app/media/transcriber.py)
[Whisper STT (Word-level timestamps)] ──► [Disk Cache (.json)]
        │
        ▼ (app/media/analyzer.py)
[LLM Viral Scoring & Sentence Builder] ◄── [Brand/Campaign Rules]
        │
        ├─────────────────────────────────────────┐
        ▼ (Mode Kurasi Draf)                      ▼ (Mode Auto-Render)
[Katalog Klip Web Dashboard]                 [Background Worker]
  - Real-time client-side preview               - OpenCV Face Tracking (9:16)
  - Interaktif: Font, Warna, Posisi             - FFmpeg Dynamic Zoom & Crop
  - Audio toggle & Copy Caption                 - ASS Karaoke Subtitle Burning
        │                                         │
        └─────────────────► [Final Render MP4] ◄──┘
```

---

## 💻 Persyaratan Sistem

- **Sistem Operasi**: Windows 10/11, Ubuntu/Debian Linux, atau macOS.
- **Python**: Versi `3.10` atau lebih baru.
- **FFmpeg & FFprobe**: Wajib terpasang dan terdaftar di `PATH`.
  - *Windows*: `winget install --id Gyan.FFmpeg -e`
  - *Linux*: `sudo apt update && sudo apt install -y ffmpeg`
- **LLM Endpoint**: Endpoint yang kompatibel dengan format OpenAI API (misal: [9Router](https://github.com/), Ollama lokal, vLLM, atau OpenAI API).

---

## 🚀 Panduan Instalasi & Menjalankan

### Langkah Awal (Setup)

```powershell
# 1. Clone repositori & masuk ke direktori
cd c:\projects\website\SpectreMotion

# 2. Buat virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1       # Windows PowerShell
# source .venv/bin/activate         # Linux / macOS

# 3. Pasang dependensi
pip install --upgrade pip
pip install -r requirements.txt

# 4. Buat berkas konfigurasi .env
cp .env.example .env
```

---

### 1. Menjalankan Web Dashboard (Rekomendasi)

Tersedia skrip instan untuk Windows:
- **Cukup klik 2x berkas `JALANKAN_WEB.bat`**  
  *Skrip ini akan otomatis memeriksa FFmpeg, mematikan proses lama yang bentrok di port 8080, menyalakan web server + worker, lalu membuka browser.*

Atau jalankan via terminal:
```powershell
uvicorn app.web:app --host 127.0.0.1 --port 8080 --reload
```
Akses di browser: **`http://127.0.0.1:8080`**

---

### 2. Menjalankan Mode Bot Telegram

SpectreMotion juga dapat dioperasikan secara penuh lewat chat Telegram:
```powershell
# Klik 2x JALANKAN_BOT.bat atau lewat CLI:
python -m app.bot
```

---

### 3. Menjalankan Worker di Background

Jika web server dan worker dijalankan secara terpisah (misalnya di lingkungan production/VPS):
```powershell
python -m app.worker          # Loop memproses antrean job
python -m app.worker --once   # Proses 1 job lalu keluar
```

---

## 🎛️ Real-Time Style Editor (Ala 2short.ai)

Saat Anda mengklik **"Atur & Render"** atau **"Ubah Gaya"** pada katalog klip:
1. **Live Phone Canvas**: Klip video sumber 480p dimuat instan di dalam bingkai handphone 9:16.
2. **Audio Unmuted**: Klik tombol speaker (`🔇` / `🔊`) untuk mendengarkan audio pembicara sambil menyetel subtitle.
3. **Penyetelan Gaya Teks**:
   - Pilihan Font (*Arial Bold, Impact, Segoe UI Bold, dll.*).
   - Ukuran font dan ketebalan outline (*stroke shadow*).
   - Pilihan warna kata sorotan (*Karaoke highlight*).
   - Pilihan posisi vertikal (*Bawah, Tengah, Atas*).
4. **Copy Caption**: Tombol salin caption di setiap kartu klip memudahkan penempelan langsung ke deskripsi TikTok/Instagram.
5. **Kirim Render**: Klik **"Render klip ini"** untuk memproses hasil akhir menggunakan GPU/CPU via FFmpeg.

---

## ⚙️ Konfigurasi `.env`

Konfigurasi disimpan di berkas `.env`. Variabel utama yang perlu diperhatikan:

| Variabel | Default | Keterangan |
| :--- | :--- | :--- |
| `LLM_BASE_URL` | `http://127.0.0.1:20128/v1` | URL endpoint AI (OpenAI format, misal 9Router/Ollama). |
| `LLM_MODEL` | `gemini-2.5-flash` | Nama model AI yang digunakan untuk analisis momen viral. |
| `WHISPER_MODEL` | `small` | Ukuran model Whisper (`tiny`, `base`, `small`, `medium`, `large-v3`). |
| `WHISPER_COMPUTE_TYPE` | `int8` | Presisi komputasi (`int8`, `float16`, `float32`). |
| `APP_TOKEN` | *(opsional)* | Token proteksi web jika diakses via jaringan publik. |
| `MAX_PROPOSED_CLIPS`| `5` | Jumlah maksimal kandidat klip yang diusulkan oleh AI. |
| `HF_HUB_OFFLINE` | `1` | Paksa Whisper berjalan 100% offline dari cache lokal. |

---

## 📋 Sistem Campaign (Aturan Brand & S&K)

Sistem *Campaign* memungkinkan penyaringan klip secara deterministik berdasarkan aturan sponsor atau pedoman komunitas. Berkas aturan disimpan dalam format JSON di `data/campaigns/<id>.json`:

```json
{
  "id": "bebas",
  "name": "General Clean Content",
  "duration_s": { "min": 20, "max": 60 },
  "banned_exact": ["judi", "slot", "pinjol", "sara"],
  "caption_required": { "all_of": ["#shorts"] },
  "curation_note": "Pilih klip yang memiliki pesan edukatif atau motivasi"
}
```
- **duration_s**: Membatalkan klip secara otomatis jika durasi di luar rentang.
- **banned_exact**: Filter kata-kata terlarang pada transkrip.
- **caption_required**: Rekomendasi tagar wajib.

---

## 🚢 Deployment (Docker & VPS)

### Docker Compose
```bash
cp .env.example .env
docker compose up -d --build
```
*Layanan `web` dan `worker` akan berjalan secara terisolasi di dalam container.*

### Linux VPS (systemd)
Tersedia berkas unit systemd pada direktori `deploy/`:
```bash
sudo cp deploy/spectremotion-web.service /etc/systemd/system/
sudo cp deploy/spectremotion-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spectremotion-web spectremotion-worker
```

---

## 🧪 Pengujian (Testing)

SpectreMotion dilengkapi dengan rangkaian pengujian komprehensif (*hermetic unit & integration tests*):

```powershell
# Jalankan seluruh test suite
.\.venv\Scripts\python.exe -m pytest -q -p no:warnings
```
*Hasil: 144 passed (menguji migrasi database, storage session, endpoint web, parsing caption, kontrol audio, dan pipeline media).*

---

## 📁 Struktur Repositori

```
SpectreMotion/
├── app/
│   ├── adapters/          # Driver konkret (queue_sqlite, llm_openai, storage_local)
│   ├── db/                # Runner migrasi SQL skema database
│   ├── ingest/            # Download YouTube (yt-dlp), Google Drive, verifikasi URL
│   ├── media/             # Transcriber (Whisper), Analyzer (LLM), Renderer (FFmpeg), Face-track
│   ├── web/               # Router FastAPI, endpoint API klip, template Jinja2, static CSS/JS
│   ├── bot.py             # Alternatif antarmuka Bot Telegram
│   ├── config.py          # Parser konfigurasi .env
│   ├── pipeline.py        # Orkestrator alur kerja pembuatan klip
│   ├── presets.py         # Konfigurasi preset subtitle (Hormozi, CapCut, dll)
│   ├── storage.py         # Lapisan akses data SQLite
│   └── worker.py          # Background worker pemroses antrean render
├── data/                  # Lokasi penyimpanan database lokal & hasil render
├── deploy/                # Konfigurasi systemd service dan Caddyfile
├── docs/                  # Dokumentasi arsitektur, gambar aset, dan panduan
├── tests/                 # 144 test suite terisolasi
├── JALANKAN_WEB.bat       # Launcher instan web dashboard untuk Windows
├── JALANKAN_BOT.bat       # Launcher instan bot telegram untuk Windows
└── requirements.txt       # Dependensi library Python
```

---

<div align="center">
  <sub>Dibangun untuk efisiensi pembuatan konten video pendek berbasis AI lokal.</sub>
</div>
