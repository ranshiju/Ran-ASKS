"""统一的受限 LLM 调用与结构化输出校验工具。

通过 QUERY_BACKEND 显式选择 query 使用 agent 或 api，默认 agent。摄入 API 可为
关键词选择与格式修复配置专用模型；专用模型失败时回退主模型，不切换到 Agent。
API 调用按 fast/standard/deep/xdeep 档位控制 provider 推理强度；输出与重试预算由调用方独立指定。
摄入 Worker 可根据任务类型与确定性校验错误做一次受控升档。
结构化结果校验失败时局部重试，失败结果不得入库。
"""
from __future__ import annotations
import json
import os
import re
import time
import hashlib
import urllib.request
import urllib.error
import uuid
from pathlib import Path

import recovery_policy as rp

REPO = Path(__file__).resolve().parent.parent
EVENTS_DIR = REPO / "temp" / "llm-events"
EXECUTION_EVENT_VERSION = "execution-event-v1"


def _log_event(operation: str, result: dict, prompt: str, output_text: str = "",
               transaction_id: str = ""):
    """Persist one canonical execution event and return the event if written.

    只记 hash + length，不记完整 prompt/output（raw/ 已是事实唯一源，日志只做轨迹）。
    写盘失败静默降级，不阻断主流程。
    """
    try:
        ph = hashlib.sha256(prompt.encode()).hexdigest()[:12] if prompt else ""
        oh = hashlib.sha256(output_text.encode()).hexdigest()[:12] if output_text else ""
        event = {
            "event_version": EXECUTION_EVENT_VERSION,
            "event_id": result.get("event_id") or uuid.uuid4().hex,
            "event_kind": result.get("event_kind") or (
                "llm_api_call" if result.get("mode") == "api" and result.get("model") else "control"
            ),
            "event_source": "llm_structured",
            "call_id": result.get("call_id", ""),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "transaction_id": transaction_id,
            "operation": operation,
            "mode": result.get("mode", ""),
            "model": result.get("model", ""),
            "profile": result.get("profile", ""),
            "reasoning_profile": result.get("reasoning_profile", ""),
            "reasoning_reason": result.get("reasoning_reason", ""),
            "reasoning_error_class": result.get("reasoning_error_class", ""),
            "provider_reasoning_effort": result.get("provider_reasoning_effort", ""),
            "attempt": result.get("attempt", 0),
            "status": result.get("status", ""),
            "latency_sec": result.get("latency_sec", 0),
            "finish_reason": result.get("finish_reason", ""),
            "usage": result.get("usage", {}),
            "reasoning_tokens": result.get("reasoning_tokens", 0),
            "reasoning_exhausted": result.get("reasoning_exhausted", False),
            "recovery_policy_version": result.get("recovery_policy_version", rp.POLICY_VERSION),
            "recovery_class": result.get("recovery_class", ""),
            "recovery_scheduled": result.get("recovery_scheduled", False),
            "retryable": result.get("retryable", False),
            "prompt_hash": ph,
            "prompt_len": len(prompt),
            "output_hash": oh,
            "output_len": len(output_text),
            "error": result.get("error", ""),
        }
        EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        day = time.strftime("%Y-%m-%d")
        with (EVENTS_DIR / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event
    except Exception:
        return None


def summarize_execution_events(transaction_id: str, events_dir: Path | None = None) -> dict:
    """Aggregate actual API calls from the canonical event stream on demand."""
    directory = Path(events_dir or EVENTS_DIR)
    summary = {
        "event_version": EXECUTION_EVENT_VERSION,
        "transaction_id": transaction_id,
        "api_calls": 0,
        "latency_sec": 0.0,
        "total_tokens": 0,
        "by_operation": {},
        "by_status": {},
    }
    if not directory.is_dir():
        return summary
    for path in sorted(directory.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if (event.get("event_version") != EXECUTION_EVENT_VERSION
                    or event.get("transaction_id") != transaction_id
                    or event.get("event_kind") != "llm_api_call"):
                continue
            summary["api_calls"] += 1
            summary["latency_sec"] += float(event.get("latency_sec") or 0)
            summary["total_tokens"] += int((event.get("usage") or {}).get("total_tokens") or 0)
            operation = str(event.get("operation") or "unknown")
            status = str(event.get("status") or "unknown")
            summary["by_operation"][operation] = summary["by_operation"].get(operation, 0) + 1
            summary["by_status"][status] = summary["by_status"].get(status, 0) + 1
    summary["latency_sec"] = round(summary["latency_sec"], 3)
    return summary

REASONING_POLICIES = {
    "fast": {"rank": 0},
    "standard": {"rank": 1},
    "deep": {"rank": 2},
    "xdeep": {"rank": 3},
}
OPERATION_REASONING_PROFILES = {
    "ingest_type_review": "fast",
    "ingest_relation_review": "fast",
    "ingest_api_keywords": "fast",
    "ingest_api_repair": "fast",
    "ingest_api_claims": "standard",
    "ingest_meeting_compile": "standard",
    "ingest_wiki_write": "standard",
    "ingest_wiki_repair": "fast",
    "ingest_semantic_extract": "standard",
    "ingest_semantic_fill": "fast",
    "ingest_semantic_recovery": "standard",
    # 命题拆分是短 JSON 抽取，不需要长链推理。
    "ingest_proposition": "fast",
    "ingest_bibliographic_review": "fast",
    "query_api_loop": "deep",
}

_SEMANTIC_ERROR_MARKERS = (
    "raw locator", "证据", "引用", "脚注", "研究方向定位", "核心结论",
    "事实", "矛盾", "遗漏", "未覆盖", "谓词", "predicate", "关系", "主体", "客体",
)


def _validation_error_class(context: dict | None) -> str:
    context = context or {}
    declared = str(context.get("failure_kind", "")).strip().lower()
    if declared in {"semantic", "structural"}:
        return declared
    errors = context.get("validation_errors") or []
    if not errors:
        return "none"
    combined = "\n".join(str(item).lower() for item in errors)
    return "semantic" if any(marker in combined for marker in _SEMANTIC_ERROR_MARKERS) else "structural"


def _upgrade_reasoning_profile(profile: str) -> str:
    return {"fast": "standard", "standard": "deep", "deep": "xdeep", "xdeep": "xdeep"}[profile]


def reasoning_decision(config: dict[str, str], operation: str,
                       requested: str | None = None,
                       context: dict | None = None) -> dict[str, str]:
    """选择推理档位；配置优先，自动策略只对语义校验失败升一级。"""
    operation_key = "LLM_REASONING_" + re.sub(r"[^A-Z0-9]+", "_", operation.upper())
    configured = (
        ("requested", requested),
        ("operation_config", config.get(operation_key)),
        ("default_config", config.get("LLM_REASONING_DEFAULT")),
    )
    source = "adaptive"
    profile = ""
    for candidate_source, candidate in configured:
        if candidate and candidate.strip():
            source = candidate_source
            profile = candidate.strip().lower()
            break
    error_class = _validation_error_class(context)
    if not profile:
        profile = OPERATION_REASONING_PROFILES.get(operation, "standard")
        retry = max(0, int((context or {}).get("retry", 0) or 0))
        if retry and error_class == "semantic" and operation in {
                "ingest_meeting_compile", "ingest_wiki_write", "ingest_semantic_extract"}:
            profile = _upgrade_reasoning_profile(profile)
            source = "adaptive_semantic_retry"
        elif retry:
            source = "adaptive_structural_retry"
        else:
            source = "adaptive_initial"
    if profile not in REASONING_POLICIES:
        raise RuntimeError(f"无效 reasoning profile '{profile}'；可用: {', '.join(REASONING_POLICIES)}")
    kind = str((context or {}).get("document_kind", "")).strip().lower() or "generic"
    return {
        "profile": profile,
        "reason": f"{source}:{operation}:{kind}",
        "error_class": error_class,
    }


def expand_env_references(values: dict[str, str]) -> dict[str, str]:
    """展开同一配置中的 `${NAME}`，避免复制 endpoint/key。"""
    pattern = re.compile(r"\$\{([A-Z0-9_]+)\}")
    expanded = dict(values)
    for _ in range(len(values) + 1):
        updated = {key: pattern.sub(lambda match: expanded.get(match.group(1), match.group(0)), value)
                   for key, value in expanded.items()}
        if updated == expanded:
            return updated
        expanded = updated
    return expanded

def load_env() -> dict[str, str]:
    values = {}
    env_file = REPO / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    known = {
        "QUERY_BACKEND", "INGEST_BACKEND", "LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL",
        "INGEST_KEYWORD_API_BASE", "INGEST_KEYWORD_API_KEY", "INGEST_KEYWORD_MODEL",
        "INGEST_REPAIR_API_BASE", "INGEST_REPAIR_API_KEY", "INGEST_REPAIR_MODEL",
        "INGEST_GENERATION_API_BASE", "INGEST_GENERATION_API_KEY", "INGEST_GENERATION_MODEL",
        "INGEST_PROPOSITION_API_BASE", "INGEST_PROPOSITION_API_KEY", "INGEST_PROPOSITION_MODEL",
        "SEMANTIC_RECOVERY_API_BASE", "SEMANTIC_RECOVERY_API_KEY", "SEMANTIC_RECOVERY_MODEL",
    }
    values.update({key: value for key, value in os.environ.items() if key in known or key.startswith("LLM_REASONING_")})
    return expand_env_references(values)


def reasoning_profile(config: dict[str, str], operation: str, requested: str | None = None,
                      context: dict | None = None) -> str:
    """兼容入口：返回 reasoning_decision 选出的档位。"""
    return reasoning_decision(config, operation, requested, context)["profile"]


def reasoning_request_options(config: dict[str, str], profile: str) -> dict[str, str]:
    """仅在调用方明确配置字段名与档位 effort 时透传 provider 专属参数。"""
    field = config.get("LLM_REASONING_FIELD", "").strip()
    effort = config.get(f"LLM_REASONING_EFFORT_{profile.upper()}", "").strip()
    if not field or not effort or effort.lower() in {"none", "off", "false", "disabled"}:
        return {}
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field):
        raise RuntimeError("LLM_REASONING_FIELD 必须是合法的 API 字段名")
    return {field: effort}


def _provider_reasoning_effort(config: dict[str, str], options: dict[str, str]) -> str:
    field = config.get("LLM_REASONING_FIELD", "").strip()
    return str(options.get(field, "")) if field else ""

def configured_model(config: dict[str, str] | None = None) -> str:
    config = config or load_env()
    return config.get("LLM_MODEL", "").strip() or "current-agent"


def api_profiles(config: dict[str, str], operation: str) -> list[dict[str, str]]:
    """返回 API 调用顺序；专用配置不完整时安全地忽略。"""
    primary = {
        "name": "primary",
        "base": config.get("LLM_API_BASE", ""),
        "key": config.get("LLM_API_KEY", ""),
        "model": config.get("LLM_MODEL", ""),
    }
    prefixes: list[str] = []
    if operation == "ingest_api_keywords":
        prefixes = ["INGEST_KEYWORD"]
    elif operation == "ingest_api_repair":
        prefixes = ["INGEST_REPAIR"]
    elif operation in {"ingest_meeting_compile", "ingest_wiki_write", "ingest_wiki_repair"}:
        prefixes = ["INGEST_GENERATION"]
    elif operation == "ingest_proposition":
        # 优先独立命题模型；未配置时复用已验证的 generation specialist，
        # 最后才回退主模型。重复 model 只保留一次。
        prefixes = ["INGEST_PROPOSITION", "INGEST_GENERATION"]
    elif operation == "ingest_semantic_recovery":
        prefixes = ["SEMANTIC_RECOVERY"]
    if not prefixes:
        return [primary]
    profiles = []
    seen_models = set()
    for prefix in prefixes:
        specialist = {
            "name": prefix.lower(),
            "base": config.get(f"{prefix}_API_BASE", ""),
            "key": config.get(f"{prefix}_API_KEY", ""),
            "model": config.get(f"{prefix}_MODEL", ""),
        }
        if all(specialist[key] for key in ("base", "key", "model")) \
                and specialist["model"] != primary["model"] \
                and specialist["model"] not in seen_models:
            profiles.append(specialist)
            seen_models.add(specialist["model"])
    profiles.append(primary)
    return profiles

def execution_mode(config: dict[str, str] | None = None) -> str:
    config = config or load_env()
    backend = config.get("QUERY_BACKEND", "agent").strip().lower() or "agent"
    if backend not in {"agent", "api"}:
        raise RuntimeError("QUERY_BACKEND 只能是 agent 或 api")
    return backend

def ingest_mode(config: dict[str, str] | None = None) -> str:
    config = config or load_env()
    mode = config.get("INGEST_BACKEND", "agent").strip().lower() or "agent"
    if mode not in {"agent", "api"}:
        raise RuntimeError("INGEST_BACKEND 只能是 agent 或 api")
    return mode

def ingest_backend_notice(config: dict[str, str] | None = None, stage: int | None = None) -> str:
    """给摄入编排器的短提示；不把后端配置重复塞进长 prompt。"""
    mode = ingest_mode(config)
    stage_text = {
        1: "阶段1编码",
        2: "阶段2巩固",
        3: "阶段3收尾",
    }.get(stage, "当前摄入")
    if mode == "agent":
        return ""
    # api
    return f"{stage_text}：LLM=API（{configured_model(config)}）；输出必须经 schema 校验，失败不得入库。"

def agent_handoff(prompt: str, schema_name: str = "structured-json") -> dict:
    """返回给当前 agent 的接管请求；子进程不能直接调用父 agent。"""
    return {
        "ok": False,
        "status": "agent_required",
        "mode": "agent",
        "model": "current-agent",
        "prompt": prompt,
        "schema": schema_name,
        "handoff_reason": "configured_agent_backend",
        "error": None,
    }

def strip_json_fence(text: str) -> str:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()

def parse_json(text: str):
    cleaned = strip_json_fence(text)
    try:
        return json.loads(cleaned), None
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败: {exc.msg} (位置 {exc.pos})"

def _classify_http_error(exc: Exception) -> tuple[bool, str]:
    """Classify request failures without retrying permanent 4xx or code errors.

    返回 (可重试, 错误标签)。
    """
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        retryable = code in (408, 425, 429) or 500 <= code < 600
        return retryable, f"HTTP {code} {exc.reason}"
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return True, type(exc).__name__ + ": " + str(exc)
    return False, type(exc).__name__ + ": " + str(exc)


def _classify_api_exception(exc: Exception) -> tuple[bool, str, str]:
    """Separate malformed provider responses from request infrastructure failures."""
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError, AttributeError)):
        return True, type(exc).__name__ + ": " + str(exc), "output_transport"
    retryable, label = _classify_http_error(exc)
    return retryable, label, "infrastructure" if retryable else "configuration"


def _backoff_sleep(attempt: int, base: float = 2.0, cap: float = 16.0) -> None:
    """指数退避：2s/4s/8s...（上限 16s）。attempt 从 1 起算。"""
    time.sleep(min(base * (2 ** max(attempt - 1, 0)), cap))


def _reasoning_token_count(usage: dict) -> int:
    try:
        details = (usage or {}).get("completion_tokens_details") or {}
        return int(details.get("reasoning_tokens") or 0)
    except (TypeError, ValueError):
        return 0


def _reasoning_exhausted(text: str, finish_reason, usage: dict) -> bool:
    """判断空输出 length 是否由思考 token 吞掉 completion 预算。"""
    if finish_reason != "length" or (text or "").strip():
        return False
    reasoning = _reasoning_token_count(usage)
    completion = int((usage or {}).get("completion_tokens") or 0)
    return reasoning > 0 and completion > 0 and reasoning >= completion * 0.7


def _retry_available(category: str, used: dict[str, int], limits: dict[str, int],
                     attempt: int, legacy_retries: int, typed: bool) -> bool:
    if typed:
        return category in limits and used.get(category, 0) < limits[category]
    return attempt < legacy_retries


def call_json(prompt: str, schema_check, *, system: str = "你是受程序约束的知识库组件，只输出要求的 JSON。", max_tokens: int = 800, retries: int = 1, recovery_limits: dict | None = None, operation: str = "query", reasoning: str | None = None, reasoning_context: dict | None = None, messages: list[dict] | None = None, transaction_id: str = "", timeout_sec: float = 90) -> dict:
    config = load_env()
    audit_prompt = (json.dumps(messages, ensure_ascii=False, sort_keys=True)
                    if messages else prompt)
    decision = reasoning_decision(config, operation, reasoning, reasoning_context)
    profile_name = decision["profile"]
    effective_max_tokens = max_tokens
    effective_retries = max(0, int(retries))
    typed_recovery = recovery_limits is not None
    client_limits = rp.llm_limits(effective_retries, recovery_limits) if typed_recovery else {}
    recovery_used = {"infrastructure": 0, "output_transport": 0}
    max_attempts = 1 + (sum(client_limits.values()) if typed_recovery else effective_retries)
    call_id = uuid.uuid4().hex
    reasoning_options = reasoning_request_options(config, profile_name)
    recovery_reasoning_options = reasoning_request_options(config, "fast")
    is_ingest = operation == "ingest" or operation.startswith("ingest_")
    mode = ingest_mode(config) if is_ingest else execution_mode(config)
    if mode == "agent":
        handoff = agent_handoff(prompt, getattr(schema_check, "__name__", "structured-json"))
        handoff.update({
            "event_kind": "agent_handoff", "call_id": call_id,
            "recovery_policy_version": rp.POLICY_VERSION,
        })
        _log_event(operation, handoff, audit_prompt, transaction_id=transaction_id)
        return handoff | ({"ingest_mode": mode} if is_ingest else {})
    profiles = api_profiles(config, operation)
    if not all(profiles[0][key] for key in ("base", "key", "model")):
        variable = "INGEST_BACKEND" if is_ingest else "QUERY_BACKEND"
        _cfg_err = {"ok": False, "status": "configuration_error", "mode": "api", "error": f"{variable}=api 时必须配置 LLM_API_BASE、LLM_API_KEY、LLM_MODEL", "event_kind": "configuration", "call_id": call_id, "recovery_policy_version": rp.POLICY_VERSION, "recovery_class": "configuration", "retryable": False}
        _log_event(operation, _cfg_err, audit_prompt, transaction_id=transaction_id)
        return _cfg_err
    last = None
    history = []
    force_low_reasoning = False
    for profile_index, profile in enumerate(profiles):
        attempt_max_tokens = effective_max_tokens
        attempt_reasoning = dict(recovery_reasoning_options if force_low_reasoning else reasoning_options)
        reasoning_strikes = 0
        for attempt in range(max_attempts):
            used_max_tokens = attempt_max_tokens
            user_prompt = prompt if attempt == 0 else f"{prompt}\n\n上次输出未通过校验：{last.get('error', 'schema failed')}。只输出修正后的 JSON，不要解释。"
            if messages:
                conv = list(messages)
                if attempt > 0:
                    conv = conv + [{"role": "user", "content": f"上次输出为空或出错：{last.get('error', 'empty output')}。请重新输出完整文本。"}]
            else:
                conv = [{"role": "system", "content": system}, {"role": "user", "content": user_prompt}]
            request_audit_prompt = json.dumps(conv, ensure_ascii=False, sort_keys=True)
            payload_data = {
                "model": profile["model"],
                "messages": conv,
                "temperature": 0,
                "max_tokens": attempt_max_tokens,
            }
            payload_data.update(attempt_reasoning)
            payload = json.dumps(payload_data).encode()
            request = urllib.request.Request(profile["base"].rstrip("/") + "/v1/chat/completions", data=payload, headers={"Authorization": "Bearer " + profile["key"], "Content-Type": "application/json"})
            started = time.time()
            fast_fail = False
            try:
                with urllib.request.urlopen(request, timeout=timeout_sec) as response:
                    data = json.loads(response.read())
                choice = (data.get("choices") or [{}])[0]
                text = (choice.get("message") or {}).get("content") or ""
                finish_reason = choice.get("finish_reason")
                usage = data.get("usage", {})
                reasoning_tokens = _reasoning_token_count(usage)
                reasoning_exhausted = _reasoning_exhausted(text, finish_reason, usage)
                obj, parse_error = parse_json(text)
                error = parse_error or (None if schema_check(obj) else "schema 校验失败")
                if error and not text.strip() and finish_reason == "length":
                    retry_same_profile = _retry_available(
                        "output_transport", recovery_used, client_limits,
                        attempt, effective_retries, typed_recovery,
                    )
                    has_later_profile = profile_index < len(profiles) - 1
                    if retry_same_profile or has_later_profile:
                        force_low_reasoning = True
                    if retry_same_profile:
                        attempt_max_tokens = min(attempt_max_tokens * 2, 65536)
                        attempt_reasoning = dict(recovery_reasoning_options)
                    if reasoning_exhausted:
                        reasoning_strikes += 1
                        error = ("空输出(推理占满预算，下一尝试降低推理强度)"
                                 if retry_same_profile or has_later_profile
                                 else "空输出(推理占满预算，已无后续尝试)")
                    else:
                        error = ("空输出(输出预算不足，下一尝试扩容)"
                                 if retry_same_profile or has_later_profile
                                 else "空输出(输出预算耗尽，已无后续尝试)")
                last = {"ok": error is None, "status": "ok" if error is None else "validation_error", "mode": "api", "model": profile["model"], "profile": profile["name"], "reasoning_profile": profile_name, "reasoning_reason": decision["reason"], "reasoning_error_class": decision["error_class"], "provider_reasoning_effort": _provider_reasoning_effort(config, attempt_reasoning), "max_tokens": used_max_tokens, "next_max_tokens": attempt_max_tokens if error and _retry_available("output_transport", recovery_used, client_limits, attempt, effective_retries, typed_recovery) else None, "attempt": attempt + 1, "text": text, "parsed": obj, "error": error, "latency_sec": round(time.time() - started, 2), "finish_reason": finish_reason, "usage": usage, "reasoning_tokens": reasoning_tokens, "reasoning_exhausted": reasoning_exhausted}
                recovery_class = "output_transport" if error else ""
                failure_retryable = bool(error)
            except Exception as exc:
                failure_retryable, err_label, recovery_class = _classify_api_exception(exc)
                fast_fail = not failure_retryable
                last = {"ok": False, "status": "api_error", "mode": "api", "model": profile["model"], "profile": profile["name"], "reasoning_profile": profile_name, "reasoning_reason": decision["reason"], "reasoning_error_class": decision["error_class"], "provider_reasoning_effort": _provider_reasoning_effort(config, attempt_reasoning), "max_tokens": attempt_max_tokens, "attempt": attempt + 1, "error": err_label, "latency_sec": round(time.time() - started, 2), "usage": {}}
            retry_same_profile = (
                failure_retryable
                and not fast_fail
                and reasoning_strikes < 2
                and _retry_available(
                    recovery_class, recovery_used, client_limits,
                    attempt, effective_retries, typed_recovery,
                )
            )
            last.update({
                "event_kind": "llm_api_call",
                "call_id": call_id,
                "recovery_policy_version": rp.POLICY_VERSION,
                "recovery_class": recovery_class,
                "recovery_scheduled": retry_same_profile,
                "retryable": failure_retryable,
            })
            history.append({key: last.get(key) for key in ("model", "profile", "reasoning_profile", "reasoning_reason", "reasoning_error_class", "provider_reasoning_effort", "max_tokens", "status", "attempt", "latency_sec", "usage", "recovery_class", "recovery_scheduled", "retryable")})
            _log_event(operation, last, request_audit_prompt, last.get("text", ""), transaction_id)
            if last["ok"]:
                last["history"] = history
                last["fallback_used"] = profile_index > 0
                last["recovery_attempts"] = dict(recovery_used)
                return last
            if not retry_same_profile:
                break
            if typed_recovery:
                recovery_used[recovery_class] += 1
            if recovery_class == "infrastructure" and "429" in str(last.get("error", "")):
                _backoff_sleep(attempt + 1)
    if last:
        last["history"] = history
        last["fallback_used"] = len(profiles) > 1
    _final = last or {"ok": False, "status": "failed", "error": "未知 LLM 错误"}
    _final["recovery_attempts"] = dict(recovery_used)
    return _final


def call_text(prompt: str, *, system: str = "你是受程序约束的知识库组件，只输出要求的文本。", max_tokens: int = 1600, retries: int = 1, recovery_limits: dict | None = None, operation: str = "query", reasoning: str | None = None, reasoning_context: dict | None = None, messages: list[dict] | None = None, transaction_id: str = "") -> dict:
    """受限 LLM 文本调用：返回原始文本，不做 JSON 解析或 schema 校验。

    用于需要长文本输出的场景（如 wiki 页面撰写 + 语义槽），镜像 call_json 的 API 调用与重试逻辑，
    但跳过 JSON 解析。agent_handoff 行为与 call_json 一致；内容校验由调用方（修复循环）负责。

    多轮对话：传入 messages（完整对话历史，含 system/user/assistant 角色条目）时，
    用它代替 system+prompt 拼装，prompt 参数仅用于重试时的提示文本。
    """
    config = load_env()
    audit_prompt = (json.dumps(messages, ensure_ascii=False, sort_keys=True)
                    if messages else prompt)
    decision = reasoning_decision(config, operation, reasoning, reasoning_context)
    profile_name = decision["profile"]
    effective_max_tokens = max_tokens
    effective_retries = max(0, int(retries))
    typed_recovery = recovery_limits is not None
    client_limits = rp.llm_limits(effective_retries, recovery_limits) if typed_recovery else {}
    recovery_used = {"infrastructure": 0, "output_transport": 0}
    max_attempts = 1 + (sum(client_limits.values()) if typed_recovery else effective_retries)
    call_id = uuid.uuid4().hex
    reasoning_options = reasoning_request_options(config, profile_name)
    recovery_reasoning_options = reasoning_request_options(config, "fast")
    is_ingest = operation == "ingest" or operation.startswith("ingest_")
    mode = ingest_mode(config) if is_ingest else execution_mode(config)
    if mode == "agent":
        handoff = agent_handoff(prompt, "structured-text")
        handoff.update({
            "event_kind": "agent_handoff", "call_id": call_id,
            "recovery_policy_version": rp.POLICY_VERSION,
        })
        _log_event(operation, handoff, audit_prompt, transaction_id=transaction_id)
        return handoff | ({"ingest_mode": mode} if is_ingest else {})
    profiles = api_profiles(config, operation)
    if not all(profiles[0][key] for key in ("base", "key", "model")):
        variable = "INGEST_BACKEND" if is_ingest else "QUERY_BACKEND"
        _cfg_err = {"ok": False, "status": "configuration_error", "mode": "api", "error": f"{variable}=api 时必须配置 LLM_API_BASE、LLM_API_KEY、LLM_MODEL", "event_kind": "configuration", "call_id": call_id, "recovery_policy_version": rp.POLICY_VERSION, "recovery_class": "configuration", "retryable": False}
        _log_event(operation, _cfg_err, audit_prompt, transaction_id=transaction_id)
        return _cfg_err
    last = None
    history = []
    force_low_reasoning = False
    for profile_index, profile in enumerate(profiles):
        attempt_max_tokens = effective_max_tokens
        attempt_reasoning = dict(recovery_reasoning_options if force_low_reasoning else reasoning_options)
        reasoning_strikes = 0
        for attempt in range(max_attempts):
            used_max_tokens = attempt_max_tokens
            user_prompt = prompt if attempt == 0 else f"{prompt}\n\n上次输出为空或出错：{last.get('error', 'empty output')}。请重新输出完整文本。"
            if messages:
                conv = list(messages)
                if attempt > 0:
                    conv = conv + [{
                        "role": "user",
                        "content": f"上次输出为空或出错：{last.get('error', 'empty output')}。请重新输出完整文本。",
                    }]
            else:
                conv = [{"role": "system", "content": system}, {"role": "user", "content": user_prompt}]
            request_audit_prompt = json.dumps(conv, ensure_ascii=False, sort_keys=True)
            payload_data = {
                "model": profile["model"],
                "messages": conv,
                "temperature": 0,
                "max_tokens": attempt_max_tokens,
            }
            payload_data.update(attempt_reasoning)
            payload = json.dumps(payload_data).encode()
            request = urllib.request.Request(profile["base"].rstrip("/") + "/v1/chat/completions", data=payload, headers={"Authorization": "Bearer " + profile["key"], "Content-Type": "application/json"})
            started = time.time()
            fast_fail = False
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    data = json.loads(response.read())
                choice = (data.get("choices") or [{}])[0]
                text = (choice.get("message") or {}).get("content") or ""
                finish_reason = choice.get("finish_reason")
                usage = data.get("usage", {})
                reasoning_tokens = _reasoning_token_count(usage)
                reasoning_exhausted = _reasoning_exhausted(text, finish_reason, usage)
                error = None if text.strip() else "空输出"
                if error == "空输出" and finish_reason == "length":
                    retry_same_profile = _retry_available(
                        "output_transport", recovery_used, client_limits,
                        attempt, effective_retries, typed_recovery,
                    )
                    has_later_profile = profile_index < len(profiles) - 1
                    if retry_same_profile or has_later_profile:
                        force_low_reasoning = True
                    if retry_same_profile:
                        attempt_max_tokens = min(attempt_max_tokens * 2, 65536)
                        attempt_reasoning = dict(recovery_reasoning_options)
                    if reasoning_exhausted:
                        reasoning_strikes += 1
                        error = ("空输出(推理占满预算，下一尝试降低推理强度)"
                                 if retry_same_profile or has_later_profile
                                 else "空输出(推理占满预算，已无后续尝试)")
                    else:
                        error = ("空输出(输出预算不足，下一尝试扩容)"
                                 if retry_same_profile or has_later_profile
                                 else "空输出(输出预算耗尽，已无后续尝试)")
                last = {"ok": error is None, "status": "ok" if error is None else "validation_error", "mode": "api", "model": profile["model"], "profile": profile["name"], "reasoning_profile": profile_name, "reasoning_reason": decision["reason"], "reasoning_error_class": decision["error_class"], "provider_reasoning_effort": _provider_reasoning_effort(config, attempt_reasoning), "max_tokens": used_max_tokens, "next_max_tokens": attempt_max_tokens if error and _retry_available("output_transport", recovery_used, client_limits, attempt, effective_retries, typed_recovery) else None, "attempt": attempt + 1, "text": text, "error": error, "latency_sec": round(time.time() - started, 2), "finish_reason": finish_reason, "usage": usage, "reasoning_tokens": reasoning_tokens, "reasoning_exhausted": reasoning_exhausted}
                recovery_class = "output_transport" if error else ""
                failure_retryable = bool(error)
            except Exception as exc:
                failure_retryable, err_label, recovery_class = _classify_api_exception(exc)
                fast_fail = not failure_retryable
                last = {"ok": False, "status": "api_error", "mode": "api", "model": profile["model"], "profile": profile["name"], "reasoning_profile": profile_name, "reasoning_reason": decision["reason"], "reasoning_error_class": decision["error_class"], "provider_reasoning_effort": _provider_reasoning_effort(config, attempt_reasoning), "max_tokens": attempt_max_tokens, "attempt": attempt + 1, "error": err_label, "latency_sec": round(time.time() - started, 2), "usage": {}}
            retry_same_profile = (
                failure_retryable
                and not fast_fail
                and reasoning_strikes < 2
                and _retry_available(
                    recovery_class, recovery_used, client_limits,
                    attempt, effective_retries, typed_recovery,
                )
            )
            last.update({
                "event_kind": "llm_api_call",
                "call_id": call_id,
                "recovery_policy_version": rp.POLICY_VERSION,
                "recovery_class": recovery_class,
                "recovery_scheduled": retry_same_profile,
                "retryable": failure_retryable,
            })
            history.append({key: last.get(key) for key in ("model", "profile", "reasoning_profile", "reasoning_reason", "reasoning_error_class", "provider_reasoning_effort", "max_tokens", "status", "attempt", "latency_sec", "usage", "recovery_class", "recovery_scheduled", "retryable")})
            _log_event(operation, last, request_audit_prompt, last.get("text", ""), transaction_id)
            if last["ok"]:
                last["history"] = history
                last["fallback_used"] = profile_index > 0
                last["recovery_attempts"] = dict(recovery_used)
                return last
            if not retry_same_profile:
                break
            if typed_recovery:
                recovery_used[recovery_class] += 1
            if recovery_class == "infrastructure" and "429" in str(last.get("error", "")):
                _backoff_sleep(attempt + 1)
    if last:
        last["history"] = history
        last["fallback_used"] = len(profiles) > 1
    _final = last or {"ok": False, "status": "failed", "error": "未知 LLM 错误"}
    _final["recovery_attempts"] = dict(recovery_used)
    return _final
