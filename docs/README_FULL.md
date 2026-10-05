# SpectreMotion

Ubah video panjang jadi klip vertikal **9:16** otomatis — subtitle ter-*burn*, hook overlay, dan sistem **campaign** (aturan/S&K) deterministik.

Dua cara pakai:
- **Bot Telegram** (utama) — kirim link/video di chat, bot kirim balik klipnya. Bisa di-host di **GitHub Actions** (gratis) atau VPS.
- **Web app** — sudah **diarsipkan** di `archive/web/` (tidak aktif). Engine inti tetap dipakai bersama.

---

## Arsitektur

```
   Telegram  ⇄  [app/bot.py]  ──▶  [Engine bersama]
   (long-poll)     │                transcriber · analyzer · renderer · campaign
                   │
                   └──▶ [state SQLite] ──(commit balik)──▶ [git repo]
```

Semua backend berat (queue, storage, LLM, transcriber) ada di belakang *protocol*
(`app/ports.py`) → bisa dipindah SQLite→Postgres / local→S3 / Actions→VPS tanpa
tulis ulang.

### Kenapa bot (bukan web) bisa jalan di GitHub Actions?
Web app butuh **menerima** koneksi masuk (runner Actions tidak punya IP publik → tidak bisa).
Bot Telegram **memanggil keluar** ke Telegram (long-polling) → jalan di runner mana pun.

**Batasnya (penting):** bot di Actions **tidak** nyala 24 jam. Tiap job polling
beberapa menit lalu mati; pesan yang masuk di antara job ditunggu oleh Telegram
(~24 jam) dan diproses di run berikutnya. Jadi ada **jeda hingga ~5 menit**.
Untuk selalu-siaga, jalankan `python -m app.bot` di VPS — kode-nya identik.

---

## Menjalankan bot

```powershell
.\.venv\Scripts\Activate.ps1
$env:TELEGRAM_BOT_TOKEN="123:ABC..."     # atau isi di .env
$env:TELEGRAM_ALLOWED_USERS="123456789"  # id Telegram kamu (@userinfobot)
python -m app.bot                        # polling terus
python -m app.bot --once                 # proses pesan tertunda lalu keluar
python -m app.bot --seconds 270          # batasi window (dipakai di Actions)
```

## Deploy via GitHub Actions

1. Push repo ke GitHub.
2. **Settings → Secrets and variables → Actions**, tambahkan secrets:
   - `TELEGRAM_BOT_TOKEN` (wajib)
   - `TELEGRAM_ALLOWED_USERS` (wajib untuk keamanan — id kamu)
   - `LLM_BASE_URL`, `LLM_API_KEY` (endpoint OpenAI-compatible)
3. Workflow `.github/workflows/telegram-bot.yml` jalan tiap **5 menit** (cron) dan
   manual (`workflow_dispatch`). Tiap run polling ~4,5 menit lalu commit state
   (`data/app.db`) balik ke repo agar bertahan antar-run.
4. Butuh **ffmpeg** — sudah di-install di workflow.

> State (DB) disimpan di git. Video **tidak** di-commit — cuma numpang lewat
> (download → proses → kirim → buang).

---

## Deploy via VPS (selalu nyala)

```bash
screen -S spectre-bot
python -m app.bot
# Ctrl-A lalu D untuk detach
```
Atau pakai systemd (lihat `deploy/`). Set `GIT_PERSIST=0` karena VPS punya disk persisten.

---

## Yang dibutuhkan

- **Python 3.10+**
- **ffmpeg + ffprobe** di `PATH`
  - Windows: `winget install --id Gyan.FFmpeg -e`
  - Linux: `sudo apt install -y ffmpeg`
- (Opsional) endpoint LLM yang OpenAI-compatible — mis. [9Router](https://github.com/) lokal, Ollama, OpenAI, dll.

---

## Setup lokal

```powershell
# 1) Buat virtualenv
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows PowerShell
# source .venv/bin/activate          # Linux/macOS

# 2) Install dependency
python -m pip install --upgrade pip
pip install -r requirements.txt

# 3) Siapkan config
Copy-Item .env.example .env
# lalu edit .env (minimal: LLM_BASE_URL / LLM_MODEL, APP_TOKEN)
```

> **Catatan Whisper:** model dimuat sekali saat worker mulai. Di laptop, `WHISPER_MODEL=small`
> (atau `base`) dengan `WHISPER_COMPUTE_TYPE=int8` sudah cukup. `medium`/`large` butuh RAM besar.

---

## Menjalankan (butuh 2 terminal)

**Terminal 1 — Web app:**
```powershell
uvicorn app.web:app --reload --host 127.0.0.1 --port 8080
```
Buka **http://127.0.0.1:8080**

**Terminal 2 — Worker:**
```powershell
python -m app.worker          # loop terus
python -m app.worker --once   # proses maksimal 1 job lalu keluar
```

Alur pakai: upload video / tempel link YouTube → pilih campaign (opsional) → **Proses** →
buka halaman job untuk lihat progres & katalog → klik **⬇ Download** di klip yang jadi.

---

## Hook Studio

Buka **http://127.0.0.1:8080/hook-studio** — editor terintegrasi:
- Ubah teks hook, ukuran font, warna highlight.
- **Preview 9:16** live (butuh `SUBTITLE_FONT_PATH` yang valid, mis. `C:/Windows/Fonts/arialbd.ttf`).
- **Simpan Template** → langsung dipakai render berikutnya (atomic write, sama file yang dibaca renderer).

---

## Campaign (aturan/S&K)

Letakkan profil di `data/campaigns/<id>.json`:
```json
{
  "id": "brand-x",
  "name": "Brand X S&K",
  "duration_s": { "min": 15, "max": 60 },
  "banned_exact": ["judi", "slot", "pinjol"],
  "caption_required": { "all_of": ["#brandx"] },
  "curation_note": "pilih momen energik & positif"
}
```
- `duration_s` → **fail** kalau di luar batas.
- `banned_exact` → **fail** (cocok per kata utuh).
- `caption_required` → **warn** (bukan veto).
- Flag dari AI → **warn** saja (AI tidak bisa mem-*fail* klip — sesuai prinsip "AI cuma menandai").

Contoh ada di `data/campaigns/example-strict.json`.

---

## Konfigurasi `.env`

Lihat `.env.example` (sudah lengkap dengan komentar). Yang penting:

| Variable | Fungsi |
|---|---|
| `LLM_BASE_URL`, `LLM_MODEL` | endpoint & nama model LLM |
| `APP_TOKEN` | **wajib diisi sebelum expose ke jaringan** (kosong = tanpa auth) |
| `WHISPER_MODEL` | `small` default, turun ke `base` kalau RAM mepet |
| `MAX_CLIPS` | jumlah klip teratas yang dirender (default 3) |
| `SUBTITLE_FONT_PATH` | path font TTF untuk subtitle & preview |
| `TEMPLATE_PATH` | template aktif untuk hook/subtitle |

---

## Test

```powershell
pytest -q
```
Test berjalan **tanpa media & tanpa jaringan** (menguji campaign engine, storage, dan pembentukan ASS subtitle).

---

## Deploy (nanti)

Aplikasi ini sengaja dibuat **hosting-neutral** supaya gampang dipindah:

- **VPS tunggal:** jalankan `uvicorn` (web) + `python -m app.worker` (worker) sebagai dua systemd service, taruh di belakang Caddy/Nginx + HTTPS. Set `APP_TOKEN`.
- **Cloud kecil (Fly/Railway/Render):** deploy web + worker sebagai 2 proses; pakai volume untuk `data/`.
- **GitHub Actions:** cocok **hanya** sebagai worker batch sekali jalan (lihat `.github/workflows/ci.yml`). Actions **tidak bisa** jadi web server — runner tidak menerima koneksi masuk dan dibunuh setelah job selesai.

### Kenapa tidak "full di GitHub Actions"?
Runner Actions: ephemeral (disk hilang tiap job), timeout 6 jam, dan **tanpa IP publik**.
Jadi web app harus tinggal di tempat lain; Actions paling banter jadi *worker* yang dipicu manual.

---

## Struktur proyek

```
app/
  config.py            # pembacaan .env
  errors.py            # exception bertipe -> mapping HTTP
  logging_setup.py     # logging terstruktur + rotating + correlation id
  ports.py             # Protocol: Queue/Storage/Event/LLM/Transcriber
  storage.py           # akses DB (delegasi schema ke migrations)
  campaign.py          # mesin aturan deterministik (info/pass/warn/fail)
  campaign_compiler.py # teks S&K -> JSON via LLM
  audit.py             # audit log JSONL (append-only)
  auth.py              # users (scrypt) + sessions + rate limit + ownership
  presets.py           # 5 preset hook + named template (anti path-traversal)
  pipeline.py          # orkestrasi: transcribe -> analyze -> render
  worker.py            # loop worker (klaim job, lease, requeue expired)
  web.py               # FastAPI (routes + auth + SSE)
  hookstudio.py        # editor hook terintegrasi
  adapters/            # driver konkret (sqlite queue, local storage, dll)
  db/migrate.py        # migration runner (schema_version + checksum)
  db/migrations/       # file .sql (imutabel, additive)
  ingest/              # youtube.py, gdrive.py, urls.py
  media/               # transcriber, analyzer, renderer, overlay
  web/templates/       # index, job, hook_studio, login
templates/default_hook.json
data/campaigns/*.json
tests/                 # unit + integration + hermetic e2e
deploy/                # systemd units + Caddyfile
docs/IMPLEMENTATION_PLAN.md
Dockerfile · docker-compose.yml · .github/workflows/ci.yml
```

---

## Deploy

Semua backend berat (queue, storage, DB, LLM) ada di belakang *protocol* (`app/ports.py`),
jadi migrasi nanti (SQLite→Postgres, local→S3) = ganti config, bukan tulis ulang.

### Docker Compose (termudah)

```bash
cp .env.example .env      # isi APP_TOKEN + LLM_BASE_URL/LLM_MODEL
docker compose up -d --build
# web: http://localhost:8080  worker berjalan paralel
```

- `web` menjalankan FastAPI, `worker` memproses job (satu job berat sekaligus).
- Opsi TLS: `docker compose --profile proxy up -d` (lihat `deploy/Caddyfile`).
- `data/` dan `templates/` di-mount sebagai volume → template edit & hasil tidak hilang.

### systemd (VPS tanpa Docker)

```bash
sudo cp deploy/spectremotion-web.service deploy/spectremotion-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spectremotion-web spectremotion-worker
sudo journalctl -u spectremotion-worker -f
```
Unit worker sudah di-set `Nice=19`, `IOSchedulingClass=idle`, `MemoryMax=3500M`.

### Untuk GitHub Actions
Actions **tidak bisa** jadi web server (runner ephemeral, tanpa IP publik, mati
setelah job). Yang benar: Actions dipakai untuk **CI** (test + build docker) dan
opsional sebagai **worker batch sekali jalan** (`workflow_dispatch` → `python -m app.worker --once`).
Web app tetap butuh VPS/Cloud.

### Sebelum expose ke internet
- Isi `APP_TOKEN` ATAU buat akun (halaman login punya form "Buat akun pertama").
- Setelah ada user, registrasi tertutup otomatis.
- Jangan commit `.env` / `cookies.txt` (sudah ada di `.gitignore`).
- Taruh di belakang Caddy/Nginx + HTTPS; app sudah kirim header keamanan dasar.
```

