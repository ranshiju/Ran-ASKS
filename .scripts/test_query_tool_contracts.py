import unittest
import query_tool_contracts as contracts


class ContractsTest(unittest.TestCase):
    def setUp(self):
        self.schemas = contracts.schemas({
            "hybrid_recall": [("query", ""), ("intent", "exploration"), ("topk", "8"), ("include_deprecated", False)],
            "semantic_search": [("query", ""), ("scope", "node")],
            "source_search": [("query", ""), ("domain", ""), ("topk", "5")],
            "read_raw": [("locator", "")],
        }, intents=("fact", "explanation", "exploration"), domains=("academic",))

    def test_recorded_invalid_enums_and_unknown_filters(self):
        for action, value in [
            ("hybrid_recall", {"query": "x", "intent": "locate_quarterly_report_clause"}),
            ("semantic_search", {"query": "x", "scope": "academic"}),
            ("source_search", {"query": "x", "source": "raw/document.md"}),
            ("read_raw", {"locator": "上一步返回的季报条款locator"}),
        ]:
            with self.subTest(action=action):
                self.assertTrue(contracts.validate(value, self.schemas[action]))

    def test_valid_types_and_legacy_numeric_strings(self):
        for topk in [1, 20, "5"]:
            self.assertEqual("", contracts.validate({"query": "x", "intent": "fact", "topk": topk}, self.schemas["hybrid_recall"]))
        for topk in [True, 0, 21, "many"]:
            self.assertTrue(contracts.validate({"query": "x", "topk": topk}, self.schemas["hybrid_recall"]))
        self.assertTrue(contracts.validate({"query": "x", "include_deprecated": "false"}, self.schemas["hybrid_recall"]))

    def test_required_parameters_and_literal_locator(self):
        self.assertTrue(contracts.validate({}, self.schemas["read_raw"]))
        self.assertEqual("", contracts.validate({"locator": "academic/raw/document.md#L1-L4"}, self.schemas["read_raw"]))

    def test_scalar_arithmetic(self):
        self.assertEqual(contracts.calculate("sum", [0.1, 0.2])["result"], "0.3")
        self.assertEqual(contracts.calculate("product", [2, 3, -4])["result"], -24)
        self.assertEqual(contracts.calculate("difference", [7, 9])["result"], -2)
        self.assertEqual(contracts.calculate("quotient", [7, 2])["result"], "3.5")

    def test_vector_composition_and_shapes(self):
        out = contracts.calculate("sum_products", [[1, 2], [3, -1], [4, 5]])
        self.assertEqual(out["vector_lengths"], [2, 2, 2])
        self.assertEqual(out["terms"], [12, -10])
        self.assertEqual(out["result"], 2)
        self.assertFalse(out["fact_evidence"])
        self.assertEqual(contracts.calculate("sum_products", [[2, 3, 4], [5, 6, 7]])["result"], 56)

    def test_vector_mismatch_never_silently_truncates(self):
        with self.assertRaisesRegex(ValueError, "lengths differ"):
            contracts.calculate("sum_products", [[1, 2], [3]])

    def test_arithmetic_bounds(self):
        for operation, operands in [("quotient", [1, 0]), ("sum", [True]), ("sum", [float("nan")]),
                                    ("sum", [10**400]), ("product", [1e100, 1e100]), ("sum", [1]*257),
                                    ("sum_products", [[1]*129, [1]*129]), ("difference", [1, 2, 3])]:
            with self.subTest(operation=operation, operands=str(operands)[:40]):
                with self.assertRaises(ValueError):
                    contracts.calculate(operation, operands)

    def test_no_expressions_or_executable_operations(self):
        for operation, operands in [("eval", [1]), ("sum", ["__import__('os')"]), ("sum", [{"code": "1+1"}])]:
            with self.assertRaises(ValueError):
                contracts.calculate(operation, operands)


if __name__ == "__main__":
    unittest.main()
