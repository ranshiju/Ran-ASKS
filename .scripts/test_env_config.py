#!/usr/bin/env python3
"""Regression tests for shared project environment parsing."""
from __future__ import annotations

import tempfile
import sqlite3
import io
import urllib.error
from pathlib import Path
from unittest.mock import patch

import env_config


def test_dotenv_syntax_and_comments() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / ".env"
        target.write_text(
            "# comment\n"
            "TOKEN=secret # 中文注释\n"
            "HASH=left\\#right\n"
            "DOUBLE=\"inside # value\" # outside\n"
            "SINGLE='literal # value'\n"
            "export EMPTY=\n",
            encoding="utf-8",
        )
        assert env_config.parse_dotenv(target) == {
            "TOKEN": "secret",
            "HASH": "left#right",
            "DOUBLE": "inside # value",
            "SINGLE": "literal # value",
            "EMPTY": "",
        }


def test_precedence_and_expansion() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / ".env"
        target.write_text(
            "LLM_API_BASE=https://file.example/v1\n"
            "EMBED_API_BASE=${LLM_API_BASE}\n"
            "CYCLE_A=${CYCLE_B}\n"
            "CYCLE_B=${CYCLE_A}\n",
            encoding="utf-8",
        )
        values = env_config.load_env(
            target,
            keys={"LLM_API_BASE"},
            environ={"LLM_API_BASE": "https://process.example/v1"},
        )
        assert values["LLM_API_BASE"] == "https://process.example/v1"
        assert values["EMBED_API_BASE"] == "https://process.example/v1"
        assert values["CYCLE_A"].startswith("${CYCLE_")


def test_api_url_join() -> None:
    assert env_config.join_api_url(
        "https://api.example/v1", "/v1/chat/completions"
    ) == "https://api.example/v1/chat/completions"
    assert env_config.join_api_url(
        "https://open.bigmodel.cn/api/paas/v4", "/embeddings"
    ) == "https://open.bigmodel.cn/api/paas/v4/embeddings"
    try:
        env_config.join_api_url("https://api.example", "embeddings")
    except ValueError:
        pass
    else:
        raise AssertionError("relative API path must be rejected")


def test_embedding_cache_migrates_and_isolates_models() -> None:
    import numpy as np
    import embed_helper

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "embeddings.db"
        with sqlite3.connect(target) as conn:
            conn.execute(
                "CREATE TABLE embeddings(text TEXT PRIMARY KEY, vector BLOB NOT NULL, "
                "last_used REAL, created REAL NOT NULL)"
            )
            conn.execute(
                "INSERT INTO embeddings VALUES(?,?,?,?)",
                ("same text", np.array([9.0, 9.0], dtype=np.float32).tobytes(), None, 1),
            )
        old_batch = embed_helper.embed_batch
        old_model = embed_helper._MODEL
        embed_helper.configure_cache(target)
        try:
            embed_helper.embed_batch = lambda _texts: np.array([[1.0, 0.0]])
            embed_helper._MODEL = "model-a"
            first = embed_helper.embed_cached_batch(["same text"])
            embed_helper.embed_batch = lambda _texts: np.array([[0.0, 1.0]])
            embed_helper._MODEL = "model-b"
            second = embed_helper.embed_cached_batch(["same text"])
            assert first.tolist() == [[1.0, 0.0]]
            assert second.tolist() == [[0.0, 1.0]]
            with sqlite3.connect(target) as conn:
                assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 2
                assert conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='embeddings_legacy_text_only'"
                ).fetchone()
        finally:
            embed_helper.embed_batch = old_batch
            embed_helper._MODEL = old_model
            embed_helper.configure_cache(None)


def test_embedding_default_and_provider_error_detail() -> None:
    import embed_helper

    assert embed_helper.DEFAULT_MODEL == "GLM-Embedding-3"
    error = urllib.error.HTTPError(
        "https://api.example/v1/embeddings", 400, "Bad Request", {},
        io.BytesIO(b'{"error":{"message":"model not found: bad-model"}}'),
    )
    detail = embed_helper._provider_error_detail(error)
    assert "HTTP 400" in detail
    assert "model not found: bad-model" in detail


def test_embedding_match_batches_required_followup_without_new_candidates() -> None:
    import numpy as np
    import embed_helper

    vectors = {
        "merge": [1.0, 0.0], "link": [0.95, 0.3122499],
        "distinct": [-1.0, -1.0], "existing": [1.0, 0.0],
        "other": [0.0, 1.0], "route": [-1.0, -1.0],
    }
    new_texts = ["merge", "link", "distinct", "merge"]
    existing_texts = ["existing", "other", "existing"]
    batches = []

    def embed_batch(texts):
        batches.append(list(texts))
        return np.asarray([vectors[text] for text in texts], dtype=np.float32)

    with tempfile.TemporaryDirectory() as directory:
        with patch.object(embed_helper, "EMBED_DB", Path(directory) / "baseline.db"), \
                patch.object(embed_helper, "embed_batch", side_effect=embed_batch):
            baseline = embed_helper.match_by_embedding(new_texts, existing_texts)
        batches.clear()
        with patch.object(embed_helper, "EMBED_DB", Path(directory) / "combined.db"), \
                patch.object(embed_helper, "embed_batch", side_effect=embed_batch):
            combined = embed_helper.match_by_embedding(
                new_texts, existing_texts, extra_texts=["route", "existing", "route"],
            )
            followup = embed_helper.embed_cached_batch(["route", "existing"])
        assert combined == baseline
        assert set(combined) == {"merge", "link"}
        assert combined["merge"][1] > 0.98
        assert 0.9 <= combined["link"][1] <= 0.98
        assert batches == [["merge", "link", "distinct", "existing", "other", "route"]]
        assert np.array_equal(followup, [vectors["route"], vectors["existing"]])


def test_embedding_match_joint_failure_preserves_original_failure_policy() -> None:
    import numpy as np
    import embed_helper

    vectors = np.asarray([[1.0, 0.0], [1.0, 0.0]])
    with patch.object(embed_helper, "embed_cached_batch", side_effect=[RuntimeError("joint"), vectors]) as cached:
        matches = embed_helper.match_by_embedding(["new"], ["old"], extra_texts=["route"])
        assert matches["new"][0] == "old"
        assert [call.args[0] for call in cached.call_args_list] == [["new", "old", "route"], ["new", "old"]]
    with patch.object(embed_helper, "embed_cached_batch", side_effect=RuntimeError("original")) as cached:
        try:
            embed_helper.match_by_embedding(["new"], ["old"])
        except RuntimeError as error:
            assert str(error) == "original"
        else:
            raise AssertionError("Original matching failures must propagate")
        assert cached.call_count == 1
    with patch.object(embed_helper, "embed_cached_batch") as cached:
        assert embed_helper.match_by_embedding([], ["old"], extra_texts=["route"]) == {}
        assert not cached.called


def test_embedding_match_does_not_repeat_rejected_provider_requests() -> None:
    import embed_helper

    for status_code in (401, 403, 429):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(embed_helper, "EMBED_DB", Path(directory) / "embeddings.db"), \
                patch.object(embed_helper, "_embedding_endpoint", return_value="https://provider.invalid/v1/embeddings"), \
                patch.object(embed_helper.urllib.request, "urlopen", side_effect=urllib.error.HTTPError(
                    "https://provider.invalid/v1/embeddings", status_code, "Rejected", {},
                    io.BytesIO(b'{"error":"provider rejected"}'),
                )) as request, patch.object(embed_helper.time, "sleep"):
            try:
                embed_helper.match_by_embedding(["new"], ["old"], extra_texts=["route"])
            except embed_helper.EmbeddingAPIError as error:
                assert isinstance(error, RuntimeError)
                assert error.status_code == status_code
            else:
                raise AssertionError("Rejected requests must preserve the provider failure")
            assert request.call_count == 2


if __name__ == "__main__":
    test_dotenv_syntax_and_comments()
    test_precedence_and_expansion()
    test_api_url_join()
    test_embedding_cache_migrates_and_isolates_models()
    test_embedding_default_and_provider_error_detail()
    test_embedding_match_batches_required_followup_without_new_candidates()
    test_embedding_match_joint_failure_preserves_original_failure_policy()
    test_embedding_match_does_not_repeat_rejected_provider_requests()
    print("test_env_config: ok")
