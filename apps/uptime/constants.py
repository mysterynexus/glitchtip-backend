from django.db import models
from django.utils.translation import gettext_lazy as _


class MonitorType(models.TextChoices):
    PING = "Ping"
    GET = "GET"
    POST = "POST"
    PORT = "TCP Port"
    SSL = "SSL"
    HEARTBEAT = "Heartbeat"


HTTP_MONITOR_TYPES = (MonitorType.PING, MonitorType.GET, MonitorType.POST)


class MonitorCheckReason(models.IntegerChoices):
    UNKNOWN = 0, _("Unknown")
    TIMEOUT = 1, _("Timeout")
    STATUS = 2, _("Wrong status code")
    BODY = 3, _("Expected response not found")
    SSL = 4, _("SSL error")
    NETWORK = 5, _("Network error")


# ``MonitorCheckReason`` says which kind of failure happened. What the probe
# observed while it happened goes under this key in ``MonitorCheck.data``, as
# facts rather than as a finished sentence: what was *expected* is already on
# the monitor, and the sentence is assembled at read time (see
# ``uptime.reasons``) so it can be assembled in the reader's language.
#
# Nested under one key so that a read path can project ``data -> 'failure'``
# and never touch ``data["payload"]``, the up-to-500 KB response body sample
# that shares the column. It shares the column rather than getting one of its
# own because the table is nested-partitioned and sized for 100M+ rows: a ninth
# column would push every tuple header from 24 to 32 bytes -- MAXALIGN(23 +
# BITMAPLEN(natts)) -- including the successful checks that record nothing.
FAILURE_KEY = "failure"

# The HTTP status actually received, when it is not the expected one.
FAILURE_STATUS_KEY = "status"

# An exception's own message, for the failures that have no expectation to
# compare against (SSL, NETWORK, UNKNOWN). The only free text stored.
FAILURE_ERROR_KEY = "error"

# The target, a redirect, or a DNS result was a private/reserved address. A
# flag rather than the exception's message, which names that address.
FAILURE_BLOCKED_KEY = "blocked"

# Bounded because the message is rendered into notification emails and POSTed
# to third-party chat webhooks. 200 chars fits one line in every transport and
# holds the longest realistic aiohttp/OpenSSL message.
FAILURE_ERROR_MAX_LENGTH = 200

# How much of the monitor's own ``expected_body`` is echoed back on a mismatch.
EXPECTED_BODY_SNIPPET_LENGTH = 60
