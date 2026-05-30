"""
Deviation alerts (PLAN_UNIFIED.md Phase 6).

Soft-threshold warnings emitted at most once per (task, kind): a tool-loop nearing the
hard cap, elapsed time past 70% of the wall budget, or a stage that produced no artifact.
Each alert is appended to ``tasks/{id}/alerts.md`` and (best-effort) pushed as a desktop
notification. Gated by ``HYPERION_HITL_ALERTS`` (on|off).
"""

from __future__ import annotations

import logging
import time

from hyperion.config import settings

logger = logging.getLogger(__name__)

# (task_id, kind) already alerted — keeps "exactly one alert" per condition per run.
_SEEN: set[tuple[str, str]] = set()


def _alerts_enabled() -> bool:
    return getattr(settings, "hyperion_hitl_alerts", "on").lower() != "off"


def _alerts_path(task_id: str):
    return settings.tasks_dir / task_id / "alerts.md"


def emit_alert(task_id: str, kind: str, message: str) -> bool:
    """Emit a single alert for (task_id, kind). Returns True if a new alert fired."""
    if not _alerts_enabled():
        return False
    key = (task_id, kind)
    if key in _SEEN:
        return False
    _SEEN.add(key)

    try:
        path = _alerts_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n### [{ts}] {kind}\n{message}\n")
    except Exception as exc:
        logger.warning("Failed to write alert for %s: %s", task_id, exc)

    _push_notification(f"Hyperion {task_id}: {kind}", message)
    logger.info("ALERT %s/%s: %s", task_id, kind, message)
    return True


def _push_notification(title: str, body: str) -> None:
    """Best-effort desktop notification (macOS osascript). Never raises."""
    try:
        import shutil
        import subprocess

        if shutil.which("osascript"):
            safe_body = body.replace('"', "'")[:200]
            safe_title = title.replace('"', "'")[:80]
            subprocess.run(
                ["osascript", "-e", f'display notification "{safe_body}" with title "{safe_title}"'],
                timeout=5,
                check=False,
            )
    except Exception:
        pass


def reset(task_id: str | None = None) -> None:
    """Clear de-dupe state (per task, or all). Mainly for tests / new runs."""
    global _SEEN
    if task_id is None:
        _SEEN = set()
    else:
        _SEEN = {k for k in _SEEN if k[0] != task_id}
