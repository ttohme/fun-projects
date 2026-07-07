"""
apps/orchestrator/gpu_worker.py
Processes 'awaiting_gpu' jobs when the Windows GPU box is reachable.

Owns the awaiting_gpu status (the normal executor never sees it). Three kinds:
    audio     — transcribe via an OpenAI-compatible Whisper server on the GPU
                box (e.g. speaches / faster-whisper-server), then feed the
                transcript through normal triage
    image     — describe/OCR via an Ollama vision model (default llava:7b),
                falling back to local tesseract when the box is off, then triage
    summarize — long-text summarization via LiteLLM (default local-private);
                result stored on the job for the weekly digest

Degradation: if the needed endpoint is unreachable the job simply stays
awaiting_gpu for the next run — nothing is lost. With WAKE_ON_QUEUE=1 a
pending queue triggers one Wake-on-LAN packet per run (scripts/wake-gpu.sh).

CLI:
    python gpu_worker.py run [--limit N]
    python gpu_worker.py digest [--days 7] [--dry-run]
    python gpu_worker.py enqueue-summary --subject "..." [--text "..."] (or stdin)

Environment variables:
    WHISPER_URL     e.g. http://windows-pc.tailnet.ts.net:8000  (audio)
    OLLAMA_BASE_URL e.g. http://windows-pc.tailnet.ts.net:11434 (image)
    VISION_MODEL    default llava:7b
    DIGEST_MODEL    default local-private (via LiteLLM)
    WAKE_ON_QUEUE   "1" to send WoL when jobs are waiting (default off)
    DB_PATH         (default: db/assistant.db)
"""
import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import requests

from db import open_db, update_job_status
from notifier import notify

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("DB_PATH", str(REPO_ROOT / "db" / "assistant.db")))
PROCESSED_DIR = REPO_ROOT / "sync" / "processed"

WHISPER_URL = os.environ.get("WHISPER_URL", "")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "")
VISION_MODEL = os.environ.get("VISION_MODEL", "llava:7b")
DIGEST_MODEL = os.environ.get("DIGEST_MODEL", "local-private")
WAKE_ON_QUEUE = os.environ.get("WAKE_ON_QUEUE", "0") == "1"

VISION_PROMPT = (
    "Extract all text and actionable information from this image "
    "(receipt, whiteboard, screenshot, or handwritten note). "
    "Return plain text only."
)


def get_awaiting_jobs(conn, limit: int = 10) -> list:
    return conn.execute(
        "SELECT * FROM jobs WHERE status = 'awaiting_gpu' ORDER BY created_at LIMIT ?",
        (limit,),
    ).fetchall()


# ── Extractors ────────────────────────────────────────────────────────────────

def transcribe_audio(path: Path) -> str:
    """OpenAI-compatible /v1/audio/transcriptions on the GPU box."""
    if not WHISPER_URL:
        raise ConnectionError("WHISPER_URL is not configured")
    with path.open("rb") as fh:
        resp = requests.post(
            f"{WHISPER_URL.rstrip('/')}/v1/audio/transcriptions",
            files={"file": (path.name, fh)},
            data={"model": "whisper-1"},
            timeout=300,
        )
    resp.raise_for_status()
    return resp.json().get("text", "").strip()


def describe_image(path: Path) -> str:
    """Ollama vision model on the GPU box; tesseract CPU fallback on the Pi."""
    if OLLAMA_BASE_URL:
        try:
            b64 = base64.b64encode(path.read_bytes()).decode()
            resp = requests.post(
                f"{OLLAMA_BASE_URL.rstrip('/')}/api/chat",
                json={
                    "model": VISION_MODEL,
                    "messages": [{"role": "user", "content": VISION_PROMPT,
                                  "images": [b64]}],
                    "stream": False,
                },
                timeout=300,
            )
            resp.raise_for_status()
            text = resp.json().get("message", {}).get("content", "").strip()
            if text:
                return text
        except requests.RequestException as exc:
            print(f"gpu_worker: vision unavailable ({exc}), trying tesseract",
                  file=sys.stderr)

    # CPU fallback: plain OCR loses layout understanding but works offline.
    try:
        out = subprocess.run(
            ["tesseract", str(path), "stdout"],
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    raise ConnectionError("No vision endpoint reachable and tesseract unavailable")


def summarize_text(text: str, subject: str = "") -> str:
    """One LiteLLM call (local model by default) to summarize long content."""
    from triage import call_litellm
    prompt = (
        "Summarize the following in 3-5 bullet points, keeping any dates, "
        "amounts, and action items:\n\n"
        + (f"Subject: {subject}\n\n" if subject else "")
        + text[:30_000]
    )
    return call_litellm(prompt, model=DIGEST_MODEL).strip()


# ── Job processing ────────────────────────────────────────────────────────────

def process_media_job(conn, job) -> dict:
    """Extract text from one media job, feed it through triage, close the job."""
    payload = json.loads(job["payload_json"])
    kind = payload.get("media_kind", "")
    source_ref = job["source_ref"]

    if kind == "summarize":
        summary = summarize_text(payload.get("text", ""), payload.get("subject", ""))
        update_job_status(conn, job["id"], "done",
                          result={"summary": summary, "kind": "summarize"})
        return {"job_id": job["id"], "status": "done", "kind": kind}

    media_path = Path(payload.get("media_path", ""))
    if not media_path.is_file():
        update_job_status(conn, job["id"], "error",
                          result={"error": f"media file missing: {media_path}"})
        return {"job_id": job["id"], "status": "error", "error": "media file missing"}

    text = transcribe_audio(media_path) if kind == "audio" else describe_image(media_path)
    if not text:
        update_job_status(conn, job["id"], "error",
                          result={"error": "extraction returned empty text"})
        return {"job_id": job["id"], "status": "error", "error": "empty text"}

    # Feed the extracted text through the normal triage pipeline. dry_run=True
    # gives us the validated, gated task; we insert it on OUR connection so
    # worker and derived job always land in the same database.
    from db import insert_approval, insert_job
    from notifier import notify_approval_needed
    from triage import triage
    derived = triage(
        input_content=text,
        source_type="file",
        source_ref=f"media:{source_ref}",
        dry_run=True,
    )
    derived_job_id = insert_job(
        conn,
        source_type="file",
        source_ref=f"media:{source_ref}",
        payload=derived,
        intent=derived.get("intent"),
        approval_required=derived["approval_required"],
    )
    if derived_job_id is not None:
        derived["_job_id"] = derived_job_id
        if derived["approval_required"]:
            approval_id = insert_approval(
                conn,
                job_id=derived_job_id,
                requested_action=f"Create task: {derived.get('title', '')}",
            )
            notify_approval_needed(derived, approval_id)

    update_job_status(conn, job["id"], "done", result={
        "kind": kind, "text_chars": len(text),
        "derived_job": derived.get("_job_id"),
        "derived_title": derived.get("title"),
    })
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    try:
        media_path.rename(PROCESSED_DIR / media_path.name)
    except OSError:
        pass
    return {"job_id": job["id"], "status": "done", "kind": kind,
            "derived_title": derived.get("title")}


def run_once(conn, *, limit: int = 10) -> list[dict]:
    jobs = get_awaiting_jobs(conn, limit=limit)
    if not jobs:
        return []

    if WAKE_ON_QUEUE:
        try:
            subprocess.run([str(REPO_ROOT / "scripts" / "wake-gpu.sh")],
                           capture_output=True, timeout=30)
        except (subprocess.TimeoutExpired, OSError) as exc:
            # The wake is opportunistic — never let it block job processing.
            print(f"gpu_worker: wake-gpu skipped: {exc}", file=sys.stderr)

    results = []
    for job in jobs:
        try:
            results.append(process_media_job(conn, job))
        except (requests.RequestException, ConnectionError) as exc:
            # GPU box unreachable — job stays awaiting_gpu for the next run.
            print(f"gpu_worker: job {job['id']} deferred: {exc}", file=sys.stderr)
            results.append({"job_id": job["id"], "status": "deferred", "error": str(exc)})
        except Exception as exc:
            update_job_status(conn, job["id"], "error", result={"error": str(exc)})
            results.append({"job_id": job["id"], "status": "error", "error": str(exc)})
    return results


# ── Weekly digest ─────────────────────────────────────────────────────────────

def build_digest(conn, *, days: int = 7) -> str:
    rows = conn.execute(
        """
        SELECT payload_json, result_json FROM jobs
        WHERE  status = 'done'
          AND  json_extract(result_json, '$.kind') = 'summarize'
          AND  updated_at >= datetime('now', ?)
        ORDER  BY updated_at
        """,
        (f"-{days} days",),
    ).fetchall()
    if not rows:
        return ""
    parts = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        result = json.loads(row["result_json"])
        parts.append(f"▸ {payload.get('subject', '(no subject)')}\n{result['summary']}")
    return f"{len(rows)} item(s) this week:\n\n" + "\n\n".join(parts)


def enqueue_summary(conn, *, text: str, subject: str = "") -> int | None:
    from db import _now, insert_job
    # Date in the ref so a recurring subject ("AI Weekly") dedupes within a
    # day but is accepted again next issue.
    job_id = insert_job(
        conn,
        source_type="email",
        source_ref=f"digest:{_now()[:10]}:{subject[:60]}",
        payload={"title": f"[summarize] {subject or text[:40]}",
                 "media_kind": "summarize", "subject": subject, "text": text},
        intent="document_triage",
        approval_required=False,
    )
    if job_id is not None:
        conn.execute("UPDATE jobs SET status='awaiting_gpu' WHERE id=?", (job_id,))
        conn.commit()
    return job_id


def main() -> None:
    parser = argparse.ArgumentParser(description="GPU media worker")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="Process awaiting_gpu jobs once")
    run_p.add_argument("--limit", type=int, default=10)
    digest_p = sub.add_parser("digest", help="Push the weekly summary digest")
    digest_p.add_argument("--days", type=int, default=7)
    digest_p.add_argument("--dry-run", action="store_true")
    enq_p = sub.add_parser("enqueue-summary", help="Queue text for summarization")
    enq_p.add_argument("--subject", default="")
    enq_p.add_argument("--text", default="")
    args = parser.parse_args()

    conn = open_db(DB_PATH)
    try:
        if args.command == "run":
            print(json.dumps(run_once(conn, limit=args.limit), indent=2))
        elif args.command == "digest":
            text = build_digest(conn, days=args.days)
            if not text:
                print("gpu_worker: nothing to digest")
            elif args.dry_run:
                print(text)
            else:
                notify(text, title="Weekly digest", tags="newspaper")
        elif args.command == "enqueue-summary":
            text = args.text or sys.stdin.read()
            job_id = enqueue_summary(conn, text=text, subject=args.subject)
            print(json.dumps({"job_id": job_id}))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
