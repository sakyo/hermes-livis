# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-07-30

首个发行版。协议对齐理想官方 openclaw 渠道插件 `@chehejia/livis-pc-kit` v2.0.0。

### 新增

- **平台适配器** `livis`：持久外拨 WebSocket 客户端，完整实现中继 v1 协议 ——
  握手、心跳、`send_message`/`ack`、`send_result`/`ack` + 重试、`cancel_chat`
  中断、`token_expiring` 刷新、指数退避重连。
- **认证**：Python 实现的 OAuth2 设备码登录（RFC 8628）、令牌刷新与轮换、
  `/revoke` 登出；服务器友好（默认不开浏览器，只打印授权链接）。
- **openclaw 凭据迁移**：`import-openclaw` 搬运 `~/.openclaw/` 的 token /
  agent_id / device_id，`agent_id` 一致因此眼镜无需重新绑定；适配器 `connect()`
  也会自动尝试。
- **两级收口 + 看门狗**：主信号 `on_processing_complete` 钩子（零延迟、区分
  SUCCESS/FAILURE/CANCELLED），兜底计时器覆盖 Hermes 不触发钩子的派发路径，
  每 job 看门狗兜底「毫无产出」的情况。三者均为计时器/回调，不阻塞派发。
- **结果持久化** `pending_results.json`：已产出但未 ack 的结果原子落盘，重连或
  **进程重启后**自动补发；`completed` 记录用于跨重启的重放去重。
- **文档投递**：pdf / html / htm / md / markdown / doc / docx，≤100 MB，经
  Hermes 的 `validate_media_delivery_path()` 路径白名单。
- **安装器**：原子安装 + 旧版本备份 + 失败回滚，自动维护 `plugins.enabled`；
  `uninstall` 默认保留凭据，`--purge` 才清；`doctor` 实探 Hermes 基类接口。
- **CLI**：`hermes-livis` 独立入口，插件装好后同一套子命令也可用
  `hermes livis <子命令>`。
- **测试**：97 个 —— 协议字节级契约、store / safeio、适配器行为、安装器，以及
  真开 WebSocket 服务器的假中继端到端。

### 设计取舍（与官方插件的有意差异）

- **断线不中断在跑的 agent**：官方断线即 abort（靠其 SQLite 重放补偿），这里改为
  结果落盘 + 重连补发，用户不丢答案。
- **不支持主动推送**：官方 `outbound.sendText` 是空实现，中继无此通路。因此不注册
  cron 投递，`send()` 找不到对应 job 时明确失败而非静默丢弃。
- **不截断长回复**：官方的 4000 字符 chunk limit 是给它那个空实现 sender 的提示；
  这里把 `MAX_MESSAGE_LENGTH` 设得足够大，避免 Hermes 把回复切成多段（协议只允许
  一条结果）。
- **默认委托上游授权**：中继已用 OAuth token 认证并只路由已绑定眼镜；设
  `LIVIS_ALLOWED_NODE_IDS` 可切回 fail-closed 的本地 allowlist。
- **版本号只告警**：硬闸门是 Hermes 基类接口实探，不是版本号 ——
  `importlib.metadata` 的版本常与实际源码树不一致，据此拒装是误伤。

### 已知风险

- 理想服务端是否接受非官方客户端**只能真连一次才能验证**。端点、`client_id`、
  握手的 `client: "openclaw"` 标签均沿用官方值以降低被拒概率。
- 私有协议，理想改版需要人工跟进（URL 上有 `protocol_version=1`）。
