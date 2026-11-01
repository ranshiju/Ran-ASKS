#!/usr/bin/env python3
"""API-only semantic adapter; shared packets and commits live in knowledge_compile."""
import json
import time

import agent_task
import knowledge_compile as kc
import knowledge_store as ks
import source_ingest as si


def run(source_id, *, repo=si.REPO, focus="", refresh=False, retry=False):
    if agent_task.ingest_backend() != "api":
        raise ValueError("API compilation requires INGEST_BACKEND=api; host Agent uses prepare/check/apply")
    import llm_structured as llm
    config = llm.load_env()
    executor = "api:" + llm.configured_model(config)
    profile_hash = si.fingerprint({k:config.get(k, "") for k in ("LLM_MODEL", "LLM_API_BASE", "LLM_API_PATH")})[:16]
    prepared = kc.prepare(source_id, repo=repo, focus=focus, executor=executor+":"+profile_hash, refresh=refresh)
    job_id = prepared["job_id"]
    folder = kc.job_dir(repo, job_id)
    new_model_calls = 0
    with ks.lock(repo, job_id):
        state, packet = kc.load_job(repo, job_id)
        if state["status"] == "completed":
            return {**kc.task_view(repo, state, packet), "reused": True, "new_model_calls": 0}
        saved_response = folder/"model-response.json"
        if not (folder/"delta.json").exists() and saved_response.exists():
            response = json.loads(saved_response.read_text())
            if response.get("ok") and kc.delta_schema(response.get("parsed")):
                si.atomic_json(folder/"delta.json", response["parsed"])
        if not (folder/"delta.json").exists():
            if state["status"] in {"running", "failed"} and not retry:
                return {**kc.task_view(repo, state, packet), "retry_required": True, "new_model_calls": 0}
            state["status"] = "running"
            kc.save_state(repo, state)
            started = time.perf_counter()
            try:
                response = llm.call_json(kc.RULES+"\n材料包：\n"+json.dumps(packet, ensure_ascii=False), kc.delta_schema,
                    operation="ingest_knowledge_delta", transaction_id=job_id, reasoning="standard", max_tokens=4096,
                    recovery_limits={"infrastructure": 1, "output_transport": 0}, timeout_sec=90)
                new_model_calls = len(response.get("history", [])) or int(response.get("event_kind") == "llm_api_call")
                si.atomic_json(folder/"model-response.json", response)
                state["api_usage"] = {"model": response.get("model"), "usage": response.get("usage", {}),
                    "history": response.get("history", []), "elapsed_sec": time.perf_counter()-started}
                if not response.get("ok"):
                    raise ValueError(response.get("error") or response.get("status", "API failure"))
                si.atomic_json(folder/"delta.json", response["parsed"])
                state["status"] = "generated"
            except Exception as exc:
                state.update(status="failed", error=str(exc)[:700])
                kc.save_state(repo, state)
                kc.notify_source(repo, state)
                return {**kc.task_view(repo, state, packet), "new_model_calls": new_model_calls}
            kc.save_state(repo, state)
    return {**kc.apply(job_id, repo=repo), "new_model_calls": new_model_calls}
