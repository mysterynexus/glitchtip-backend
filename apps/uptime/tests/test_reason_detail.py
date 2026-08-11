"""Unit tests for the failure detail that rides along with a check's reason.

The detail is written into notification emails and POSTed to third-party chat
webhooks, so ``sanitize_detail`` is what stands between a remote host's error
text and those messages.
"""

from django.test import SimpleTestCase

from apps.uptime.constants import MONITOR_CHECK_DETAIL_MAX_LENGTH, MonitorCheckReason
from apps.uptime.models import MonitorCheck
from apps.uptime.utils import sanitize_detail, set_failure


class SanitizeDetailTests(SimpleTestCase):
    def test_empty_input_is_dropped(self):
        for value in (None, "", "   ", "\n\t"):
            with self.subTest(value=value):
                self.assertIsNone(sanitize_detail(value))

    def test_newlines_collapse_to_one_line(self):
        # Notification bodies are assembled line by line, so a detail with
        # newlines could otherwise forge extra "Reason:"/"URL:" fields.
        self.assertEqual(
            sanitize_detail("Cannot connect\nReason: bogus\n  to host"),
            "Cannot connect Reason: bogus to host",
        )

    def test_unprintable_characters_are_stripped(self):
        self.assertEqual(sanitize_detail("bad\x00\x1bresponse"), "badresponse")

    def test_long_detail_is_truncated_with_an_ellipsis(self):
        result = sanitize_detail("x" * 500)
        self.assertEqual(len(result), MONITOR_CHECK_DETAIL_MAX_LENGTH)
        self.assertTrue(result.endswith("…"))

    def test_non_string_is_accepted(self):
        self.assertEqual(sanitize_detail(ValueError("boom")), "boom")

    def test_set_failure_omits_an_empty_detail(self):
        monitor = {}
        set_failure(monitor, MonitorCheckReason.UNKNOWN, "")
        self.assertEqual(monitor["reason"], MonitorCheckReason.UNKNOWN)
        self.assertNotIn("reason_detail", monitor)


class ReasonTextTests(SimpleTestCase):
    def test_up_check_has_no_reason_text(self):
        check = MonitorCheck(is_up=True, reason=MonitorCheckReason.STATUS)
        self.assertIsNone(check.get_reason_text())

    def test_label_and_detail_are_combined(self):
        check = MonitorCheck(
            is_up=False,
            reason=MonitorCheckReason.STATUS,
            data={"detail": "Received HTTP 503, expected 200"},
        )
        self.assertEqual(
            check.get_reason_text(),
            "Wrong status code: Received HTTP 503, expected 200",
        )

    def test_unknown_reason_is_not_dropped(self):
        # UNKNOWN is 0, which the old truthiness check treated as "no reason".
        check = MonitorCheck(is_up=False, reason=MonitorCheckReason.UNKNOWN)
        self.assertEqual(check.get_reason_text(), "Unknown")

    def test_heartbeat_miss_has_a_detail_but_no_label(self):
        check = MonitorCheck(
            is_up=False, reason=None, data={"detail": "No heartbeat received"}
        )
        self.assertEqual(check.get_reason_text(), "No heartbeat received")

    def test_annotation_wins_over_the_data_column(self):
        # Both API read paths project ``data ->> 'detail'`` under this alias so
        # the potentially 500 KB payload never has to be selected.
        check = MonitorCheck(is_up=False, reason=MonitorCheckReason.NETWORK)
        check.reason_detail = "from annotation"
        self.assertEqual(check.get_reason_detail(), "from annotation")

    def test_detail_is_rebounded_on_read(self):
        # ``data`` is a JSONField, so a future writer could put anything there.
        check = MonitorCheck(is_up=False, reason=None, data={"detail": "y" * 5000})
        self.assertEqual(
            len(check.get_reason_detail()), MONITOR_CHECK_DETAIL_MAX_LENGTH
        )

    def test_missing_detail_returns_none(self):
        check = MonitorCheck(is_up=False, reason=MonitorCheckReason.TIMEOUT, data=None)
        self.assertIsNone(check.get_reason_detail())
        self.assertEqual(check.get_reason_text(), "Timeout")
