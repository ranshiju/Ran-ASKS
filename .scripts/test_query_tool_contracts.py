import unittest
from decimal import Decimal
import query_tool_contracts as contracts


class ContractsTest(unittest.TestCase):
    def setUp(self):
        self.schemas = contracts.schemas({
            "hybrid_recall": [("query", ""), ("intent", "exploration"), ("topk", "8"), ("include_deprecated", False)],
            "semantic_search": [("query", ""), ("scope", "node")],
            "source_search": [("query", ""), ("domain", ""), ("topk", "5")],
            "source_outline": [("source_id", ""), ("query", ""), ("start_line", "1"), ("limit", "40")],
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

    def test_outline_identity_and_pagination_contracts(self):
        schema = self.schemas["source_outline"]
        sid = "S-" + "a" * 24
        for extra in [{}, {"query": ""}, {"query": "Table 10", "limit": 80, "start_line": "120"}]:
            self.assertEqual("", contracts.validate({"source_id": sid, **extra}, schema))
        for bad in [{}, {"source_id": "../private/file"}, {"source_id": sid, "limit": 81},
                    {"source_id": sid, "limit": "81"}, {"source_id": sid, "start_line": 0},
                    {"source_id": sid, "limit": True}, {"source_id": sid, "domain": "private"}]:
            self.assertTrue(contracts.validate(bad, schema), bad)

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

    def test_no_expressions_in_legacy_operands_or_executable_operations(self):
        for operation, operands in [("eval", [1]), ("sum", ["__import__('os')"]), ("sum", [{"code": "1+1"}])]:
            with self.assertRaises(ValueError):
                contracts.calculate(operation, operands)

    def test_complete_schedule_from_original_parameters(self):
        formula = "d_model**(-0.5)*min(step**(-0.5),step*warmup**(-1.5))"
        expected = {1000: "0.0001746928107421710700319666928696309558938",
                    4000: "0.0006987712429686842801278667714785238235753",
                    8000: "0.0004941058844013092706248271163176122708935"}
        rates = {}
        for step, reference in expected.items():
            variables = {"d_model": 512, "step": step, "warmup": 4000}
            out = contracts.calculate("expression", expression=formula, variables=variables)
            rates[step] = Decimal(out["result"])
            self.assertLess(abs(rates[step]-Decimal(reference)), Decimal("1e-36"))
            self.assertEqual(out["variables"], variables)
            self.assertEqual(out["expression"], formula)
            self.assertFalse(out["fact_evidence"])
        self.assertLess(rates[1000], rates[8000])
        self.assertLess(rates[8000], rates[4000])

    def test_independent_threshold_and_decimal_literal_fidelity(self):
        out = contracts.calculate("expression", expression="epsilon*norm/sqrt(d-1)",
                                  variables={"epsilon": 0.01, "norm": 10, "d": 6})
        self.assertLess(abs(Decimal(out["result"])-Decimal("0.04472135954999579392818347337462552")), Decimal("1e-34"))
        for expr, expected in [("0.1+0.2", "0.3"),
                               ("1.00000000000000000000001-1", "1E-23"),
                               ("max(-2,+3,1)/2", "1.5"),
                               (" 2**-3 ", "0.125"), ("(-2)**3", -8)]:
            self.assertEqual(contracts.calculate("expression", expression=expr)["result"], expected)

    def test_expression_rejects_nonarithmetic_syntax_and_invalid_domains(self):
        expressions = ["__import__('os').getcwd()", "x.real", "[1][0]", "[x for x in [1]]",
                       "'1'", "True", "unknown+1", "sqrt(x=4)", "abs(-1)", "sqrt()",
                       "sqrt(1,2)", "min()", "max(*[1,2])", "(lambda: 1)()", "1 if 1 else 2",
                       "1//2", "1%2", "2^3", "0x10", "1_000", "1/0", "sqrt(-1)", "(-2)**0.5"]
        for expr in expressions:
            with self.subTest(expression=expr), self.assertRaises(ValueError):
                contracts.calculate("expression", expression=expr)

    def test_expression_work_and_numerical_limits(self):
        expressions = ["1"*1025, "+"*18+"1", "+".join(["1"]*70),
                       "min("+",".join(["1"]*17)+")", "2**1001", "2**-1001",
                       "1e101", "1e-101", "1e100*2", "1e-100/2", "1e100**1000",
                       "1e-100**1000"]
        for expr in expressions:
            with self.subTest(expression=expr[:80]), self.assertRaises(ValueError):
                contracts.calculate("expression", expression=expr)
        for variables in [{"x": True}, {"x": "1"}, {"x": float("inf")}, {"x": 1e-101},
                          {"sqrt": 1}, {"_x": 1}, {"x"*33: 1}, {f"x{i}": i for i in range(17)}]:
            with self.subTest(variables=variables), self.assertRaises(ValueError):
                contracts.calculate("expression", expression="1", variables=variables)
        for expr in ["1e100", "1e-100", "1**1000", "0", "1-1"]:
            self.assertFalse(contracts.calculate("expression", expression=expr)["fact_evidence"])

    def test_expression_and_legacy_shapes_are_exclusive(self):
        for inputs in [{"operation": "expression", "expression": "1", "operands": [1]},
                       {"operation": "sum", "operands": [1], "expression": "1"},
                       {"operation": "sum", "operands": [1], "variables": {}},
                       {"operation": "expression"},
                       {"operation": "expression", "expression": "x", "variables": {"x": [1]}}]:
            with self.subTest(inputs=inputs):
                self.assertTrue(contracts.validate(inputs, self.schemas["calculate"]))
                with self.assertRaises(ValueError):
                    contracts.calculate(**inputs)


if __name__ == "__main__":
    unittest.main()
