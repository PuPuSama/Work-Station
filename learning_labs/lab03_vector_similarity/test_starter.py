import unittest

from learning_labs.lab03_vector_similarity.starter import (
    cosine_similarity,
    dot_product,
    vector_norm,
)


class VectorMathTests(unittest.TestCase):
    def test_dot_product(self) -> None:
        self.assertEqual(dot_product([1, 2, 3], [4, 5, 6]), 32)

    def test_vector_norm(self) -> None:
        self.assertAlmostEqual(vector_norm([3, 4]), 5.0)

    def test_same_direction_has_similarity_one(self) -> None:
        self.assertAlmostEqual(cosine_similarity([1, 1], [2, 2]), 1.0)

    def test_perpendicular_vectors_have_similarity_zero(self) -> None:
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_mismatched_dimensions_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            cosine_similarity([1, 0], [1, 0, 0])

    def test_zero_vector_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            cosine_similarity([0, 0], [1, 0])


if __name__ == "__main__":
    unittest.main()

