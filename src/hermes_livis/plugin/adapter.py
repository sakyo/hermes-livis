"""理想眼镜 (Livis) 平台适配器 —— Hermes Agent 插件。

适配器作为**持久外拨的 WebSocket 客户端**连到理想 ``livis-pc-kit`` 中继，
收眼镜（经手机 APP / 理想云）下发的语音指令，交给 hermes agent 跑，再把答案
回传给眼镜朗读。链路里不再需要 openclaw —— 本插件替代理想官方的闭源 openclaw
渠道插件 ``@chehejia/livis-pc-kit`` v2.0.0（协议逆向自它）。

    眼镜 ──► 手机 APP / 理想云 ──► 中继 wss://livis-pc-kit-gateway.livis.com
                                        │  ▲       （按 agent_id + token 路由）
                          send_message  │  │  send_result
                                        ▼  │
                                  LivisAdapter ──► run_conversation()

结果收口：两级信号 + 看门狗
---------------------------------
协议要求"一个 job 只回一条 ``send_result``"，但 hermes 的投递顺序是**先
``send()`` 发文本、再 ``send_document()`` 发附件**，所以 ``send()`` 不能立刻
回包。这里用三个信号叠加，兼顾精确与不卡死：

1. **主信号** —— ``on_processing_complete(event, outcome)``：hermes 在一轮
   （含附件投递）真正结束时触发，此时立即收口，**零额外延迟**。
2. **兜底计时器**（``LIVIS_RESULT_FALLBACK_MS``，默认 5s）—— hermes 有几条
   提前 return 的派发路径不触发上面的钩子（活跃会话下的 bypass 命令、clarify
   文本拦截等）。第一次 ``send()`` 时启动，钩子来了就取消。
3. **每 job 看门狗**（``LIVIS_JOB_WATCHDOG_SECONDS``，默认 300s）—— 派发后
   连一次 ``send()`` 都没发生（消息被授权拒绝、被丢弃等）时兜底回一条提示，
   避免眼镜侧永远等不到回应。

三者都是计时器/回调，**不阻塞派发线程**：任何一个 job 出问题都不会拖住同一副
眼镜的后续请求。

与官方插件的其他有意差异
-----------------------
* **断线不中断在跑的 agent**：官方断线即 abort（靠它的 SQLite 重放补偿）。
  这里不 abort，结果先落 :class:`PendingResultStore`，重连后补发。
* **不支持主动推送**：官方 ``outbound.sendText`` 是空实现，中继没有"PC 主动
  找眼镜说话"的通路。因此不注册 cron 投递，``send()`` 找不到对应 job 时明确
  失败而不是静默丢弃。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)
from gateway.session import build_session_key

from . import protocol
from .auth import LivisAuthError, LivisCredentials
from .constants import (
    ACK_TIMEOUT,
    DEFAULT_JOB_WATCHDOG_SECONDS,
    DEFAULT_RESULT_FALLBACK_MS,
    HEARTBEAT_INTERVAL,
    JOB_TABLE_LIMIT,
    MAX_ACK_RETRIES,
    MAX_WIRE_MESSAGE_BYTES,
    PENDING_STORE_TTL_SECONDS,
    PLATFORM_LABEL,
    PLATFORM_NAME,
    PLUGIN_VERSION,
    PONG_TIMEOUT,
    PROTOCOL_VERSION,
    RECONNECT_MAX_DELAY,
    SEEN_JOBS_LIMIT,
    TOKEN_REFRESH_ACK_TIMEOUT,
    TOKEN_REFRESH_MAX_FAILURES,
    client_name,
    node_name,
    state_dir,
    ws_url,
)
from .documents import upload_document, upload_rejection_reason
from .safeio import ensure_private_dir, redact_secret
from .store import PendingResultStore

logger = logging.getLogger(__name__)

# agent 一句话都没产出时的兜底文本 —— 眼镜端需要有东西可念。
EMPTY_RESULT_TEXT = "抱歉，我这次没能生成回复，请再说一次。"
FAILED_RESULT_TEXT = "抱歉，这次处理失败了，请稍后再试。"
WATCHDOG_RESULT_TEXT = "抱歉，这次请求没有得到处理，请稍后再试。"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class LivisJob:
    """一轮对话：眼镜的一次 ``send_message`` 到 PC 的一条 ``send_result``。"""

    job_id: str
    msg_id: str
    chat_id: str
    from_node_id: str
    from_node_type: str = ""
    session_key: str = ""
    text: str = ""
    files: list[dict[str, Any]] = field(default_factory=list)
    answered: bool = False
    cancelled: bool = False
    produced: bool = False
    created_at: float = field(default_factory=time.time)
    fallback_task: asyncio.Task | None = None
    watchdog_task: asyncio.Task | None = None

    def append_text(self, chunk: str) -> None:
        chunk = (chunk or "").strip()
        if not chunk:
            return
        self.text = f"{self.text}\n\n{chunk}" if self.text else chunk


class LivisAdapter(BasePlatformAdapter):
    """理想眼镜渠道适配器（持久外拨 WebSocket 客户端）。"""

    # 协议只允许一个 job 回一条结果，绝不能让 hermes 按长度把回复切片。
    # 给一个足够大的上限（而不是 0 —— base 会把 0 当"未设置"并退回 4096）。
    MAX_MESSAGE_LENGTH = 1_000_000

    def __init__(self, config: PlatformConfig, **kwargs: Any) -> None:
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))

        extra = getattr(config, "extra", {}) or {}
        self.creds = LivisCredentials()
        self.node_name = extra.get("node_name") or node_name()
        self.client_name = extra.get("protocol_client") or client_name()

        self._fallback_seconds = max(
            0.2, _int_env("LIVIS_RESULT_FALLBACK_MS", DEFAULT_RESULT_FALLBACK_MS) / 1000
        )
        self._watchdog_seconds = max(
            10.0,
            _float_env("LIVIS_JOB_WATCHDOG_SECONDS", DEFAULT_JOB_WATCHDOG_SECONDS),
        )

        root = ensure_private_dir(state_dir())
        self._store = PendingResultStore(
            root / "pending_results.json", ttl_seconds=PENDING_STORE_TTL_SECONDS
        )

        # 运行状态
        self._running = False
        self._ws: Any = None
        self._ws_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._reconnect_attempts = 0
        self._agent_id = ""
        self._device_id = ""

        # job 台账
        self._jobs: dict[str, LivisJob] = {}
        self._latest_job_by_chat: dict[str, LivisJob] = {}
        self._seen_jobs: list[str] = []
        self._pending_cancels: set[str] = set()
        self._ack_tasks: dict[str, asyncio.Task] = {}

        # token 刷新
        self._token_refresh_failures = 0
        self._token_refresh_ack_task: asyncio.Task | None = None
        self._token_refresh_task: asyncio.Task | None = None

        logger.info(
            "[livis] 适配器 v%s 已初始化: node_name=%s fallback=%.1fs "
            "watchdog=%.0fs state=%s",
            PLUGIN_VERSION, self.node_name, self._fallback_seconds,
            self._watchdog_seconds, self._store.path.parent,
        )

    # ------------------------------------------------------------------
    # 授权
    # ------------------------------------------------------------------

    @property
    def authorization_is_upstream(self) -> bool:
        """默认把授权委托给理想中继。

        中继用 OAuth2 token 认证连接，并且只把消息路由给**在理想 APP 里绑定到
        这个 agent_id 的眼镜** —— 发送方身份是理想侧的不透明 node id，不是运营
        者能在本地配置的账号，缺 allowlist 就默认拒绝会让渠道完全不通（而且要
        填的值只能先被拒一次、再去日志里翻才知道）。

        需要严格本地管控时设 ``LIVIS_ALLOWED_NODE_IDS``：此时本属性返回 False，
        回到 hermes 标准 allowlist 判定（fail-closed）。
        """
        return not bool(os.getenv("LIVIS_ALLOWED_NODE_IDS", "").strip())

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """校验凭据并拉起 WebSocket 主循环。"""
        for module, hint in (("websockets", "websockets"), ("aiohttp", "aiohttp")):
            try:
                __import__(module)
            except ImportError:
                logger.error("[livis] 缺少 %s 包: pip install %s", module, hint)
                return False

        # 装过官方 kit 的机器：直接接管它的凭据（含 agent_id，眼镜无需重绑）。
        with contextlib.suppress(Exception):
            self.creds.import_from_openclaw()

        if not self.creds.refresh_token:
            logger.error(
                "[livis] 未登录。执行 `hermes-livis login` 完成理想账号登录，"
                "或 `hermes-livis import-openclaw` 导入 openclaw 已有凭据。"
            )
            return False

        self._agent_id = self.creds.agent_id
        self._device_id = self.creds.device_id
        if not self._agent_id:
            logger.error(
                "[livis] 缺少 agent_id。执行 `hermes-livis login` 生成，"
                "并在理想 APP 里把它绑定到眼镜。"
            )
            return False

        # 提前验一次凭据：拿不到 access_token 就没必要建连（报错也更清楚）。
        try:
            await self.creds.get_access_token()
        except LivisAuthError as exc:
            logger.error("[livis] 凭据校验失败: %s", exc)
            return False

        pruned = self._store.prune()
        if pruned:
            logger.info("[livis] 清理了 %d 条过期投递记录", pruned)

        self._running = True
        self._reconnect_attempts = 0
        self._ws_task = asyncio.create_task(self._ws_loop(), name="livis-transport")
        if hasattr(self, "_mark_connected"):
            self._mark_connected()
        logger.info(
            "[livis] 已启动，agent_id=%s device_id=%s 待投递=%d",
            self._agent_id, redact_secret(self._device_id, keep=10),
            self._store.snapshot()["pending"],
        )
        return True

    async def disconnect(self) -> None:
        """停掉所有任务并关闭连接。"""
        self._running = False

        tasks = [
            self._ws_task,
            self._heartbeat_task,
            self._token_refresh_ack_task,
            self._token_refresh_task,
            *self._ack_tasks.values(),
        ]
        for job in self._jobs.values():
            tasks.extend([job.fallback_task, job.watchdog_task])
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        for task in tasks:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        self._ws_task = None
        self._heartbeat_task = None
        self._token_refresh_ack_task = None
        self._token_refresh_task = None
        self._ack_tasks.clear()

        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

        if hasattr(self, "_mark_disconnected"):
            self._mark_disconnected()
        logger.info("[livis] 已断开")

    # ------------------------------------------------------------------
    # WebSocket 主循环
    # ------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        """持久连接 + 指数退避重连。

        注意：``_run_connection()`` **正常返回**（服务端干净关闭 code 1000）也
        必须退避。websockets 的 ``async for`` 在正常关闭时不抛异常，只是迭代
        结束；而"服务端策略性拒绝我们"最可能就表现为一次干净关闭 —— 若只在异常
        分支退避，那一刻就会变成对中继的高频重连风暴。
        """
        while self._running:
            try:
                await self._run_connection()
            except asyncio.CancelledError:
                break
            except LivisAuthError as exc:
                logger.error("[livis] 认证失败: %s", exc)
            except Exception as exc:
                logger.warning("[livis] 连接异常: %s", exc, exc_info=True)
            finally:
                self._ws = None
                await self._stop_heartbeat()

            if not self._running:
                break

            self._reconnect_attempts += 1
            base = min(2 ** (self._reconnect_attempts - 1), RECONNECT_MAX_DELAY)
            jitter = base * 0.2 * (random.random() * 2 - 1)
            delay = max(1.0, base + jitter)
            logger.info(
                "[livis] %.1fs 后重连（第 %d 次）", delay, self._reconnect_attempts
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    async def _run_connection(self) -> None:
        """建立一次连接并处理消息，直到对端关闭。"""
        import websockets

        token = await self.creds.get_access_token()
        url = f"{ws_url()}?protocol_version={PROTOCOL_VERSION}"
        logger.info("[livis] 正在连接 %s", url)

        async with websockets.connect(
            url,
            ping_interval=HEARTBEAT_INTERVAL,
            ping_timeout=PONG_TIMEOUT,
            close_timeout=10,
            max_size=MAX_WIRE_MESSAGE_BYTES,
        ) as ws:
            self._ws = ws
            self._reconnect_attempts = 0
            self._token_refresh_failures = 0

            await self._send_frame(
                protocol.connect_frame(
                    agent_id=self._agent_id,
                    device_id=self._device_id,
                    node_name=self.node_name,
                    access_token=token,
                    refresh_token=self.creds.refresh_token,
                    client=self.client_name,
                )
            )
            logger.info("[livis] 握手已发送 (agent_id=%s)", self._agent_id)

            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="livis-heartbeat"
            )
            await self._redeliver_pending()

            async for raw in ws:
                if not self._running:
                    break
                try:
                    frame = protocol.parse_frame(raw)
                except protocol.ProtocolError as exc:
                    logger.warning("[livis] 丢弃非法帧: %s", exc)
                    continue
                try:
                    await self._handle_frame(frame)
                except Exception:
                    logger.exception("[livis] 处理帧出错")

        logger.info("[livis] 连接已关闭")

    async def _heartbeat_loop(self) -> None:
        """30s 一次应用层 ``heartbeat``（WS 协议 ping 由库负责）。"""
        while self._running:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
            except asyncio.CancelledError:
                return
            if not self._running or self._ws is None:
                return
            await self._send("heartbeat", job_id=protocol.new_id())

    async def _stop_heartbeat(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ------------------------------------------------------------------
    # 出站原语
    # ------------------------------------------------------------------

    async def _send_frame(self, frame: dict[str, Any]) -> bool:
        ws = self._ws
        if ws is None:
            logger.warning(
                "[livis] 连接未就绪，丢弃出站帧 type=%s", frame.get("type")
            )
            return False
        try:
            await ws.send(protocol.encode(frame))
            return True
        except Exception as exc:
            logger.warning("[livis] 发送失败 type=%s: %s", frame.get("type"), exc)
            return False

    async def _send(
        self,
        message_type: str,
        *,
        job_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> bool:
        return await self._send_frame(
            protocol.envelope(
                message_type,
                agent_id=self._agent_id,
                device_id=self._device_id,
                client=self.client_name,
                job_id=job_id,
                payload=payload,
            )
        )

    # ------------------------------------------------------------------
    # 入站分发
    # ------------------------------------------------------------------

    async def _handle_frame(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")

        if kind == "connected":
            logger.info("[livis] 中继已确认连接")
            return
        if kind == "send_message":
            await self._handle_send_message(frame)
            return
        if kind == "cancel_chat":
            await self._handle_cancel(frame)
            return
        if kind == "ack_send_result":
            self._handle_result_ack(frame)
            return
        if kind == "token_expiring":
            expires_at = (frame.get("payload") or {}).get("expires_at")
            logger.info("[livis] token 即将过期 (%s)，立即刷新", expires_at)
            # 不能在读循环里 await：刷新要走一次 HTTP，会把心跳和后续消息堵住。
            self._token_refresh_task = asyncio.create_task(
                self._refresh_token_on_relay(), name="livis-token-refresh"
            )
            return
        if kind == "token_refreshed":
            task = self._token_refresh_ack_task
            self._token_refresh_ack_task = None
            if task and not task.done():
                task.cancel()
            self._token_refresh_failures = 0
            logger.info("[livis] token 已在中继侧更新")
            return
        if kind == "heartbeat":
            return

        logger.warning("[livis] 未知帧: %s", protocol.frame_summary(frame))

    async def _handle_send_message(self, frame: dict[str, Any]) -> None:
        """眼镜下发的一轮请求。

        **先无条件回 ack，再解析。** ack 是让中继停止重投的信号；若因为解析
        失败而不回 ack，中继会无限重投同一条消息（官方插件也是先 ack 再解析）。
        """
        meta = frame.get("metadata") or {}
        job_id = str(meta.get("job_id") or "")

        await self._send(
            "ack_send_message", job_id=job_id or protocol.new_id()
        )

        if not job_id:
            logger.warning("[livis] send_message 缺少 job_id，已 ack 并忽略")
            return

        # 重放：中继有自己的补偿逻辑，同一 job 可能被重投。
        if job_id in self._seen_jobs or self._store.is_completed(job_id):
            if self._store.is_pending(job_id):
                # 结果还没被 ack ⇒ 对端没收到，补发即可，不要重跑 agent。
                logger.info("[livis] job %s 被重放，补发已有结果", job_id)
                self._store.reset_attempts(job_id)
                await self._deliver_result(job_id)
            else:
                logger.info("[livis] job %s 已处理过，跳过", job_id)
            return

        if job_id in self._pending_cancels:
            self._pending_cancels.discard(job_id)
            self._remember_job(job_id)
            logger.info("[livis] job %s 在开始前已被取消", job_id)
            return

        try:
            request = protocol.parse_exec_request(frame)
        except protocol.ProtocolError as exc:
            logger.info("[livis] job %s 不受支持，已 ack 并忽略: %s", job_id, exc)
            self._remember_job(job_id)
            return

        self._remember_job(job_id)
        await self._dispatch(request, frame)

    async def _dispatch(
        self, request: protocol.ExecRequest, frame: dict[str, Any]
    ) -> None:
        chat_id = request.from_node_id
        source = self.build_source(
            chat_id=chat_id,
            chat_name="理想眼镜",
            chat_type="dm",
            user_id=request.from_node_id,
            user_name="理想眼镜",
            message_id=request.job_id,
        )
        session_key = build_session_key(
            source,
            group_sessions_per_user=self.config.extra.get(
                "group_sessions_per_user", True
            ),
            thread_sessions_per_user=self.config.extra.get(
                "thread_sessions_per_user", False
            ),
        )

        job = LivisJob(
            job_id=request.job_id,
            msg_id=request.msg_id,
            chat_id=chat_id,
            from_node_id=request.from_node_id,
            from_node_type=request.from_node_type,
            session_key=session_key,
        )
        self._register_job(job)
        job.watchdog_task = asyncio.create_task(
            self._watchdog(job), name=f"livis-watchdog-{request.job_id[:12]}"
        )

        logger.info(
            "[livis] job %s 来自 %s: %s",
            request.job_id, request.from_node_id,
            request.content[:80].replace("\n", " "),
        )

        event = MessageEvent(
            text=request.content,
            message_type=MessageType.TEXT,
            source=source,
            # message_id == job_id：base 的 _reply_anchor_for_event 会把它当作
            # reply_to 传回 send()，是回复对上 job 的主要依据。
            message_id=request.job_id,
            raw_message=frame,
            # metadata 里再存一份：on_processing_complete 钩子拿到的是同一个
            # event 对象，靠这个键定位 job（比 message_id 更明确、不易被改写）。
            metadata={"livis_job_id": request.job_id},
            timestamp=datetime.now(tz=timezone.utc),
        )
        await self.handle_message(event)

    async def _handle_cancel(self, frame: dict[str, Any]) -> None:
        """``cancel_chat``：回执 + 中断对应会话。"""
        meta = frame.get("metadata") or {}
        job_id = str(meta.get("job_id") or "")
        if not job_id:
            logger.warning("[livis] cancel_chat 缺少 job_id，忽略")
            return

        await self._send("ack_cancel_chat", job_id=job_id)

        job = self._jobs.get(job_id)
        if job is None:
            # 取消先于消息到达：记下来，等 send_message 时直接丢弃。
            self._pending_cancels.add(job_id)
            logger.info("[livis] cancel_chat(%s) 早到，已记录", job_id)
            return

        job.cancelled = True
        self._cancel_timers(job)
        self._drop_pending(job_id, remember=True)
        logger.info("[livis] cancel_chat(%s)：中断会话", job_id)
        with contextlib.suppress(Exception):
            await self.interrupt_session_activity(job.session_key, job.chat_id)

    def _handle_result_ack(self, frame: dict[str, Any]) -> None:
        job_id = protocol.ack_target(frame)
        if not job_id:
            return
        if self._store.is_pending(job_id):
            self._store.complete(job_id)
            task = self._ack_tasks.pop(job_id, None)
            if task and not task.done():
                task.cancel()
            logger.info("[livis] job %s 结果已确认送达", job_id)
        else:
            logger.debug("[livis] ack_send_result 无对应待确认项: %s", job_id)

    # ------------------------------------------------------------------
    # job 台账
    # ------------------------------------------------------------------

    def _remember_job(self, job_id: str) -> None:
        self._seen_jobs.append(job_id)
        if len(self._seen_jobs) > SEEN_JOBS_LIMIT:
            del self._seen_jobs[: len(self._seen_jobs) - SEEN_JOBS_LIMIT]

    def _register_job(self, job: LivisJob) -> None:
        self._jobs[job.job_id] = job
        self._latest_job_by_chat[job.chat_id] = job
        if len(self._jobs) > JOB_TABLE_LIMIT:
            stale = sorted(
                (j for j in self._jobs.values() if j.answered),
                key=lambda j: j.created_at,
            )
            for old in stale[: max(1, len(self._jobs) - JOB_TABLE_LIMIT)]:
                self._cancel_timers(old)
                self._jobs.pop(old.job_id, None)

    def _resolve_job(self, chat_id: str, reply_to: str | None) -> LivisJob | None:
        """把一次 ``send()`` 对应回某个 job。

        ``reply_to`` 是首选（base 会把 ``event.message_id`` 即 job_id 回传）；
        退化到该会话最近一个未回包的 job。
        """
        if reply_to:
            job = self._jobs.get(str(reply_to))
            if job is not None:
                return job
        job = self._latest_job_by_chat.get(chat_id)
        if job is not None and not job.answered:
            return job
        return None

    def _adopt_orphan_job(self, job_id: str, chat_id: str) -> LivisJob:
        """为"只剩 job_id"的回复重建一个最小 job。

        ``send_result`` 只需要 job_id 就能投递，所以这条路是通的。
        """
        job = LivisJob(
            job_id=job_id, msg_id="", chat_id=chat_id, from_node_id=chat_id
        )
        self._register_job(job)
        logger.info("[livis] 为孤立回复重建 job %s", job_id)
        return job

    def _cancel_timers(self, job: LivisJob) -> None:
        for task in (job.fallback_task, job.watchdog_task):
            if task is not None and not task.done():
                task.cancel()
        job.fallback_task = None
        job.watchdog_task = None

    # ------------------------------------------------------------------
    # 收口：钩子（主）/ 兜底计时器 / 看门狗
    # ------------------------------------------------------------------

    async def on_processing_complete(
        self, event: MessageEvent, outcome: ProcessingOutcome
    ) -> None:
        """hermes 一轮处理结束（含附件投递）—— 主收口信号，立即回包。"""
        job_id = str((event.metadata or {}).get("livis_job_id") or "")
        if not job_id:
            job_id = str(getattr(event, "message_id", "") or "")
        job = self._jobs.get(job_id)
        if job is None or job.answered:
            return

        if outcome == ProcessingOutcome.CANCELLED or job.cancelled:
            job.cancelled = True
            self._cancel_timers(job)
            self._drop_pending(job_id, remember=True)
            logger.info("[livis] job %s 已取消，不回结果", job_id)
            return

        self._cancel_timers(job)
        await self._emit_result(
            job,
            failed=(outcome == ProcessingOutcome.FAILURE),
            reason="hook",
        )

    def _arm_fallback(self, job: LivisJob) -> None:
        """启动/重置兜底计时器。附件到达会再次延后，合并进同一条结果。"""
        if job.answered or job.cancelled:
            return
        if job.fallback_task is not None and not job.fallback_task.done():
            job.fallback_task.cancel()
        job.fallback_task = asyncio.create_task(
            self._fallback_flush(job), name=f"livis-fallback-{job.job_id[:12]}"
        )

    async def _fallback_flush(self, job: LivisJob) -> None:
        try:
            await asyncio.sleep(self._fallback_seconds)
        except asyncio.CancelledError:
            return
        if job.answered or job.cancelled:
            return
        logger.info(
            "[livis] job %s 未收到处理完成钩子，按兜底计时器收口", job.job_id
        )
        self._cancel_timers(job)
        with contextlib.suppress(Exception):
            await self._emit_result(job, failed=False, reason="fallback")

    async def _watchdog(self, job: LivisJob) -> None:
        """派发后长时间毫无产出的兜底 —— 眼镜不能永远等不到回应。"""
        try:
            await asyncio.sleep(self._watchdog_seconds)
        except asyncio.CancelledError:
            return
        if job.answered or job.cancelled:
            return
        logger.error(
            "[livis] job %s 在 %.0fs 内没有任何产出，回兜底提示",
            job.job_id, self._watchdog_seconds,
        )
        job.append_text(WATCHDOG_RESULT_TEXT)
        self._cancel_timers(job)
        with contextlib.suppress(Exception):
            await self._emit_result(job, failed=True, reason="watchdog")

    async def _emit_result(
        self, job: LivisJob, *, failed: bool = False, reason: str = ""
    ) -> None:
        """把收集好的文本+附件作为一条 ``send_result`` 发出。"""
        if job.cancelled or job.answered:
            return
        job.answered = True

        text = (job.text or "").strip()
        if not text and not job.files:
            text = FAILED_RESULT_TEXT if failed else EMPTY_RESULT_TEXT
        data = protocol.result_data(text, job.files)

        # 先落盘再发：进程在这两步之间挂掉时，重启后仍能补发。
        self._store.put(job.job_id, data, job.chat_id)
        logger.info(
            "[livis] job %s 回复(%s): %d 字%s",
            job.job_id, reason or "-", len(text),
            f" + {len(job.files)} 个附件" if job.files else "",
        )
        await self._deliver_result(job.job_id)

    async def _deliver_result(self, job_id: str) -> None:
        entry = self._store.get(job_id)
        if entry is None:
            return
        self._store.bump_attempts(job_id)
        sent = await self._send(
            "send_result", job_id=job_id, payload={"data": entry["data"]}
        )
        if not sent:
            # 连接不在 —— 留着，重连后 _redeliver_pending 会补发。
            logger.info("[livis] job %s 结果暂存，等重连补发", job_id)
            return
        self._arm_ack_timeout(job_id)

    def _arm_ack_timeout(self, job_id: str) -> None:
        old = self._ack_tasks.pop(job_id, None)
        if old and not old.done():
            old.cancel()
        self._ack_tasks[job_id] = asyncio.create_task(
            self._ack_timeout(job_id), name=f"livis-ack-{job_id[:12]}"
        )

    async def _ack_timeout(self, job_id: str) -> None:
        try:
            await asyncio.sleep(ACK_TIMEOUT)
        except asyncio.CancelledError:
            return
        entry = self._store.get(job_id)
        if entry is None:
            return
        if self._ws is None:
            logger.info("[livis] job %s 等 ack 超时且连接已断，等重连补发", job_id)
            return
        if int(entry.get("attempts") or 0) > MAX_ACK_RETRIES:
            logger.error(
                "[livis] job %s 重试 %d 次仍未收到 ack，放弃",
                job_id, MAX_ACK_RETRIES,
            )
            self._drop_pending(job_id, remember=True)
            return
        logger.warning(
            "[livis] job %s ack 超时，第 %d 次重发",
            job_id, int(entry.get("attempts") or 0),
        )
        await self._deliver_result(job_id)

    async def _redeliver_pending(self) -> None:
        """重连后补发所有未确认的结果（含上次进程遗留的）。"""
        for job_id in self._store.pending_ids():
            self._store.reset_attempts(job_id)
            logger.info("[livis] 补发 job %s 的结果", job_id)
            await self._deliver_result(job_id)

    def _drop_pending(self, job_id: str, *, remember: bool) -> None:
        self._store.discard(job_id, remember=remember)
        task = self._ack_tasks.pop(job_id, None)
        if task and not task.done():
            task.cancel()

    # ------------------------------------------------------------------
    # 出站 —— hermes 侧接口
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """把 agent 的回复文本挂到对应 job 上，等收口信号统一回包。

        返回 ``success=True`` 表示"已进入投递队列"——真正的 ``send_result`` 在
        收口时发出，并有独立的 ack 重试与持久化补发。这是收口设计的必然结果：
        若在这里等待投递完成，随后的 ``send_document()`` 就赶不上同一条结果了。
        """
        job = self._resolve_job(chat_id, reply_to)
        if job is None:
            if reply_to:
                job = self._adopt_orphan_job(str(reply_to), chat_id)
            else:
                logger.warning(
                    "[livis] 无法投递（没有对应的 job）：本渠道不支持主动推送，"
                    "只能回复眼镜发起的请求。chat_id=%s", chat_id,
                )
                return SendResult(
                    success=False,
                    error=(
                        "livis: 没有待回复的请求。该渠道不支持主动推送消息，"
                        "只能响应眼镜发起的对话。"
                    ),
                )

        if job.cancelled:
            logger.info("[livis] job %s 已取消，丢弃回复", job.job_id)
            return SendResult(success=True, message_id=job.job_id)
        if job.answered:
            logger.warning(
                "[livis] job %s 已回过包，丢弃迟到的文本（协议只允许一条结果）",
                job.job_id,
            )
            return SendResult(success=True, message_id=job.job_id)

        job.produced = True
        job.append_text(content)
        self._arm_fallback(job)
        return SendResult(success=True, message_id=job.job_id)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: str | None = None,
        file_name: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """上传文档并挂到当前 job 的 ``files`` 上（pdf/html/md/doc(x)，≤100MB）。"""
        job = self._resolve_job(chat_id, reply_to)
        if job is None:
            return SendResult(
                success=False, error="livis: 没有待回复的请求，无法投递附件。"
            )
        if job.cancelled or job.answered:
            return SendResult(success=True, message_id=job.job_id)

        # 路径白名单：挡住被诱导的 agent 把任意本地文件传到理想的对象存储。
        safe_path = self._safe_media_path(file_path)
        if not safe_path:
            logger.warning("[livis] 拒绝上传越界路径: %s", Path(file_path).name)
            return SendResult(
                success=False,
                error="livis: 该文件不在允许投递的媒体目录内，已拒绝上传。",
            )

        reason = upload_rejection_reason(safe_path)
        if reason:
            logger.warning("[livis] 拒绝上传 %s: %s", Path(safe_path).name, reason)
            return SendResult(success=False, error=f"livis: {reason}")

        try:
            descriptor = await upload_document(
                self.creds,
                safe_path,
                job_id=job.job_id,
                client=self.client_name,
                display_name=file_name,
            )
        except (LivisAuthError, RuntimeError, ValueError) as exc:
            logger.error("[livis] 上传失败: %s", exc)
            return SendResult(success=False, error=f"livis: {exc}")

        job.produced = True
        job.files.append(descriptor)
        if caption:
            job.append_text(caption)
        logger.info("[livis] job %s 已附加文档 %s", job.job_id, descriptor.get("name"))
        # 重新计时：让文本和附件合并进同一条 send_result。
        self._arm_fallback(job)
        return SendResult(success=True, message_id=job.job_id)

    def _safe_media_path(self, file_path: str) -> str | None:
        """走 hermes 的媒体投递路径白名单；旧版本没有该 API 时保持可用。"""
        validator = getattr(self, "validate_media_delivery_path", None)
        if not callable(validator):
            return str(file_path)
        try:
            return validator(str(file_path))
        except Exception:
            logger.debug("[livis] 路径校验异常", exc_info=True)
            return None

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """理想中继不接受图片。"""
        return SendResult(
            success=False,
            error=(
                "livis: 该渠道不支持图片附件"
                "（中继仅接受 pdf/html/md/doc(x) 文档）。"
            ),
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: str | None = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self.send_image(chat_id, image_path, caption)

    async def send_voice(
        self, chat_id: str, audio_path: str, **kwargs: Any
    ) -> SendResult:
        """眼镜端自己做 TTS，不需要（也不接受）音频附件。"""
        return SendResult(
            success=False,
            error="livis: 该渠道不支持音频附件，眼镜端会朗读回复文本。",
        )

    async def send_video(
        self, chat_id: str, video_path: str, caption: str | None = None, **kwargs: Any
    ) -> SendResult:
        return SendResult(success=False, error="livis: 该渠道不支持视频附件。")

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        """中继协议没有 typing 指示，空实现。"""
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": "理想眼镜", "type": "dm", "chat_id": chat_id}

    # ------------------------------------------------------------------
    # token 刷新
    # ------------------------------------------------------------------

    async def _refresh_token_on_relay(self) -> None:
        try:
            access_token = await self.creds.get_access_token(force=True)
        except LivisAuthError as exc:
            await self._note_token_failure(f"刷新 access_token 失败: {exc}")
            return

        sent = await self._send(
            "token_refresh",
            job_id="",
            payload={
                "token": access_token,
                "refresh_token": self.creds.refresh_token,
            },
        )
        if not sent:
            await self._note_token_failure("token_refresh 发送失败")
            return

        old = self._token_refresh_ack_task
        if old and not old.done():
            old.cancel()
        self._token_refresh_ack_task = asyncio.create_task(
            self._token_refresh_ack_timeout(), name="livis-token-ack"
        )

    async def _token_refresh_ack_timeout(self) -> None:
        try:
            await asyncio.sleep(TOKEN_REFRESH_ACK_TIMEOUT)
        except asyncio.CancelledError:
            return
        self._token_refresh_ack_task = None
        await self._note_token_failure("等待 token_refreshed 超时（30s）")

    async def _note_token_failure(self, reason: str) -> None:
        self._token_refresh_failures += 1
        logger.error(
            "[livis] token 刷新失败 (%d/%d): %s",
            self._token_refresh_failures, TOKEN_REFRESH_MAX_FAILURES, reason,
        )
        if self._token_refresh_failures >= TOKEN_REFRESH_MAX_FAILURES:
            logger.error("[livis] token 连续刷新失败，断开重连")
            ws = self._ws
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close(code=1008, reason="token refresh failure")


# ---------------------------------------------------------------------------
# 平台注册
# ---------------------------------------------------------------------------

PLATFORM_HINT = (
    "You are reachable through 理想眼镜 (Li Auto smart glasses). Your replies "
    "are read aloud by the glasses' text-to-speech engine and shown in the "
    "companion phone app, so write for the ear: plain conversational prose, "
    "no markdown, no headings, no bullet lists, no tables, no code blocks, "
    "no emoji, and no bare URLs unless the user explicitly asks for a link. "
    "Keep answers short — two or three sentences for most questions — and "
    "front-load the answer instead of building up to it. Spell numbers, dates "
    "and units the way a person would say them. If the user speaks Chinese, "
    "answer in Chinese. There is no typing indicator and no way to send a "
    "follow-up message later: the user hears exactly one reply per request, "
    "so never promise to 'get back to you'. For a long answer, give a short "
    "spoken summary and attach the detail as a document with a "
    "MEDIA:<absolute path> tag — only .pdf, .html, .md, .doc and .docx are "
    "deliverable (100 MB limit); images, audio and video cannot be sent on "
    "this channel. Never claim a document was sent unless a tool returned a "
    "real local path."
)


def has_credentials() -> bool:
    """hermes 目录或 openclaw 目录里存在可用凭据。"""
    from .constants import OPENCLAW_AGENT_ID_FILE, OPENCLAW_TOKENS_FILE, REFRESH_TOKEN_KEY
    from .safeio import read_json

    if LivisCredentials().is_configured():
        return True
    # 装过官方 kit 但还没导入的情况也算"已配置"——connect() 会自动导入。
    try:
        if OPENCLAW_TOKENS_FILE.exists() and OPENCLAW_AGENT_ID_FILE.exists():
            data = read_json(OPENCLAW_TOKENS_FILE, default={})
            agent = OPENCLAW_AGENT_ID_FILE.read_text(encoding="utf-8").strip()
            return bool(
                isinstance(data, dict) and data.get(REFRESH_TOKEN_KEY) and agent
            )
    except OSError:
        return False
    return False


def check_requirements() -> bool:
    """依赖 + 凭据都就绪才允许网关实例化适配器。

    依赖在这里**惰性**导入（而不是模块顶层），这样缺包时插件仍然可被发现、
    ``hermes config`` 也能正确描述它，而不是整个模块 import 失败。
    """
    if os.getenv("LIVIS_ENABLED", "").strip().lower() in {"0", "false", "no"}:
        return False
    try:
        import aiohttp  # noqa: F401
        import websockets  # noqa: F401
    except ImportError:
        return False
    return has_credentials()


def validate_config(config: Any) -> bool:
    return has_credentials()


def is_connected(config: Any) -> bool:
    return has_credentials()


def env_enablement() -> dict[str, Any] | None:
    """凭据就绪时给 ``PlatformConfig.extra`` 播种，让 gateway status 能显示。"""
    if not has_credentials():
        return None
    seed: dict[str, Any] = {
        "node_name": node_name(),
        "protocol_client": client_name(),
    }
    agent_id = LivisCredentials().peek_agent_id()
    if agent_id:
        seed["agent_id"] = agent_id
    return seed


def register(ctx: Any) -> None:
    """插件入口，由 hermes 插件系统在启动时调用。"""
    from . import cli as livis_cli

    ctx.register_platform(
        name=PLATFORM_NAME,
        label=PLATFORM_LABEL,
        adapter_factory=lambda cfg: LivisAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[],
        install_hint="hermes-livis login   # 登录理想账号并生成 Agent ID",
        setup_fn=livis_cli.interactive_setup,
        env_enablement_fn=env_enablement,
        allowed_users_env="LIVIS_ALLOWED_NODE_IDS",
        allow_all_env="LIVIS_ALLOW_ALL_USERS",
        emoji="🕶️",
        # 会话标识是理想侧的不透明 node id，不含手机号/邮箱。
        pii_safe=True,
        # 语音渠道不适合触发自更新这类长流程命令。
        allow_update_command=False,
        platform_hint=PLATFORM_HINT,
        # 故意不设 cron_deliver_env_var / standalone_sender_fn：
        # 理想中继没有"PC 主动找眼镜"的通路（官方插件的 outbound.sendText
        # 是空实现），注册了只会让 cron 投递静默失败。
    )
    with contextlib.suppress(Exception):
        ctx.register_cli_command(
            name="livis",
            help="管理理想眼镜渠道（登录 / 状态 / 登出）",
            setup_fn=livis_cli.register_cli,
            handler_fn=livis_cli.dispatch,
        )
