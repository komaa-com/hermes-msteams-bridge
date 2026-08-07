"""Durable background jobs — a promised delivery survives a restart (task #1).

``hermes_agent_task`` tells the caller "I'll send you the result". Before this
module, that promise lived in an ``asyncio.Task``: a serve restart erased it
silently. Now every job is a small JSON file under the Hermes home, written
BEFORE the work starts and deleted only after delivery. On serve startup,
:func:`resume_pending_jobs` re-runs whatever a previous process left behind
(same filesystem-store pattern as the pending-outbound registry).

Scope: resumed jobs deliver to their Teams chat thread (the path the live flow
prefers too). Jobs without a thread target cannot be delivered by a fresh
process and are dropped with a log line. Jobs older than the TTL are dropped
rather than surprising someone with a two-hour-late answer.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

logger = logging.getLogger(__name__)

_JOB_TTL_S = 2 * 60 * 60  # don't resurrect ancient promises
_RESUME_MAX = 5  # startup backlog cap; older beyond this are dropped loudly


def _jobs_dir():
    try:
        from .hermes_api import hermes_home

        d = hermes_home() / "cache" / "teams_call" / "jobs"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:  # noqa: BLE001 — no Hermes home: durability unavailable
        return None


def job_create(query: str, thread_id: str, session_key: str = "") -> str | None:
    """Persist a job before it starts; returns the job id (None = no store)."""
    d = _jobs_dir()
    if d is None:
        return None
    job_id = uuid.uuid4().hex
    try:
        tmp = d / f".{job_id}.tmp"
        tmp.write_text(
            json.dumps(
                {"query": query, "thread_id": thread_id, "session_key": session_key,
                 "created": time.time()}
            ),
            encoding="utf-8",
        )
        tmp.rename(d / f"{job_id}.json")  # atomic publish
        return job_id
    except OSError:
        logger.warning("[teams_call] job persist failed; running non-durable", exc_info=True)
        return None


def job_begin(job_id: str | None) -> None:
    """Mark a live job as running (``.json`` -> ``.claimed``): a crash mid-run
    is recovered by the resume path's stale-claim logic instead of also being
    re-run immediately by the next startup (double-delivery guard)."""
    if not job_id:
        return
    d = _jobs_dir()
    if d is None:
        return
    try:
        f = d / f"{job_id}.json"
        if f.exists():
            f.rename(d / f"{job_id}.claimed")
    except OSError:
        pass


def job_complete(job_id: str | None) -> None:
    """Delivery happened — retire the durable record (either phase)."""
    if not job_id:
        return
    d = _jobs_dir()
    if d is None:
        return
    for suffix in (".json", ".claimed"):
        try:
            (d / f"{job_id}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass


_CLAIM_STALE_S = 600.0  # a .claimed older than this = the claimer crashed


def claim_pending_jobs() -> list[dict]:
    """Two-phase claim: rename ``.json`` -> ``.claimed`` (crash-safe), return
    entries WITH their claim path. The record is deleted only after delivery
    (:func:`finish_job`) and restored on failure — a crash mid-resume or a
    failed Teams send never loses the promise. Beyond the per-startup cap,
    files are LEFT IN PLACE for the next cycle, never discarded."""
    d = _jobs_dir()
    if d is None:
        return []
    now = time.time()
    # Recover claims orphaned by a crashed previous resume.
    for f in d.glob("*.claimed"):
        try:
            if now - f.stat().st_mtime > _CLAIM_STALE_S:
                f.rename(f.with_suffix(".json"))
        except OSError:
            pass
    claimed: list[dict] = []
    for f in sorted(d.glob("*.json")):
        if len(claimed) >= _RESUME_MAX:
            logger.info("[teams_call] resume cap reached; remaining jobs stay queued for the next cycle")
            break
        try:
            entry = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            try:
                f.unlink(missing_ok=True)  # corrupt record: unrecoverable
            except OSError:
                pass
            continue
        age = now - float(entry.get("created") or 0)
        if age > _JOB_TTL_S:
            logger.info("[teams_call] dropping stale background job (%.0f min old)", age / 60)
            f.unlink(missing_ok=True)
            continue
        if not entry.get("thread_id"):
            logger.info("[teams_call] dropping resumed job with no delivery target")
            f.unlink(missing_ok=True)
            continue
        claim = f.with_suffix(".claimed")
        try:
            f.rename(claim)
        except OSError:
            continue  # another process won the claim
        entry["_claim_path"] = str(claim)
        claimed.append(entry)
    return claimed


def finish_job(entry: dict, delivered: bool) -> None:
    """Retire a claimed record on success; restore it for retry on failure."""
    import pathlib

    claim = pathlib.Path(str(entry.get("_claim_path") or ""))
    if not claim.name:
        return
    try:
        if delivered:
            claim.unlink(missing_ok=True)
        elif claim.exists():
            claim.rename(claim.with_suffix(".json"))
    except OSError:
        pass


async def resume_pending_jobs() -> int:
    """Re-run interrupted jobs and deliver to their chat threads; returns count.

    Called once at serve startup. Sequential on purpose: a restart storm must
    not fan out N concurrent agents.
    """
    from .agent_consult import AgentConsult
    from .hermes_api import send_teams_message

    delivered = 0
    for job in claim_pending_jobs():
        query = str(job.get("query") or "")
        thread_id = str(job.get("thread_id") or "")
        session_key = str(job.get("session_key") or "")
        logger.info("[teams_call] resuming interrupted background job: %.60s", query)
        consult = AgentConsult(session_id=session_key or None)
        try:
            result = await consult.ask(query, timeout_s=300.0)
        except Exception:  # noqa: BLE001
            logger.error("[teams_call] resumed job failed", exc_info=True)
            result = "I couldn't complete that task after a restart."
        outcome = await send_teams_message(
            thread_id,
            f"✅ (finishing up after a restart) {result}",
        )
        ok = bool(outcome.get("success"))
        finish_job(job, delivered=ok)  # restore-for-retry on failure
        if ok:
            delivered += 1
        else:
            logger.warning(
                "[teams_call] resumed job delivery failed (kept for retry): %s",
                outcome.get("error"),
            )
    return delivered
