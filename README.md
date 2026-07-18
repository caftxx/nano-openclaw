# nano-openclaw

[![Tests](https://github.com/caftxx/nano-openclaw/actions/workflows/test.yml/badge.svg)](https://github.com/caftxx/nano-openclaw/actions/workflows/test.yml)

用最少的代码复刻 OpenClaw 的 agent 运行原理：**真实可跑，但只保留骨架，删掉一切可选层**。

读完这个仓库里的核心 `.py` 文件，你就理解了一个"会用工具的 LLM agent"的全部秘密。

## 安装

推荐用 [uv](https://github.com/astral-sh/uv)：

```bash
# 一行装好全局命令
uv tool install nano-openclaw

# 或者不安装直接跑（每次自动拉取最新版）
uvx nano-openclaw
```

第一次运行 `nano-openclaw` 会自动把模板配置拷到 `~/.nano-openclaw/`，编辑里面的 `nano-openclaw.json5` 填入 API key 即可。

可选安装 Zvec 本地索引 provider：

```bash
uv tool install "nano-openclaw[zvec]"
```

默认配置仍使用 `lexical` 记忆搜索；安装 extra 只提供 `memorySearch.provider: "zvec"` 的可选能力，不会自动开启。`zvec` provider 的 FTS/BM25 模式不需要 embedding 模型；`local_dense` dense/hybrid 模式会在首次运行时从 Hugging Face 或 ModelScope 下载本地 embedding 模型。Zvec 0.5.0 发布 Linux x86_64/aarch64、Windows amd64、macOS arm64 wheel；Intel macOS 上该 extra 不安装 Zvec，`memory_search` 会继续回退到 `lexical`。配置示例见 [memorySearch — 记忆搜索配置](docs/CONFIG_EXAMPLE.md#memorysearch--记忆搜索配置)。

升级 / 卸载：

```bash
uv tool upgrade nano-openclaw
uv tool uninstall nano-openclaw
# 想顺手清掉配置：Linux/macOS 用 `rm -rf ~/.nano-openclaw`，
# Windows 用 `Remove-Item -Recurse -Force $HOME\.nano-openclaw`
```

默认安装支持 Linux / macOS / Windows（纯 Python wheel，无平台特定二进制依赖）。可选 extras 可能引入对应平台的原生 wheel。

### Development setup

想改代码 / 跑测试的话走源码：

```bash
git clone git@github.com:caftxx/nano-openclaw.git
cd nano-openclaw
uv sync

# 跑测试（不需要 API key，纯本地工具单测）
uv run pytest tests/

# 仓库自带一份 .nano-openclaw-dev/ 作为模板。nano-openclaw 默认识别项目根的
# .nano-openclaw/ —— 把模板拷过去再改：
cp -r .nano-openclaw-dev .nano-openclaw

# 然后编辑 .nano-openclaw/nano-openclaw.json5，填入你的 API key 和 provider 信息
```

下面命令在源码模式用 `uv run nano-openclaw …` 形式；全局安装（`uv tool install` 之后）去掉 `uv run` 前缀即可。

```bash
# 默认：进入 TUI（自动探测本机 daemon；没探测到就走单进程 embedded REPL）
uv run nano-openclaw

# 显式 TUI（同上）
uv run nano-openclaw tui

# TUI 远程连远程 daemon
uv run nano-openclaw tui --connect ws://remote-host:5000/rpc

# 启动 / 停止 / 查看 daemon —— daemon 内部跑 WebUI、WeChat、cron、subagent 等
uv run nano-openclaw gateway start              # 后台 spawn detached
uv run nano-openclaw gateway start --port 8080  # 覆盖 config 端口
uv run nano-openclaw gateway status             # 多行结构化状态报告
uv run nano-openclaw gateway stop
uv run nano-openclaw gateway run                # 前台模式（systemd / docker 用）

# 顶层 back-compat flags（等价于 tui --resume / tui --list-sessions）
uv run nano-openclaw --resume
uv run nano-openclaw --sessions

# 指定配置文件走环境变量（CLI 不再有 --config 顶层 flag）
NANO_OPENCLAW_CONFIG_PATH=./my-config.json5 uv run nano-openclaw
```

**架构一览：**

```
daemon / adapters / api
          │
          ▼
       services
          │
          ▼
        core
          │
          ▼
 config / session primitives / provider SDKs / filesystem
```

`gateway run/start` 只负责进程生命周期和组装：WebUI/TUI/WeChat/xiaozhi adapters、`/rpc` API、BackendService、runtime、scheduler。业务逻辑集中在 `services/` 和 `features/`，模型循环、provider、工具执行等纯 agent 内核在 `core/`。

**接入方式**：daemon 起来后可被 TUI remote、WebUI/voice、WeChat、xiaozhi-esp32 channel 共享接入，详见下文 [接入方式](#接入方式) 章节。WebUI/TUI 是 frontend adapters，不算 `channels`；`/channels` 只列 WeChat、xiaozhi 这类外部消息通道。

---

### Docker Compose 启动

不想装 Python 环境？用 Docker Compose 一键启动 gateway daemon：

```bash

# 仓库自带一份 .nano-openclaw-dev/ 作为模板。nano-openclaw 默认识别项目根的
# .nano-openclaw/ —— 把模板拷过去再改：
cp -r .nano-openclaw-dev .nano-openclaw

# 复制环境变量模板并填入 API key
cp .env.example .env

# 启动 daemon（后台运行，访问 http://localhost:5000）
docker compose --profile gateway up -d

# 进入 TUI（连本机 daemon）
docker compose run --rm tui

# 停止 daemon
docker compose --profile gateway down
```

**端口**：默认 `5000`，通过 `.env` 里的 `GATEWAY_PORT` 修改宿主机端口：

```bash
GATEWAY_PORT=9000 docker compose --profile gateway up -d
# 访问 http://localhost:9000
```

**Volume 映射**：

| 宿主机路径 | 容器路径 | 用途 |
| --- | --- | --- |
| `./.nano-openclaw/` | `/data/.nano-openclaw/` | 会话、配置、记忆、PID/log 等 |

第一次启动前先拷模板：`cp -r .nano-openclaw-dev .nano-openclaw`，然后编辑 `.nano-openclaw/nano-openclaw.json5` 即可自动加载。容器中 agent 的工作目录由配置文件中的 `workspaceDir` 决定，默认为 `~/.nano-openclaw/workspace/`。WebUI 支持斜杠命令、thinking 开关、图片/文件附件、活动历史回放、session 删除、亮色/暗色/跟随系统主题，移动端自适应。

配置详解见 [CONFIG_EXAMPLE.md](docs/CONFIG_EXAMPLE.md)。

## 接入方式

daemon 起来后，同一个 backend service 和同一份 session 列表可被多个 adapter 共享接入。所有入口共享 daemon 内**单一** `BackendSessionManager`，`/sessions` 在任何一端看到的都是同一份列表。

| 方式 | 入口 | 说明 |
| --- | --- | --- |
| **tui** | `nano-openclaw tui [--connect ws://host:5000/rpc]` | 终端 REPL；本机自动探测 daemon，或 `--connect` 接远程 |
| **wechat** | `nano-openclaw wechat login` + `gateway start` | 微信扫码，每个 uid 一个持久 session |
| **xiaozhi** | ESP32 OTA URL → `/xiaozhi/ota/` | 小智语音、拍照和设备 MCP；每个 Device-Id 一个持久 session |
| **web_chat** | 浏览器 `http://host:5000/` | WebUI 聊天页：斜杠命令、thinking、附件、活动回放、主题、session 删除 |
| **web_voice** | 浏览器 `https://host:5000/voice` | 聊天页内的开车免提语音模式（**手机需 HTTPS**） |

### tui（终端）

`nano-openclaw tui` 进入终端 REPL：本机有 daemon 时自动接管，没有则走单进程 embedded 模式自建 runtime。`--connect ws://host:5000/rpc` 接远程 daemon。remote TUI 默认每次打开新建 session；需要接回 daemon 的 last session 时显式加 `--resume`。命令示例见上文 [Development setup](#development-setup)。

### wechat（微信扫码）

WeChat 通过 iLink 协议接入，**只支持扫码登录**，nano-openclaw.json5 不再有 `wechat` 配置块。

```bash
# 1. 扫码登录（默认账号）
uv run nano-openclaw wechat login

# 多账号：换个 --account 标签即可（默认是 'default'）
uv run nano-openclaw wechat login --account=work
uv run nano-openclaw wechat login --account=personal

# 2. 启动 daemon — 自动发现 state_dir/wechat-tokens.*.json，每个文件 = 一个账号
uv run nano-openclaw gateway start
uv run nano-openclaw gateway status         # channels: 应列出所有登录的 WeChat channel
```

登录流程：终端打印 ASCII 二维码 → 微信扫码 → 手机端确认。登录成功后 token 写入 `state_dir/wechat-tokens.{account}.json`（`default` 账号无后缀），daemon 启动时自动加载。

会话过期（iLink `errcode=-14`）时 daemon 不会疯狂重试，而是 long-poll 退避 5 分钟并在日志里高优先级提示重新运行 `wechat login`。再登录后 daemon 会自动捡起新 token，不需要重启。

WeChat 作为 daemon 内的外部 ChannelAdapter 运行；每个 uid 自动绑定一个真实的持久化 session（与 tui / web_chat 共用 `/sessions` 列表）。uid → session_id 映射持久化在 `state_dir/wechat-sessions.{account}.json`。WebUI/TUI 不属于 channel，所以只打开浏览器页面时 `/channels` 仍会显示 `(no channels running)`。

### xiaozhi（ESP32 语音与拍照）

首版原生支持 xiaozhi-esp32 WebSocket v1：设备裸 Opus 经阿里云 ASR 转文字，交给 nano agent，最终回答再经阿里云 TTS 和 Opus 播放。带摄像头的板卡可以调用 `self.camera.take_photo` 拍照识图；灯光、音量等设备 MCP 工具也会动态发现，但只注入当前设备发起的 turn。

先安装 extra：

```bash
# 全局安装
uv tool install "nano-openclaw[xiaozhi]"

# 源码开发
uv sync --extra xiaozhi
```

配置需同时具备独立 `agents.defaults.imageModel` 和阿里云 `voice` 三要素，并让 gateway 可从局域网访问：

```json5
gateway: { host: "0.0.0.0", port: 5000 },
voice: {
  provider: "aliyun",
  appkey: "你的项目Appkey",
  accessKeyId: "${ALIYUN_AK_ID}",
  accessKeySecret: "${ALIYUN_AK_SECRET}",
  region: "cn-shanghai",
  ttsEnabled: true,
  ttsVoice: "xiaoxian",
},
xiaozhi: {
  enabled: true,
  token: "${XIAOZHI_TOKEN}",
  websocketUrl: "",       // 局域网直连可留空；公网/反代请填 wss://.../xiaozhi/v1/
  mcpTimeoutMs: 10000,
  maxPhotoBytes: 5242880,
  ttsVoice: "zhiqi",       // 需选择支持 24 kHz 的阿里云音色
  ttsSampleRate: 24000,    // 下行直出 24 kHz，匹配立创 S3 音频输出
  opusBitrate: 64000,
},
```

然后在 xiaozhi-esp32 源码运行 `idf.py menuconfig`，进入 `Xiaozhi Assistant → Default OTA URL`，填写：

```text
http://<运行 nano 的电脑局域网 IP>:5000/xiaozhi/ota/
```

保存、编译并刷机即可，不需要改固件协议源码或提交生成的 `sdkconfig`。每个 `Device-Id` 的 session 映射原子保存在 `state_dir/xiaozhi-sessions.json`，重连后继续原会话；照片不会作为附件或长期文件保存。WebUI 能查看同一 session 的文本历史，但不会获得该设备的硬件工具，避免跨入口误控。

当前只支持 v1 的单声道/60 ms Opus：设备上行保持 16 kHz 供 ASR，TTS 下行默认使用 24 kHz/64 kbps Opus，避免立创 S3 播放前再做 16→24 kHz 重采样。`ttsVoice` 必须支持所选采样率；默认 `zhiqi` 支持 24 kHz。暂不支持 v2/v3、MQTT/UDP 或服务端 AEC。配置不完整只会把 `xiaozhi/default` 标为 `error`，不阻止 WebUI 启动。外网部署必须显式配置 `wss`、可信证书，并在反向代理限制 `/xiaozhi/ota/` 访问。完整字段见 [配置说明](docs/CONFIG_EXAMPLE.md#xiaozhi--xiaozhi-esp32-原生接入)。

### web_chat（WebUI 聊天页）

`gateway start` 后浏览器打开 `http://127.0.0.1:5000`（端口默认 5000，可在 [Gateway 配置](#gateway-配置) 改）。支持斜杠命令、thinking 开关、图片/文件附件、活动历史回放、亮/暗/跟随系统主题，移动端自适应。

浏览器每次打开 WebUI 默认创建一个新的 session；WebSocket 断线重连、手机锁屏后恢复页面时，会继续停留在当前 session。唯一例外是同一个浏览器 tab 中有正在进行的群聊播客：刷新/恢复页面会优先接回该 podcast 所属 session，避免后端任务变成无人可控的孤儿 run。不同浏览器/设备只读取自己的 `sessionStorage`，不会因为这个恢复逻辑自动串会话；但同一个 gateway/token 下，session 列表本来就是共享可见的。

输入框行为：`Enter` 发送，`Shift+Enter` 换行。带附件发送时，用户气泡只显示原始 prompt / 附件名，不会把内部图片描述文本回写到 UI。session 列表中鼠标悬停或键盘聚焦某条 session 会出现 `×` 删除按钮，删除后自动切到剩余 session。

### web_voice（语音页 /voice，需 HTTPS）

**开车免提**的语音交互，已并入聊天页（不再是独立页面）：聊天输入框点麦克风 🎙 进入全屏免提，或直接访问 `/voice` 深链。点一下进入连续对话——浏览器原生语音识别把你说的话转成文字 → 走 `/ws` 发给 agent → 回复用 `speechSynthesis` 朗读出来；朗读时点屏幕任意处可打断。与聊天**共享同一 session / runtime**，退出后历史原样还在。顶栏可选**播报声音**（系统提供的 TTS 声音），底部有**思考等级**下拉（跟随后端）。免提期间用 Wake Lock 保持屏幕常亮（挡自动锁屏；手动息屏 / 切 App 仍无解）。

> **语音模式会让回答更适合朗读**：该模式下每一轮都给模型追加一段口语化指令——简短、先说结论、不用 Markdown / 列表 / emoji，避免 TTS 把符号念成杂音。只影响语音轮，文字聊天不受影响。

> 依赖浏览器原生 `webkitSpeechRecognition` + `speechSynthesis`，目前在 **Android Chrome** 上体验最佳；iOS Safari 的语音识别能力较弱。可选的播报声音由系统/浏览器提供，Android Chrome 常只有一个，需去系统「文字转语音(TTS)」设置切换。

**可选：阿里云实时语音识别 + 流式语音合成**（比浏览器原生更准、中文音色更自然）。配齐阿里云凭据后，浮层底部有**语音输入🎤**与**语音输出🔊**两个下拉，右上角是跟随输出引擎变化的**音色🗣**下拉：

- **语音输入🎤（识别引擎，底部中）**：「本地」(浏览器 Web Speech) / 「阿里云」(NLS 实时识别——浏览器经 WebSocket 直连网关，后端只动态签发临时 Token，AK/SK 绝不下发浏览器)。
- **语音输出🔊（合成引擎，底部右）**：「本地」(浏览器 `speechSynthesis`) / 「阿里云」(RESTful 代理合成) / 「阿里云流式」(`FlowingSpeechSynthesizer`，Web Audio 无缝播放)。默认在凭据齐全时为**阿里云流式**。
- **音色🗣（右上角）**：随语音输出引擎变化——输出选「本地」时列系统/浏览器声音，选阿里云任一时列含中文的阿里云音色目录。

三者相互独立、可任意搭配；凭据缺失或服务不可用时自动回退浏览器原生，互不影响。识别与合成是阿里云智能语音交互下**两个独立产品**——其中流式语音合成需开通**商用版**（不支持试用）。合成的回退链是 **流式 →（不可用/失败）RESTful 代理合成 →（再失败）浏览器本地**：流式不可用或失败时自动改用阿里云 **RESTful 语音合成**（标准「语音合成」产品，试用版亦可用），经后端 `/api/voice/tts` 代理（浏览器永不接触 appkey/AK/SK），本会话内记住回退结果不每轮重试；RESTful 仍失败才退浏览器本地并提示真实原因。配置见 [语音（阿里云语音）配置](#语音阿里云语音配置)。

**群聊播客模式**：语音浮层里可添加多个角色进入群聊，设置主题后由 host 串场、多角色轮流发言并朗读。群聊角色、主题、轮数、host 模型和正在运行的 `run_id` 按当前 WebUI session 保存在浏览器 `sessionStorage`；切换 session 会保存/恢复各自的群聊状态。文字、附件和语音统一从底部话题输入面板发起；群聊进行中可继续输入或点击麦克风插话，后端确认收到插话后才会切到新的 generation；如果插话提交失败，前端会继续播放旧内容，不会丢弃已在队列里的发言。

**为什么必须 HTTPS**：手机浏览器只在 *secure context* 下才允许访问麦克风（`getUserMedia` / 语音识别）。`localhost` / `127.0.0.1` 是例外，但通过局域网 IP（如 `http://192.168.x.x:5000`）的明文 HTTP **会被直接拒绝**，页面会提示"需要 HTTPS"。所以手机用 `/voice` 必须走 HTTPS。

**本地自签证书**（局域网场景最省事）：

```bash
# 生成自签证书（CN 随意，-days 自定有效期）
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout key.pem -out cert.pem -days 365 -subj "/CN=nano-openclaw"
```

**带 TLS 启动 gateway**（cert 和 key 必须成对，只给一个会启动失败）：

```bash
# 方式一：CLI flags（仅本次启动生效）
nano-openclaw gateway start --host 0.0.0.0 \
  --tls-cert ./cert.pem --tls-key ./key.pem

# 方式二：写进配置长期生效（gateway 块加 tls_cert / tls_key，见 Gateway 配置）
nano-openclaw gateway start --host 0.0.0.0
```

启动后手机访问 `https://<电脑局域网IP>:5000/voice`。自签证书第一次会弹"不安全"警告，**点继续 / 信任**后页面即为 secure context，麦克风就能用了。想免证书警告，改用 cloudflared / ngrok 隧道（受信 HTTPS）即可，页面同样放行。

> ⚠️ 证书私钥（`key.pem`）不要提交进仓库。`--host 0.0.0.0` 还会触发"非回环无 auth"启动告警——v1 网关无鉴权，放到不可信网络前请加反代或限制网段。

## 配置文件

配置文件采用 JSON5 格式（支持注释和尾逗号），路径按优先级查找：

1. `$NANO_OPENCLAW_CONFIG_PATH` — 环境变量
2. `{stateDir}/nano-openclaw.json5` — 状态目录下
3. `{cwd}/workspace/nano-openclaw.json5` — 项目 workspace 目录
4. `~/.nano-openclaw/nano-openclaw.json5` — 用户全局配置

状态目录 (`stateDir`) 解析：`$NANO_OPENCLAW_STATE_DIR` > `{cwd}/.nano-openclaw` > `~/.nano-openclaw`

### 快速开始

1. 复制示例配置：`cp nano-openclaw-example.json5 nano-openclaw.json5`
2. 编辑配置文件，填入你的 API key 和 provider 信息
3. 运行 `uv run nano-openclaw`

### 模型引用格式

所有模型统一使用 `provider/model-id` 格式：

| 引用示例 | 说明 |
| --- | --- |
| `anthropic/claude-sonnet-4` | 内置 Anthropic provider |
| `openai/gpt-4o` | 内置 OpenAI provider |
| `openrouter/anthropic/claude-sonnet-4` | 自定义 provider + 远程模型 |

内置 provider（`anthropic`、`openai`）无需配置，自动从环境变量读取 API key。

### Gateway 配置

```json5
gateway: {
  host: "127.0.0.1",   // 默认 loopback；改成 0.0.0.0 时启动会打 warning（v1 无 auth）
  port: 5000,
  log_path: "",        // 留空 → state_dir/log/gateway.log
  tls_cert: "",        // PEM 证书路径；与 tls_key 成对设置则启用 HTTPS（手机用 /voice 必需）
  tls_key: "",         // PEM 私钥路径
  restart_strategy: "exec",  // exec（原地 re-exec）| exit（退出交给 supervisor 拉起）
}
```

`gateway status` / `gateway run` 的 URL 输出会反映实际 scheme（启用 TLS 时为 `https`/`wss`），绑定 `0.0.0.0` 时自动探测并显示局域网 IP。

CLI 覆盖：`gateway start --host 0.0.0.0 --port 8080` 仅本次启动生效。`tls_cert` / `tls_key` 的用法（自签证书 + 带 TLS 启动）见 [接入方式](#接入方式) 章节的 web_voice 小节。

### 语音（阿里云语音）配置

语音浮层默认用浏览器原生引擎，**无需配置**。想用**阿里云实时语音识别 + 流式语音合成**（更准、中文音色更自然），在配置文件加 `voice` 块：

```json5
voice: {
  provider: "aliyun",                    // 目前仅支持阿里云
  appkey: "你的项目Appkey",               // 智能语音交互控制台创建的项目 Appkey
  accessKeyId: "${ALIYUN_AK_ID}",        // 支持 ${VAR} 取环境变量；AK/SK 绝不下发浏览器
  accessKeySecret: "${ALIYUN_AK_SECRET}",
  region: "cn-shanghai",                 // 区域，决定默认网关 endpoint
  // endpoint: "",                       // 留空则按 region 推导 wss://nls-gateway-{region}.aliyuncs.com/ws/v1
  ttsEnabled: true,                      // 是否启用流式语音合成（关闭则朗读回退浏览器 speechSynthesis）
  ttsVoice: "xiaoxian",                  // 默认合成音色（用户可在浮层「音色」下拉切换）
  ttsSampleRate: 16000,                  // 合成采样率（Hz）
  wakeWord: "",                          // 唤醒词（如 "小克,小可"，逗号分隔同音变体）；配了即启用待唤醒模式
}
```

- **三要素**（`appkey` + `accessKeyId` + `accessKeySecret`）齐全才视为可用；任一缺失则前端整体回退浏览器原生引擎。
- AK/SK 支持 `${VAR}` 语法，加载阶段从环境变量替换；**后端动态签发临时 Token（约 24h，自动缓存续期），浏览器只拿临时 Token、永不接触 AK/SK**。识别与合成复用同一套凭据 / 网关 / Token，仅请求 `namespace` 不同。
- **实时语音识别**与**流式文本语音合成**是智能语音交互下两个独立计费产品，需分别开通；其中**流式语音合成仅商用版可用、不支持试用**。流式不可用/失败时自动回退**RESTful 代理语音合成**（标准「语音合成」产品，试用版亦可用），经后端 `/api/voice/tts` 代理（浏览器不接触 appkey/AK/SK），本会话内记住回退结果；RESTful 再失败（如试用到期 `FREE_TRIAL_EXPIRED`）才在界面显示真实原因并退浏览器本地音色。
- **唤醒词（可选）**：配置 `wakeWord` 后，免提进入「待唤醒」💤 待机——待机用**免费的浏览器本地识别**只做关键词匹配（阿里云不计费、听到的话不发给 agent），喊出唤醒词（或点屏手动唤醒）后「叮」一声切回所选引擎连续对话；聆听中静默 20 秒自动回落待机。支持"唤醒词+指令"一句话直达（如"小克今天天气"）。匹配**按拼音同音等价**（内置常用字读音表）——ASR 把"小克"写成"小课/小柯/小科"都照常命中，无须手工列举同音字；逗号变体留给不同读音的别名（如中英文双唤醒词）。
- 浏览器侧硬依赖 `getUserMedia` + `AudioWorklet`（识别）/ `AudioContext`（合成），**Android Chrome** 体验最佳，且需 secure context（手机用 `/voice` 必须走 HTTPS，见 [接入方式](#接入方式) 章节的 web_voice 小节）。

## 日志系统

支持结构化 JSON Lines 日志，通过环境变量或配置文件控制：

```bash
# 通过环境变量设置日志等级
NANO_LOG_LEVEL=DEBUG uv run nano-openclaw

# 或在配置文件中设置
# config.logging.level = "INFO"
```

日志文件位于 `{stateDir}/log/`：

| 文件 | 说明 |
|---|---|
| `nano-openclaw.log` | 结构化 JSON Lines（自动轮转 + Gzip） |
| `gateway.log` | daemon 进程的 stdout/stderr（仅 `gateway start` 后台启动时写入） |

`nano-openclaw.log` 支持 JSON Lines、自动轮转（>10MB 滚动）、Gzip 压缩、上下文注入（session_id、run_id、tool_call_id）。日志等级：`DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`（默认 `WARNING`）。

---

## 60 秒架构图

```
 adapters/daemon/api
  ├─ adapters/cli       TUI embedded / remote
  ├─ adapters/webui     FastAPI WebUI + voice static/API
  ├─ adapters/channels  WeChat and future external channels
  ├─ daemon             pidfile, start/stop/status/run, TLS, composition
  └─ api                /rpc WebSocket protocol + method handlers
                 │
                 ▼
 services
  ├─ BackendService / EmbeddedBackend
  ├─ sessions, runs, approvals, runtime_update
  ├─ ChannelManager
  └─ slash registry / renderer / dispatcher
                 │
                 ▼
 core
  ├─ AgentSession.run_turn
  ├─ provider streaming
  ├─ tools/runtime tools
  ├─ prompt, compaction, attachments, images
  └─ workspace bootstrap
                 │
                 ▼
 config / session store / provider SDKs / filesystem
```

## 三条不变量

读 `loop.py` 时记住这三句话：

1. 每一轮把**完整 history** 发回模型——若超出 token 预算，`compact_if_needed` 会先把旧消息替换成一条摘要，再发送压缩后的 history。
2. 多个 `tool_use` 并存时，所有结果合并成**一条** user 消息回灌。
3. 循环只在 `stop_reason != "tool_use"` 时终止；其它都是中间态。

Backend / RPC 不变量：

4. `EmbeddedBackend` 和 `WebSocketBackend` 满足同一个 `Backend` Protocol，TUI 切换 backend 不感知差异。
5. `chat.abort(turn_id)` 是统一的取消接口：chat / cron / channel 任何 origin 的 in-flight turn 都能从这里中断（依靠 `RunRegistry`）。
6. daemon 内**单一** `BackendSessionManager` 实例同时被 WebUI、`/rpc`、外部 Channels 共享 —— `/sessions` 在所有入口永远看到同一份 list。

Subagent 编排：模型可调用 `sessions_spawn` 启动 isolated 后台子 agent，适合复杂、慢、可并行的任务。子 agent 继承 workspace、模型和 thinking 默认值，可通过顶层 `subagents` 配置限制并发、超时和默认模型；它不会继承 `sessions_spawn` 等会话管理工具，避免递归派生。完成、失败或超时后，结果会自动作为一条 user message 注入父 session。后台子 agent 不能弹出前台审批 UI：触发 approval 的工具调用走 `NonInteractiveApprovalHandler`——allowlist 命中即放行，否则拒绝。

图片处理遵循**双路径架构**：未配置 `image_model` 时走 Native Vision（图片直接发给主模型）；配置后走 Media Understanding（图片模型先描述，文字注入 prompt）。若图片模型描述失败，会优先回退到主模型 Native Vision；主模型也无视觉能力时，会把“图片处理失败”的文本上下文注入给模型，避免模型误以为用户没有发送图片。`parse_image_refs` 在循环入口处统一处理用户输入中的 `@file.png`、Markdown `![]()` 和 URL 引用。

MCP 工具集成：通过 `config.mcp.servers` 配置外部 MCP 服务器，启动时建立持久连接（支持 stdio/SSE/streamable-http 三种传输），工具自动注册到 ToolRegistry。服务器连接在后台 asyncio 线程中运行，daemon 退出时自动清理。

Web 工具集成：内置 `web_search`（DuckDuckGo 搜索）和 `web_fetch`（URL 内容抓取）工具，默认启用，可通过 `tools.web` 配置单独控制。所有外部内容通过 `<EXTERNAL_UNTRUSTED_CONTENT>` 边界标记包装并清洗 LLM 特殊 token，防止 prompt injection。`web_fetch` 带有 SSRF 两阶段防护（预 DNS 黑名单 + 后 DNS 私有 IP 验证）。搜索结果和抓取内容均有 10 分钟缓存。

Thinking 支持：通过 `agents.defaults.thinkingDefault` 配置思考等级（`off|minimal|low|medium|high|xhigh|adaptive|max`）。Anthropic provider 使用原生 thinking API；OpenAI-compatible provider 使用 `reasoning_content` 流。Thinking 块会持久化到消息历史，CLI 以 dim 样式在 assistant 输出前渲染。

Workspace 引导文件：从 `workspaceDir` 加载 8 个标准引导文件（AGENTS.md、SOUL.md、IDENTITY.md、USER.md、MEMORY.md、TOOLS.md、BOOTSTRAP.md、HEARTBEAT.md），应用安全防护和预算截断，注入到系统提示的项目上下文部分。支持 session-scoped 缓存。

Memory 系统：包含多层机制：
- **Daily Memory**：启动时自动加载 `workspace/memory/*.md` 中最近 N 天的记忆文件（默认 2 天）。
- **Memory Tools**：`memory_get` / `memory_search` 工具。`memory_search` 通过 provider 层检索，默认 `lexical` provider 用词法匹配；安装 `nano-openclaw[zvec]` 后可选启用 `zvec` provider，用 Zvec 本地 FTS/BM25 或 hybrid 检索。
- **Active Memory**：每次用户消息前自动子 agent 搜索记忆。需提供 `activeMemory` 块才启用（缺省不配置 = 关；块内 `enabled` 默认 true）。
- **Dreaming**：定期将高频记忆提升到 MEMORY.md（默认开）。通过 `dreaming` 配置。
- **Extract Memories**：stop-hook 后台 extractor，把对话蒸馏进 `memory/topics/*.md` 并更新 `memory/MEMORY.md`（默认开）。通过 `extractMemories` 配置。

Background Review Fork（自进化）：每 N 个 `end_turn` 后台启动一个受限 sub-agent，让它读最近对话决定是否把"用户偏好/教训/可复用方法"沉淀进 `MEMORY.md` 或现有 `SKILL.md`。**默认开启**（成本：每次触发 ~1 次 LLM 调用），通过 `reviewFork` 顶层字段配置：

```jsonc
{
  "reviewFork": {
    "enabled": true,        // 默认 true；设为 false 关闭
    "trigger_n": 10,        // 每 N 个 end_turn 触发（默认 10）
    "cooldown_s": 60,       // 两次触发之间的最短间隔（秒）
    "timeout_s": 90,        // sub-agent 单次 run 的硬超时
    "model_aux": null       // null = 跟父 agent 模型；可指定如 "anthropic/claude-haiku-4-5" 省钱
  }
}
```

运行时控制：`/review-fork on/off` 即时切换；`/review-fork run` 绕过 N + cooldown 立即触发一次（debug 用）。每次 spawn 写一行到 `state_dir/review-fork.jsonl`（含 ts/run_id/session_key/messages_count），结果写 `state_dir/review-fork-results.jsonl` 方便观测。Active-Update Bias：sub-agent 系统提示要求"9/10 turn 默认 NOOP，写则优先 update 现有条目，禁止凭空新建 SKILL.md"。

Cron 任务：通过 `cron_create` 工具或配置文件定义；daemon 内部跑 cron scheduler；任务完成可定向通知发起方（wechat 用户收到 daemon 推送的消息）。daemon 重启不会重复触发同一时间窗已经跑过的任务（`last_run_at_ms` 去重）。

Session Status 工具：内置 `session_status` 工具用于查询当前日期时间和会话上下文信息（模型 ID、session ID、token 使用量等）。

## License

MIT — 见 [LICENSE](./LICENSE)。
