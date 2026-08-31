import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("run.py")
SPEC = importlib.util.spec_from_file_location("e2_physh_runner", MODULE_PATH)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class RunnerUnitTests(unittest.TestCase):
    def test_normalize_doi(self) -> None:
        self.assertEqual(
            RUNNER.normalize_doi("https://doi.org/10.1103/PhysRevB.95.064110"),
            "10.1103/physrevb.95.064110",
        )

    def test_set_f1(self) -> None:
        self.assertAlmostEqual(RUNNER.set_f1({"a", "b"}, {"b", "c"}), 0.5)
        self.assertEqual(RUNNER.set_f1(set(), set()), 0.0)

    def test_quantile(self) -> None:
        self.assertEqual(RUNNER.quantile([0.0, 1.0], 0.5), 0.5)
        self.assertEqual(RUNNER.quantile([3.0], 0.975), 3.0)

    def test_chunk_seed_is_stable_and_distinct(self) -> None:
        first = RUNNER.permutation_seed(20260830, 0)
        self.assertEqual(first, RUNNER.permutation_seed(20260830, 0))
        self.assertNotEqual(first, RUNNER.permutation_seed(20260830, 1))


if __name__ == "__main__":
    unittest.main()
