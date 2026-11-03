"""Offline regressions for aggregation, scoped edits and API repair consumption."""
from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".scripts"))
import meeting_compiler_contract as contract
import meeting_compiler_repair as repair
from dsh.meeting_compiler_agent import MeetingCompilerAgent, MeetingCompilerTask


def case():
    ir = contract.meeting_ir_template()
    ir["attendees"] = [{"person": "known", "label": "甲", "evidence_ids": ["s0001"]}]
    ir["topics"] = [{"label": "知识库", "predicate": "讨论", "evidence_ids": ["s0001"]}]
    ir["decisions"] = [{"text": "继续验证知识库", "evidence_ids": ["s0099"]}]
    ir["relations"] = [
        {"subject": "known", "predicate": "指导", "object": "", "evidence_ids": []},
        {"subject": "another", "predicate": "指导", "object": "", "evidence_ids": []},
    ]
    ir["person_updates"] = [{"person": "academic/wiki/authors/student", "person_label": "学生甲",
                             "kind": "next_step", "text": "验证知识库", "evidence_ids": ["s0001"]}]
    preprocess = {"protocol_version": contract.PROTOCOL_VERSION, "transcript_replacements": [], "entity_resolutions": []}
    text = ('<<<META>>>\ntitle: 会议\ndoc_type: meeting\n<<</META>>>\n<<<PREPROCESS>>>\n'
            + json.dumps(preprocess) + '\n<<<WIKI>>>\n# 会议\n## Navigation\n已核对正文保持不变。\n## Content\n正文。\n<<<MEETING_IR>>>\n'
            + json.dumps(ir, ensure_ascii=False))
    context = {"source_text": "学生甲继续验证知识库。",
               "evidence_catalog": [{"evidence_id": "s0001", "line": 1, "text": "学生甲继续验证知识库。"}],
               "guidance_catalog": {"protocol_version": "student-guidance-candidates-v1",
                                    "students": [{"student_key": "student", "label": "学生甲",
                                                  "people_page": "academic/wiki/authors/student"}]}}
    return text, ir, context


def patch_for(request):
    edits = []
    for row in request["records"]:
        value = copy.deepcopy(row["record"])
        if row["section"] == "relations":
            value = None
        elif row["section"] == "decisions":
            value["evidence_ids"] = ["s0001"]
        elif row["section"] == "person_updates":
            value["person"] = "student"
        edits.append({key: row[key] for key in ("section", "index", "record_sha256")} | {
            "replacement": value, "reason": "使用来源与学生目录；未知关系不入图",
        })
    return {"schema": repair.PATCH_VERSION, "base_sha256": request["base_sha256"], "edits": edits}


def test_aggregate_independent_errors_and_patch_preserves_other_content():
    text, original, context = case()
    proposal, error, diagnostic = repair.inspect_output(text, **context)
    assert proposal is None and error
    issues = diagnostic["issues"]
    assert any("relations[0]" in issue for issue in issues)
    assert any("decisions[0].evidence_ids" in issue for issue in issues)
    assert any("person_updates[0].person" in issue for issue in issues)
    request = repair.prepare_row_repair(text, diagnostic)
    assert len(request["records"]) == 4
    merged = repair.apply_row_repair(text, request, json.dumps(patch_for(request)))
    proposal, error, diagnostic = repair.inspect_output(merged, **context)
    assert proposal is not None, (error, diagnostic)
    assert merged.split(contract.MEETING_IR_DELIMITER)[0] == text.split(contract.MEETING_IR_DELIMITER)[0]
    ir = proposal["meeting_ir"]
    assert ir["relations"] == []  # Two removals use original indexes, not shifting indexes.
    for section in ("attendees", "topics", "reports", "tasks"):
        assert ir[section] == original[section]
    assert ir["person_updates"][0]["text"] == original["person_updates"][0]["text"]
    # An invalid relation must not prevent detecting an unsupported term elsewhere.
    malformed = text.replace('"label": "知识库"', '"label": "UNSUPPORTED"')
    issues = repair.inspect_output(malformed, **context)[2]["issues"]
    assert any("unsupported acronym" in issue for issue in issues)


def test_patch_rejects_tampering_or_expanded_scope():
    text, _ir, context = case()
    request = repair.prepare_row_repair(text, repair.inspect_output(text, **context)[2])
    valid = patch_for(request)
    corruptions = []
    for field, value in (("base_sha256", "wrong"), ("schema", "wrong"), ("wiki", "changed")):
        candidate = copy.deepcopy(valid); candidate[field] = value; corruptions.append(candidate)
    candidate = copy.deepcopy(valid); candidate["edits"].pop(); corruptions.append(candidate)
    candidate = copy.deepcopy(valid); candidate["edits"][1] = candidate["edits"][0]; corruptions.append(candidate)
    for field, value in (("record_sha256", "wrong"), ("section", "topics"), ("index", -1),
                         ("index", True), ("reason", ""), ("extra", "unexpected")):
        candidate = copy.deepcopy(valid); candidate["edits"][0][field] = value; corruptions.append(candidate)
    candidate = copy.deepcopy(valid)
    next(row for row in candidate["edits"] if row["section"] == "decisions")["replacement"] = None
    corruptions.append(candidate)
    for candidate in corruptions:
        try:
            repair.apply_row_repair(text, request, json.dumps(candidate))
        except ValueError:
            pass
        else:
            raise AssertionError("invalid or out-of-scope patch accepted")
    try:
        repair.apply_row_repair(text + "\n", request, json.dumps(valid))
    except ValueError:
        pass
    else:
        raise AssertionError("stale full output must fail")
    outside = {"stage": "validate_candidate", "issues": ["缺少 ## Content"]}
    assert repair.prepare_row_repair(text, outside) is None
    # Unbound PREPROCESS work cannot be hidden by selecting just the IR rows.
    source_error = text.replace('"transcript_replacements": []',
        '"transcript_replacements": [{"original":"缺失", "replacement":"替换", "reason":"来源"}]')
    assert repair.prepare_row_repair(source_error, repair.inspect_output(source_error, **context)[2]) is None
    too_many = {"stage": "validate_candidate", "issues": [f"MEETING_IR.relations[{i}] has invalid fields" for i in range(17)]}
    expanded = json.loads(text.split(contract.MEETING_IR_DELIMITER, 1)[1])
    expanded["relations"] = expanded["relations"][:1] * 17
    expanded_text = text.split(contract.MEETING_IR_DELIMITER, 1)[0] + contract.MEETING_IR_DELIMITER + json.dumps(expanded)
    assert repair.prepare_row_repair(expanded_text, too_many) is None


def test_api_repairs_once_and_revalidates_assembled_candidate():
    text, _ir, context = case()
    task = MeetingCompilerTask("txn", "source.txt", "meeting", "raw/source.txt", "ctx", "original full prompt", **context)
    first = MeetingCompilerAgent(task, lambda *args, **kwargs: {"ok": True, "text": text}).run()
    assert first.status == "rejected"
    retry = replace(task, previous_output=text, previous_diagnostic=first.diagnostic, errors=(first.reason,))
    calls = []

    def llm(prompt, **kwargs):
        calls.append((prompt, kwargs))
        assert "已核对正文保持不变" not in prompt and "original full prompt" not in prompt
        assert context["source_text"] in prompt
        request = json.loads(prompt.rsplit("[修复请求]\n", 1)[1])
        return {"ok": True, "text": json.dumps(patch_for(request), ensure_ascii=False)}

    result = MeetingCompilerAgent(retry, llm).run()
    assert result.status == "compiled", result.diagnostic
    assert len(calls) == 1 and calls[0][1]["messages"] is None and calls[0][1]["retries"] == 0
    assert json.loads(result.response_text)["schema"] == repair.PATCH_VERSION
    assert result.assembled_output and result.repair_request
    assert result.proposal["meeting_ir"]["person_updates"][0]["person"] == "student"
    assert "response_text" not in result.trace() and "assembled_output" not in result.trace()

    def bad_evidence(prompt, **kwargs):
        request = json.loads(prompt.rsplit("[修复请求]\n", 1)[1])
        patch = patch_for(request)
        next(row for row in patch["edits"] if row["section"] == "decisions")["replacement"]["evidence_ids"] = ["s9999"]
        return {"ok": True, "text": json.dumps(patch)}

    invalid = MeetingCompilerAgent(retry, bad_evidence).run()
    assert invalid.status == "rejected" and "decisions[0].evidence_ids" in str(invalid.diagnostic)
    scope_error = MeetingCompilerAgent(retry, lambda *args, **kwargs: {"ok": True, "text": "{}"}).run()
    assert scope_error.status == "rejected" and scope_error.assembled_output == text


def test_student_projection_removal_requires_no_existing_target():
    text, _ir, context = case()
    diagnostic = repair.inspect_output(text, **context)[2]
    existing = context["guidance_catalog"]["students"][0]
    # Known key, page (with or without .md), label, or page stem independently
    # protects a student record even when the other identity field is wrong.
    for student in (
        existing,
        {"student_key": "student", "label": "另一姓名", "people_page": ""},
        {"student_key": "other-key", "label": "另一姓名", "people_page": existing["people_page"]},
        {"student_key": "other-key", "label": "另一姓名", "people_page": existing["people_page"] + ".md"},
        {"student_key": "other-key", "label": existing["label"], "people_page": ""},
    ):
        catalog = {"protocol_version": "student-guidance-candidates-v1", "students": [student]}
        request = repair.prepare_row_repair(text, diagnostic, guidance_catalog=catalog)
        assert not next(row for row in request["records"] if row["section"] == "person_updates")["allow_remove"]
    assert not repair._unmatched_student_projection(
        {"person": "student", "person_label": "错误姓名"}, context["guidance_catalog"])

    unknown_catalog = {"protocol_version": "student-guidance-candidates-v1", "students": [
        {"student_key": "other-key", "label": "另一姓名", "people_page": "academic/wiki/authors/other"}]}
    request = repair.prepare_row_repair(text, diagnostic, guidance_catalog=unknown_catalog)
    target = next(row for row in request["records"] if row["section"] == "person_updates")
    assert target["allow_remove"] is True
    patch = patch_for(request)
    next(row for row in patch["edits"] if row["section"] == "person_updates")["replacement"] = None
    merged = repair.apply_row_repair(text, request, json.dumps(patch))
    proposal, error, detail = repair.inspect_output(merged, **(context | {"guidance_catalog": unknown_catalog}))
    assert proposal is not None, (error, detail)
    assert proposal["meeting_ir"]["person_updates"] == []
    assert merged.split(contract.MEETING_IR_DELIMITER)[0] == text.split(contract.MEETING_IR_DELIMITER)[0]

    for catalog in (None, {}, {"students": []}, {"protocol_version": "student-guidance-candidates-v1"},
                    {"protocol_version": "student-guidance-candidates-v1", "students": [{}]},
                    context["guidance_catalog"]):
        protected = repair.prepare_row_repair(text, diagnostic, guidance_catalog=catalog)
        try:
            repair.apply_row_repair(text, protected, json.dumps(patch))
        except ValueError:
            pass
        else:
            raise AssertionError("student deletion requires a valid, unmatched catalog")
    for person, label in (("", "学生甲"), ("student", ""), (None, "学生甲")):
        assert not repair._unmatched_student_projection({"person": person, "person_label": label}, unknown_catalog)
    # Even program metadata cannot authorize deletion of a core decision.
    next(row for row in request["records"] if row["section"] == "decisions")["allow_remove"] = True
    next(row for row in patch["edits"] if row["section"] == "decisions")["replacement"] = None
    try:
        repair.apply_row_repair(text, request, json.dumps(patch))
    except ValueError:
        pass
    else:
        raise AssertionError("core records cannot be removed")


def test_failed_patch_calls_preserve_complete_resume_base():
    text, _ir, context = case()
    diagnostic = repair.inspect_output(text, **context)[2]
    task = MeetingCompilerTask("txn", "source.txt", "meeting", "raw/source.txt", "ctx", "prompt",
                               previous_output=text, previous_diagnostic=diagnostic,
                               errors=("invalid meeting IR",), **context)
    cases = [
        (task, {"ok": False, "error": "transport_error", "text": "partial patch"}, "transport_error"),
        (replace(task, budget=replace(task.budget, max_elapsed_sec=-1)),
         {"ok": True, "text": "partial patch"}, "time_budget_exhausted"),
        (replace(task, budget=replace(task.budget, max_model_calls=1)),
         {"ok": True, "text": "partial patch", "history": [{"model": "a"}, {"model": "b"}]},
         "model_call_budget_exhausted"),
        (task, {"status": "agent_required", "text": "partial patch"}, "host_agent_required"),
    ]
    for current, response, reason in cases:
        result = MeetingCompilerAgent(current, lambda *args, **kwargs: response).run()
        assert result.reason == reason
        assert result.assembled_output == text and result.repair_request
        assert result.response_text == "partial patch"


def main():
    test_aggregate_independent_errors_and_patch_preserves_other_content()
    test_patch_rejects_tampering_or_expanded_scope()
    test_api_repairs_once_and_revalidates_assembled_candidate()
    test_student_projection_removal_requires_no_existing_target()
    test_failed_patch_calls_preserve_complete_resume_base()
    print("meeting compiler row repair: 5/5 PASS")


if __name__ == "__main__":
    main()
