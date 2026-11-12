#!/usr/bin/env python3
"""API-only semantic adapter; shared packets and commits live in knowledge_compile."""
import json
import re
import time

import agent_task
import knowledge_compile as kc
import knowledge_store as ks
import source_ingest as si

REQUEST_POLICY = "request-bound-job-v1"
API_INSTRUCTIONS = "\n本次 API 请求的 job_id 由程序绑定；输出 schema、operations 和必要 unresolved 即可，不必复述 job_id。其余标识仍使用包内原值。\n"


def bind_job(value, job_id):
    """Only the trusted request chooses the destination; semantic fields stay intact."""
    return {**value, "job_id": job_id} if isinstance(value, dict) else value


def request_binding(state):
    return {"job_id": state["job_id"], "packet_sha256": state["packet_sha256"],
            "policy": REQUEST_POLICY}


def response_delta(response, state):
    value = response.get("parsed")
    if "request_binding" in response:
        if response["request_binding"] != request_binding(state):
            raise ValueError("saved API response request/packet binding mismatch")
        value = bind_job(value, state["job_id"])
    if not kc.delta_schema(value) or value["job_id"] != state["job_id"]:
        raise ValueError("invalid API delta schema/job identity")
    return value


def selective_usage(state, response, filename):
    """Account for saved attempts once, including transport retries and failures."""
    attempts = state.setdefault("api_attempts", [])
    if not any(a["response_file"] == filename for a in attempts):
        history = response.get("history", [])
        usages = [h["usage"] for h in history if h.get("usage")]
        if not usages:
            usages = [response.get("usage") or {}]
        attempts.append({"response_file": filename, "model": response.get("model"),
                         "usage": {k: sum(u.get(k, 0) or 0 for u in usages)
                                   for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
                         "history": history, "elapsed_sec": response.get("elapsed_sec", 0)})
    state["api_usage"] = {"model": response.get("model"),
        "usage": {k: sum(a["usage"][k] for a in attempts) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
        "history": [h for a in attempts for h in a["history"]],
        "elapsed_sec": sum(a["elapsed_sec"] for a in attempts)}


def run_selective(job_id, *, repo, retry=False):
    import llm_structured as llm
    folder = kc.job_dir(repo, job_id)
    new_calls = 0
    with ks.lock(repo, job_id):
        state, packet = kc.load_job(repo, job_id)
        if state["status"] == "completed" or state.get("reading_exhausted"):
            return {**kc.task_view(repo, state, packet), "reused": True, "new_model_calls": 0}
        kc.validate_comparison_snapshots(repo, state, packet)
        while not (folder/"delta.json").exists():
            filename = state.get("response_file")
            if filename and not re.fullmatch(r"round-\d+-response-\d+\.json", filename):
                raise ValueError("invalid selective response filename")
            response_path = si.managed(repo, str((folder/filename).relative_to(repo))) if filename else None
            response = json.loads(response_path.read_text()) if response_path and response_path.exists() else None
            if response and not response.get("ok") and retry:
                selective_usage(state, response, filename)
                response = None
            if response is None and state["status"] in {"running", "failed"} and not retry:
                return {**kc.task_view(repo, state, packet), "retry_required": True, "new_model_calls": new_calls}
            try:
                if response is None:
                    round_id = packet["reading"]["round"]
                    attempt = len(list(folder.glob(f"round-{round_id}-response-*.json")))+1
                    filename = f"round-{round_id}-response-{attempt}.json"
                    state.update(status="running", response_file=filename)
                    kc.save_state(repo, state)
                    started = time.perf_counter()
                    response = llm.call_json(kc.instructions(packet)+API_INSTRUCTIONS+"\n材料包：\n"+json.dumps(packet, ensure_ascii=False),
                        lambda v: kc.read_request_schema(v) or kc.delta_schema(bind_job(v, job_id)),
                        operation="ingest_knowledge_selective", transaction_id=job_id, reasoning="standard", max_tokens=4096,
                        recovery_limits={"infrastructure": 1, "output_transport": 0}, timeout_sec=90)
                    new_calls += len(response.get("history", [])) or int(response.get("event_kind") == "llm_api_call")
                    response = {**response, "request_binding": request_binding(state), "elapsed_sec": time.perf_counter()-started}
                    si.atomic_json(folder/filename, response)
                if response.get("request_binding") != request_binding(state):
                    raise ValueError("saved API response request/packet binding mismatch")
                selective_usage(state, response, filename)
                kc.save_state(repo, state)
                if not response.get("ok"):
                    raise ValueError(response.get("error") or "API failure")
                value = response.get("parsed")
                if kc.read_request_schema(value):
                    if packet["reading"]["round"] >= packet["reading"]["max_expansions"]:
                        state.update(status="partial", reading_exhausted=True,
                                     unresolved=["补读额度耗尽；仍缺必要依据："+value["reason"]])
                        kc.save_state(repo, state)
                        kc.notify_source(repo, state)
                        return {**kc.task_view(repo, state, packet), "new_model_calls": new_calls}
                    packet = kc.expand_locked(repo, state, packet, value)
                    retry = False
                    continue
                si.atomic_json(folder/"delta.json", response_delta(response, state))
                state["status"] = "generated"
                kc.save_state(repo, state)
            except Exception as exc:
                state.update(status="failed", error=str(exc)[:700])
                kc.save_state(repo, state)
                kc.notify_source(repo, state)
                return {**kc.task_view(repo, state, packet), "new_model_calls": new_calls}
    return {**kc.apply(job_id, repo=repo), "new_model_calls": new_calls}


def run(source_id, *, repo=si.REPO, focus="", refresh=False, retry=False, target=None, source_locators=None, comparison_ids=None, strategy="survey"):
    if agent_task.ingest_backend() != "api":
        raise ValueError("API compilation requires INGEST_BACKEND=api; host Agent uses prepare/check/apply")
    import llm_structured as llm
    config = llm.load_env()
    executor = "api:" + llm.configured_model(config)
    profile_hash = si.fingerprint({k:config.get(k, "") for k in ("LLM_MODEL", "LLM_API_BASE", "LLM_API_PATH")})[:16]
    prepared = kc.prepare(source_id, repo=repo, focus=focus, executor=executor+":"+profile_hash+":"+REQUEST_POLICY, refresh=refresh,
                          target=target, source_locators=source_locators, comparison_ids=comparison_ids, strategy=strategy)
    job_id = prepared["job_id"]
    if strategy == "selective":
        return run_selective(job_id, repo=repo, retry=retry)
    folder = kc.job_dir(repo, job_id)
    new_model_calls = 0
    with ks.lock(repo, job_id):
        state, packet = kc.load_job(repo, job_id)
        if state["status"] == "completed":
            return {**kc.task_view(repo, state, packet), "reused": True, "new_model_calls": 0}
        kc.validate_comparison_snapshots(repo, state, packet)
        saved_response = folder/"model-response.json"
        if not (folder/"delta.json").exists() and saved_response.exists():
            response = json.loads(saved_response.read_text())
            if response.get("ok"):
                try:
                    si.atomic_json(folder/"delta.json", response_delta(response, state))
                except ValueError as exc:
                    state.update(status="failed", error=str(exc))
                    kc.save_state(repo, state)
                    kc.notify_source(repo, state)
                    return {**kc.task_view(repo, state, packet), "new_model_calls": 0}
        if not (folder/"delta.json").exists():
            if state["status"] in {"running", "failed"} and not retry:
                return {**kc.task_view(repo, state, packet), "retry_required": True, "new_model_calls": 0}
            state["status"] = "running"
            kc.save_state(repo, state)
            started = time.perf_counter()
            try:
                response = llm.call_json(kc.instructions(packet)+API_INSTRUCTIONS+"\n材料包：\n"+json.dumps(packet, ensure_ascii=False),
                    lambda value: kc.delta_schema(bind_job(value, job_id)),
                    operation="ingest_knowledge_delta", transaction_id=job_id, reasoning="standard", max_tokens=4096,
                    recovery_limits={"infrastructure": 1, "output_transport": 0}, timeout_sec=90)
                new_model_calls = len(response.get("history", [])) or int(response.get("event_kind") == "llm_api_call")
                response = {**response, "request_binding": request_binding(state)}
                si.atomic_json(folder/"model-response.json", response)
                state["api_usage"] = {"model": response.get("model"), "usage": response.get("usage", {}),
                    "history": response.get("history", []), "elapsed_sec": time.perf_counter()-started}
                if not response.get("ok"):
                    raise ValueError(response.get("error") or response.get("status", "API failure"))
                si.atomic_json(folder/"delta.json", response_delta(response, state))
                state["status"] = "generated"
            except Exception as exc:
                state.update(status="failed", error=str(exc)[:700])
                kc.save_state(repo, state)
                kc.notify_source(repo, state)
                return {**kc.task_view(repo, state, packet), "new_model_calls": new_model_calls}
            kc.save_state(repo, state)
    return {**kc.apply(job_id, repo=repo), "new_model_calls": new_model_calls}
