"""待投递结果的持久化。

为什么需要它：hermes 的 delivery ledger 在适配器的 ``send()`` 返回
``success=True`` 时就把这一轮标成"已送达"。本适配器的 ``send()`` 只是把文本
挂进缓冲（真正的 ``send_result`` 稍后才发），所以 ledger **不会**在崩溃后补发。
换句话说：从 ``send()`` 返回到中继回 ``ack_send_result`` 之间进程挂掉，答案就
永久丢了。这个文件就是补上那一段。

刻意不上 SQLite：需要保证的只有"未确认的结果要能重发"和"已完成的 job 不要
重跑"，一个原子写的 JSON 足够，且没有额外依赖、没有 WAL 文件、没有并发连接
问题。数据量本来就是个位数条目。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from .safeio import read_json, write_private_json

logger = logging.getLogger(__name__)

STORE_VERSION = 1


class PendingResultStore:
    """``<state_dir>/pending_results.json``

    结构::

        {
          "version": 1,
          "pending":   {"<job_id>": {"data": "...", "attempts": 0,
                                     "chat_id": "...", "created_at": 1720000000}},
          "completed": {"<job_id>": 1720000000}
        }

    ``pending``   —— 已产出但未收到 ``ack_send_result`` 的结果，重连后补发。
    ``completed`` —— 已确认送达的 job，用于跨重启的重放去重（中继可能重投）。
    """

    def __init__(self, path: str | Path, *, ttl_seconds: int = 24 * 60 * 60) -> None:
        self._path = Path(path)
        self._ttl = max(60, int(ttl_seconds))
        self._lock = threading.RLock()
        self._data = self._load()

    # -- 载入 / 落盘 --------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        raw = read_json(self._path, default=None)
        if not isinstance(raw, dict) or raw.get("version") != STORE_VERSION:
            if raw is not None:
                logger.warning(
                    "Livis: %s 格式不认识，重建（旧内容忽略）", self._path
                )
            return {"version": STORE_VERSION, "pending": {}, "completed": {}}
        pending = raw.get("pending")
        completed = raw.get("completed")
        return {
            "version": STORE_VERSION,
            "pending": pending if isinstance(pending, dict) else {},
            "completed": completed if isinstance(completed, dict) else {},
        }

    def _flush(self) -> None:
        try:
            write_private_json(self._path, self._data)
        except OSError as exc:
            # 落盘失败不能影响投递：内存态仍然有效，只是失去了崩溃恢复能力。
            logger.warning("Livis: 无法写入 %s: %s", self._path, exc)

    @property
    def path(self) -> Path:
        return self._path

    # -- 待投递 -------------------------------------------------------------

    def put(self, job_id: str, data: str, chat_id: str = "") -> None:
        """记录一条已产出、等待 ack 的结果。"""
        if not job_id:
            return
        with self._lock:
            self._data["pending"][job_id] = {
                "data": data,
                "attempts": 0,
                "chat_id": chat_id,
                "created_at": int(time.time()),
            }
            self._flush()

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._data["pending"].get(job_id)
            return dict(entry) if isinstance(entry, dict) else None

    def bump_attempts(self, job_id: str) -> int:
        with self._lock:
            entry = self._data["pending"].get(job_id)
            if not isinstance(entry, dict):
                return 0
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            self._flush()
            return int(entry["attempts"])

    def reset_attempts(self, job_id: str) -> None:
        with self._lock:
            entry = self._data["pending"].get(job_id)
            if isinstance(entry, dict):
                entry["attempts"] = 0
                self._flush()

    def pending_ids(self) -> list[str]:
        with self._lock:
            items = [
                (str(job_id), int((entry or {}).get("created_at") or 0))
                for job_id, entry in self._data["pending"].items()
                if isinstance(entry, dict)
            ]
        items.sort(key=lambda pair: pair[1])
        return [job_id for job_id, _ in items]

    def is_pending(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._data["pending"]

    # -- 完成 / 丢弃 --------------------------------------------------------

    def complete(self, job_id: str) -> None:
        """确认送达：移出 pending，记进 completed 用于去重。"""
        with self._lock:
            self._data["pending"].pop(job_id, None)
            self._data["completed"][job_id] = int(time.time())
            self._flush()

    def discard(self, job_id: str, *, remember: bool = True) -> None:
        """放弃这条结果（取消 / 重试耗尽）。"""
        with self._lock:
            self._data["pending"].pop(job_id, None)
            if remember:
                self._data["completed"][job_id] = int(time.time())
            self._flush()

    def is_completed(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._data["completed"]

    # -- 维护 ---------------------------------------------------------------

    def prune(self) -> int:
        """清掉超过 TTL 的记录，返回清理条数。"""
        cutoff = int(time.time()) - self._ttl
        removed = 0
        with self._lock:
            for job_id, stamp in list(self._data["completed"].items()):
                if int(stamp or 0) < cutoff:
                    self._data["completed"].pop(job_id, None)
                    removed += 1
            for job_id, entry in list(self._data["pending"].items()):
                created = int((entry or {}).get("created_at") or 0)
                if created < cutoff:
                    self._data["pending"].pop(job_id, None)
                    removed += 1
            if removed:
                self._flush()
        return removed

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "pending": len(self._data["pending"]),
                "completed": len(self._data["completed"]),
            }
