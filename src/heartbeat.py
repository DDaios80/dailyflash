"""Lightweight heartbeat pinger for uptime monitoring.

After a successful cron run, call ``ping_heartbeat()`` to notify an external
monitoring service (BetterUptime / Better Stack / Healthchecks.io / etc.)
that the pipeline completed. If the service doesn't receive a ping within
its configured grace period (e.g., 26h for a daily cron), it alerts the
operator.

Configuration: set the ``HEARTBEAT_URL`` env var on Railway to the
service-provided endpoint. If unset, the function is a no-op (safe for
local development and for ops without monitoring configured).

Failures are silenced — a heartbeat outage must never cause the cron to
fail. Errors are logged to stderr for visibility in Railway logs.

Why this exists: the May 6 → May 13 deploy gap saga (2026) showed that
the pipeline can silently degrade for a week without anyone noticing.
Heartbeat monitoring closes that gap: you find out in hours, not days.
"""
from __future__ import annotations

import os
import sys
import urllib.request
import urllib.parse


_HEARTBEAT_TIMEOUT_SECONDS = 5


def ping_heartbeat(label: str | None = None) -> None:
    """Notify the monitoring service that this run succeeded.

    Args:
        label: Optional short label to disambiguate runs in the monitoring
            service's UI (e.g., ``"daily"``, ``"auto-quick"``). Appended as
            a query string so BetterUptime / Healthchecks.io / etc. can
            show it as the most recent ping note.

    Behavior:
        - No-op if ``HEARTBEAT_URL`` env var is unset.
        - Best-effort HTTP GET with a 5-second timeout.
        - All exceptions caught and logged to stderr; never raises.
    """
    url = os.environ.get("HEARTBEAT_URL")
    if not url:
        # Quietly skip — not configured. Don't spam logs in local dev.
        return

    if label:
        # Append as ?label=... or &label=... depending on existing query
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}label={urllib.parse.quote(label)}"

    try:
        urllib.request.urlopen(url, timeout=_HEARTBEAT_TIMEOUT_SECONDS)
        print(f"[heartbeat] ping OK ({label or 'no-label'})")
    except Exception as e:
        # Heartbeat failure is non-fatal. We want to see it in logs but
        # never want it to crash the cron run that just succeeded.
        print(
            f"[heartbeat] ping FAILED ({label or 'no-label'}): "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
