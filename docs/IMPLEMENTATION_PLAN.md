# SpectreMotion — Implementation Plan

> Tujuan: menyempurnakan versi web dari sistem klip video (setara/lebih baik dari repo referensi
> OriontClipper/ContentClipper), dengan alur **full melalui halaman website**, siap jalan di laptop
> dulu lalu naik ke VPS. Dokumen ini adalah peta kerja resmi sebelum penulisan kode lanjutan.

Status saat ini: **MVP sudah jalan & terverifikasi** (upload→transcribe→analyze→render subtitle,
SQLite job store, worker, Hook Studio dasar, campaign deterministik dasar). Lihat `README.md`.

---

## 1. Prinsip kerja

1. **Ports & adapters** untuk tiap backend yang mungkin ganti (queue, storage, events, LLM,
   transcriber, DB). Caller hanya tahu *protocol*, bukan driver → migrasi (SQLite→Postgres,
   local→S3, in-proc→Redis) jadi ganti config, bukan tulis ulang.
2. **Domain intelligence = prioritas.** Plumbing (web/DB/API) sudah ada; yang kurang adalah
   "otak" dari repo contoh: ingest spesialis, kedalaman analyzer, renderer overlay berlapis,
   campaign compiler. Itu fokus utama.
3. **AI cuma menandai, aturan keras yang memutuskan.** Semua keputusan compliance dari engine
   deterministik, bukan LLM.
4. **Hermetic CI.** Test jalan tanpa jaringan/L service eksternal (pakai fake driver).
5. **Satu job berat pada satu waktu** (HEAVY lane = 1), sisanya ringan boleh paralel.

---

## 2. Gap analysis vs repo referensi

### Yang sudah SETARA/lebih (SpectreMotion-only)
- Web UI (FastAPI + Jinja): upload, daftar job, detail job, katalog klip, download.
- SQLite job/clip store + REST API + claim job multi-proses (`BEGIN IMMEDIATE`).
- Dua proses terpisah (web + worker), Hook Studio terintegrasi (bukan server kedua tanpa auth).
- CI GitHub Actions + pytest, config-provider LLM yang netral (bukan hardcoded 9Router).

### Yang PARTIAL / MISSING (harus dibangun) — diurut dampak

| # | Item | Status | Fase |
|---|------|--------|------|
| 1 | Multi-clip UX: pilih klip, generate-all, re-render per klip | MISSING | 3 |
| 2 | YouTube transcript-first + segment download + cookies | MISSING | 5 |
| 3 | Render hook via Pillow berlapis (badge/hook/subtitle) — sekarang drawtext saja | PARTIAL | 2 |
| 4 | Google Drive public-link ingestion | MISSING | 5 |
| 5 | Campaign compiler (teks S&K bebas → JSON via LLM) | MISSING | 4 |
| 6 | Compliance depth: banned_topics, human_gate, strictness, any_of, verdict `info`, evidence, audit trail | PARTIAL | 4 |
| 7 | Transcript cache + confidence filtering + initial_prompt bahasa | MISSING | 2 |
| 8 | Analyzer: 5-pilar rubric, rekonstruksi kalimat, boundary snapping, re-snap whisper | PARTIAL | 2 |
| 9 | Chunking transcript panjang (sekarang TRUNCATE = kehilangan konten) | PARTIAL | 2 |
| 10 | Preset hook (5) + per-user override + switch runtime + preset library | PARTIAL | 3 |
| 11 | Hook editor kaya: layer, drag, undo/redo, galeri style, font discovery | MISSING | 3 |
| 12 | Ops hardening: log rotation, nice/ionice, failed artifacts, atomic output, deploy units | MISSING | 6,7 |
| 13 | Position expression aman (AST) + CSS color/shadow + token `*word*` | MISSING | 2 |
| 14 | Metadata klip kaya: rank, bgm_mood, alasan, campaign_notes, language enforcement | PARTIAL | 2,3 |
| 15 | Font resolution defensif (bundled font, probing Windows, fontsdir libass) | PARTIAL | 2 |

---

## 3. Arsitektur target

```
Browser (Hook Studio / Katalog / Progress)
      │  SSE /events  +  REST /api/*
      ▼
┌─────────────────────────────────────────────────────────────┐
│  WEB/API (FastAPI, app/web.py)                               │
│  routers: auth · jobs · clips · catalog · campaigns · hooks  │
│  SERVICES (app/services/*): ownership & RBAC ada di sini     │
└───┬───────────────┬────────────────┬───────────────┬─────────┘
    │ AuthPort      │ QueuePort      │ StoragePort   │ EventPort/LLMPort
    ▼               ▼                ▼               ▼
 users/sessions  SqliteQueue     LocalStorage    In-proc bus + job_events
 (→JWT/DB)       (→Redis/RQ)     (→S3/R2)        (→Redis pubsub)  + OpenAI/fake
                    │
                    ▼
        ┌───────────────────────────────────────┐
        │ WORKER POOL (app/worker.py × M)        │
        │  HEAVY lane (1): transcribe/render     │
        │  LIGHT lane (K): analyze/probe/verify  │
        │  lease+heartbeat, resume-after-crash,  │
        │  graceful shutdown                     │
        │  STAGES idempotent:                    │
        │  ingest→probe→transcribe→analyze→      │
        │  select→render→verify                  │
        └───────────────────────────────────────┘
```

**Adapter seams (kunci migrasi tanpa rewrite caller):**

```python
class QueuePort(Protocol):
    def enqueue(self, kind, payload, *, dedupe_key=None, priority=5, max_attempts=3) -> str: ...
    def claim(self, lanes, lease_seconds) -> JobLease | None: ...
    def heartbeat(self, lease) -> bool: ...
    def complete(self, lease, result) -> None: ...
    def fail(self, lease, error, retryable) -> None: ...
    def requeue_expired(self, now) -> int: ...   # crash recovery

class StoragePort(Protocol):
    def put(self, key, data, *, content_type) -> StoredBlob: ...
    def open(self, key) -> BinaryIO: ...
    def local_path(self, key) -> str | None: ...  # ffmpeg butuh path nyata
    def url(self, key, *, ttl, download=False) -> str: ...
    def delete(self, key) -> None: ...
    def stat(self, key) -> BlobMeta: ...

class EventPort(Protocol):
    def publish(self, channel, event) -> None: ...
    def subscribe(self, channel) -> Iterator[dict]: ...
```

---

## 4. Data model (evolusi SQLite)

Tabel baru/ubah: `schema_version`, `users`, `sessions`, `rate_limits`, `jobs` (+kolom
state/lane/lease), `job_stages` (checkpoint resume), `job_events` (replay SSE), `clips`
(+rank/status/video_key), `campaigns` (+source_text/profile_json/version),
`campaign_verdicts` (+engine_ver/profile_sha/results_json), `template_overrides`, `audit_log`.

Migrasi: `app/db/migrations/NNNN_*.sql`, dijalankan runner dengan `schema_version` +
checksum SHA256, single-transaction per migrasi, advisory lock saat startup, boot-gate
`MIN_SCHEMA_VERSION`. Baseline `0001_initial` mencerminkan schema MVP sekarang.

---

## 5. Roadmap per fase

> Effort: **S** ≤2 hari · **M** 3–7 hari · **L** >1 minggu.

### Phase 0 — Foundation
- **Goal:** settings terpusat, error bertipe, logging terstruktur, migration runner.
- **Deliverables:** `app/config.py` (rapi + korelasi id), `app/errors.py`, logging JSON +
  rotating file, `app/db/migrate.py` + `0001_initial.sql` + `schema_version`.
- **Depends:** —
- **Effort:** M
- **Verifikasi (demo):** `python -m app.db.migrate status` menampilkan head; boot web+worker
  di DB kosong bikin schema; migrasi rusak gagal atomik & `schema_version` tidak berubah.

### Phase 1 — Adapter seams (tanpa ubah perilaku)
- **Goal:** perkenalkan protocol & pindahkan caller ke sana.
- **Deliverables:** `SqliteQueue` (lease/heartbeat/requeue), `LocalStorage`, EventBus +
  `job_events`, `LLMPort`/`TranscriberPort` dengan driver `fake`.
- **Depends:** Phase 0
- **Effort:** M
- **Verifikasi:** pipeline lama jalan lewat ports; `llm_driver=fake` membuat pipeline hermetik;
  kill worker di tengah job → `requeue_expired` klaim ulang & resume.

### Phase 2 — Pipeline parity (renderer + analyzer depth)
- **Goal:** kualitas output setara/lebih baik dari referensi.
- **Deliverables:** WS-A (overlay Pillow berlapis + ASS hardening), WS-C sebagian
  (5-pilar rubric, rekonstruksi kalimat, boundary snap, re-snap whisper), transcript cache +
  confidence filter + initial_prompt, chunking benar, position expression aman + token `*word*`,
  font resolution defensif.
- **Depends:** Phase 1
- **Effort:** L
- **Verifikasi:** video test tetap → N klip vertikal dengan 4 layer overlay + subtitle burn-in;
  golden-frame test lulus; transcript panjang tidak terpotong.

### Phase 3 — Web UX (SSE, katalog, editor)
- **Goal:** produk terasa nyata, tanpa polling.
- **Deliverables:** WS-F (SSE `/events` + replay `Last-Event-ID`, cancel), katalog grid +
  thumbnail + pilih klip + generate-all, WS-B UI (5 preset, per-user override, preview, sanitasi
  nama anti path-traversal).
- **Depends:** Phase 1–2
- **Effort:** L
- **Verifikasi:** submit job → progress live SSE → katalog muncul → pilih klip → render → preview
  → edit hook → re-render. Test reconnect mid-job + terminal event replay.

### Phase 4 — Campaigns
- **Goal:** compliance deterministik, explainable, auditable.
- **Deliverables:** WS-D (compiler teks→JSON via LLMPort, rule deterministik lengkap, verdict +
  engine_ver + profile_sha, audit trail), CRUD API + UI, surfacing verdict di klip.
- **Depends:** Phase 1 (LLMPort), Phase 3 (UI)
- **Effort:** L
- **Verifikasi:** paste S&K → profile terkompilasi → tempel ke job → verdict per klip + alasan →
  audit trail merekonstruksi *kenapa* klip diblokir.

### Phase 5 — Ingestion (YouTube, Google Drive)
- **Goal:** ingest tanpa upload manual.
- **Deliverables:** WS-E (YouTube transcript-first + segment download + cookies per-user,
  GDrive public-link streaming), typed ingest errors.
- **Depends:** Phase 1, Phase 3
- **Effort:** M–L
- **Verifikasi:** ingest transcript-first URL publik; segment download untuk window terpilih;
  Drive ingest; test mock di CI.

### Phase 6 — Multi-user & hardening
- **Goal:** aman untuk >1 orang.
- **Deliverables:** WS-G (users/sessions — scrypt + opaque session, ownership check → 404,
  rate limit → 429), WS-H (nice/ionice, cancellation ke subprocess, housekeeping), secrets pass.
- **Depends:** Phase 0–5
- **Effort:** L
- **Verifikasi:** dua user tidak bisa lihat job satu sama lain (test enforce); rate limit nyala;
  HEAVY concurrency = 1; cancel kill ffmpeg < T detik.

### Phase 7 — Deploy
- **Goal:** deploy reproducible ke laptop lalu VPS kecil.
- **Deliverables:** `Dockerfile` (multi-stage, non-root), `docker-compose.yml` (web, worker×M,
  caddy, volume) + systemd units alternatif, Caddyfile (TLS, `/api`, `/events` tanpa buffering),
  healthcheck, CI unit+integration+api+smoke, **opsional** GH Actions batch (worker, bukan web).
- **Depends:** semua
- **Effort:** M–L
- **Verifikasi:** `docker compose up` di mesin bersih → smoke penuh di belakang Caddy TLS; CI hijau
  di runner baru; batch GH Actions manual menghasilkan klip sebagai artifact.

### Phase 8 (opsional, saat load nyata) — Scale-out
- **Goal:** ganti driver saat memang perlu, tanpa sentuh caller.
- **Deliverables:** `PgQueue`/`RedisQueue`, `S3Storage`/`R2Storage`, Redis EventBus (semua di
  belakang port yang sama).
- **Verifikasi:** flip `queue_driver`/`storage_driver` → test yang sama lulus di backend baru;
  tidak ada diff di luar `app/adapters/`.

---

## 6. Risks & mitigasi (top 10)

1. **Resource exhaustion (ffmpeg/whisper).** HEAVY lane=1, nice/ionice/affinity, timeout,
   precheck disk, scratch quota + GC.
2. **LLM flaky/timeout.** Timeout keras, retry jitter, circuit breaker, validasi JSON schema +
   1 repair-retry, degrade ke heuristik (jangan gagalkan job karena LLM), driver fake untuk CI.
3. **Prompt injection via transcript.** Perlakukan transcript sebagai *data*, bungkus dalam blok
   berlabel; LLM hanya emit JSON sesuai schema; keputusan compliance SELALU dari engine.
4. **Path traversal (nama template/filename).** Jangan pernah bentuk path dari input user; slug
   allow-list `[a-z0-9-_]`; key server-built + containment check.
5. **Crash recovery job panjang.** Checkpoint `job_stages`, lease expiry + `requeue_expired`,
   stage idempotent, atomic write.
6. **SQLite write contention.** WAL + busy_timeout, transaksi pendek, `BEGIN IMMEDIATE` untuk
   claim; escape hatch Postgres + `SKIP LOCKED` di belakang port.
7. **Secrets.** Dari env/`.env` (gitignored), redacting logger, cookie/token pihak ketiga
   terenkripsi (stdlib hmac + master key), CI inject runtime saja.
8. **SSE correctness (reconnect/proxy).** `job_events` + seq monotonik + `Last-Event-ID` replay,
   heartbeat, Caddy `flush_interval -1` untuk `/events`.
9. **Untrusted ingestion.** Validasi URL/id ketat, sandbox yt-dlp, cap size/duration, quota,
   cookie per-user permission-restricted.
10. **Migration/upgrade web+worker.** Startup migration advisory-locked, boot-gate
    `MIN_SCHEMA_VERSION`, expand/contract, checksum migrasi.

---

## 7. Testing strategy

- **Unit (pure):** campaign (tiap rule + matriks pass/warn/fail + hash stabil), compiler (fake
  LLM + kanonik), ASS/overlay builder (golden string/image), storage key + sanitizer (tolak
  traversal), queue state machine.
- **Integration (hermetik):** generate video test via ffmpeg `testsrc` + audio tone; jalankan
  pipeline dengan `llm_driver=fake`+`transcriber_driver=fake`; assert clip jadi, `job_stages`
  done, verdict/audit ada; crash-recovery (kill worker lalu resume, tanpa artifact duplikat);
  concurrency (HEAVY tak >1).
- **API (TestClient):** auth (register/login/logout, revoke instan, legacy token shim),
  ownership (user B 404 untuk resource A), rate limit (429 + Retry-After), jobs (enqueue→status→
  SSE terminal + replay), campaigns (CRUD + compile + verdict).
- **E2E smoke (hermetik, tanpa LLM eksternal):** satu perintah (`pytest -m smoke`) → upload video
  fixture → tunggu artifact → verifikasi klip subtitle + verdict campaign. Marker `-m live`
  (exclude CI) untuk run LLM asli.
- **CI:** unit → integration → api → smoke, SQLite temp + DB storage, ffmpeg di image, upload
  artifact saat gagal.

---

## 8. Definition of Done

### MVP+ (milestone terkecil yang layak dikirim)
Fondasi (Phase 0 + core Phase 1), **SSE progress**, **preset hook + per-user override**, **verdict
campaign deterministik + audit**, **crash-resume**, **smoke test hermetik**. *Done bila:* user
submit video → progress live → katalog → pilih klip → pilih preset hook → render → lihat verdict
compliance deterministik + audit, **selamat dari restart worker mid-render**, semua di laptop,
`pytest -m smoke` hijau tanpa jaringan.

### Project Done (Phase 0–7)
1. Tiap backend yang mungkin ganti ada di belakang protocol (dibuktikan test driver kedua).
2. Renderer setara referensi (overlay berlapis, 5 preset + override, katalog ~25 klip, ASS burn);
   semua stage idempotent & resumable; HEAVY=1; graceful shutdown terverifikasi.
3. Teks S&K → profile tervalidasi & berversi; verdict pass/warn/block reproducible dari
   `engine_ver`+`profile_sha`; audit merekonstruksi tiap keputusan.
4. YouTube+GDrive jalan end-to-end dengan typed errors + kredensial per-user terenkripsi.
5. Multi-user: users/sessions/rate-limit/ownership di services; cross-tenant → 404; token legacy
   dipensiunkan; secrets tak pernah ter-log/ter-commit.
6. UX: SSE progress dengan reconnect/replay; katalog + editor hook; cancel.
7. Ops: deploy reproducible (Docker/compose &/atau systemd) di belakang Caddy TLS; healthcheck;
   CI hermetik hijau; runbook (migrate, backup, restore, rotate secret).
8. Quality gate: migrasi checksum + boot-gate; golden-frame; resume test; ownership test;
   rate-limit test; semua hijau di CI.
9. Docs: README (setup lokal, deploy) + catatan arsitektur memetakan port→driver.
10. *Done* = clone bersih bisa `docker compose up`, smoke hijau, dan dev kompeten bisa tukar
    SQLite→Postgres & local→S3 hanya dengan edit config + satu file adapter.

---

## 9. Rencana eksekusi bertahap (urutan kerja aktual)

Kita kerjakan berurutan, satu fase = satu batch kerja + verifikasi:

1. **Phase 0** — foundation & migrations.
2. **Phase 1** — adapter seams (queue/storage/events/LLM), driver fake.
3. **Phase 2** — renderer overlay berlapis + analyzer depth + transcript cache.
4. **Phase 3** — SSE + katalog (pilih/generate-all) + preset & editor hook.
5. **Phase 4** — campaign compiler + policy depth + audit.
6. **Phase 5** — ingestion YouTube + GDrive.
7. **Phase 6** — multi-user + hardening.
8. **Phase 7** — Docker/systemd/Caddy + CI.
9. **Phase 8** — (opsional) scale-out driver.

Tiap fase punya kriteria verifikasi sendiri (bagian 5) dan harus **hijau** sebelum lanjut.
