#!/usr/bin/env python3

import importlib.util
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("e2b_run", HERE / "run.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class E2bRunnerTests(unittest.TestCase):
    def test_select_preamble_abstract_prefers_scientific_block(self):
        text = """# A title

Long University Department and Laboratory affiliation text with corresponding author details that are not an abstract and should be rejected by the selector because this paragraph mainly lists institutions.

We propose a quantum tensor method for an emerging graph-learning problem. The method connects a physical representation with machine-learning applications and demonstrates consistent numerical results on several scientific tasks.
"""
        selected = MODULE.select_preamble_abstract(text)
        self.assertTrue(selected.startswith("We propose"))

    def test_choose_negative_matches_stratum_and_domain(self):
        papers = {
            "P1": {"paper_id": "P1", "year": 2020, "macro_domain": "TN-core"},
            "P2": {"paper_id": "P2", "year": 2020, "macro_domain": "many-body"},
            "P3": {"paper_id": "P3", "year": 2021, "macro_domain": "TN-core"},
            "P4": {"paper_id": "P4", "year": 2024, "macro_domain": "TN-core"},
        }
        selected, macro, unused = MODULE.choose_negative(
            papers,
            {"P1"},
            papers["P1"],
            set(),
            "hub",
            7,
            [[2010, 2018], [2019, 2021], [2022, 2026]],
        )
        self.assertEqual(selected["paper_id"], "P3")
        self.assertTrue(macro)
        self.assertTrue(unused)

    def test_fallback_abstract_after_repeated_cover_material(self):
        text = """## unrelated cover entry

Other material.

# A quantum method for emerging graph learning

Authors and affiliations

We propose a quantum method for emerging graph learning that connects graph representations with variational circuits. Numerical results demonstrate the method on scientific graph tasks and show how the interdisciplinary construction supports a new application domain.

Keywords: quantum, graphs

## 1. Introduction

Long introduction.
"""
        abstract = MODULE.fallback_abstract_after_title(text, "A quantum method for emerging graph learning")
        self.assertTrue(abstract.startswith("We propose"))

    def test_metadata_is_not_scientific_evidence(self):
        metadata = "Copyright 2026. Exclusive licensee University Laboratory. Distributed under a Creative Commons license."
        self.assertFalse(MODULE.evidence_quality(metadata))

    def test_fallback_prefers_first_full_title_occurrence(self):
        text = """# Repeated scientific paper title

Authors

We propose a quantum model and demonstrate numerical results for an interdisciplinary physics application. The method provides a compact algorithm for the scientific problem and is evaluated through simulation.

## Introduction

Body.

# Repeated scientific paper title

Copyright and citation metadata only.
"""
        abstract = MODULE.fallback_abstract_after_title(text, "Repeated scientific paper title")
        self.assertTrue(abstract.startswith("We propose"))

    def test_validate_judge_response(self):
        hub = {
            "hub_id": "H1",
            "membership_trials": [{"trial_id": "M1"}],
            "set_trial": None,
        }
        obj = {
            "schema": MODULE.MEMBERSHIP_SCHEMA,
            "hub_id": "H1",
            "membership": [{
                "trial_id": "M1",
                "choice": "a",
                "fit_scores": {"A": 5, "B": 2},
                "relation_basis": ["method", "cross_field_bridge"],
                "cross_or_emerging": True,
                "confidence": 4,
                "reason": "The method connects the paper to the Hub.",
            }],
            "set_coherence": None,
        }
        normalized = MODULE.validate_judge_response(obj, hub)
        self.assertEqual(normalized["membership"][0]["choice"], "A")

    def test_exact_sign_flip(self):
        result = MODULE.exact_sign_flip([1.0, 1.0, 1.0])
        self.assertAlmostEqual(result["observed_minus_null"], 0.5)
        self.assertAlmostEqual(result["exact_one_sided_p"], 0.125)
        self.assertEqual(result["sign_flips"], 8)

    def test_kappa_perfect_agreement(self):
        pairs = [("true", "true"), ("control", "control"), ("tie", "tie")]
        self.assertAlmostEqual(MODULE.cohens_kappa(pairs), 1.0)


if __name__ == "__main__":
    unittest.main()
