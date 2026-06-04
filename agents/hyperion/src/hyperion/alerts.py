"""
Deviation alerts (PLAN_UNIFIED.md Phase 6).

Soft-threshold warnings emitted at most once per (task, kind): a tool-loop nearing the
hard cap, elapsed time past 70% of the wall budget, or a stage that produced no artifact.
Each alert is appended to ``tasks/{id}/alerts.md`` and (best-effort) pushed as a desktop
notification. Gated by ``HYPERION_HITL_ALERTS`` (on|off).

Role in the system
------------------
This module is the human-in-the-loop (HITL) "early warning" channel for a Hyperion run.
Other parts of the orchestrator (the crew runner / observability hooks) call
:func:`emit_alert` when they detect a run drifting toward a hard limit, so an operator can
notice and intervene *before* the run is force-stopped. Alerts are advisory only — they do
not abort the run or change control flow.

Key design decisions / non-obvious context
------------------------------------------
- **At-most-once dedupe.** A process-global in-memory set, :data:`_SEEN`, keyed by
  ``(task_id, kind)``, guarantees each distinct condition fires exactly once per run. The
  state lives only in this process; it is NOT persisted, so a server restart resets dedupe
  and a previously-alerted condition could fire again.
- **Best-effort, never fatal.** Both the file write and the desktop notification swallow
  their exceptions. Alerting is observability, so a failure here must never break the run
  that triggered it.
- **macOS-only desktop notifications.** :func:`_push_notification` shells out to
  ``osascript`` and is silently a no-op on platforms where that binary is absent.
- **Feature gate.** When ``HYPERION_HITL_ALERTS`` is set to ``off`` (case-insensitive),
  :func:`emit_alert` short-circuits and records nothing — note the (task, kind) is then NOT
  added to :data:`_SEEN`, so re-enabling the gate mid-run lets that condition still fire.
"""

from __future__ import annotations

import logging
import time

from hyperion.config import settings

logger = logging.getLogger(__name__)

# (task_id, kind) already alerted — keeps "exactly one alert" per condition per run.
_SEEN: set[tuple[str, str]] = set()


def _alerts_enabled() -> bool:
    """Report whether alert emission is currently enabled.

    Reads the ``hyperion_hitl_alerts`` setting (env ``HYPERION_HITL_ALERTS``), defaulting
    to ``"on"`` when unset. Any value other than ``"off"`` (case-insensitive) counts as
    enabled.

    Returns:
        True if alerts should be emitted; False if the feature is gated off.
    """
    return getattr(settings, "hyperion_hitl_alerts", "on").lower() != "off"


def _alerts_path(task_id: str):
    """Build the per-task alert log path.

    Args:
        task_id: Identifier of the run whose alert log is wanted.

    Returns:
        A ``pathlib.Path`` to ``{settings.tasks_dir}/{task_id}/alerts.md``. The path may
        not exist yet; the parent directory is created lazily by :func:`emit_alert`.
    """
    return settings.tasks_dir / task_id / "alerts.md"


def emit_alert(task_id: str, kind: str, message: str) -> bool:
    """Emit a single alert for ``(task_id, kind)``, deduped per process run.

    The first call for a given ``(task_id, kind)`` pair appends a timestamped entry to the
    task's ``alerts.md`` log and fires a best-effort desktop notification. Subsequent calls
    with the same pair are no-ops, so each deviation condition surfaces exactly once.

    Args:
        task_id: Run identifier; selects the target ``alerts.md`` file.
        kind: Short machine-friendly category of the condition (e.g. ``"tool_loop"``,
            ``"time_budget"``, ``"no_artifact"``). Also forms part of the dedupe key.
        message: Human-readable detail written to the log and notification body.

    Returns:
        True if a brand-new alert fired; False if alerts are disabled or this
        ``(task_id, kind)`` already fired in this process.

    Side effects:
        - Appends to ``tasks/{task_id}/alerts.md`` (creating the directory if needed).
        - Adds the key to :data:`_SEEN`.
        - Triggers a desktop notification and an info-level log line.

    Note:
        File-write failures are caught and logged at WARNING but do NOT prevent the
        function from returning True — the alert is still considered fired (key already
        recorded, notification still attempted).
    """
    if not _alerts_enabled():
        return False
    key = (task_id, kind)
    # Dedupe guard: bail before recording anything if this condition already fired.
    if key in _SEEN:
        return False
    # Mark seen *before* the side effects so a partial failure still won't re-fire.
    _SEEN.add(key)

    try:
        path = _alerts_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        # Append a Markdown section so alerts.md reads as a chronological log.
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n### [{ts}] {kind}\n{message}\n")
    except Exception as exc:
        logger.warning("Failed to write alert for %s: %s", task_id, exc)

    _push_notification(f"Hyperion {task_id}: {kind}", message)
    logger.info("ALERT %s/%s: %s", task_id, kind, message)
    return True


def _push_notification(title: str, body: str) -> None:
    """Best-effort desktop notification (macOS ``osascript``). Never raises.

    On macOS, shells out to ``osascript`` to display a system notification. On any other
    platform (or if ``osascript`` is missing) this is a silent no-op. All exceptions are
    swallowed so notification problems can never disrupt the caller.

    Args:
        title: Notification title; quotes are sanitized and it is truncated to 80 chars.
        body: Notification body; quotes are sanitized and it is truncated to 200 chars.

    Side effects:
        Spawns an ``osascript`` subprocess (5s timeout) when available.
    """
    try:
        # Imported lazily so importing this module stays cheap and platform-agnostic.
        import shutil
        import subprocess

        if shutil.which("osascript"):
            # Replace double quotes with single quotes and clamp length: the strings are
            # interpolated into an AppleScript literal, so unescaped quotes would break it.
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
    """Clear the in-memory dedupe state so conditions can fire again.

    Used at the start of a new run (or in tests) to ensure prior alerts do not suppress
    fresh ones. Because :data:`_SEEN` is process-global, callers should reset it for a
    ``task_id`` before re-running that same id.

    Args:
        task_id: If None, wipes all dedupe state. Otherwise, removes only the entries
            belonging to this task, leaving other tasks' state intact.

    Side effects:
        Mutates the module-global :data:`_SEEN`.
    """
    global _SEEN
    if task_id is None:
        _SEEN = set()
    else:
        _SEEN = {k for k in _SEEN if k[0] != task_id}
