"""理想眼镜渠道的协议常量与端点。

全部取自理想官方 openclaw 渠道插件 ``@chehejia/livis-pc-kit`` v2.0.0 的打包
产物，可用环境变量覆盖（抓包/联调用）。
"""

from __future__ import annotations

import os
from pathlib import Path

PLUGIN_VERSION = "1.0.0"
# 本实现对齐的官方插件版本。官方发新版时先做协议比对再动这里。
REFERENCE_PLUGIN_VERSION = "2.0.0"
PROTOCOL_VERSION = "1"

PLATFORM_NAME = "livis"
PLATFORM_LABEL = "理想眼镜 (Livis)"

DEFAULT_IDAAS_ENDPOINT = "https://id.lixiang.com/api"
DEFAULT_WS_URL = "wss://livis-pc-kit-gateway.livis.com/api/v1/ws"
DEFAULT_HTTP_URL = "https://livis-pc-kit-gateway.livis.com"
DEFAULT_LOGOUT_REDIRECT_URI = "https://li-center.lixiang.com/livis-pc-kit/README.html"

# 公开的 OAuth 应用标识（不是密钥），出现在官方 v2.0.0 客户端里。
DEFAULT_CLIENT_ID = "6qxd1MLZhAtdWipnmXe1dd"
DEFAULT_APP_AUDIENCE = "rZgT0SETDNueMVAhfRN10"
DEFAULT_APP_SCOPE = "super"

# ``metadata.client``。中继按 agent_id + bearer token 路由，这个字段只是节点
# 类型标签；但服务端有可能校验它，默认沿用官方值。
DEFAULT_CLIENT_NAME = "openclaw"

# ``payload.nodeType``。上游拼写如此（personl 少一个 a），必须逐字一致。
NODE_TYPE = "personl-device"

DEFAULT_NODE_NAME = "我的电脑"

REFRESH_TOKEN_KEY = "relay_refresh_token"

# 超时/重试参数，与官方插件一致。
HEARTBEAT_INTERVAL = 30.0
PONG_TIMEOUT = 60.0
ACK_TIMEOUT = 30.0
MAX_ACK_RETRIES = 3
TOKEN_REFRESH_ACK_TIMEOUT = 30.0
TOKEN_REFRESH_MAX_FAILURES = 3
RECONNECT_MAX_DELAY = 60.0

# 收口兜底窗口（毫秒）。正常路径由 ``on_processing_complete`` 钩子精确收口，
# 这个计时器只在钩子没触发时兜底（hermes 有几条提前返回的派发路径不触发钩子）。
DEFAULT_RESULT_FALLBACK_MS = 5_000

# 每个 job 的看门狗（秒）：派发后这么久还没有任何产出就回一条失败提示，
# 避免眼镜侧永远等不到回应。
DEFAULT_JOB_WATCHDOG_SECONDS = 300.0

# 未登录时多久检查一次凭据（秒）。登录可以发生在网关运行期间，适配器挂在等待态
# 轮询即可，不必要求用户重启网关。只读一个小文件，不写任何东西。
DEFAULT_CREDENTIAL_POLL_SECONDS = 5.0

# 中继单帧上限，防止异常大帧打爆内存。
MAX_WIRE_MESSAGE_BYTES = 16 * 1024 * 1024

# 中继只接受这几种文档。
ALLOWED_DOCUMENT_EXTENSIONS = frozenset(
    {"pdf", "html", "htm", "md", "markdown", "doc", "docx"}
)
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MIME_TYPES = {
    "pdf": "application/pdf",
    "html": "text/html",
    "htm": "text/html",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "doc": "application/msword",
    "docx": (
        "application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document"
    ),
}

# 台账上限。
SEEN_JOBS_LIMIT = 512
JOB_TABLE_LIMIT = 64
PENDING_STORE_TTL_SECONDS = 24 * 60 * 60

# openclaw 官方插件的运行时文件，用于凭据迁移。
OPENCLAW_DIR = Path.home() / ".openclaw"
OPENCLAW_TOKENS_FILE = OPENCLAW_DIR / "livis-pc-kit-tokens.json"
OPENCLAW_AGENT_ID_FILE = OPENCLAW_DIR / "livis-agent.id"
OPENCLAW_DEVICE_ID_FILE = OPENCLAW_DIR / "livis-device.id"


def _env(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


def idaas_endpoint() -> str:
    return _env("LIVIS_IDAAS_ENDPOINT", DEFAULT_IDAAS_ENDPOINT).rstrip("/")


def ws_url() -> str:
    return _env("LIVIS_WS_URL", DEFAULT_WS_URL)


def http_url() -> str:
    return _env("LIVIS_HTTP_URL", DEFAULT_HTTP_URL).rstrip("/")


def client_id() -> str:
    return _env("LIVIS_CLIENT_ID", DEFAULT_CLIENT_ID)


def app_audience() -> str:
    return _env("LIVIS_APP_AUDIENCE", DEFAULT_APP_AUDIENCE)


def app_scope() -> str:
    return _env("LIVIS_APP_SCOPE", DEFAULT_APP_SCOPE)


def client_name() -> str:
    return _env("LIVIS_CLIENT_NAME", DEFAULT_CLIENT_NAME)


def node_name() -> str:
    return _env("LIVIS_NODE_NAME", DEFAULT_NODE_NAME)


def hermes_home() -> Path:
    """Hermes 主目录。优先用 hermes 自己的解析逻辑，拿不到再退回环境变量。"""
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        raw = os.getenv("HERMES_HOME", "").strip() or "~/.hermes"
        return Path(raw).expanduser().resolve()


def state_dir() -> Path:
    """凭据与运行状态目录：``LIVIS_STATE_DIR`` 或 ``<hermes-home>/livis``。"""
    override = os.getenv("LIVIS_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return hermes_home() / "livis"
