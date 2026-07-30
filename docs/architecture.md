# 架构

## 分层

```
src/hermes_livis/
├── cli.py            hermes-livis 命令行入口（安装 + 凭据管理）
├── installer.py      原子安装 / 卸载 / doctor / plugins.enabled 维护
└── plugin/           ← 被整目录复制到 <hermes-home>/plugins/livis-platform/
    ├── plugin.yaml   插件清单（kind: platform）
    ├── constants.py  端点、协议常量、超时参数、路径解析
    ├── protocol.py   帧构造与解析（纯函数，与传输解耦）
    ├── auth.py       IDaaS 设备码 / 刷新 / 撤销 + 三个身份要素的存储
    ├── documents.py  文档上传（send_result.files 的来源）
    ├── store.py      待投递结果的持久化 + 跨重启去重
    ├── safeio.py     原子私有写入 + 日志脱敏
    ├── cli.py        hermes livis <子命令>
    └── adapter.py    LivisAdapter（BasePlatformAdapter 子类）+ register()
```

`plugin/` 目录**必须自包含**：内部只用相对导入，不依赖外层 `hermes_livis` 可导入
（有测试与 CI 步骤守着这一点）。

## Hermes 侧接线

| 环节 | 机制 |
|---|---|
| 插件发现 | 装在 `<hermes-home>/plugins/livis-platform/` ⇒ 用户插件 ⇒ 由 `plugins.enabled` 白名单 opt-in（安装器自动写入） |
| 平台名 | 清单名 `livis-platform` 去掉 `-platform` ⇒ `livis`；`Platform("livis")` 经 `platform_registry.is_registered` 的运行时回退解析 |
| 平台启用 | `check_requirements()` / `is_connected()` 检查凭据是否齐备；齐备则 `load_gateway_config()` 自动置 `enabled=True` 并播种 `extra` |
| 入站 | `handle_message(MessageEvent)` → 后台跑 agent |
| 出站 | `send()` / `send_document()` 写入 job 缓冲 |
| 收口 | `on_processing_complete(event, outcome)` |
| 中断 | `interrupt_session_activity(session_key, chat_id)` |
| 附件安全 | `validate_media_delivery_path(path)` |

## 一次请求的生命周期

```
中继 ──send_message──►  _handle_frame
                          │
                          ├─► ack_send_message        （无条件，先于解析）
                          ├─► 去重（内存 seen + store.completed）
                          │     └─ 命中且结果未 ack ⇒ 直接补发，不重跑 agent
                          ├─► parse_exec_request      （失败也已 ack）
                          └─► _dispatch
                                ├─ build_source / build_session_key
                                ├─ 注册 job + 启动看门狗
                                └─ handle_message(event)   ← 立即返回
                                        │
                        Hermes 后台跑 agent
                                        │
                          send(text) ──► job.text += ; 启动兜底计时器
                          send_document ─► 路径白名单 → 上传 → job.files += ; 重置计时器
                                        │
                    on_processing_complete ─► 立即收口（取消所有计时器）
                                        │
                                  _emit_result
                                        ├─ store.put(job_id, data)   ← 先落盘
                                        └─ send_result ──► 中继
                                                │
                              ack_send_result ─► store.complete(job_id)
                              （30s 未 ack ⇒ 重发，最多 3 次）
```

## 三条不变量

1. **一个 `job_id` 只回一条 `send_result`。** `job.answered` 是唯一开关，钩子、
   兜底计时器、看门狗都要过它。
2. **收到 `send_message` 一定回 `ack_send_message`。** 解析失败、类型不支持、
   内容为空，都在 ack 之后才判断。
3. **`_run_connection()` 返回即退避。** 无论是抛异常还是干净关闭，一律走
   `min(2^(n-1), 60s)` ±20% jitter。

## 状态目录

`<hermes-home>/livis/`（`LIVIS_STATE_DIR` 可覆盖），目录 `0700`、文件 `0600`：

| 文件 | 内容 | 谁写 |
|---|---|---|
| `tokens.json` | `relay_refresh_token` | auth（登录 / 轮换） |
| `agent.id` | `openclaw-<uuid>` | auth（首次登录 / reset） |
| `device.id` | `pc_<sha256>` | auth（首次使用 / openclaw 导入） |
| `pending_results.json` | 未确认结果 + 去重记录 | store |
