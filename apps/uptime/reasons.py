"""Why a check failed, in words, built at read time.

A check stores what the probe observed -- the status it got, the message an
exception carried -- and the monitor stores what was expected. Neither stores a
sentence. The sentence is assembled here, on read, so that it is assembled in
the reader's language and so that a table sized for 100M+ rows is not carrying
prose that is identical on every row.

Nothing here imports models: the monitor is duck-typed, which keeps this
importable from the probe, the API, the email and the webhooks alike.
"""

import json
from typing import Any, NamedTuple

from django.utils.translation import gettext

from .constants import (
    EXPECTED_BODY_SNIPPET_LENGTH,
    FAILURE_BLOCKED_KEY,
    FAILURE_ERROR_KEY,
    FAILURE_ERROR_MAX_LENGTH,
    FAILURE_STATUS_KEY,
    MonitorCheckReason,
    MonitorType,
)


class FailureFacts(NamedTuple):
    """What the probe observed, as stored under ``MonitorCheck.data['failure']``."""

    status: int | None = None
    error: str | None = None
    blocked: bool = False

    @classmethod
    def from_data(cls, value: Any) -> "FailureFacts":
        """Read the facts back out of storage, trusting none of them.

        ``data`` is a JSONField, so nothing in it is guaranteed to still have
        the type it was written with, and a projected ``data -> 'failure'``
        arrives decoded through the ORM but as text through a raw cursor.
        Anything unrecognisable is simply dropped.
        """
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return cls()
        if not isinstance(value, dict):
            return cls()
        try:
            status = int(value.get(FAILURE_STATUS_KEY))
        except (TypeError, ValueError):
            status = None
        error = value.get(FAILURE_ERROR_KEY)
        if isinstance(error, str) and error:
            # Re-bounded on read: a future writer could put anything of any
            # length in a JSONField, and this text reaches third parties.
            error = error[:FAILURE_ERROR_MAX_LENGTH]
        else:
            error = None
        blocked = value.get(FAILURE_BLOCKED_KEY)
        return cls(status=status, error=error, blocked=blocked in (True, "true"))


def describe_failure(
    reason: int | None, facts: FailureFacts, monitor=None
) -> str | None:
    """Describe one failed check, or return None if there is nothing to add.

    ``monitor`` supplies the other half of every comparison -- the status that
    was expected, the text that was looked for, how long the probe was given.
    It is optional because callers serialize these objects inside async views,
    where a lazily fetched relation would raise; a caller that has not loaded
    the monitor gets a shorter sentence rather than a query.
    """
    match reason:
        case None:
            # No reason means nothing was probed: a heartbeat monitor that did
            # not check in. There is no reason label to pair this with either,
            # so it is the whole explanation.
            if monitor is None or monitor.monitor_type != MonitorType.HEARTBEAT:
                return None
            return gettext("No heartbeat received in the last %(seconds)s seconds") % {
                "seconds": monitor.interval
            }
        case MonitorCheckReason.STATUS:
            if facts.status is None:
                return None
            if monitor is None or monitor.expected_status is None:
                return gettext("Received HTTP %(got)s") % {"got": facts.status}
            return gettext("Received HTTP %(got)s, expected %(want)s") % {
                "got": facts.status,
                "want": monitor.expected_status,
            }
        case MonitorCheckReason.BODY:
            # The monitor's own expected text, never the response: the response
            # is untrusted remote content, and it already has a home in
            # ``data["payload"]``, which no API exposes.
            if monitor is None or not monitor.expected_body:
                return None
            return gettext(
                'Expected text "%(text)s" was not found in the response body'
            ) % {"text": monitor.expected_body[:EXPECTED_BODY_SNIPPET_LENGTH]}
        case MonitorCheckReason.TIMEOUT:
            if monitor is None:
                return None
            return gettext("No response within %(seconds)s seconds") % {
                "seconds": monitor.int_timeout
            }
        case MonitorCheckReason.NETWORK if facts.blocked:
            return gettext(
                "Blocked: the target, a redirect, or its DNS result is a "
                "private or reserved address"
            )
        case _:
            # SSL, NETWORK and UNKNOWN have no expectation to compare against.
            # The exception's own message is the only detail there is.
            return facts.error
