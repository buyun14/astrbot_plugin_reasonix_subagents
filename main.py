"""Reasonix read-only subagents plugin entry (agent-as-tool).

Ports DeepSeek-Reasonix's read-only subagents (explore / research / review /
security-review) plus an enhanced deep_review onto AstrBot. The plugin class
only owns lifecycle and the status command; all logic lives in the ``reasonix``
package. Unlike the old single-file design, tool instances are created here
(after config exists), so there is no module-level global config reference.
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .reasonix.config import ConfigHolder, subagent_enabled
from .reasonix.policy import SPECS, SPEC_ORDER, resolve_policy
from .reasonix.tools.agents import build_tools


class ReasonixSubagentsPlugin(Star):
    """Registers the Reasonix subagent delegation tools on the main LLM."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        # Provider mode: read self.config lazily on every holder.get() so
        # any later mutation/replacement of plugin.config (whether by a
        # future AstrBot hot-reload path or by tests that poke the
        # attribute directly) is picked up immediately without a manual
        # reload hook. Until the Star base class assigns self.config we
        # fall back to the initial value passed in here. The fallback
        # uses an explicit isinstance(dict) check so an empty {} plugin
        # config is treated as a real (empty) config, not as "use the
        # initial config" -- otherwise resetting a plugin to defaults
        # would silently keep the old settings.
        initial = dict(config or {})

        def _read_config() -> dict:
            current = getattr(self, "config", None)
            if isinstance(current, dict):
                return current
            return initial

        self.config_holder = ConfigHolder(_read_config)
        self.git_tool, self.tools = build_tools(self.config_holder)
        self._register()

    def _register(self) -> None:
        cfg = self.config_holder.get()
        enabled = [t for t in self.tools if subagent_enabled(cfg, t.name)]
        disabled = [t.name for t in self.tools if not subagent_enabled(cfg, t.name)]
        try:
            self.context.add_llm_tools(*enabled)
            names = ", ".join(t.name for t in enabled)
            if disabled:
                logger.info(
                    "Registered Reasonix subagent tools: %s (disabled: %s).",
                    names,
                    ", ".join(disabled),
                )
            else:
                logger.info("Registered Reasonix subagent tools: %s.", names)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to register Reasonix subagent tools.")

    async def shutdown(self) -> None:
        """Cleanup on plugin unload.

        In provider mode there is no snapshot to clear (the provider closure
        just reads ``self.config`` which will be torn down with the plugin
        instance), but keeping the hook around documents the lifecycle for
        callers and gives a place to add cleanup later if needed.
        """

    @filter.command(
        "reasonix_subagents", alias={"reasonix-subagents", "reasonix子代理"}
    )
    async def reasonix_subagents_status(self, event: AstrMessageEvent):
        """Show registered tools and their currently resolved policy."""
        cfg = self.config_holder.get()
        lines = [
            "Reasonix 子代理（agent-as-tool）：",
            "- explore: 只读代码库调查",
            "- research: 代码 + 网页研究",
            "- review: 只读代码评审（git diff）",
            "- security_review: 只读安全评审",
            "- deep_review: 并行多专家评审 + 置信度门禁（更慢更细）",
            "",
            "当前生效策略：",
        ]
        for name in SPEC_ORDER:
            if not subagent_enabled(cfg, name):
                lines.append(f"- {name}: ❌ 已禁用 (subagents.{name}.enabled=false)")
                continue
            policy = resolve_policy(cfg, SPECS[name])
            provider = policy.provider_id or "(沿用当前会话)"
            extras = ", ".join(policy.extra_tool_names) or "(空)"
            budget = (
                f", overall_budget={policy.overall_timeout}s"
                if policy.overall_timeout
                else ""
            )
            lines.append(
                f"- {name}: max_steps={policy.max_steps}, timeout={policy.timeout}s"
                f"{budget}, discover_web={policy.discover_web}, provider={provider}"
            )
            lines.append(f"    白名单: {', '.join(policy.allowed_tools) or '(空)'}")
            lines.append(f"    附加工具: {extras}")
        lines += [
            "",
            "依赖：代码读取需启用 Computer Use；research 需配置 web 搜索；",
            "review/security_review 需把 git 仓库放进会话 workspace。",
            "直接对主 LLM 说『explore 一下 …』或『review 当前改动』即可触发委派。",
        ]
        yield event.plain_result("\n".join(lines))
