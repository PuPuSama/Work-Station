from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.server_outline_generation import (  # noqa: E402
    PublishedGenerationContextChunk,
    published_generation_context_text,
)


class ServerBlogReferenceBoundaryTests(unittest.TestCase):
    def test_blog_chunk_is_labelled_as_body_reference_not_evidence(self) -> None:
        rendered = published_generation_context_text(
            (
                PublishedGenerationContextChunk(
                    chunk_id="blog-snapshot:0",
                    heading_path=("Blog", "Selection guide"),
                    text="A practical editorial comparison.",
                    canonical_url="https://example.test/blog/selection-guide",
                    source_kind="official_blog",
                ),
            )
        )

        self.assertIn("Source kind: official_blog", rendered)
        self.assertIn(
            "Allowed use: body-writing reference only; not evidence",
            rendered,
        )
        self.assertIn("may be cited in the article body", rendered)


if __name__ == "__main__":
    unittest.main()
