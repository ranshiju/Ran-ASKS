#!/usr/bin/env python3
"""Regression tests for shared project environment parsing."""
from __future__ import annotations

import tempfile
import sqlite3
from pathlib import Path

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


if __name__ == "__main__":
    test_dotenv_syntax_and_comments()
    test_precedence_and_expansion()
    test_api_url_join()
    test_embedding_cache_migrates_and_isolates_models()
    print("test_env_config: ok")
