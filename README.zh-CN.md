# claude-model-router(中文说明)

[English](README.md)

一套让 **Claude Code** 和 **Codex** 共用的多模型路由方案：

- **Claude Code 侧**：本地 Anthropic 兼容 shim 夹在 CLI 和任意 OpenAI 兼容服务商之间，
  plan 模式 / 执行 / 后台任务可以各用不同模型，自动切换。
- **Codex 侧**:`multi-model-review-gate` skill 用异构外部审查者做计划和实现评审，
  并把执行模型建议写进路由文件，shim 直接读取生效。

不绑定任何厂商。参考实现用 OpenCode Go(GLM / DeepSeek）做执行、Kimi + Grok 做评审，
但每个模型都只是一条配置——可以换成任何 OpenAI 兼容端点。

## 架构

```
Codex(做计划与评审)                          Claude Code(日常使用)
─────────────────────────                    ─────────────────────────
multi-model-review-gate skill
  ├─ 审查者走 remote-cpa ──► 任意 OpenAI 兼容评审模型
  │                          (默认:Kimi 覆盖审查,Grok 对抗审查)
  └─ set_exec_route.py ──► ~/.claude/exec_route.json ──┐
                                                       ▼
Claude Code ──► shim 127.0.0.1:11437 ──► OpenAI 兼容上游
                (opencode_anthropic_shim)  (默认 OpenCode Go)
                plan 模式  → 强模型          (默认 glm-5.3)
                执行       → 优先路由文件,否则难度启发式
                             (默认 deepseek-v4-flash / glm-5.2)
                后台任务   → 便宜快模型       (默认 deepseek-v4-flash)
```

`exec_route.json` 是两侧唯一的接口：skill 写,shim 读。带 TTL(默认 2 小时),
过期后 shim 自动退回启发式路由。

## 组件

| 路径 | 说明 |
|---|---|
| `shims/opencode_anthropic_shim.py` | Anthropic `/v1/messages` → OpenAI `/chat/completions` 代理,带执行档自动路由、会话粘性、skill 路由文件支持、`reasoning_effort` 默认值注入;内置 SSE 心跳(15s ping,防上游慢导致客户端指数退避)、上游失败自动重试(3 次指数退避)、流中断降级为 SSE error 事件、绕过系统代理直连上游;支持双上游分流——`glm-5.3` 留 opencode,其余模型透传给本地 ollama shim |
| `shims/ollama_anthropic_shim.py` | 同样的思路,上游是本地 Ollama |
| `shims/cpa_anthropic_shim.py` | 同样的思路,上游是 cli-proxy-api(ChatGPT);依赖 `skills/remote-cpa` |
| `skills/multi-model-review-gate` | 有预算约束的计划/执行评审:≤2 个外部审查者 + 一次裁决;输出 `EXECUTION_ROUTE` |
| `skills/remote-cpa` | CPA 客户端库,评审 skill 和 cpa shim 都依赖它 |
| `skills/claude-cpa-switch` | 在三条 shim 路由(端口 11435/11436/11437)之间切换 Claude Code |

## 配置步骤

### 1. 选定并配置你的服务商

这套栈里所有模型都通过 OpenAI 兼容端点接入。想用别的模型,只改下面这些配置点,
不用改代码(除非服务商不说 OpenAI 协议)。

**执行服务商(shim 用)**。创建 `~/.claude/opencode_keys.json`:

```json
{
  "api_key": "你的KEY",
  "base_url": "https://opencode.ai/zen/go/v1"
}
```

把 `base_url` 指向任何 OpenAI 兼容服务商,再把它提供的模型 ID 填进 shim 环境变量
(见下)和 `set_exec_route.py` 的 `MODEL_ALIASES`。

**评审模型(review skill 用)**。`review_batch.py` 通过 `remote-cpa` 的实时模型列表
解析审查者。把你手上有的评审端点(Kimi、Grok 或其他)配进 remote-cpa 即可——
skill 会按角色(`coverage` / `adversarial` / `implementation` / `safety`)
自动挑选最接近的可用候选。

### 2. 启动 shim

```bash
python shims/opencode_anthropic_shim.py --host 127.0.0.1 --port 11437
```

可选环境变量:

| 变量 | 默认值 | 含义 |
|---|---|---|
| `OPENCODE_SHIM_MODEL` | `glm-5.3` | 请求没指名模型时的兜底 |
| `OPENCODE_EXEC_MODEL` | `glm-5.2` | 执行档的"困难"档 |
| `OPENCODE_EXEC_MODEL_EASY` | `deepseek-v4-flash` | 执行档的"简单"档 |
| `OPENCODE_EXEC_ROUTING` | `on` | 设为 `off` 关闭全部自动路由 |
| `OPENCODE_REASONING_EFFORT` | `low` | 客户端没发时注入,防止推理模型把 token 预算全烧在隐藏思考上 |
| `OPENCODE_EXEC_ROUTE_FILE` | `~/.claude/exec_route.json` | skill 路由文件位置 |
| `OPENCODE_SPLIT` | `on` | 双上游分流:`glm-5.3` 走 opencode,其余模型转给 ollama shim;`off` 关闭 |
| `OLLAMA_SHIM_URL` | `http://127.0.0.1:11435` | 分流目标(本地 ollama shim) |
| `OPENCODE_SHIM_LOG` | `~/.claude/opencode_shim.log` | pythonw 后台运行(stderr 为空)时的日志落盘位置 |

### 3. 把 Claude Code 指向 shim

在 `~/.claude/settings.json` 里:

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "opencode-local",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:11437",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.2",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
    "ANTHROPIC_SMALL_FAST_MODEL": "deepseek-v4-flash"
  },
  "model": "opusplan"
}
```

`opusplan` 是 Claude Code 内置的分工:plan 模式走 Opus 映射,执行回落到 Sonnet 映射。
这两个别名映射到什么模型,由你的服务商决定。

### 4. 使用评审 skill(Codex)

把 `skills/multi-model-review-gate` 和 `skills/remote-cpa` 装进 Codex 的 skills 目录,
配好审查者(第 1 步),然后按 skill 文档跑评审。计划达到 `READY_FOR_EXECUTION` 后:

```bash
python scripts/set_exec_route.py --model glm-5.2 --plan-sha256 <sha> --ttl 7200
```

之后 shim 在 TTL 内优先用这个模型处理执行档请求;`--clear` 可提前清除。
没有有效路由文件时,shim 按启发式逐请求判断(困难信号关键词、工具报错、大上下文),
且有会话粘性:一旦判难就锁定,不会来回振荡。

## 备注

- shim 是无状态的;对话中途逐请求切模型是安全的,因为客户端每次都重发完整上下文。
- API key 不要进仓库:`opencode_keys.json`、`exec_route.json` 和日志都已在
  .gitignore 里。
