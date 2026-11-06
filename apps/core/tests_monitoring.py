"""Error reporting stays off until asked for, and leaves family data behind."""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from config.settings import monitoring


class ErrorReportingTests(SimpleTestCase):
    def test_nothing_starts_without_a_dsn(self):
        with mock.patch("sentry_sdk.init") as init:
            self.assertFalse(monitoring.start(""))
            self.assertFalse(monitoring.start("   "))
        init.assert_not_called()

    def test_a_dsn_starts_it(self):
        with mock.patch("sentry_sdk.init") as init:
            self.assertTrue(monitoring.start("https://key@example.test/1"))
        init.assert_called_once()

    def test_personal_data_is_switched_off(self):
        options = monitoring.options("https://key@example.test/1", "production")

        self.assertIs(options["send_default_pii"], False)
        self.assertIs(options["include_local_variables"], False)
        self.assertEqual(options["max_request_body_size"], "never")
        self.assertEqual(options["traces_sample_rate"], 0.0)

    def test_searches_and_form_contents_are_removed_from_a_report(self):
        event = {
            "request": {
                "url": "https://example.test/students/",
                "query_string": "q=Chinaza",
                "data": {"parent_phone": "08031234567"},
                "cookies": {"sessionid": "abc"},
                "env": {"REMOTE_ADDR": "203.0.113.1"},
            }
        }

        cleaned = monitoring.strip_request_data(event, {})

        self.assertEqual(cleaned["request"], {"url": "https://example.test/students/"})

    def test_an_event_with_no_request_passes_through(self):
        self.assertEqual(monitoring.strip_request_data({"message": "x"}, {}), {"message": "x"})
