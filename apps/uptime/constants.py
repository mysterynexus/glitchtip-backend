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


# ``MonitorCheckReason`` says which kind of failure happened; the detail says
# what actually happened ("Received HTTP 503, expected 200"). It lives under
# this key in ``MonitorCheck.data`` rather than in a column of its own: the
# table is nested-partitioned and sized for 100M+ rows, and a ninth column
# would push every tuple header from 24 to 32 bytes -- MAXALIGN(23 +
# BITMAPLEN(natts)) -- including the successful checks that have no detail to
# store.
MONITOR_CHECK_DETAIL_KEY = "detail"

# Bounded because the detail is rendered into notification emails and POSTed to
# third-party chat webhooks. 200 chars fits one line in every transport and
# holds the longest realistic aiohttp/OpenSSL message.
MONITOR_CHECK_DETAIL_MAX_LENGTH = 200

# How much of the monitor's own ``expected_body`` is echoed back on a mismatch.
EXPECTED_BODY_SNIPPET_LENGTH = 60

# Fixed text for SSRF blocks. Never derived from ``BlockedRedirectError``,
# which is raised carrying the private address that was refused.
BLOCKED_TARGET_DETAIL = _(
    "Blocked: the target, a redirect, or its DNS result is a private or reserved address"
)
