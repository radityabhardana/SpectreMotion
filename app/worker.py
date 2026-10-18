"""Worker loop: claims queued jobs and processes them one at a time.

Run as a separate process from the web app:
    python -m app.worker          # loop forever
    python -m app.worker --once   # process at most one job then exit
"""

from __future__ import annotations

import argparse
import logging
import time

from app import config, storage
from app.adapters import registry
from app.db import migrate
from app.logging_setup import new_correlation_id, setup_logging
from app.ports import LANE_HEAVY
from app.pipeline import process_job

setup_logging(log_dir=config.LOG_DIR, level=getattr(logging, config.LOG_LEVEL, logging.INFO))
log = logging.getLogger("spectre.worker")

WORKER_ID = None


def run(once: bool = False) -> None:
    global WORKER_ID
    import socket
    import uuid
    config.ensure_dirs()
    migrate.ensure_schema()
    storage.init_db()
    WORKER_ID = f"{socket.gethostname()}:{uuid.uuid4().hex[:6]}"

    queue = registry.get_queue()
    log.info("Worker %s started (whisper=%s, poll=%.1fs, once=%s)",
             WORKER_ID, config.WHISPER_MODEL, config.WORKER_POLL_INTERVAL_S, once)

    while True:
        # Recover jobs whose lease expired (crashed workers).
        queue.requeue_expired(time.time())

        lease = queue.claim([LANE_HEAVY], WORKER_ID, config.JOB_LEASE_SECONDS)
        if lease is None:
            if once:
                log.info("No queued jobs; exiting (--once)")
                return
            time.sleep(config.WORKER_POLL_INTERVAL_S)
            continue

        new_correlation_id("job")
        job = storage.get_job(lease.job_id)
        if job is None:
            queue.fail(lease, "Job hilang setelah diklaim")
            continue

        if job.status == storage.JOB_CANCELED:
            log.info("Job %s was canceled before work started", job.id[:8])
            continue

        from app import cancel as cancel_mod
        cancel_mod.new_token(job.id)

        log.info("Claimed job %s (%s)", job.id[:8], job.source_name or job.source_url)
        queue.heartbeat(lease, config.JOB_LEASE_SECONDS)
        try:
            kind = (job.settings or {}).get("kind", "process_video")
            if kind == "render_clip":
                from app.pipeline import render_clip_job
                render_clip_job(job)
            else:
                process_job(job)
            refreshed = storage.get_job(job.id)
            if refreshed and refreshed.status == storage.JOB_CANCELED:
                log.info("Job %s was canceled by user; not completing queue lease", job.id[:8])
            elif refreshed and refreshed.status == storage.JOB_FAILED:
                queue.fail(lease, refreshed.error or "pipeline failed", retryable=False)
            else:
                queue.complete(lease, {"status": "done"})
        except cancel_mod.JobCancelled:
            log.info("Job %s canceled mid-flight", job.id[:8])
        except Exception as exc:  # noqa: BLE001
            log.exception("Unhandled worker error for job %s", job.id[:8])
            queue.fail(lease, str(exc), retryable=True)
        finally:
            cancel_mod.clear(job.id)

        if once:
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="SpectreMotion worker")
    parser.add_argument("--once", action="store_true", help="Process one job then exit")
    args = parser.parse_args()
    run(once=args.once)


if __name__ == "__main__":
    main()
