from __future__ import annotations

from pathlib import Path
import sys
import unittest


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.server_official_links import classify_official_link  # noqa: E402


class OfficialLinkClassificationTests(unittest.TestCase):
    def test_contact_page_is_preferred_for_cta(self) -> None:
        self.assertEqual(
            classify_official_link(
                display_name="Contact Us",
                canonical_url="https://example.com/contact/",
                source_kind="knowledge_page",
            ),
            ("contact", 100),
        )

    def test_chinese_contact_page_is_recognized(self) -> None:
        self.assertEqual(
            classify_official_link(
                display_name="联系我们",
                canonical_url="https://example.cn/lian-xi/",
                source_kind="knowledge_page",
            ),
            ("contact", 100),
        )

    def test_blog_and_privacy_pages_are_not_cta_candidates(self) -> None:
        self.assertIsNone(
            classify_official_link(
                display_name="How to Select Fasteners",
                canonical_url="https://example.com/blog/select-fasteners/",
                source_kind="official_blog",
            )
        )
        self.assertIsNone(
            classify_official_link(
                display_name="Privacy Policy",
                canonical_url="https://example.com/privacy/",
                source_kind="knowledge_page",
            )
        )


if __name__ == "__main__":
    unittest.main()
