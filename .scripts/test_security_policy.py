#!/usr/bin/env python3
"""Public threat-model and source-data control-boundary regression."""
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def test_agent_constitution_treats_source_instructions_as_data() -> None:
    text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    assert "不可信数据面" in text
    assert "不得触发代码执行、远程传输" in text


def test_public_security_policy_has_private_reporting_and_support_scope() -> None:
    text = (REPO / "operations/engineering/open-source-assets/SECURITY.md").read_text(encoding="utf-8")
    assert "Private Vulnerability Reporting" in text
    assert "Supported versions" in text
    assert "docs/security/threat-model.md" in text


def test_threat_model_covers_declared_boundaries_and_residual_parser_risk() -> None:
    text = (REPO / "operations/engineering/open-source-assets/docs/security/threat-model.md").read_text(encoding="utf-8")
    for threat in range(1, 11):
        assert f"T{threat}" in text
    assert "does not yet claim parser sandboxing" in text
    assert "never become control instructions" in text


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
