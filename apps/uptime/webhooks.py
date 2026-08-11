from datetime import datetime
from uuid import UUID

from django.utils.translation import gettext

from apps.alerts.constants import RecipientType
from apps.alerts.models import AlertRecipient
from apps.alerts.webhooks import (
    DiscordEmbed,
    GoogleChatCard,
    WebhookAttachment,
    send_discord_webhook,
    send_googlechat_webhook,
    send_ntfy,
    send_teams_webhook,
    send_webhook,
    send_zulip_message,
)

from .models import MonitorCheck


async def _resolve_monitor_check(monitor_check_id):
    qs = MonitorCheck.objects.select_related("monitor__project__organization")
    if isinstance(monitor_check_id, (list, tuple)):
        mid = (
            UUID(monitor_check_id[0])
            if isinstance(monitor_check_id[0], str)
            else monitor_check_id[0]
        )
        oid = int(monitor_check_id[1])
        return await qs.aget(id=mid, organization_id=oid)
    return await qs.aget(id=monitor_check_id)


async def _send_uptime_generic(recipient, monitor, subject, message):
    attachment = WebhookAttachment(monitor.name, monitor.get_detail_url(), message)
    return await send_webhook(recipient.url, subject, [attachment])


async def _send_uptime_googlechat(recipient, monitor, subject, message):
    card = GoogleChatCard().construct_uptime_card(
        title=subject,
        subtitle=monitor.name,
        # The only transport that does not render "\n": a decoratedText widget
        # takes limited HTML instead.
        text=message.replace("\n", "<br>"),
        url=monitor.get_detail_url(),
    )
    return await send_googlechat_webhook(recipient.url, [card])


async def _send_uptime_discord(recipient, monitor, subject, message):
    embed = DiscordEmbed(
        title=monitor.name,
        description=message,
        color=None,
        fields=[],
        url=monitor.get_detail_url(),
    )
    return await send_discord_webhook(recipient.url, subject, [embed])


async def _send_uptime_ntfy(recipient, monitor, subject, message):
    return await send_ntfy(
        recipient.url,
        title=subject,
        message=f"**{monitor.name}**\n{message}",
        click_url=monitor.get_detail_url(),
        tags=["warning"],
    )


async def _send_uptime_teams(recipient, monitor, subject, message):
    body = [
        {"type": "TextBlock", "size": "Large", "weight": "Bolder", "text": subject},
        {"type": "TextBlock", "weight": "Bolder", "text": monitor.name},
        {"type": "TextBlock", "text": message, "wrap": True},
    ]
    actions = [
        {
            "type": "Action.OpenUrl",
            "title": "View Monitor",
            "url": monitor.get_detail_url(),
        }
    ]
    return await send_teams_webhook(recipient.url, body, actions)


async def _send_uptime_zulip(recipient, monitor, subject, message):
    config = recipient.config or {}
    content = f"## {subject}\n\n**{monitor.name}**\n{message}\n\n[View Monitor]({monitor.get_detail_url()})"
    return await send_zulip_message(
        server_url=recipient.url,
        bot_email=config.get("bot_email", ""),
        api_key=config.get("api_key", ""),
        channel=config.get("channel", ""),
        topic=config.get("topic", "GlitchTip Alerts"),
        content=content,
    )


UPTIME_NOTIFICATION_HANDLERS = {
    RecipientType.GENERAL_WEBHOOK: _send_uptime_generic,
    RecipientType.DISCORD: _send_uptime_discord,
    RecipientType.GOOGLE_CHAT: _send_uptime_googlechat,
    RecipientType.NTFY: _send_uptime_ntfy,
    RecipientType.MICROSOFT_TEAMS: _send_uptime_teams,
    RecipientType.ZULIP: _send_uptime_zulip,
}


def _uptime_message(monitor, monitor_check, went_down: bool) -> str:
    """Notification body, shared by every transport.

    Kept as one string rather than per-transport structures so that every
    handler -- including any added later -- carries the failure reason without
    a signature change.
    """
    if not went_down:
        # A recovery has no failure to explain, so it stays one clean line.
        return gettext("The monitored site is back up.")

    lines = [gettext("The monitored site has gone down.")]
    if reason_text := monitor_check.get_reason_text():
        lines.append(gettext("Reason: %s") % reason_text)
    if monitor.url:
        lines.append(gettext("URL: %s") % monitor.url)
    if monitor_check.response_time is not None:
        lines.append(gettext("Response time: %s ms") % monitor_check.response_time)
    return "\n".join(lines)


async def send_uptime_as_webhook(
    recipient: AlertRecipient,
    monitor_check_id: tuple | list | int,
    went_down: bool,
    last_change: datetime,
):
    """
    Notification about uptime event via webhook.
    """
    monitor_check = await _resolve_monitor_check(monitor_check_id)
    monitor = monitor_check.monitor

    message = _uptime_message(monitor, monitor_check, went_down)
    subject = "GlitchTip Uptime Alert"

    handler = UPTIME_NOTIFICATION_HANDLERS.get(
        recipient.recipient_type, _send_uptime_generic
    )
    return await handler(recipient, monitor, subject, message)
