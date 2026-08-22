"""Unit tests for the failure detail that rides along with a check's reason.

The check stores facts and the monitor stores expectations; the sentence a user
reads is built from both at read time. Two things are load-bearing here: that
``describe_failure`` builds the same sentence the old stored string held, and
that ``sanitize_error`` stands between a remote host's error text -- or a
monitor URL's credentials -- and the emails and third-party chat webhooks that
text is written into.
"""

from django.test import SimpleTestCase

from apps.uptime.constants import FAILURE_ERROR_MAX_LENGTH, MonitorCheckReason
from apps.uptime.models import Monitor, MonitorCheck, MonitorType
from apps.uptime.reasons import FailureFacts, describe_failure
from apps.uptime.utils import sanitize_error, set_failure


class SanitizeErrorTests(SimpleTestCase):
    def test_empty_input_is_dropped(self):
        for value in (None, "", "   ", "\n\t"):
            with self.subTest(value=value):
                self.assertIsNone(sanitize_error(value))

    def test_newlines_collapse_to_one_line(self):
        # Notification bodies are assembled line by line, so a message with
        # newlines could otherwise forge extra "Reason:"/"URL:" fields.
        self.assertEqual(
            sanitize_error("Cannot connect\nReason: bogus\n  to host"),
            "Cannot connect Reason: bogus to host",
        )

    def test_unprintable_characters_are_stripped(self):
        self.assertEqual(sanitize_error("bad\x00\x1bresponse"), "badresponse")

    def test_long_message_is_truncated_with_an_ellipsis(self):
        result = sanitize_error("x" * 500)
        self.assertEqual(len(result), FAILURE_ERROR_MAX_LENGTH)
        self.assertTrue(result.endswith("…"))

    def test_url_credentials_are_redacted(self):
        # aiohttp interpolates the request URL into its messages, and yarl does
        # not mask the password: str(ClientResponseError) on a monitor pointed
        # at https://user:secret@host hands the credentials to every recipient.
        self.assertEqual(
            sanitize_error(
                "500, message='Server Error', url='https://user:secret@example.com/p'"
            ),
            "500, message='Server Error', url='https://***@example.com/p'",
        )

    def test_credentials_are_redacted_before_truncation(self):
        # Truncating first could cut mid-userinfo and leave the password shown.
        message = "https://user:secret@example.com/" + "x" * 500
        self.assertNotIn("secret", sanitize_error(message))

    def test_exception_is_accepted(self):
        self.assertEqual(sanitize_error(ValueError("boom")), "boom")

    def test_message_less_exception_falls_back_to_its_class(self):
        self.assertEqual(sanitize_error(ConnectionResetError()), "ConnectionResetError")


class SetFailureTests(SimpleTestCase):
    def test_status_is_stored_as_a_number(self):
        monitor = {}
        set_failure(monitor, MonitorCheckReason.STATUS, status=503)
        self.assertEqual(monitor["reason"], MonitorCheckReason.STATUS)
        self.assertEqual(monitor["failure"], {"status": 503})

    def test_reasons_derived_entirely_from_the_monitor_store_nothing(self):
        for reason in (MonitorCheckReason.BODY, MonitorCheckReason.TIMEOUT):
            with self.subTest(reason=reason):
                monitor = {}
                set_failure(monitor, reason)
                self.assertEqual(monitor["reason"], reason)
                self.assertNotIn("failure", monitor)

    def test_blocked_is_a_flag(self):
        monitor = {}
        set_failure(monitor, MonitorCheckReason.NETWORK, blocked=True)
        self.assertEqual(monitor["failure"], {"blocked": True})

    def test_empty_error_is_omitted(self):
        monitor = {}
        set_failure(monitor, MonitorCheckReason.UNKNOWN, error="")
        self.assertEqual(monitor["reason"], MonitorCheckReason.UNKNOWN)
        self.assertNotIn("failure", monitor)


class DescribeFailureTests(SimpleTestCase):
    def setUp(self):
        self.monitor = Monitor(
            monitor_type=MonitorType.GET,
            expected_status=200,
            expected_body="",
            interval=60,
            timeout=None,
        )

    def describe(self, reason, monitor=None, **facts):
        return describe_failure(reason, FailureFacts(**facts), monitor)

    def test_status_is_compared_against_the_monitor(self):
        self.assertEqual(
            self.describe(MonitorCheckReason.STATUS, self.monitor, status=503),
            "Received HTTP 503, expected 200",
        )

    def test_status_alone_when_the_monitor_expects_nothing(self):
        self.monitor.expected_status = None
        self.assertEqual(
            self.describe(MonitorCheckReason.STATUS, self.monitor, status=503),
            "Received HTTP 503",
        )

    def test_body_echoes_the_monitors_own_expected_text(self):
        self.monitor.expected_body = "OK"
        self.assertEqual(
            self.describe(MonitorCheckReason.BODY, self.monitor),
            'Expected text "OK" was not found in the response body',
        )

    def test_timeout_reports_the_monitors_effective_timeout(self):
        # Blank means the 20 second default, via Monitor.int_timeout.
        self.assertEqual(
            self.describe(MonitorCheckReason.TIMEOUT, self.monitor),
            "No response within 20 seconds",
        )

    def test_heartbeat_miss_reports_the_interval(self):
        self.monitor.monitor_type = MonitorType.HEARTBEAT
        self.assertEqual(
            self.describe(None, self.monitor),
            "No heartbeat received in the last 60 seconds",
        )

    def test_no_reason_on_a_probed_monitor_describes_nothing(self):
        # Only a heartbeat can fail without a reason; anything else would be a
        # bug, and claiming a missed heartbeat would be a misleading one.
        self.assertIsNone(self.describe(None, self.monitor))

    def test_blocked_never_names_the_address_that_was_refused(self):
        detail = self.describe(
            MonitorCheckReason.NETWORK, self.monitor, blocked=True, error="169.254.1.1"
        )
        self.assertEqual(
            detail,
            "Blocked: the target, a redirect, or its DNS result is a private "
            "or reserved address",
        )

    def test_network_error_is_the_exceptions_own_message(self):
        self.assertEqual(
            self.describe(
                MonitorCheckReason.NETWORK, self.monitor, error="DNS lookup failed"
            ),
            "DNS lookup failed",
        )

    def test_text_degrades_rather_than_querying_for_an_absent_monitor(self):
        # get_reason_detail only reads an already-cached monitor: these objects
        # are serialized in async views, where a lazy fetch raises.
        self.assertEqual(
            self.describe(MonitorCheckReason.STATUS, status=503), "Received HTTP 503"
        )
        self.assertIsNone(self.describe(MonitorCheckReason.TIMEOUT))


class FailureFactsTests(SimpleTestCase):
    def test_values_are_coerced_from_whatever_json_held(self):
        facts = FailureFacts.from_data({"status": "503", "blocked": "true"})
        self.assertEqual(facts.status, 503)
        self.assertTrue(facts.blocked)

    def test_undecoded_json_is_accepted(self):
        # The LATERAL join reads through a raw cursor, which hands back jsonb
        # as text where the ORM hands back a dict.
        self.assertEqual(FailureFacts.from_data('{"status": 503}').status, 503)
        self.assertEqual(FailureFacts.from_data("not json"), FailureFacts())

    def test_unusable_values_are_dropped(self):
        for value in (None, "detail", [], {"status": "nope", "error": 5}):
            with self.subTest(value=value):
                self.assertEqual(FailureFacts.from_data(value), FailureFacts())

    def test_error_is_rebounded_on_read(self):
        # ``data`` is a JSONField, so a future writer could put anything there.
        facts = FailureFacts.from_data({"error": "y" * 5000})
        self.assertEqual(len(facts.error), FAILURE_ERROR_MAX_LENGTH)


class ReasonTextTests(SimpleTestCase):
    def check(self, **kwargs):
        kwargs.setdefault("is_up", False)
        kwargs.setdefault(
            "monitor", Monitor(monitor_type=MonitorType.GET, expected_status=200)
        )
        return MonitorCheck(**kwargs)

    def test_up_check_has_no_reason_text(self):
        check = self.check(is_up=True, reason=MonitorCheckReason.STATUS)
        self.assertIsNone(check.get_reason_text())

    def test_label_and_detail_are_combined(self):
        check = self.check(
            reason=MonitorCheckReason.STATUS, data={"failure": {"status": 503}}
        )
        self.assertEqual(
            check.get_reason_text(),
            "Wrong status code: Received HTTP 503, expected 200",
        )

    def test_unknown_reason_is_not_dropped(self):
        # UNKNOWN is 0, which the old truthiness check treated as "no reason".
        check = self.check(reason=MonitorCheckReason.UNKNOWN)
        self.assertEqual(check.get_reason_text(), "Unknown")

    def test_heartbeat_miss_has_a_detail_but_no_label(self):
        check = self.check(
            reason=None,
            monitor=Monitor(monitor_type=MonitorType.HEARTBEAT, interval=90),
        )
        self.assertEqual(
            check.get_reason_text(), "No heartbeat received in the last 90 seconds"
        )

    def test_annotation_wins_over_the_data_column(self):
        # Every read path projects ``data -> 'failure'`` under this alias so
        # the potentially 500 KB payload never has to be selected.
        check = self.check(
            reason=MonitorCheckReason.STATUS, data={"failure": {"status": 500}}
        )
        check.failure = {"status": 503}
        self.assertEqual(check.get_reason_detail(), "Received HTTP 503, expected 200")

    def test_missing_facts_leave_the_label_alone(self):
        check = self.check(reason=MonitorCheckReason.SSL, data=None)
        self.assertIsNone(check.get_reason_detail())
        self.assertEqual(check.get_reason_text(), "SSL error")
