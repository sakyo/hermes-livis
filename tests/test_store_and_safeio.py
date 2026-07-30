"""待投递结果存储与私有文件写入/脱敏的测试。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from hermes_livis.plugin.safeio import (
    read_json,
    redact_body,
    redact_secret,
    redact_text,
    write_private_json,
    write_private_text,
)
from hermes_livis.plugin.store import PendingResultStore

# ---------------------------------------------------------------------------
# safeio
# ---------------------------------------------------------------------------

def test_private_write_is_owner_only(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "tokens.json"
    write_private_json(target, {"relay_refresh_token": "secret"})
    assert read_json(target) == {"relay_refresh_token": "secret"}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_private_write_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "agent.id"
    write_private_text(target, "openclaw-x\n")
    assert [p.name for p in tmp_path.iterdir()] == ["agent.id"]


def test_private_write_replaces_atomically(tmp_path: Path) -> None:
    target = tmp_path / "a.json"
    write_private_json(target, {"v": 1})
    write_private_json(target, {"v": 2})
    assert read_json(target) == {"v": 2}


def test_read_json_tolerates_garbage(tmp_path: Path) -> None:
    broken = tmp_path / "b.json"
    broken.write_text("{oops", encoding="utf-8")
    assert read_json(broken, default={"fallback": True}) == {"fallback": True}


def test_redaction_covers_json_form_and_bearer() -> None:
    assert "eyJhbGci" not in redact_text('{"access_token":"eyJhbGciOiJI"}')
    assert "[redacted]" in redact_text('{"access_token":"eyJhbGciOiJI"}')
    assert "[redacted]" in redact_text("refresh_token=abcdef123456&x=1")
    assert "[redacted]" in redact_text("Authorization: Bearer abcdef123456")
    # 非敏感内容原样保留
    assert redact_text('{"error":"invalid_grant"}') == '{"error":"invalid_grant"}'


def test_redact_body_handles_dicts_and_truncates() -> None:
    body = {"error": "bad", "refresh_token": "supersecretvalue"}
    out = redact_body(body)
    assert "supersecretvalue" not in out
    assert "bad" in out
    assert len(redact_body({"x": "y" * 5000}, limit=100)) <= 100


def test_redact_secret_never_returns_the_value() -> None:
    assert redact_secret("abcdefghijklmnop").startswith("abcdef")
    assert "ghijklmnop" not in redact_secret("abcdefghijklmnop")
    assert redact_secret("") == "<none>"
    assert redact_secret("abc") == "***"


# ---------------------------------------------------------------------------
# PendingResultStore
# ---------------------------------------------------------------------------

def test_pending_survives_a_new_instance(tmp_path: Path) -> None:
    """核心用途：进程重启后仍能补发未确认的结果。"""
    path = tmp_path / "pending.json"
    first = PendingResultStore(path)
    first.put("job-1", '{"text":"hi"}', "glasses-1")

    reopened = PendingResultStore(path)
    assert reopened.pending_ids() == ["job-1"]
    assert reopened.get("job-1")["data"] == '{"text":"hi"}'


def test_complete_moves_to_dedup_set(tmp_path: Path) -> None:
    store = PendingResultStore(tmp_path / "p.json")
    store.put("job-1", "data")
    store.complete("job-1")
    assert store.pending_ids() == []
    assert store.is_completed("job-1") is True
    assert store.is_pending("job-1") is False


def test_discard_can_forget(tmp_path: Path) -> None:
    store = PendingResultStore(tmp_path / "p.json")
    store.put("job-1", "d")
    store.discard("job-1", remember=False)
    assert store.is_completed("job-1") is False


def test_attempts_counter(tmp_path: Path) -> None:
    store = PendingResultStore(tmp_path / "p.json")
    store.put("job-1", "d")
    assert store.bump_attempts("job-1") == 1
    assert store.bump_attempts("job-1") == 2
    store.reset_attempts("job-1")
    assert store.get("job-1")["attempts"] == 0
    # 不存在的 job 不应炸
    assert store.bump_attempts("nope") == 0


def test_pending_ids_are_oldest_first(tmp_path: Path) -> None:
    store = PendingResultStore(tmp_path / "p.json")
    store.put("a", "1")
    store.put("b", "2")
    raw = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    raw["pending"]["a"]["created_at"] = 1
    raw["pending"]["b"]["created_at"] = 2
    (tmp_path / "p.json").write_text(json.dumps(raw), encoding="utf-8")
    assert PendingResultStore(tmp_path / "p.json").pending_ids() == ["a", "b"]


def test_prune_drops_expired(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    store = PendingResultStore(path, ttl_seconds=60)
    store.put("old", "d")
    store.complete("old")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["completed"]["old"] = 1
    path.write_text(json.dumps(raw), encoding="utf-8")

    reopened = PendingResultStore(path, ttl_seconds=60)
    assert reopened.prune() == 1
    assert reopened.is_completed("old") is False


def test_unknown_schema_is_rebuilt_not_crashed(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"version": 99, "junk": 1}), encoding="utf-8")
    store = PendingResultStore(path)
    assert store.snapshot() == {"pending": 0, "completed": 0}
    store.put("job", "d")
    assert store.pending_ids() == ["job"]


def test_store_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    PendingResultStore(path).put("job", "d")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_flush_failure_does_not_break_delivery(tmp_path: Path, monkeypatch) -> None:
    """落盘失败只损失崩溃恢复能力，绝不能影响投递本身。"""
    store = PendingResultStore(tmp_path / "p.json")

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("hermes_livis.plugin.store.write_private_json", boom)
    store.put("job-1", "data")  # 不抛
    assert store.get("job-1")["data"] == "data"


def test_missing_directory_is_created(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "p.json"
    PendingResultStore(path).put("j", "d")
    assert path.exists()
    assert os.path.isdir(path.parent)
