"""Loopback health watch for fide-service (read-only, no imports of app code).

Polls the local API and reports failed/stale import jobs plus service
reachability as single structured lines:

    FIDE-OK jobs=3 failed=0 stale=0
    FIDE-ALERT FAILED 2026-09/standard NETWORK_TIMEOUT
    FIDE-ALERT STALE 2026-09/rapid state=importing heartbeat=...

Designed for a systemd timer with journald capture: an operator alert
hook (mail/webhook of choice) can trigger on ``FIDE-ALERT`` lines. No
notification destination is configured here on purpose — this host has
no MTA and no webhook credentials exist. Exit codes: 0 healthy,
1 problems found, 2 service unreachable/misconfigured.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_URL = os.environ.get("FIDE_SERVICE_URL", "http://127.0.0.1:5001")
HTTP_TIMEOUT = 15

ACTIVE_STATES = ("downloading", "validating", "importing", "verifying")


def _parse_ts(value):
    """Parse an API ISO timestamp (naive UTC from the service)."""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def evaluate_jobs(jobs, now, timeout_seconds):
    """Return problem strings for failed/stale jobs (pure function).

    ``jobs`` are ``/admin/jobs`` dicts; ``now`` is an aware UTC datetime.
    A job in ``failed`` is always a problem (retry stays explicit, so a
    failure must stay visible until an operator acts). An active job
    whose heartbeat is older than ``timeout_seconds`` (or missing, which
    a live claim never has) is stale: the worker sweep will mark it
    failed on the next tick, but the condition is reported now.
    """
    problems = []
    for job in jobs or []:
        period = job.get("period", "?")
        rtype = job.get("rating_type", "?")
        state = job.get("state", "?")
        if state == "failed":
            problems.append("FAILED %s/%s %s"
                            % (period, rtype, job.get("error_code") or "?"))
        elif state in ACTIVE_STATES:
            beat = _parse_ts(job.get("heartbeat_at"))
            if beat is None or now - beat > timedelta(seconds=timeout_seconds):
                problems.append(
                    "STALE %s/%s state=%s heartbeat=%s"
                    % (period, rtype, state, job.get("heartbeat_at")))
    return problems


def _get(path, headers=None):
    req = urllib.request.Request(BASE_URL + path, headers=headers or {})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.load(resp)


def main(argv=None):
    admin_key = os.environ.get("FIDE_ADMIN_KEY", "")
    timeout_s = int(os.environ.get("IMPORT_TIMEOUT_SECONDS", "600") or 600)
    try:
        body = _get("/api/v1/healthz")
        if not body.get("ok"):
            print("FIDE-ALERT service unhealthy: /healthz not ok")
            return 2
        if not admin_key:
            print("FIDE-ALERT service misconfigured: FIDE_ADMIN_KEY unset")
            return 2
        data = _get("/api/v1/admin/jobs?limit=100",
                    headers={"X-Admin-Key": admin_key})
    except Exception as exc:  # unreachable, 401/403, malformed: all alert
        print("FIDE-ALERT service unreachable: %s"
              % str(exc).splitlines()[0][:160])
        return 2
    jobs = (data.get("data") or {}).get("jobs", [])
    now = datetime.now(timezone.utc)
    problems = evaluate_jobs(jobs, now, timeout_s)
    failed = sum(1 for p in problems if p.startswith("FAILED"))
    stale = len(problems) - failed
    if problems:
        for problem in problems:
            print("FIDE-ALERT %s" % problem)
        return 1
    print("FIDE-OK jobs=%d failed=%d stale=%d" % (len(jobs), failed, stale))
    return 0


if __name__ == "__main__":
    sys.exit(main())
