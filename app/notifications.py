import subprocess
import logging

logger = logging.getLogger("p4_integ.notifications")

SENDMAIL = "/usr/sbin/sendmail"
FROM_ADDR = "p4-integ@amd.com"
EMAIL_DOMAIN = "amd.com"

_STATUS_LABELS = {
    "DONE": "Completed Successfully",
    "ERROR": "Failed",
    "NEEDS_RESOLVE": "Needs Manual Conflict Resolution",
}

_TERMINAL_STAGES = {"CLEANUP", "ERROR", "DONE"}


def _last_real_stage(job: dict) -> str:
    """Return the last non-terminal stage from history (the stage where work stopped)."""
    last = ""
    for h in job.get("history", []):
        if h.get("to") not in _TERMINAL_STAGES:
            last = h.get("to", "")
    return last


def send_job_notification(job: dict, base_url: str = "") -> None:
    owner = job.get("owner", "")
    if not owner:
        return

    to_addr = f"{owner}@{EMAIL_DOMAIN}"
    job_id = job.get("job_id", "")
    stage = job.get("stage", "")

    is_cancelled = stage == "ERROR" and job.get("error") == "Cancelled by user"

    if is_cancelled:
        status_label = "Cancelled"
    else:
        status_label = _STATUS_LABELS.get(stage, stage)

    display_stage = _last_real_stage(job) if stage == "ERROR" else stage

    subject = f"[P4 Integ] Job {job_id[:8]} — {status_label}"
    # Strip newlines from headers to prevent injection
    subject = subject.replace("\r", "").replace("\n", "")
    to_addr_safe = to_addr.replace("\r", "").replace("\n", "")

    spec = job.get("spec", {})
    workspace = spec.get("workspace", "")
    branch_spec = spec.get("branch_spec", "")

    lines = [
        f"Job {job_id} has reached status: {status_label}",
        "",
        f"  Workspace     : {workspace}",
        f"  Branch        : {branch_spec}",
        f"  Cancelled at  : {display_stage}" if is_cancelled else f"  Stage         : {display_stage}",
    ]

    if stage == "ERROR" and not is_cancelled:
        error = job.get("error", "")
        if error:
            lines.append(f"  Error         : {error}")

    if stage == "NEEDS_RESOLVE":
        conflicts = job.get("conflicts", [])
        if conflicts:
            lines.append(f"  Conflicts : {len(conflicts)} file(s) need manual resolution")

    if base_url:
        lines.extend(["", f"View job: {base_url.rstrip('/')}/jobs/{job_id}"])

    body = "\n".join(lines)
    message = "\n".join([
        f"To: {to_addr_safe}",
        f"From: {FROM_ADDR}",
        f"Subject: {subject}",
        "Content-Type: text/plain; charset=utf-8",
        "",
        body,
    ])

    try:
        result = subprocess.run(
            [SENDMAIL, "-t"],
            input=message.encode("utf-8"),
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            err = result.stderr[:200].decode("utf-8", errors="replace")
            logger.warning("sendmail failed (status %d): %s", result.returncode, err)
        else:
            logger.info("notification sent to %s for job %s (%s)", to_addr, job_id[:8], stage)
    except Exception as e:
        logger.warning("sendmail unavailable: %s", e)
