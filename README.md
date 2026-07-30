# hermes-livis

**理想眼镜 (Li Auto Livis glasses) 的 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 平台适配器。**

以持久外拨的 WebSocket 客户端连接理想 `livis-pc-kit` 中继：收眼镜（经手机 APP /
理想云）下发的语音指令，交给 Hermes agent 处理，再把答案回传给眼镜朗读。

**链路里不再需要 openclaw** —— 本插件替代理想官方的闭源 openclaw 渠道插件
`@chehejia/livis-pc-kit`（协议逆向自其 v2.0.0）。

```
眼镜 ──► 手机 APP / 理想云 ──► 中继 wss://livis-pc-kit-gateway.livis.com
                                    │  ▲        （按 agent_id + token 路由）
                      send_message  │  │  send_result
                                    ▼  │
                              LivisAdapter ──► run_conversation()
```

> 独立实现，与理想汽车无从属或背书关系。端点与 OAuth 应用标识取自公开分发的
> 官方客户端，不含任何密钥。

---

## 快速开始

```bash
pip install hermes-livis          # 或：pip install -e .

hermes-livis install              # 装进 <hermes-home>/plugins/ 并启用
hermes-livis login                # 登录理想账号（打印授权链接，不开浏览器）
# 把打印出来的 Agent ID 填进理想 APP 的「眼镜 → 设备绑定」页
hermes gateway start
```

装过官方 openclaw kit 的机器可以**跳过登录**：

```bash
hermes-livis import-openclaw      # 搬 ~/.openclaw/ 的 token / agent_id / device_id
```

`agent_id` 一致，**眼镜不需要重新绑定**。适配器 `connect()` 时也会自动尝试这次导入。

> ⚠️ 同一个 `agent_id` 只能有一条活跃连接。迁移时**先停掉 openclaw 的
> livis-pc-kit 渠道**，否则两边会互相顶掉。

## 命令

| 命令 | 作用 |
|---|---|
| `hermes-livis install [--home DIR] [--no-enable] [--allow-any-hermes]` | 原子安装到 `<hermes-home>/plugins/livis-glass/` 并写入 `plugins.enabled` |
| `hermes-livis uninstall [--purge]` | 卸载；**默认保留**凭据与投递状态 |
| `hermes-livis doctor [--json]` | 检查安装、启用状态与 Hermes 基类接口 |
| `hermes-livis login [--force] [--open-browser]` | OAuth2 设备码登录 |
| `hermes-livis logout [--local-only] [--show-browser-url]` | 在 IDaaS 撤销 refresh_token 并清本地 |
| `hermes-livis status [--json]` | 凭据与待投递状态（只读，不生成 agent_id） |
| `hermes-livis probe [--timeout N] [--hold 秒]` | **连一次真实中继验证握手是否被接受**；`--hold` 保持连接以便在 APP 里确认「在线」（先停掉 gateway） |
| `hermes-livis echo [--reply 模板] [--duration 秒]` | **联调回声模式**：绕过 Hermes 生命周期直接跑适配器，用桩回复代替 agent |
| `hermes-livis import-openclaw` | 从 `~/.openclaw/` 导入凭据 |
| `hermes-livis reset-agent-id` | 重置 Agent ID（需在 APP 里重新绑定） |

插件装好后，同一套子命令也可用 `hermes livis <子命令>` 调用。

## 工作原理

### 结果收口：两级信号 + 看门狗

协议要求「一个 `job_id` 只回一条 `send_result`」，但 Hermes 的投递顺序是
**先 `send()` 发文本、再 `send_document()` 发附件**，所以 `send()` 不能立刻回包。
本实现叠加三个信号：

1. **主信号** —— `on_processing_complete(event, outcome)`：Hermes 在一轮（含附件
   投递）真正结束时触发，此时立即收口，**零额外延迟**，并能区分
   SUCCESS / FAILURE / CANCELLED。
2. **兜底计时器**（`LIVIS_RESULT_FALLBACK_MS`，默认 5s）—— Hermes 有几条提前
   return 的派发路径不触发上面的钩子（活跃会话下的 bypass 命令、clarify 文本
   拦截等）。第一次 `send()` 时启动，钩子来了就取消。
3. **每 job 看门狗**（`LIVIS_JOB_WATCHDOG_SECONDS`，默认 300s）—— 派发后连一次
   `send()` 都没发生（被授权拒绝、被丢弃等）时兜底回一条提示。

三者都是计时器/回调，**不阻塞派发**：任何一个 job 出问题都不会拖住同一副眼镜的
后续请求。

### 登录可以晚于网关启动

`connect()` 在**没有凭据时也返回 True**，主循环挂在等待态（每 5 秒读一次凭据
文件，不写任何东西、只在状态变化时打日志），`hermes-livis login` 之后自动连上，
**不必重启网关**。返回 False 会让网关认为这个平台坏了、不再重试。

只有「不会自己恢复」的前置条件才返回 False：缺 Python 依赖。

配套地，`LIVIS_ENABLED=true` 让平台在**还没有凭据**时也被启用 —— 否则
`check_requirements()` 返回 False，平台压根不会被实例化，等待循环也就无从谈起。
有凭据时不需要设它（自动启用）。

### 崩溃不丢答案

Hermes 的 delivery ledger 在适配器 `send()` 返回 `success=True` 时就把这一轮标成
「已送达」。本适配器的 `send()` 只是把文本挂进缓冲，真正的 `send_result` 稍后才发
—— 所以 ledger **不会**在崩溃后补发。已产出但未收到 `ack_send_result` 的结果因此
落盘在 `<state>/pending_results.json`（原子写 + fsync + 0600），重连或重启后自动
补发；`completed` 记录同时用于跨重启的重放去重。

### 协议保真

| 细节 | 处理 |
|---|---|
| `connect` 握手 | **裸帧**：不注入 `metadata.client` / `metadata.device_id` / `payload.nodeType`，逐字对齐官方 |
| 其余出站帧 | 一律注入 `metadata.client` + `payload.nodeType` |
| `nodeType` 拼写 | 保留上游的 `personl-device`（少一个 a），有测试防止「顺手修正」 |
| `payload.data` | 入站对象/JSON 字符串两种都吃；出站 `send_result.data` 必须是 **JSON 字符串** |
| `ack_send_message` | **先无条件回 ack，再解析**——ack 是让中继停止重投的信号 |
| `ack_send_result` | `payload.ref_msg_id` → `metadata.job_id` → `metadata.msg_id` 三级兜底 |
| 重连退避 | `min(2^(n-1), 60s)` ±20% jitter，下限 1s；**干净关闭也退避** |
| `device_id` | `pc_<sha256(机器码)>`，复刻 node-machine-id（mac / Linux / Windows 三平台） |
| IDaaS 响应 | 令牌嵌在 `appAudience` 键下要先解一层；轮换的 refresh_token 必须落盘 |

「干净关闭也退避」不是小事：`websockets` 的 `async for` 在服务端正常关闭
（code 1000）时**不抛异常**，而「服务端策略性拒绝我们」最可能就表现为一次干净
关闭。只在异常分支退避会变成对中继的高频重连风暴。

### 安全

* **上传路径白名单** —— 附件走 Hermes 的 `validate_media_delivery_path()`，挡住
  被诱导的 agent 把任意本地文件传到理想的对象存储。
* **凭据落盘** —— `O_EXCL` + `fsync` + `os.replace`，文件 `0600` / 目录 `0700`。
* **日志脱敏** —— 服务端错误正文写日志前先过滤 `*_token` / `Bearer` / form 里的
  令牌字段。
* **不吃环境代理** —— 令牌交换与上传显式 `trust_env=False`，不读 `HTTP_PROXY`。
* **授权** —— 默认委托给理想中继（`authorization_is_upstream`）：它用 OAuth token
  认证连接，且只把消息路由给已绑定到本 `agent_id` 的眼镜。需要严格本地管控时设
  `LIVIS_ALLOWED_NODE_IDS`（值填眼镜的 `from_node_id`），此时自动切回 Hermes 标准
  allowlist（fail-closed）。

## 配置

凭据默认在 `<hermes-home>/livis/`（`LIVIS_STATE_DIR` 可覆盖）：

| 文件 | 内容 |
|---|---|
| `tokens.json` | `{"relay_refresh_token": "..."}`，`0600` |
| `agent.id` | `openclaw-<uuid>`，**理想 APP 里绑定眼镜用的路由键** |
| `device.id` | `pc_<sha256(机器码)>` |
| `pending_results.json` | 未确认的结果 + 去重记录 |

常用环境变量（全部可选，完整列表见 `src/hermes_livis/plugin/plugin.yaml`）：

| 变量 | 默认 | 含义 |
|---|---|---|
| `LIVIS_ENABLED` | 未设 | 设 `true` 可在**没有凭据时也启用**（适配器等待登录，登录后自动连，无需重启网关）；设 `false` 临时停用 |
| `LIVIS_STATE_DIR` | `<hermes-home>/livis` | 凭据与状态目录 |
| `LIVIS_NODE_NAME` | `我的电脑` | 理想 APP 设备列表里的显示名 |
| `LIVIS_RESULT_FALLBACK_MS` | `5000` | 收口兜底窗口 |
| `LIVIS_JOB_WATCHDOG_SECONDS` | `300` | 每请求看门狗 |
| `LIVIS_ALLOWED_NODE_IDS` | 空 | 本地 allowlist；留空=委托中继 |
| `LIVIS_CREDENTIAL_POLL_SECONDS` | `5` | 未登录时多久检查一次凭据（只读文件，不写盘） |
| `LIVIS_LOG_RAW_FRAMES` | 未设 | 设 `1` 记录中继原始帧（令牌脱敏），协议考古用 |
| `LIVIS_CLIENT_NAME` | `openclaw` | 握手 client 标签（改动有被拒风险） |
| `LIVIS_REFRESH_TOKEN` / `LIVIS_AGENT_ID` / `LIVIS_DEVICE_ID` | 读文件 | 容器化时用 secret 注入（注意 refresh_token 会被轮换） |

## 能力与限制

* **附件**：只能投递 pdf / html / htm / md / markdown / doc / docx，单文件 ≤100 MB。
  agent 在回复里写 `MEDIA:/abs/path/report.pdf` 即可。图片 / 音频 / 视频**发不出去**
  （对应 `send_*` 返回明确失败）。眼镜端自己做 TTS，不需要音频附件。
* **不支持主动推送**：官方插件的 `outbound.sendText` 是空实现，中继没有「PC 主动
  找眼镜说话」的通路。因此不注册 cron 投递，`deliver=livis` 不可用；`send()` 找不到
  对应 job 时明确返回失败而不是静默丢弃。
* **不截断长回复**：`MAX_MESSAGE_LENGTH` 设得足够大，避免 Hermes 把一条回复按长度
  切成多段（协议只允许一条结果）。
* **输出风格**：`platform_hint` 已把「给耳朵写字」的规则注入系统提示 —— 纯口语、
  无 markdown / 列表 / 表格 / 代码块 / emoji、两三句话说完、一次请求只有一次回复。

## 兼容性

硬性要求是 **Hermes 基类接口齐备**（安装时实探）：

```
handle_message · build_source · on_processing_complete
interrupt_session_activity · validate_media_delivery_path
```

版本号只做**告警**不做硬闸门 —— `importlib.metadata` 里的 `hermes-agent` 版本经常
与实际运行的源码树不一致（editable 安装、直接跑仓库、多环境混装），据此拒绝安装
会误伤真正兼容的环境。已验证区间 `0.19.0 ≤ v < 0.21.0`。

## 开发

```bash
pip install -e ".[dev]"
HERMES_REPO=/path/to/hermes-agent pytest -q     # 135 个测试
ruff check .
```

测试分两层：协议 / store / safeio / installer 是纯逻辑，不需要 Hermes；适配器与假
中继端到端需要能 import `gateway.*`（用 `HERMES_REPO` 指过去，找不到会自动 skip）。

假中继端到端会真开 WebSocket 服务器与假 IDaaS，覆盖握手字段、先 ack 后处理、
一 job 一结果、干净关闭必须退避、断线后补发未确认结果。

`tests/test_protocol.py` 末尾固化了 **2026-07-30 与真实理想账号联调时抓到的原始
帧**（`REAL_CONNECTED` / `REAL_SEND_MESSAGE` / `REAL_ACK_SEND_RESULT`）。生产
中继的 ack 里**没有** `ref_msg_id`、却带 `{"code":"0"}` 业务状态码 —— 这些是纯
代码逆向看不出来的，只有对上真实字节才算数。

## 已知风险

* **理想是否接受非官方客户端** —— 端点、`client_id`、握手的 `client: "openclaw"`
  标签都沿用官方值以降低被拒概率，但服务端是否校验客户端身份**只能真连一次才知道**。
  用 `hermes-livis probe` 单独验证这一点（它把这个未知从凭据 / 绑定 / agent /
  授权等变量里孤立出来，并把 1008、401、干净关闭等失败翻译成可操作的判词）。
* **协议漂移** —— 私有协议，理想改版需要人工跟。URL 上有 `protocol_version=1`；
  `LIVIS_LOG_RAW_FRAMES=1` 可以看到中继实际发来的全部字段。
* **入站没有图片通路** —— 实测对眼镜说「拍个照片儿」也只送来语音转写的文本，
  帧里没有任何附件字段。有测试盯着，中继哪天开始带媒体字段会立刻失败提醒。
* **env 注入 refresh_token** —— 服务端轮换后失效，长期运行请用文件存储。

## 许可

MIT。见 [LICENSE](LICENSE)。
