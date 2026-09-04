# astrbot_plugin_reasonix_subagents

把 **DeepSeek-Reasonix** 的 4 个内置只读子代理（`explore` / `research` / `review` / `security-review`）
迁移到 AstrBot，采用官方推荐的 **agent-as-tool** 模式（`FunctionTool` + `tool_loop_agent`）。

设计参考：
- 提示词：`DeepSeek-Reasonix/internal/skill/builtins.go`（explore / research / review / security-review body）
- AstrBot 多 Agent 示例：`docs/dev/star/guides/ai.md`（中文：`docs/zh/dev/star/guides/ai.md`）
- 本地迁移资产文档（可选阅读）：
  `E:\ProjectCollection\AI_sandbox\docs_person\DeepSeek-Reasonix子代理迁移资产\`

## 它提供了什么

注册到主 LLM 的 4 个“委派工具”（agent-as-tool）：

| 工具 | 作用 | 只读工具集 |
| --- | --- | --- |
| `explore` | 只读代码库调查，返回一条蒸馏结论 | file-read / grep / 只读 shell / 只读 git |
| `research` | 代码 + 网页交叉研究 | 上 + 已配置的 web 搜索/extract |
| `review` | 对 workspace 内 git 仓库改动做代码评审 | file-read / grep / 只读 git / 只读 shell |
| `security-review` | 安全视角评审（威胁模型分级 + 危险 API 清单） | 同 review |
| `deep_review` | 并行多专家评审 + 置信度门禁（更细更慢） | 同 review（多轮） |

外加一个**只读 git 工具** `reasonix_git_read`（白名单子命令：`status diff log show blame ls-files rev-parse branch remote describe shortlog tag`；禁止写子命令；输出截断；`repo_path` 默认限制在会话 workspace 内）。

每个子代理调用时都在**新建的独立上下文**里跑 `tool_loop_agent`（不携带父对话历史，只带自身系统提示词 + 只读工具集），完成后**只把最终文本**回给主 LLM——对应 Reasonix 的“隔离子会话 + 只回最终答案”。

## 安装 / 启用

1. 把本目录放入 `data/plugins/astrbot_plugin_reasonix_subagents/`。
2. WebUI 重载插件。
3. 确保当前会话人格能挂载这些工具：
   - 若人格工具列表为“全部工具”，自动生效；
   - 若人格指定了工具白名单，需把 `explore research review security_review deep_review` 加进去。
4. 让主 LLM 委派即可，例如：
   - “explore 一下 `astrbot/core/star/context.py` 里 tool_loop_agent 的实现”
   - “review 当前 workspace 仓库的改动”
   - “security-review 一下最近改的鉴权代码”

## deep_review（融合增强，2026-09-04）

`deep_review` 是 `review` 的“深水区”版本，思路改编自 Anthropic 官方
[claude-plugins-official](https://github.com/anthropics/claude-plugins-official)：

1. **并行 5 个专项评审者**（`asyncio` 并发上限 3），各自在独立上下文、只读工具集里审同一份 diff：
   - `correctness`（正确性 & 隐藏行为变化）
   - `guidelines`（项目规范/AGENTS.md 一致性 + 代码质量）
   - `silent-failures`（错误处理：静默失败、宽泛 catch、吞错、掩盖性 fallback）
   - `tests`（变更的测试覆盖质量，行为导向）
   - `comments-types`（注释/文档与类型设计是否保值）
2. 每个评审者对每个 issue 给出 **0-100 置信度** 与 file:line 证据；
3. 一个 **merge/arbiter** 步骤按评分卡（0/25/50/75/100）**过滤掉 <80** 的误报、跨评审去重，
   输出 `verdict / blocking_findings / non_blocking / required_changes`。

相比 `review`：能显著降低误报、覆盖多个视角，但更慢、更耗 token——适合大改动/高风险改动，
或当 `review` 首次返回大量不确定发现时。入参同 `review`（`task` + 可选 `diff`/`repo_path`）；
若两者都没有且 workspace 仓库不可达会快速失败并索要 diff。

## 前置条件与限制

- **explore / review 的代码读取**依赖 Computer Use 的文件/grep/shell 工具：
  在 WebUI → 模型提供商 → 开启 Computer Use（`computer_use_runtime` = local/sandbox），
  否则子代理只会拿到 `reasonix_git_read`，读取类任务无法完成（子代理会明确回报）。
- **review / security-review 需要 git 仓库**：把仓库放进 AstrBot 会话 workspace
  （或通过 `reasonix_git_read(repo_path=...)` 显式给路径；路径必须落在 workspace 内）。
  若仓库不可达，子代理会要求父代理提供 diff 文本。
- **research 需要可用的 web 搜索工具**：可以是 AstrBot 内置搜索（tavily/bocha/brave/firecrawl/exa/anysearch，需配置 key），也可以是**任意第三方插件注册的只读搜索/抓取工具**——research 会运行时自动发现（如 `astrbot_plugin_web_searcher_pro` 的 `searxng_*` 工具），无需改代码。若某插件的工具名/描述既不带 search/fetch/extract/searxng 等特征、也没被安全排除规则放过，可在 `main.py` 的 `_WEB_TOOLS` 里显式加入其工具名。
- 只读是靠“只给读工具”实现（AstrBot 没有只读子代理注册表）；请勿把这些子代理工具集里加入写工具。
- 子代理是同步执行（会占用主循环直到返回）；`max_steps` 已调小（review 系 8 步）。

## 开发 / 自检

```bash
# 语法检查
uv run python -m py_compile data/plugins/astrbot_plugin_reasonix_subagents/main.py
# 格式与 lint（如已安装 ruff）
uv run ruff check data/plugins/astrbot_plugin_reasonix_subagents
uv run ruff format data/plugins/astrbot_plugin_reasonix_subagents
```

## 发布到插件市场（检查清单）

发布入口：[AstrBot 插件发布页面](https://cloud.astrbot.app/publish)（需注册 AstrBot Cloud），或维护自定义插件源。
发布前请逐项确认：

- [ ] 把本目录放进**独立 GitHub 仓库**（建议命名为 `astrbot_plugin_reasonix_subagents`，metadata.yaml 在仓库根目录）。
- [ ] `metadata.yaml`：把 `author` 改为你的发布者名/GitHub 用户名，把 `repo` 填为真实 HTTPS 仓库地址（仓库地址用于更新，缺失将无法更新）。
- [ ] 压缩包 ≤ 16MB；不要把 `.git/`、`__pycache__/`、`.venv/`、`.ruff_cache/` 等提交进仓库（见 `.gitignore`）。
- [ ] （可选）添加 `logo.png`（1:1，256x256）；补充 `social_link`。
- [ ] 仅用 AstrBot 自带依赖（pydantic 等），无需 `requirements.txt`；若以后引入第三方库需补 `requirements.txt`。
- [ ] 改动遵循 [插件发布规范](https://docs.astrbot.app/zh/dev/star/plugin-publish) 与 `docs/zh/dev/star/plugin-new.md`。

### 来源与许可（合规声明）

本插件移植了 [DeepSeek-Reasonix](https://github.com/esengine/DeepSeek-Reasonix) 的 4 个内置只读子代理提示词
（`internal/skill/builtins.go` 中的 explore / research / review / security-review body）及只读纪律设计。
DeepSeek-Reasonix 采用 **MIT License**，按协议要求保留如下版权声明：

```text
MIT License

Copyright (c) 2026 Reasonix Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

本插件自身代码与文档采用 **MIT 许可**（见仓库 `LICENSE` 文件）。Reasonix 提示词部分的版权声明已保留于上文。

### 融合素材来源（claude-plugins-official，Apache-2.0）

`deep_review` 的“并行多专家评审 + 0-100 置信度门禁（<80 过滤）”改编自
[claude-plugins-official](https://github.com/anthropics/claude-plugins-official)：
- `plugins/code-review`（`commands/code-review.md`：并行独立评审 + 置信度评分卡 + 误报清单）；
- `plugins/pr-review-toolkit`（`agents/`：correctness / guidelines / silent-failure / tests /
  comments-types 专项评审提示词）。
`security_review` 的 “Additional dangerous-API scan” 清单改编自
`plugins/security-guidance/hooks/patterns.py`（25 条漏洞模式规则，规则名与触发条件见该文件）。
以上内容均为 **Apache-2.0** 许可，使用时保留本来源署名。适配改动要点：移除 CLAUDE.md / `gh` /
PR 评论 / Haiku·Sonnet 模型标签等 Claude Code 运行时绑定，改为读 workspace git diff 或粘贴的
`diff` + file/grep 工具，并把原“每 issue 单独打分 agent”折叠为一个 merge/arbiter 步骤以控制成本。

## 实测反馈与已知短板应对（2026-09-02，Linux 主机 / QQ 渠道）

4 个子代理已在真实 AstrBot（Linux / QQ）上逐个验收通过（research / explore / review /
security_review）。实测暴露的短板与本次应对：

| 短板 | 性质 | 应对 |
| --- | --- | --- |
| research 无法 live 联网核验 | 环境配置（未配 web 搜索 key） | **配置侧**：配置任一 web 搜索渠道——AstrBot 内置服务商 key，或直接用插件搜索（如 `web_searcher_pro`）。**代码侧**：遇到 "API key not configured" 不再逐个重试其它 web 工具；并自动发现只读插件搜索/抓取工具（如 `searxng_*`）纳入 research 工具集。 |
| review / security_review 强依赖 git diff、宿主/沙箱环境错位 | 载体/环境 | **代码侧**：二者新增 `diff`（直接粘贴 diff 文本审查，无需 git）与 `repo_path`（指定 workspace 内仓库路径）入参；提示词会识别 "Parent-provided diff" 后不再要求 git。 |
| explore / review 读不到"另一环境"的文件 | 架构（宿主 vs 沙箱） | 子代理跑在 AstrBot 宿主侧；要审沙箱文件请把文件/diff 带出来，或把仓库放进宿主会话 workspace 后再用 `repo_path` 指定。 |
| ops_reviewer | 非本插件 | 不在本插件范围内，忽略。 |

注意：review/security_review 默认审查的是**宿主会话 workspace**（实测环境里即 AstrBot 安装目录）里
git 仓库的未提交改动。若你只想审自己项目的改动，请把目标仓库放进 workspace 并用 `repo_path`
指定，或直接传 `diff`。
