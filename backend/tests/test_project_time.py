from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from services.project_time import (
    PROJECT_TIMEZONE,
    PROJECT_TIMEZONE_NAME,
    postgres_connect_args,
    project_now,
    project_now_iso,
)


class ProjectTimeTests(unittest.TestCase):
    def test_project_now_is_aware_and_east_eight(self) -> None:
        current = project_now()

        self.assertIs(current.tzinfo, PROJECT_TIMEZONE)
        self.assertEqual(current.utcoffset(), timedelta(hours=8))

    def test_project_now_iso_includes_unambiguous_offset(self) -> None:
        value = datetime.fromisoformat(project_now_iso())

        self.assertEqual(value.utcoffset(), timedelta(hours=8))

    def test_postgres_connections_request_project_timezone(self) -> None:
        self.assertEqual(
            postgres_connect_args(),
            {"options": f"-c timezone={PROJECT_TIMEZONE_NAME}"},
        )

    def test_project_now_tracks_current_instant(self) -> None:
        current = project_now().astimezone(timezone.utc)
        delta = abs((current - datetime.now(timezone.utc)).total_seconds())

        self.assertLess(delta, 5)


if __name__ == "__main__":
    unittest.main()
