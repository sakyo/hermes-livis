# 部署到远程 Hermes 服务器

## 前置

* 一台跑着 Hermes Agent 的服务器（Linux / macOS）
* 一个理想账号，且已有一副绑定到该账号的理想眼镜
* Python ≥ 3.10；`websockets` / `aiohttp` / `pyyaml`（Hermes 通常已带）

## 步骤

### 1. 安装

```bash
# 在 hermes 所在的虚拟环境里
pip install hermes-livis
hermes-livis install
```

`install` 会：把插件负载原子复制到 `<hermes-home>/plugins/livis-glass/`
（有旧版本则先备份，失败自动回滚），并把 `livis-glass` 写进 `config.yaml` 的
`plugins.enabled`。

先自检一下：

```bash
hermes-livis doctor
```

「基类接口 齐备」是唯一的硬性条件。版本号不匹配只会告警，不阻止安装。

> 从 1.0 之前的版本升级时，安装器会自动把旧目录 `livis-platform` 删掉、并从
> `plugins.enabled` 里换成 `livis-glass` —— 两个目录同时存在会各自注册同一个
> 平台名，谁生效取决于扫描顺序。

### 2. 登录

```bash
hermes-livis login
```

会打印一个授权链接（**不会**在服务器上尝试开浏览器），用手机或本地电脑打开，
以手机号 + 短信验证码完成理想账号登录。成功后打印 Agent ID。

**已装过官方 openclaw kit 的机器可以跳过这步：**

```bash
hermes-livis import-openclaw
```

`agent_id` 一致，眼镜不需要重新绑定。

### 3. 在理想 APP 里绑定

把打印出来的 `openclaw-<uuid>` 填进理想 APP 的「眼镜 → 设备/电脑绑定」页。

> ⚠️ **同一个 Agent ID 只能有一条活跃连接。** 若那台机器上还跑着 openclaw 的
> `livis-pc-kit` 渠道，先停掉（`openclaw plugins disable livis-pc-kit` 或停掉
> openclaw gateway），否则两边会互相顶掉连接。

### 4. 启动并核对

```bash
hermes-livis status        # 应显示 ✅ 已就绪
hermes gateway start
```

日志里应看到（`[livis]` 前缀）：

```
[livis] 适配器 v1.0.0 已初始化: node_name=我的电脑 fallback=5.0s watchdog=300s ...
[livis] access_token 已刷新 (...)
[livis] 已启动，agent_id=openclaw-... device_id=... 待投递=0
[livis] 正在连接 wss://livis-pc-kit-gateway.livis.com/api/v1/ws?protocol_version=1
[livis] 握手已发送 (agent_id=openclaw-...)
[livis] 中继已确认连接                     ← 看到这行说明服务端接受了我们
```

对眼镜说一句话，应出现：

```
[livis] job <id> 来自 <node>: <你说的话>
[livis] job <id> 回复(hook): N 字
[livis] job <id> 结果已确认送达
```

## systemd 示例

Hermes 本身怎么跑就怎么跑，本插件不需要独立进程。若用 systemd 管理 Hermes，
把 `LIVIS_*` 放进 `EnvironmentFile` 即可：

```ini
[Service]
Environment=HERMES_HOME=/var/lib/hermes
EnvironmentFile=-/etc/hermes/livis.env
ExecStart=/opt/hermes/venv/bin/hermes gateway start
```

`/etc/hermes/livis.env`（可选，全部有默认值）：

```
LIVIS_NODE_NAME=生产服务器
LIVIS_RESULT_FALLBACK_MS=5000
LIVIS_JOB_WATCHDOG_SECONDS=300
```

凭据不要放进 env —— refresh_token 会被服务端轮换，放 env 里写不回去。让它留在
`<hermes-home>/livis/tokens.json`（`0600`）。

**注意 systemd 下的 `HOME`**：CLI 与网关必须解析到同一个 `<hermes-home>/livis/`，
否则两边各有一份 `agent.id`，APP 里绑的是 CLI 那份，网关却拿另一份去连 —— 表现
就是「已绑定但显示离线」。`hermes-livis probe` 会把它实际用的状态目录打出来，
和网关日志里 `[livis] 适配器已初始化 … state=…` 那一行比对即可。

先起网关、后登录是支持的：适配器会挂在等待态（每 5 秒读一次凭据文件），
`hermes-livis login` 之后自动连上，不必重启网关。前提是设了 `LIVIS_ENABLED=true`
（否则无凭据时平台不会被启用）。

## 容器化

镜像里不要烤进凭据。把状态目录挂成卷：

```yaml
volumes:
  - livis-state:/var/lib/hermes/livis
environment:
  LIVIS_STATE_DIR: /var/lib/hermes/livis
```

首次在容器里执行一次 `hermes-livis login` 完成绑定即可，之后重启不需要再登录。

## 升级

```bash
pip install -U hermes-livis
hermes-livis install          # 原子换入，旧版本自动备份
# 重启 hermes gateway
```

凭据与投递状态不在插件目录里，升级不受影响。

## 卸载

```bash
hermes-livis uninstall            # 保留凭据，随时可以装回来
hermes-livis uninstall --purge    # 连凭据与投递状态一起删
```
