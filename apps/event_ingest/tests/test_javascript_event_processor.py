from unittest import mock
from uuid import uuid4

from django.test import SimpleTestCase

from apps.event_ingest.javascript_event_processor import JavascriptEventProcessor


class FindSourceFilesDebugIdTestCase(SimpleTestCase):
    """Regression tests for RN/Hermes OTA Debug ID matching."""

    def _bundle(self, *, debug_id, file_name, code_file=None):
        bundle = mock.Mock()
        bundle.debug_id = debug_id
        bundle.file = mock.Mock()
        bundle.file.name = file_name
        bundle.sourcemap_file = mock.Mock()
        bundle.sourcemap_file.name = f"{file_name}.map"
        bundle.data = {"code_file": code_file} if code_file else {}
        return bundle

    def test_debug_id_match_wins_over_earlier_filename_match(self):
        """
        React Native rewrites every frame to index.android.bundle / main.jsbundle.

        When a native release map (same basename via code_file) is listed before
        the correct OTA artifact bundle, Debug ID matching must still win.
        Previously, filename fallback in the same loop returned the first
        basename match and applied the wrong map.
        """
        wrong_id = uuid4()
        right_id = uuid4()
        wrong = self._bundle(
            debug_id=wrong_id,
            file_name="index.android.bundle",
            code_file="app:///index.android.bundle",
        )
        right = self._bundle(
            debug_id=right_id,
            file_name="entry-ota.hbc",
        )
        processor = JavascriptEventProcessor(
            release_id=None,
            data=mock.Mock(),
            debug_bundles=[wrong, right],
        )
        frame = mock.Mock()
        frame.abs_path = "app:///index.android.bundle"

        minified, sourcemap = processor.find_source_files(
            frame, {"index.android.bundle": str(right_id)}
        )

        self.assertIs(minified, right.file)
        self.assertIs(sourcemap, right.sourcemap_file)

    def test_filename_fallback_when_debug_id_absent(self):
        bundle = self._bundle(
            debug_id=uuid4(),
            file_name="bundle.js",
            code_file="http://localhost/dist/bundle.js",
        )
        processor = JavascriptEventProcessor(
            release_id=1,
            data=mock.Mock(),
            debug_bundles=[bundle],
        )
        frame = mock.Mock()
        frame.abs_path = "http://localhost/dist/bundle.js"

        minified, sourcemap = processor.find_source_files(frame, {})

        self.assertIs(minified, bundle.file)
        self.assertIs(sourcemap, bundle.sourcemap_file)

    def test_no_match_returns_none(self):
        bundle = self._bundle(debug_id=uuid4(), file_name="other.js")
        processor = JavascriptEventProcessor(
            release_id=None,
            data=mock.Mock(),
            debug_bundles=[bundle],
        )
        frame = mock.Mock()
        frame.abs_path = "app:///index.android.bundle"

        self.assertIsNone(
            processor.find_source_files(
                frame, {"index.android.bundle": str(uuid4())}
            )
        )
