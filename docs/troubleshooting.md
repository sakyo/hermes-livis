# 排查

**先跑这一条**，它会把下面大部分情况直接判出来：

```bash
hermes-livis why-offline
```

所有日志行都带 `[livis]` 前缀。`hermes-livis doctor` / `status` / `logs`
也都是只读的。

## 装了、绑定了，但一行 `[livis]` 日志都没有

适配器**从未被实例化**。按概率排：

1. **装完没重启网关** —— 插件只在**进程启动时**被发现，往 `plugins/` 放文件、
   往 `config.yaml` 写 `enabled`，对已经在跑的进程毫无影响。`hermes gateway restart`。
2. **装错 profile** —— 非默认 profile 的 `HERMES_HOME` 是
   `<root>/profiles/<name>/`，插件/凭据/日志全在那底下。用
   `HERMES_HOME=~/.hermes/profiles/<name> hermes-livis install`。
3. **没写进 `plugins.enabled`** —— 用户插件是 opt-in 的，光复制目录不会被加载。
4. **无凭据且没设 `LIVIS_ENABLED=true`** —— 平台不会被启用（这是刻意的，
   免得打扰从没配过这条渠道的人）。

## 平台压根没出现

`hermes gateway status` 里看不到 livis。

| 检查 | 命令 / 现象 |
|---|---|
| 插件装了吗 | `hermes-livis doctor` → 「已安装」 |
| 写进 `plugins.enabled` 了吗 | `hermes-livis doctor` → 「已启用」；没有就重跑 `hermes-livis install` |
| 凭据齐了吗 | `hermes-livis status` → refresh_token 与 agent_id 都要有 |
| 被显式关掉了吗 | `LIVIS_ENABLED=false` |
| 依赖装了吗 | `python -c "import websockets, aiohttp"` |

凭据不齐时平台**故意不出现** —— 免得网关反复去连一个必然失败的中继。

## 握手之后就断

日志里有「握手已发送」但没有「中继已确认连接」。

* **收到关闭码 1008 / 401** —— 大概率是服务端拒绝了这个客户端，或 token 无效。
  先 `hermes-livis logout && hermes-livis login` 排除令牌问题；仍然被拒就是服务端
  在校验客户端身份，本方案暂时走不通（可退回「openclaw 当边车 + hermes 当大脑」）。
* **反复重连，每次都很快断** —— 检查是不是**同一个 agent_id 有两条连接**：那台机器
  上还跑着 openclaw 的 `livis-pc-kit` 渠道，或者你在两台机器上用了同一个 agent_id。
  停掉其中一个。
* **退避一直在涨** —— 正常。退避是 `min(2^(n-1), 60s)`，连不上时会退到 60 秒一次。

## 眼镜说了话，但没反应

按日志出现的顺序定位：

1. **没有 `job <id> 来自 ...`** —— 消息没到。确认 APP 里绑定的 Agent ID 与
   `hermes-livis status` 显示的一致。
2. **有 job 行，但没有「回复」行** —— agent 没产出。等看门狗（默认 300s）会回一条
   兜底提示；同时查 hermes 主日志里这一轮的报错。
3. **回复行显示 `回复(watchdog)`** —— 派发后完全没有产出。最常见的原因是**授权被拒**
   （主日志里有 `Unauthorized user`）：检查是不是设了 `LIVIS_ALLOWED_NODE_IDS` 但
   值不对。留空即委托中继授权，开箱可用。
4. **回复行显示 `回复(fallback)`** —— 走的是兜底计时器而不是钩子。功能正常，只是
   多等了 `LIVIS_RESULT_FALLBACK_MS`。通常发生在 Hermes 的 bypass 命令 / clarify
   拦截路径上。
5. **有「回复」但没有「结果已确认送达」** —— 中继没回 ack。会 30 秒重发一次、最多
   3 次；期间结果一直留在 `pending_results.json`，重连或重启后自动补发。

## 附件发不出去

* `不在允许投递的媒体目录内` —— 触发了 Hermes 的媒体路径白名单。把文件写到 Hermes
  允许的输出目录里（通常是 `<hermes-home>` 下的工作目录），不要用任意绝对路径。
* `不支持 .xxx 附件` —— 中继只收 pdf / html / htm / md / markdown / doc / docx。
* `不支持图片附件` —— 中继确实不接受图片/音频/视频。让 agent 输出 HTML 或 PDF。

## 回复被念成一长串 markdown

`platform_hint` 已经要求纯口语无格式，但模型可能不听。可以在 profile 的系统提示里
再强调一次，或降低回复长度上限。本插件**不截断**回复（协议只允许一条结果，截断会
静默丢内容）。

## 登录相关

* **`/aux 返回 404`** —— 该 client_id 没开设备码授权。改用官方
  `openclaw livis-pc-kit login` 登录，再 `hermes-livis import-openclaw` 导入。
* **`refresh_token 已过期`** —— 重新 `hermes-livis login`。agent_id 保留着，
  眼镜不用重新绑定。
* **想换账号** —— `hermes-livis logout --show-browser-url`，按提示在浏览器里清掉
  IDaaS 会话，再 `hermes-livis login --force`。

## 待投递积压

```bash
hermes-livis status        # 看「待确认结果」
```

正常应该是 0 或个位数。持续增长说明中继一直不回 `ack_send_result` —— 检查连接是否
稳定。记录超过 24 小时会被自动清理。

## 把日志开到 DEBUG

```bash
HERMES_LOG_LEVEL=DEBUG hermes gateway start 2>&1 | grep livis
```

日志里的令牌一律脱敏（`[redacted]` / `abcdef…128chars`），可以直接贴出来求助。
