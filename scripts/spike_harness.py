"""ADR-6 spike harness — criteria 5/6/8/9 measured without Teams.

Simulates gateway co-residency: a VoiceBridgeService (echo handler) carries N
synthetic StandIn calls streaming real-cadence 20 ms audio, while an
event-loop-lag probe stands in for the chat plane (criterion 9: would voice
load starve chat?). Criterion 6 benchmarks the resample cost per frame (the
realtime handler's per-frame CPU). Criterion 5 stalls a reader and watches
RSS + the heartbeat reap. Criterion 8 stops the service mid-call and verifies
clean closes with the host loop unharmed.

Run inside the Hermes venv:
    python scripts/spike_harness.py
"""

from __future__ import annotations

import asyncio
import json
import resource
import statistics
import sys
import time

sys.path.insert(0, "src")

from hermes_msteams_bridge import audio  # noqa: E402
from hermes_msteams_bridge.config import BYTES_PER_FRAME, TeamsVoiceConfig  # noqa: E402
from hermes_msteams_bridge.handlers import EchoCallSessionHandler  # noqa: E402
from hermes_msteams_bridge.service import VoiceBridgeService  # noqa: E402
from hermes_msteams_bridge.smoke import SyntheticCall, free_port  # noqa: E402

N_CALLS = 8
LOAD_SECONDS = 15
SECRET = "spike-secret"


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


class LoopLagProbe:
    """The chat-plane stand-in: how late does a 10 ms sleep wake up?"""

    def __init__(self) -> None:
        self.samples_ms: list[float] = []
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(0.010)
            lag = (time.perf_counter() - t0 - 0.010) * 1000
            self.samples_ms.append(max(0.0, lag))

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def stop(self) -> dict:
        if self._task:
            self._task.cancel()
        s = sorted(self.samples_ms)
        if not s:
            return {}
        return {
            "samples": len(s),
            "p50_ms": round(statistics.median(s), 3),
            "p95_ms": round(s[int(len(s) * 0.95)], 3),
            "max_ms": round(s[-1], 3),
        }


def bench_resample(frames: int = 2000) -> dict:
    """Criterion 6: per-frame CPU of the realtime path's resampling."""
    frame = b"\x01\x02" * (BYTES_PER_FRAME // 2)
    t0 = time.perf_counter()
    for _ in range(frames):
        audio.resample_pcm16(frame, 16_000, 24_000)
    per_frame_ms = (time.perf_counter() - t0) / frames * 1000
    try:
        import numpy  # noqa: F401

        backend = "numpy"
    except ImportError:
        backend = "pure-python"
    return {"backend": backend, "per_frame_ms": round(per_frame_ms, 4),
            "budget_ms": 20.0, "frames_per_budget": round(20.0 / per_frame_ms)}


async def run_call(port: int, call_id: str, seconds: int) -> int:
    call = SyntheticCall("127.0.0.1", port, SECRET, call_id)
    await call.connect()
    await call.start_call(caller_name=f"Load {call_id}")
    await call.stream_audio(frames=seconds * 50)  # 50 fps = real cadence
    await call.end_call()
    return call.echo_frames


async def main() -> None:
    report: dict = {"n_calls": N_CALLS, "load_seconds": LOAD_SECONDS}

    # ── criterion 6: CPU per frame ───────────────────────────────────────────
    report["criterion_6_resample"] = bench_resample()

    # ── load run: N calls + loop-lag probe (criteria 9-proxy, 5 baseline) ───
    port = free_port()
    cfg = TeamsVoiceConfig(shared_secret=SECRET, host="127.0.0.1", port=port,
                           allow_all=True, heartbeat_s=5.0)
    service = VoiceBridgeService(cfg, handler_factory=EchoCallSessionHandler)
    await service.start()
    probe = LoopLagProbe()
    probe.start()
    rss_before = rss_mb()
    t0 = time.perf_counter()
    echoes = await asyncio.gather(
        *[run_call(port, f"load-{i}", LOAD_SECONDS) for i in range(N_CALLS)],
        return_exceptions=True,
    )
    elapsed = time.perf_counter() - t0
    report["criterion_9_loop_lag_under_load"] = probe.stop()
    ok_calls = [e for e in echoes if isinstance(e, int)]
    report["load_run"] = {
        "calls_completed": len(ok_calls),
        "echo_frames_total": sum(ok_calls),
        "elapsed_s": round(elapsed, 1),
        "rss_before_mb": round(rss_before, 1),
        "rss_after_mb": round(rss_mb(), 1),
        "errors": [str(e) for e in echoes if not isinstance(e, int)][:3],
    }

    # ── criterion 5: stalled reader + heartbeat reap ─────────────────────────
    stall = SyntheticCall("127.0.0.1", port, SECRET, "stalled-1")
    await stall.connect()
    await stall.start_call(caller_name="Stalled Reader")
    payload_frames = 0
    rss_stall_start = rss_mb()
    reaped = False
    t0 = time.perf_counter()
    try:
        # Send frames but NEVER read: the echo responses back up on our side.
        import base64 as _b64
        import json as _json

        payload = _b64.b64encode(b"\x00" * BYTES_PER_FRAME).decode("ascii")
        while time.perf_counter() - t0 < 12:
            await stall._ws.send_str(_json.dumps({
                "type": "audio.frame", "seq": payload_frames,
                "timestampMs": payload_frames * 20, "payloadBase64": payload,
            }))
            payload_frames += 1
            await asyncio.sleep(0.02)
    except Exception as exc:  # noqa: BLE001 — the reap closes us: that's the pass
        reaped = True
        report["criterion_5_reap_exception"] = type(exc).__name__
    report["criterion_5_backpressure"] = {
        "frames_sent_unread": payload_frames,
        "rss_delta_mb": round(rss_mb() - rss_stall_start, 2),
        "reaped_by_heartbeat": reaped or bool(stall._ws.closed),
    }
    try:
        await stall._session.close()
    except Exception:  # noqa: BLE001
        pass

    # ── criterion 8: stop mid-call, host loop unharmed ───────────────────────
    live = SyntheticCall("127.0.0.1", port, SECRET, "midcall-1")
    await live.connect()
    await live.start_call(caller_name="Mid Call")
    probe2 = LoopLagProbe()
    probe2.start()
    stop_t0 = time.perf_counter()
    await service.stop()  # drain with the call still up
    stop_ms = (time.perf_counter() - stop_t0) * 1000
    closed = False
    try:
        msg = await asyncio.wait_for(live._ws.receive(), timeout=3)
        closed = msg.type.name in ("CLOSE", "CLOSING", "CLOSED")
    except asyncio.TimeoutError:
        pass
    await live._session.close()
    lag2 = probe2.stop()
    report["criterion_8_restart_during_call"] = {
        "service_stop_ms": round(stop_ms, 1),
        "client_saw_close": closed or bool(live._ws.closed),
        "host_loop_lag_during_stop": lag2,
    }

    # ── verdict: explicit thresholds, nonzero exit on failure (round 8 —
    # a harness that always exits 0 is a demo, not evidence) ────────────────
    lag = report.get("criterion_9_loop_lag_under_load") or {}
    checks = {
        "all_calls_completed": report["load_run"]["calls_completed"] == N_CALLS,
        "no_call_errors": not report["load_run"]["errors"],
        "echo_flowing": report["load_run"]["echo_frames_total"] > 0,
        # chat-plane proxy: the loop must stay responsive under full load
        "loop_lag_p50_under_5ms": bool(lag) and lag["p50_ms"] < 5.0,
        "loop_lag_max_under_250ms": bool(lag) and lag["max_ms"] < 250.0,
        # per-frame CPU must fit far inside the 20 ms frame budget
        "resample_under_2ms_per_frame": report["criterion_6_resample"]["per_frame_ms"] < 2.0,
        "stalled_reader_bounded": report["criterion_5_backpressure"]["rss_delta_mb"] < 64.0,
        "stalled_reader_reaped": report["criterion_5_backpressure"]["reaped_by_heartbeat"],
        "stop_under_5s_mid_call": report["criterion_8_restart_during_call"]["service_stop_ms"] < 5000.0,
        "client_saw_clean_close": report["criterion_8_restart_during_call"]["client_saw_close"],
    }
    report["checks"] = checks
    report["pass"] = all(checks.values())
    print(json.dumps(report, indent=2))
    if not report["pass"]:
        failed = [name for name, ok in checks.items() if not ok]
        print(f"FAIL: {', '.join(failed)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
