# 更新日志

本项目的所有重要改动都会记录在此文件。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.4.0] - 2026-09-27

### 新增 (Added)

- 子代理策略可配置：支持自定义 provider / 模型、`enabled` 开关、高级标记与聚合器步数。
- 新增提示词 / 工具集防漂移守卫与包布局守卫（见「测试」）。
- 补齐 `CHANGELOG.md`，并在 README 的发布检查清单中加入版本 / 日志更新项。

### 变更 (Changed)

- **重构**：单文件 `main.py` 拆分为 `reasonix` 包（`main` / `constants` / `policy` / `config` /
  `tasks` / `tools` / `prompts`），便于维护与测试。
- 只读子代理的工具集收敛为「文件读 + grep + 只读 git」，`research` 另加网页搜索；
  不再包含 shell，结构探测改走只读 git 的 `ls-files`。
- 子代理提示词、README、`metadata.yaml` 的描述统一按「无 shell」口径更新。

### 修复 (Fixed)

- **关键**：改用包内相对导入，修复 AstrBot 加载插件时报
  `No module named 'reasonix'`。AstrBot 以 `data.plugins.<插件名>.main` 方式注入插件，
  不会把插件目录加入 `sys.path`，此前的顶层绝对导入在生产环境必然失败。
- 只读子代理提示词中残留的「read-only shell tool」悬空引用。
- 只读 git 工具：
  - 检查 git 返回码（此前会把 git 报错文本当作有效快照交给评审者）；
  - 空 diff 明确报错「无可审查变更」，不再静默进入评审流程；
  - `git branch -Dfoo` 一类附加短选项的删除形式被正确拒绝；
  - 权限错误不再被吞掉，如实透出。
- 整体超时现在覆盖聚合器阶段（此前聚合阶段可无限期挂起）。
- 配置热更新生效：`ConfigHolder` 读取实时配置；空字典配置不再错误回退到初始值。
- 工具发现：先校验描述的副作用标记、再按名称匹配；`excluded_tools` 同样约束自动发现的工具。

### 安全 (Security)

- 只读默认工具集移除 shell（`astrbot_execute_shell` / `astrbot_shell_session`）。
- `git_guard` 强化：工作区逃逸 fail-closed、最小环境变量（不泄漏 `os.environ`）、
  禁用 `GIT_EXTERNAL_DIFF` 与 textconv、拒绝 `-c/-C` 与 no-index。

### 测试 (Tests)

- 测试改与生产同构：以包形式导入插件，monkeypatch 目标指向加载器真正加载的模块。
- 新增守卫：插件源码禁止顶层 `reasonix` 绝对导入；monkeypatch 目标必须包限定（AST 解析）；
  只读子代理提示词与文档不得宣称 shell。
- 共 70 项测试通过，且与运行目录无关（从插件根或任意 cwd 运行结果一致）。

## [0.3.0] - 2026-09-04

### 新增 (Added)

- `deep_review`：并行 5 个专项评审者（正确性 / 规范 / 静默失败 / 测试 / 注释类型），
  经 0-100 置信度门禁（<80 过滤、去重合并）后输出统一报告，适合大改动与高风险改动。
- `security_review` 融入改编自 Anthropic `claude-plugins-official` 的危险 API 扫描清单。

## [0.1.0] - 2026-09-02

### 新增 (Added)

- 初次迁移：以 agent-as-tool 方式引入 `explore` / `research` / `review` / `security_review`
  四个只读子代理。
- 注册 `reasonix_git_read` 只读 git 工具（status / diff / log / show / blame / ls-files 白名单）。

[0.4.0]: https://github.com/buyun14/astrbot_plugin_reasonix_subagents/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/buyun14/astrbot_plugin_reasonix_subagents/compare/v0.1.0...v0.3.0
[0.1.0]: https://github.com/buyun14/astrbot_plugin_reasonix_subagents/releases/tag/v0.1.0
