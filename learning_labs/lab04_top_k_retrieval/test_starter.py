import unittest

from learning_labs.lab04_top_k_retrieval.starter import (
    SearchDocument,
    SearchHit,
    top_k_search,
)


WOODSCREWS_CATEGORY = (
    "https://www.qewitfastener.com/category/fasteners/screws/"
    "woodscrews-dry-wall-screws/"
)


class TopKSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.documents = [
            SearchDocument(
                "product-wood-a",
                "Wood screw product",
                (1.0, 0.0),
                "product",
                WOODSCREWS_CATEGORY,
            ),
            SearchDocument(
                "blog-wood-guide",
                "Wood screw buying guide",
                (1.0, 0.0),
                "blog",
                WOODSCREWS_CATEGORY,
            ),
            SearchDocument(
                "product-bolt",
                "Bolt product",
                (0.8, 0.2),
                "product",
                "https://www.qewitfastener.com/category/fasteners/bolts/",
            ),
            SearchDocument(
                "product-wood-b",
                "Another wood screw product",
                (0.9, 0.1),
                "product",
                WOODSCREWS_CATEGORY,
            ),
        ]

    def test_filters_blog_and_wrong_category_before_ranking(self) -> None:
        hits = top_k_search(
            (1.0, 0.0),
            self.documents,
            k=5,
            allowed_page_types={"product"},
            required_category_url=WOODSCREWS_CATEGORY,
        )

        self.assertEqual(
            [hit.document_id for hit in hits],
            ["product-wood-a", "product-wood-b"],
        )

    def test_returns_only_k_hits(self) -> None:
        hits = top_k_search((1.0, 0.0), self.documents, k=1)

        self.assertEqual(len(hits), 1)
        self.assertIsInstance(hits[0], SearchHit)

    def test_rejects_non_positive_k(self) -> None:
        with self.assertRaises(ValueError):
            top_k_search((1.0, 0.0), self.documents, k=0)


if __name__ == "__main__":
    unittest.main()

