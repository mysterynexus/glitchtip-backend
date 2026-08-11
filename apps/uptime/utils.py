import asyncio
import logging
import re
import time
from datetime import timedelta
from ssl import SSLError

from aiohttp import ClientTimeout
from aiohttp.client_exceptions import (
    ClientConnectorError,
    ClientResponseError,
    ServerConnectionError,
)
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext

from glitchtip.partition_manager import UUID7Helper
from glitchtip.ssrf_protection import (
    BlockedRedirectError,
    request_with_validated_redirects,
    resolve_validated_ip,
    safe_session,
)
from glitchtip.url_validation import is_ip_blocked  # re-exported for uptime.schema

from .constants import (
    BLOCKED_TARGET_DETAIL,
    EXPECTED_BODY_SNIPPET_LENGTH,
    MONITOR_CHECK_DETAIL_MAX_LENGTH,
    MonitorCheckReason,
    MonitorType,
)
from .models import MonitorCheck

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TIMEOUT",
    "PAYLOAD_LIMIT",
    "PAYLOAD_SAVE_LIMIT",
    "fetch",
    "fetch_all",
    "is_ip_blocked",
    "process_response",
    "sanitize_detail",
    "set_failure",
]

DEFAULT_TIMEOUT = 20  # Seconds
PAYLOAD_LIMIT = 2_000_000  # 2mb
PAYLOAD_SAVE_LIMIT = 500_000  # pseudo 500kb

_WHITESPACE = re.compile(r"\s+")


def sanitize_detail(text) -> str | None:
    """Bound and flatten a failure detail before it can reach a user.

    The result is rendered into notification emails and POSTed to third-party
    chat webhooks, so it is collapsed to a single printable line -- a detail
    containing newlines could otherwise forge extra fields in a message body
    that is assembled line by line -- and truncated.

    This bounds what a caller passes; it does not decide what is safe to pass.
    See ``fetch`` for why ``BlockedRedirectError``'s message never is.
    """
    if not text:
        return None
    flat = _WHITESPACE.sub(" ", str(text)).strip()
    flat = "".join(char for char in flat if char.isprintable())
    if not flat:
        return None
    if len(flat) > MONITOR_CHECK_DETAIL_MAX_LENGTH:
        flat = flat[: MONITOR_CHECK_DETAIL_MAX_LENGTH - 1].rstrip() + "…"
    return flat


def set_failure(monitor: dict, reason: MonitorCheckReason, detail=None) -> None:
    """Record a failed probe on the in-flight monitor dict.

    ``monitor`` is the plain dict the probe carries around, so the detail rides
    out on a new optional ``reason_detail`` key rather than changing any
    signature. ``save_monitor_checks`` folds it into ``MonitorCheck.data``.
    """
    monitor["reason"] = reason
    if detail := sanitize_detail(detail):
        monitor["reason_detail"] = detail


async def process_response(monitor, response):
    if response.status == monitor["expected_status"]:
        if monitor["expected_body"]:
            # Limit size to 2MB
            body = await response.content.read(PAYLOAD_LIMIT)
            try:
                encoding = response.get_encoding()
                payload = body.decode(encoding, errors="ignore")
            except RuntimeError:
                payload = body.decode(errors="ignore")
            if monitor["expected_body"] in payload:
                monitor["is_up"] = True
            else:
                # Echo the monitor's own expected_body, never the response. The
                # response is untrusted remote content and already has a home
                # in data["payload"], which no API exposes.
                set_failure(
                    monitor,
                    MonitorCheckReason.BODY,
                    gettext(
                        'Expected text "%(text)s" was not found in the response body'
                    )
                    % {"text": monitor["expected_body"][:EXPECTED_BODY_SNIPPET_LENGTH]},
                )
                if monitor["latest_is_up"] != monitor["is_up"]:
                    # Save only first 500k chars, to roughly reduce disk usage
                    # Note that a unicode char is not always one byte
                    # Only save on changes
                    monitor["data"] = {"payload": payload[:PAYLOAD_SAVE_LIMIT]}
        else:
            monitor["is_up"] = True
    else:
        set_failure(
            monitor,
            MonitorCheckReason.STATUS,
            gettext("Received HTTP %(got)s, expected %(want)s")
            % {"got": response.status, "want": monitor["expected_status"]},
        )


async def fetch(session, monitor):
    monitor["is_up"] = False
    if monitor["monitor_type"] == MonitorType.HEARTBEAT:
        interval = timedelta(seconds=monitor["interval"])
        since = timezone.now() - interval
        # Partition-aware: organization_id prunes hash sub-partitions and
        # id__gte prunes RANGE (UUIDv7) partitions to the interval window.
        # Without both, Postgres locks every partition and exhausts
        # max_locks_per_transaction. Bounding id also *is* the time filter --
        # the check's start time is the timestamp inside its id.
        if await MonitorCheck.objects.filter(
            organization_id=monitor["organization_id"],
            monitor_id=monitor["id"],
            id__gte=UUID7Helper._uuid7_for_timestamp(since, min_random=True),
        ).aexists():
            monitor["is_up"] = True
        else:
            # Detail only, no reason: nothing was probed, so no
            # MonitorCheckReason applies. Clients distinguish a heartbeat miss
            # by reason being null, which this preserves.
            monitor["reason_detail"] = sanitize_detail(
                gettext("No heartbeat received in the last %(seconds)s seconds")
                % {"seconds": monitor["interval"]}
            )
        return monitor

    url = monitor["url"]
    timeout = monitor["timeout"] or DEFAULT_TIMEOUT
    allow_private = settings.GLITCHTIP_UPTIME_ALLOW_PRIVATE_IPS

    try:
        start = time.monotonic()
        if monitor["monitor_type"] == MonitorType.PORT:
            # Resolve and validate once, then connect to the pinned IP so the
            # address checked is the address dialed (no DNS-rebind window).
            host, port = url.split(":")
            ip = await resolve_validated_ip(host, allow_private)
            fut = asyncio.open_connection(ip, port)
            await asyncio.wait_for(fut, timeout=timeout)
            monitor["is_up"] = True
        else:
            client_timeout = ClientTimeout(total=monitor["timeout"] or DEFAULT_TIMEOUT)
            if monitor["monitor_type"] == MonitorType.PING:
                async with await request_with_validated_redirects(
                    session,
                    "HEAD",
                    url,
                    allow_private=allow_private,
                    timeout=client_timeout,
                ):
                    monitor["is_up"] = True
            elif monitor["monitor_type"] == MonitorType.GET:
                async with await request_with_validated_redirects(
                    session,
                    "GET",
                    url,
                    allow_private=allow_private,
                    timeout=client_timeout,
                ) as response:
                    await process_response(monitor, response)
            elif monitor["monitor_type"] == MonitorType.POST:
                async with await request_with_validated_redirects(
                    session,
                    "POST",
                    url,
                    allow_private=allow_private,
                    timeout=client_timeout,
                ) as response:
                    await process_response(monitor, response)
        monitor["response_time"] = (
            timedelta(seconds=time.monotonic() - start).total_seconds() * 1000
        )
    except SSLError as e:
        # aiohttp's ClientConnectorSSLError/CertificateError land here first;
        # their message names the monitor's own host and the OpenSSL reason.
        set_failure(monitor, MonitorCheckReason.SSL, str(e) or type(e).__name__)
    except TimeoutError:
        set_failure(
            monitor,
            MonitorCheckReason.TIMEOUT,
            gettext("No response within %(seconds)s seconds") % {"seconds": timeout},
        )
    except BlockedRedirectError:
        logger.warning(
            "Monitor %s blocked: target (or redirect/DNS) is a private/reserved IP",
            monitor["id"],
        )
        # Fixed text, never str(e): the exception carries the private address
        # that was refused, and this string is emailed to users and POSTed to
        # third-party webhooks.
        set_failure(monitor, MonitorCheckReason.NETWORK, BLOCKED_TARGET_DETAIL)
    except (ClientConnectorError, ServerConnectionError) as e:
        # Safe to echo: every hop is validated public before it is dialed, so
        # the host named here is never internal. Carries the DNS/connect
        # reason, which is the most useful thing a user can be told.
        set_failure(monitor, MonitorCheckReason.NETWORK, str(e) or type(e).__name__)
    except (OSError, ClientResponseError) as e:
        logger.warning(f"Monitor {monitor['id']} check failed", exc_info=e)
        set_failure(monitor, MonitorCheckReason.UNKNOWN, str(e) or type(e).__name__)
    except Exception as e:
        logger.error(f"Monitor {monitor['id']} check failed", exc_info=e)
        # Class name only. An unanticipated exception's message can carry
        # anything -- connection strings, file paths, driver internals.
        set_failure(monitor, MonitorCheckReason.UNKNOWN, type(e).__name__)
    return monitor


async def fetch_all(monitors):
    async with safe_session(settings.GLITCHTIP_UPTIME_ALLOW_PRIVATE_IPS) as session:
        results = await asyncio.gather(
            *[fetch(session, monitor) for monitor in monitors], return_exceptions=True
        )
        return results
