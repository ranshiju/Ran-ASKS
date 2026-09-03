#!/usr/bin/env python3
"""Regression tests for the versioned typed recovery policy."""
import recovery_policy as rp


def test_limits_are_typed_and_independent():
    limits = rp.normalize_limits({"infrastructure": 2, "semantic_revision": 0})
    assert limits["infrastructure"] == 2
    assert limits["output_transport"] == 1
    assert limits["semantic_revision"] == 0


def test_unknown_and_negative_limits_are_rejected():
    for value in ({"generic_retry": 1}, {"subagent": -1}):
        try:
            rp.normalize_limits(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid policy accepted: {value}")


def test_legacy_stage_counters_migrate_without_becoming_api_counts():
    state = {"wiki_retry": 1, "slots_retry": 2}
    recovery = rp.ensure_state(state)
    assert recovery["policy_version"] == rp.POLICY_VERSION
    assert recovery["attempts"] == {"wiki_revision": 1, "semantic_revision": 2}
    assert "llm_calls" not in recovery


def test_budget_exhaustion_is_per_class_and_persistent():
    state = {}
    limits = rp.normalize_limits({"wiki_revision": 1, "semantic_revision": 1})
    assert rp.consume(state, "wiki_revision", limits, "bad wiki")
    assert not rp.consume(state, "wiki_revision", limits, "same bad wiki")
    assert state["recovery"]["last_action"]["category"] == "wiki_revision"
    assert state["recovery"]["last_action"]["outcome"] == "exhausted"
    assert rp.consume(state, "semantic_revision", limits, "bad slots")
    assert rp.remaining(state, "wiki_revision", limits) == 0
    assert state["recovery"]["last_action"]["category"] == "semantic_revision"
    assert state["recovery"]["attempts"] == {
        "wiki_revision": 1, "semantic_revision": 1,
    }


def test_llm_limits_only_accept_client_owned_classes():
    assert rp.llm_limits(0, {"infrastructure": 2}) == {
        "infrastructure": 2, "output_transport": 0,
    }
    try:
        rp.llm_limits(1, {"semantic_revision": 1})
    except ValueError:
        pass
    else:
        raise AssertionError("LLM client accepted pipeline-owned recovery class")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"  {test.__name__}: PASS")
    print(f"recovery policy regression: {len(tests)}/{len(tests)} PASS")
