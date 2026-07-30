"""理想 IDaaS 认证：设备码登录、令牌刷新、撤销，以及三个身份要素的存储。

身份三要素：

* ``refresh_token`` —— OAuth2 设备码登录（RFC 8628）拿到，会被服务端轮换；
* ``agent_id``      —— 本地生成的 ``openclaw-<uuid>``，在理想 APP 里绑定到眼镜，
                       中继按它路由；
* ``device_id``     —— ``pc_<sha256(机器码)>``，复刻 node-machine-id 的算法。

装过官方 openclaw kit 的机器可以一键导入这三样（``agent_id`` 一致，眼镜无需
重新绑定）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import (
    DEFAULT_LOGOUT_REDIRECT_URI,
    OPENCLAW_AGENT_ID_FILE,
    OPENCLAW_DEVICE_ID_FILE,
    OPENCLAW_TOKENS_FILE,
    REFRESH_TOKEN_KEY,
    app_audience,
    app_scope,
    client_id,
    idaas_endpoint,
    state_dir,
)
from .safeio import (
    ensure_private_dir,
    read_json,
    redact_body,
    redact_secret,
    write_private_json,
    write_private_text,
)

logger = logging.getLogger(__name__)


class LivisAuthError(RuntimeError):
    """认证/凭据类错误。"""


@dataclass(frozen=True)
class AccessToken:
    value: str
    expires_at: float


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


# ---------------------------------------------------------------------------
# device_id —— 复刻 node-machine-id 的 machineIdSync()
# ---------------------------------------------------------------------------

def _raw_machine_id() -> str:
    """取原始机器码，语义与 node-machine-id 的平台命令一致。

    node-machine-id 对结果做 ``replace(/\\r+|\\n+|\\s+/g, "").toLowerCase()``，
    这里同样先剔除所有空白再小写，保证同一台机器上 Python 与 Node 得到相同
    的输入（因此 device_id 也相同）。
    """
    plat = sys.platform
    raw = ""
    try:
        if plat == "darwin":
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            if "IOPlatformUUID" in out:
                tail = out.split("IOPlatformUUID", 1)[1].split("\n", 1)[0]
                raw = tail.replace("=", "").replace('"', "")
        elif plat == "win32":
            out = subprocess.run(
                [
                    "REG.exe", "QUERY",
                    r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Cryptography",
                    "/v", "MachineGuid",
                ],
                capture_output=True, text=True, timeout=10,
            ).stdout
            if "REG_SZ" in out:
                raw = out.split("REG_SZ", 1)[1]
        else:
            # Linux/*BSD：node 的命令是
            #   ( cat /var/lib/dbus/machine-id /etc/machine-id 2>/dev/null
            #     || hostname ) | head -n 1
            # 即"存在的那些文件拼起来的第一行"。**顺序不能反**，dbus 那个在前。
            for candidate in ("/var/lib/dbus/machine-id", "/etc/machine-id"):
                try:
                    text = Path(candidate).read_text(encoding="utf-8")
                except OSError:
                    continue
                lines = text.strip().splitlines()
                if lines and lines[0].strip():
                    raw = lines[0]
                    break
            if not raw:
                import socket

                raw = socket.gethostname()
    except (OSError, subprocess.SubprocessError, IndexError) as exc:
        logger.warning("Livis: 读取机器码失败（%s），退化为随机 UUID", exc)

    if not raw:
        raw = str(uuid.uuid4())
    return "".join(raw.split()).lower()


def compute_device_id() -> str:
    return "pc_" + hashlib.sha256(_raw_machine_id().encode("utf-8")).hexdigest()


def new_agent_id() -> str:
    """新建 agent_id。

    前缀沿用 ``openclaw-``：中继与理想 APP 按这个格式识别 PC Kit 节点，改前缀
    有被拒的风险。Hermes 才是真正的运行时，这里只保留协议兼容。
    """
    return f"openclaw-{uuid.uuid4()}"


# ---------------------------------------------------------------------------
# 凭据存储
# ---------------------------------------------------------------------------

class LivisCredentials:
    """``<state_dir>`` 下的 ``tokens.json`` / ``agent.id`` / ``device.id``。

    三者都能用环境变量直接覆盖（``LIVIS_REFRESH_TOKEN`` / ``LIVIS_AGENT_ID``
    / ``LIVIS_DEVICE_ID``），便于容器化。注意 refresh_token 会被服务端轮换，
    用 env 注入时轮换值写不回去，长期运行建议用文件。
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        self._dir = Path(directory) if directory else state_dir()
        self._tokens_file = self._dir / "tokens.json"
        self._agent_file = self._dir / "agent.id"
        self._device_file = self._dir / "device.id"
        self._access: AccessToken | None = None
        self._lock = asyncio.Lock()

    # -- 路径 ---------------------------------------------------------------

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def tokens_file(self) -> Path:
        return self._tokens_file

    # -- refresh token ------------------------------------------------------

    def _tokens(self) -> dict[str, Any]:
        value = read_json(self._tokens_file, default={})
        return value if isinstance(value, dict) else {}

    @property
    def refresh_token(self) -> str:
        env_token = os.getenv("LIVIS_REFRESH_TOKEN", "").strip()
        if env_token:
            return env_token
        return str(self._tokens().get(REFRESH_TOKEN_KEY) or "")

    def set_refresh_token(self, token: str) -> None:
        tokens = self._tokens()
        tokens[REFRESH_TOKEN_KEY] = token
        tokens["updated_at"] = int(time.time())
        write_private_json(self._tokens_file, tokens)

    def clear_tokens(self) -> None:
        """清空本地令牌（登出，或 refresh_token 被服务端判定失效）。"""
        with contextlib.suppress(FileNotFoundError):
            self._tokens_file.unlink()
        self._access = None

    # -- agent / device id --------------------------------------------------

    @property
    def agent_id(self) -> str:
        env_id = os.getenv("LIVIS_AGENT_ID", "").strip()
        if env_id:
            return env_id
        try:
            return self._agent_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def set_agent_id(self, agent_id: str) -> None:
        write_private_text(self._agent_file, agent_id + "\n")

    def ensure_agent_id(self) -> str:
        return self.agent_id or self._mint_agent_id()

    def _mint_agent_id(self) -> str:
        fresh = new_agent_id()
        self.set_agent_id(fresh)
        return fresh

    def reset_agent_id(self) -> str:
        return self._mint_agent_id()

    @property
    def device_id(self) -> str:
        env_id = os.getenv("LIVIS_DEVICE_ID", "").strip()
        if env_id:
            return env_id
        try:
            existing = self._device_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass
        computed = compute_device_id()
        # 写不下去也要返回算出来的值：device_id 是可复算的，落盘只是缓存。
        with contextlib.suppress(OSError):
            write_private_text(self._device_file, computed + "\n")
        return computed

    # -- 只读探针（不产生副作用） -------------------------------------------

    def peek_agent_id(self) -> str:
        """读 agent_id 但**不生成**——给 status 这类只读命令用。"""
        return self.agent_id

    def is_configured(self) -> bool:
        return bool(self.refresh_token and self.agent_id)

    # -- 从 openclaw 迁移 ---------------------------------------------------

    def import_from_openclaw(self) -> list[str]:
        """搬运官方 kit 已有的凭据（缺什么补什么），返回搬动过的项。

        device_id 一并搬：绑定关系有可能与它相关，重算一个新的可能被服务端拒。
        """
        imported: list[str] = []

        if not self.refresh_token and OPENCLAW_TOKENS_FILE.exists():
            data = read_json(OPENCLAW_TOKENS_FILE, default={})
            token = data.get(REFRESH_TOKEN_KEY) if isinstance(data, dict) else None
            if token:
                self.set_refresh_token(str(token))
                imported.append("refresh_token")

        if not self.agent_id and OPENCLAW_AGENT_ID_FILE.exists():
            try:
                agent = OPENCLAW_AGENT_ID_FILE.read_text(encoding="utf-8").strip()
            except OSError:
                agent = ""
            if agent:
                self.set_agent_id(agent)
                imported.append("agent_id")

        if not self._device_file.exists() and OPENCLAW_DEVICE_ID_FILE.exists():
            try:
                device = OPENCLAW_DEVICE_ID_FILE.read_text(encoding="utf-8").strip()
            except OSError:
                device = ""
            if device:
                write_private_text(self._device_file, device + "\n")
                imported.append("device_id")

        if imported:
            logger.info("Livis: 已从 openclaw 导入 %s", ", ".join(imported))
        return imported

    # -- access token -------------------------------------------------------

    async def get_access_token(self, force: bool = False) -> str:
        """用 refresh_token 换 access_token（带缓存与并发锁）。

        锁很关键：refresh_token 是轮换的，两个协程拿同一个旧 token 并发刷新时，
        第二个会收到 401，而 401 分支会清空本地凭据 —— 等于把整个登录态抹掉。
        """
        async with self._lock:
            now = time.time()
            if not force and self._access and self._access.expires_at - now > 60:
                return self._access.value

            token = self.refresh_token
            if not token:
                raise LivisAuthError(
                    "没有 refresh_token。先执行 `hermes-livis login` 登录理想账号。"
                )

            status, body = await _post_form_async(
                f"{idaas_endpoint()}/token",
                {"grant_type": "refresh_token", "refresh_token": token},
            )
            if status == 401:
                self.clear_tokens()
                raise LivisAuthError(
                    "refresh_token 已过期，请重新登录：hermes-livis login"
                )
            if status >= 400 or not isinstance(body, dict):
                raise LivisAuthError(
                    f"IDaaS /token 失败 HTTP {status}: {redact_body(body)}"
                )

            self._access = self._store_token_response(body)
            logger.info(
                "Livis: access_token 已刷新 (%s)", redact_secret(self._access.value)
            )
            return self._access.value

    def _store_token_response(self, body: dict[str, Any]) -> AccessToken:
        """解析 /token 响应；理想 IDaaS 会把令牌嵌在 appAudience 键下面。"""
        data = body
        audience = app_audience()
        if audience and isinstance(body.get(audience), dict):
            data = body[audience]

        access = str(data.get("access_token") or "")
        if not access:
            raise LivisAuthError(
                f"IDaaS 响应里没有 access_token: {redact_body(body)}"
            )
        rotated = str(data.get("refresh_token") or "")
        if rotated and rotated != self.refresh_token:
            self.set_refresh_token(rotated)
        try:
            expires_in = int(data.get("expires_in") or body.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        return AccessToken(access, time.time() + max(60, expires_in))

    # -- 同步版本（CLI 用） -------------------------------------------------

    def get_access_token_sync(self, force: bool = False) -> str:
        now = time.time()
        if not force and self._access and self._access.expires_at - now > 60:
            return self._access.value
        token = self.refresh_token
        if not token:
            raise LivisAuthError("没有 refresh_token，请先登录。")
        status, body = _post_form_sync(
            f"{idaas_endpoint()}/token",
            {"grant_type": "refresh_token", "refresh_token": token},
        )
        if status == 401:
            self.clear_tokens()
            raise LivisAuthError("refresh_token 已过期，请重新登录。")
        if status >= 400 or not isinstance(body, dict):
            raise LivisAuthError(
                f"IDaaS /token 失败 HTTP {status}: {redact_body(body)}"
            )
        self._access = self._store_token_response(body)
        return self._access.value


# ---------------------------------------------------------------------------
# HTTP 辅助（aiohttp 惰性导入；hermes 本身就依赖它）
# ---------------------------------------------------------------------------

async def _post_form_async(
    url: str, form: dict[str, str], *, timeout: float = 30.0
) -> tuple[int, Any]:
    import aiohttp

    # aiohttp 的 ClientSession 默认 trust_env=False：不读 HTTP_PROXY 等环境
    # 变量，令牌交换不会被环境里的代理劫持。这里显式写出来以免日后被改掉。
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(
            timeout=client_timeout, trust_env=False
        ) as session, session.post(
            url, data=form, allow_redirects=False
        ) as resp:
            return resp.status, await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise LivisAuthError(f"IDaaS 网络错误: {exc}") from exc


def _post_form_sync(
    url: str, form: dict[str, str], *, timeout: float = 30.0
) -> tuple[int, Any]:
    import json as _json
    import urllib.error
    import urllib.parse
    import urllib.request

    data = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    # 显式使用不带代理的 opener：与异步路径的 trust_env=False 保持一致。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, _json.loads(raw) if raw else {}
        except ValueError:
            return exc.code, raw
    except urllib.error.URLError as exc:
        raise LivisAuthError(f"IDaaS 网络错误: {exc.reason}") from exc


# ---------------------------------------------------------------------------
# 设备码登录 / 登出（同步，供 CLI 使用）
# ---------------------------------------------------------------------------

def request_device_code(force: bool = False) -> DeviceCode:
    """第 1 步：``POST /aux``。"""
    form = {
        "client_id": client_id(),
        "scope": f"{app_scope()} offline_access",
        "audience": app_audience(),
        "offline_access": "true",
    }
    if force:
        form["prompt"] = "login"

    status, body = _post_form_sync(f"{idaas_endpoint()}/aux", form)
    if status == 404:
        raise LivisAuthError(
            "IDaaS /aux 返回 404 —— 该 client_id 未开启设备码授权。\n"
            "改用官方 `openclaw livis-pc-kit login` 登录后，执行 "
            "`hermes-livis import-openclaw` 导入凭据。"
        )
    if status >= 400 or not isinstance(body, dict) or not body.get("device_code"):
        raise LivisAuthError(
            f"请求设备码失败 HTTP {status}: {redact_body(body)}"
        )
    return DeviceCode(
        device_code=str(body["device_code"]),
        user_code=str(body.get("user_code") or ""),
        verification_uri=str(
            body.get("verification_uri_complete")
            or body.get("verification_uri")
            or ""
        ),
        expires_in=int(body.get("expires_in") or 600),
        interval=max(1, int(body.get("interval") or 5)),
    )


def poll_for_token(
    credentials: LivisCredentials,
    code: DeviceCode,
    *,
    on_pending: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """第 2 步：轮询 ``POST /token`` 直到用户完成授权，返回 refresh_token。"""
    deadline = time.monotonic() + code.expires_in
    interval = float(code.interval)
    while time.monotonic() < deadline:
        sleep(interval)
        status, body = _post_form_sync(
            f"{idaas_endpoint()}/token",
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": code.device_code,
                "client_id": client_id(),
            },
        )
        if status < 400 and isinstance(body, dict):
            credentials._store_token_response(body)
            refresh = credentials.refresh_token
            if not refresh:
                raise LivisAuthError(
                    "IDaaS 没有返回 refresh_token —— 无法维持长连接。"
                    "请确认该 client 已开启 offline_access。"
                )
            return refresh

        error = body.get("error") if isinstance(body, dict) else ""
        if error == "authorization_pending":
            if on_pending:
                on_pending()
            continue
        if error == "slow_down":
            interval += 5.0
            continue
        if error == "expired_token":
            raise LivisAuthError("设备码已过期，请重新执行登录。")
        if error == "access_denied":
            raise LivisAuthError("用户拒绝了授权。")
        if status >= 400:
            raise LivisAuthError(
                f"授权失败 HTTP {status}: {redact_body(body)}"
            )
    raise LivisAuthError("等待授权超时，设备码已过期。")


def revoke(credentials: LivisCredentials) -> bool:
    """在 IDaaS 上撤销 refresh_token，然后清空本地（服务端失败也清本地）。"""
    refresh = credentials.refresh_token
    if not refresh:
        return False
    revoked = False
    try:
        status, _ = _post_form_sync(
            f"{idaas_endpoint()}/revoke",
            {
                "token": refresh,
                "token_type_hint": "refresh_token",
                "client_id": client_id(),
            },
        )
        revoked = status < 400
    except LivisAuthError:
        revoked = False
    finally:
        credentials.clear_tokens()
    return revoked


def browser_logout_url() -> str:
    """浏览器侧登出 URL（清掉 IDaaS 会话，便于换账号）。"""
    import urllib.parse

    params = urllib.parse.urlencode(
        {
            "client_id": client_id(),
            "post_logout_redirect_uri": DEFAULT_LOGOUT_REDIRECT_URI,
        }
    )
    return f"{idaas_endpoint()}/logout?{params}"


def ensure_state_dir() -> Path:
    return ensure_private_dir(state_dir())
