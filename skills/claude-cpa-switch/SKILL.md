---
name: claude-cpa-switch
description: >-
  在 Claude Code 的三套模型路由之间一键切换：CPA(ChatGPT，gpt-5.6-sol 走 Mac mini 的 CLIProxyAPI)、
  DeepSeek(Ollama，deepseek-v4-flash 走本地 11435 shim)、OpenCode Go(glm-5.3 主 / deepseek-v4-flash 副，
  走 opencode.ai)。当用户说「切到 CPA / 切到 ChatGPT / 切回 DeepSeek / 切到 OpenCode /
  切到 glm / 换模型路由 / 看看现在 Claude 走哪个模型」时使用。
  Switch Claude Code routing between CPA (ChatGPT via CLIProxyAPI), DeepSeek
  (Ollama local shim), and OpenCode Go (opencode.ai). Use when the user asks
  to switch Claude's model backend.
---

# claude-cpa-switch — Claude Code 模型路由切换

Claude Code 有三套互斥的路由配置，本 skill 负责在它们之间切换：

| 模式 | 后端 | 主模型 | 副模型 | 端口 |
|---|---|---|---|---|
| **cpa** | `cpa_anthropic_shim.py` → CPA(Mac mini) → ChatGPT | gpt-5.6-sol | 同 | 127.0.0.1:11436 |
| **deepseek** | `ollama_anthropic_shim.py` → Ollama Cloud | deepseek-v4-flash | 同 | 127.0.0.1:11435 |
| **opencode** | `opencode_anthropic_shim.py` → opencode Go(opencode.ai) | glm-5.3 | deepseek-v4-flash | 127.0.0.1:11437 |

切换只改 `~/.claude/settings.json` 的 `env` 块（base_url + 模型映射 + auth token），
其余设置（model、effortLevel 等）原样保留，切换前自动备份到
`~/.claude/backups/claude-cpa-switch/`。

## 核心命令

```bash
python "~/.claude\skills\claude-cpa-switch\scripts\switch.py" status
python "~/.claude\skills\claude-cpa-switch\scripts\switch.py" cpa
python "~/.claude\skills\claude-cpa-switch\scripts\switch.py" cpa --model grok-4.5
python "~/.claude\skills\claude-cpa-switch\scripts\switch.py" deepseek
python "~/.claude\skills\claude-cpa-switch\scripts\switch.py" opencode
python "~/.claude\skills\claude-cpa-switch\scripts\switch.py" opencode --main glm-5.2 --small deepseek-v4-flash
```

- `status`：看当前路由、三个 shim 是否在监听
- `cpa`：切到 CPA(ChatGPT)，默认 `gpt-5.6-sol`；`--model` 可指定 CPA 上任意可用模型
- `deepseek`：切回 Ollama DeepSeek（`deepseek-v4-flash`）
- `opencode`：切到 OpenCode Go（主力 `glm-5.3` / 轻量 `deepseek-v4-flash`）；`--main`/`--small` 可换模型
- 都支持 `--dry-run` 预检

## 切换后必须重启

**完全退出并重启 Claude Code**，env 变量在启动时读取，重启后才生效。
当前会话不受影响（继续用旧路由跑完）。

## 架构说明

- **cpa 模式**：Claude Code → `cpa_anthropic_shim.py`(11436) → CPA `/v1/chat/completions` → ChatGPT。
  shim 做 Anthropic ↔ OpenAI 双向翻译（含流式 SSE、工具调用），key 复用
  `~/.codex/skills/remote-cpa/scripts/cpa_request.py` 的解析逻辑（`CPA_API_KEY` 环境变量优先，SSH 兜底）。
- **deepseek 模式**：Claude Code → `ollama_anthropic_shim.py`(11435) → Ollama Cloud。
- **opencode 模式**：Claude Code → `opencode_anthropic_shim.py`(11437) → opencode.ai/zen/go/v1
  `/chat/completions`。同样做 Anthropic ↔ OpenAI 双向翻译。API key 从
  `~/.claude/opencode_keys.json` 读取（含 `api_key` + `base_url`），独立于进程 env（shim 由
  pythonw 后台拉起，不继承 setx 变量）。主力槽映射 `glm-5.3`，轻量槽映射 `deepseek-v4-flash`。
  出站请求带浏览器 UA——opencode.ai 在 Cloudflare 后面，会按 UA 拦 Python-urllib 默认签名（1010）。
- 三个 shim 可以同时常驻，切换只是改 settings.json 指向哪个端口。

## 注意

- CPA 的配额/限速属于它管理的 OAuth 账号，不是无限容量。
- CPA 上非 OpenAI 模型（grok/claude 等）在 Claude Code 里可用，但个别特性可能不兼容。
- 切到 cpa / opencode 时若对应端口没在监听，switch.py 会自动拉起 shim（后台 pythonw，无窗口）。
- 想手动起 shim：`python "~/.claude\opencode_anthropic_shim.py" --port 11437`
- 健康检查：`curl http://127.0.0.1:11437/health`
- OpenCode Go 是订阅制（$10/月），5 小时 $12 / 每周 $30 / 每月 $60 用量额度，限流按美元折算。
- opencode 自家的 reasoning_content 字段 shim 不转发，glm-5.3 / deepseek-v4-flash 最终回答都在 content 里。
