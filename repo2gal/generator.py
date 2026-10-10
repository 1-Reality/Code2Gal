"""剧本生成的确定性部分：选角、上下文渲染与 prompt 组装。

分两步：
1. 选角（cast）—— 确定性代码完成，不交给 LLM。
   角色名一旦由 LLM 自由发挥，validator 就无法区分「幻觉命令」和「新角色」，
   所以角色表必须在生成之前就固定下来，并作为白名单传给 validator。
2. prompt 组装 —— 依据素材与剧本模式渲染上下文并套用模板；
   LLM 调用本身在 ``llm.py``。

本模块不做网络、不做打包；LLM 只负责叙事创作。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import (
    DEFAULT_BACKGROUNDS,
    DEFAULT_BGM,
    DEFAULT_GAME_MODE,
    GAME_MODES,
)
from .errors import UsageError
from .fetcher import RepoContext

PROMPT_DIR = Path(__file__).parent / "prompts"

_MODE_TEMPLATES = {
    "chronicle": "chronicle.md",
    "overview": "overview.md",
    "quickstart": "quickstart.md",
}


@dataclass
class Cast:
    """出场角色表。由代码确定，不由 LLM 决定。"""

    entries: list[tuple[str, str]]  # (角色名, 人设说明)

    @property
    def names(self) -> set[str]:
        return {name for name, _ in self.entries}

    def render(self) -> str:
        return "\n".join(f"- {name}：{desc}" for name, desc in self.entries)


def _sanitize_name(login: str) -> str:
    """把 GitHub login 变成安全的角色名。

    角色名会出现在冒号左边，因此不能含 ':' ';' '-' 等解析器敏感字符。
    """
    name = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", login)
    return name or "Dev"


def build_cast(ctx: RepoContext, *, mode: str = DEFAULT_GAME_MODE) -> Cast:
    """从仓库数据推导角色表。

    Chronicle 使用三类角色：旁白者（项目本体拟人）、核心贡献者、技术栈精灵。
    Overview 是新手引导，只保留项目向导与技术栈精灵，避免把历史讨论角色
    带入“快速了解项目”的剧本。
    Quick Start 是贡献者上手，保留项目化身（带新人的维护者）、最多两位
    维护者与技术栈精灵：改动的评审者会真实出场，历史争论角色不出现。
    """
    if mode not in GAME_MODES:
        raise UsageError(f"未知剧本模式：{mode}")
    entries: list[tuple[str, str]] = []
    existing_names: set[str] = set()

    def add(name: str, description: str) -> None:
        if name and name not in existing_names:
            entries.append((name, description))
            existing_names.add(name)

    project = _sanitize_name(ctx.name)
    if mode == "overview":
        add(
            project,
            f"{ctx.full_name} 的项目化身，这次担任新手村向导。{ctx.description or '只讲资料能证明的内容'}",
        )
    elif mode == "quickstart":
        add(
            project,
            f"{ctx.full_name} 的项目化身，负责带第一次参与项目的人走完上手流程。"
            f"{ctx.description or '只讲资料能证明的做法'}",
        )
    else:
        add(
            project,
            f"{ctx.full_name} 的项目化身，见证全部历史。{ctx.description or '沉稳的叙述者'}",
        )

    if mode == "chronicle":
        for c in ctx.contributors[:4]:
            add(
                _sanitize_name(c.login),
                f"社区参与者，在筛选后的叙事素材中出现 {c.contributions} 次",
            )
    elif mode == "quickstart":
        for c in ctx.contributors[:2]:
            add(
                _sanitize_name(c.login),
                f"维护者之一，评审外部贡献者的改动，在起步任务中出现 {c.contributions} 次",
            )

    lang = _sanitize_name(ctx.language)
    if lang and lang.lower() != "未知":
        descriptions = {
            "overview": f"{ctx.language} 语言的拟人化身，负责解释技术栈和配置层面的问题",
            "quickstart": f"{ctx.language} 语言的拟人化身，负责解释开发环境、依赖与构建问题",
        }
        add(
            lang,
            descriptions.get(mode, f"{ctx.language} 语言的拟人化身，代表这个项目的技术底色"),
        )

    return Cast(entries=entries)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) > max_chars:
        return text[:max_chars] + "\n…（素材过长，已截断）"
    return text


def render_context(ctx: RepoContext, *, max_chars: int = 14000) -> str:
    """把 RepoContext 渲染成给 LLM 读的文本。

    按价值排序后截断：讨论 > Release > README。讨论是 Chronicle 模式的核心素材。
    """
    parts: list[str] = [
        f"## 仓库：{ctx.full_name}",
        f"- 简介：{ctx.description or '（无）'}",
        f"- 主语言：{ctx.language}　创建于：{ctx.created_at or '未知'}",
    ]
    if ctx.stars:
        parts.append(f"- Star：{ctx.stars}")
    if ctx.topics:
        parts.append(f"- 主题标签：{', '.join(ctx.topics[:10])}")

    if ctx.contributors:
        who = "，".join(f"{c.login}（{c.contributions}）" for c in ctx.contributors[:6])
        parts.append(f"- 核心贡献者：{who}")

    if ctx.releases:
        parts.append("\n## 版本里程碑")
        for r in ctx.releases:
            line = f"- {r.tag}（{r.published_at}）{r.name}"
            if r.body:
                line += f"\n  {r.body[:200]}"
            parts.append(line)

    if ctx.threads:
        parts.append("\n## 社区讨论（按热度排序，剧情主要素材）")
        for t in ctx.threads:
            parts.append(
                f"\n### #{t.number} [{t.kind.upper()}/{t.state}] {t.title}"
                f"\n发起人：{t.author}　时间：{t.created_at}　评论数：{t.comment_count}"
            )
            if t.body:
                parts.append(f"正文：{t.body}")
            for c in t.comments:
                parts.append(f"  · {c.author}：{c.body}")

    if ctx.readme_excerpt:
        parts.append(f"\n## README 摘录\n{ctx.readme_excerpt}")

    if ctx.wiki_excerpt:
        parts.append(f"\n## Wiki 摘录\n{ctx.wiki_excerpt}")

    return _truncate("\n".join(parts), max_chars)


def render_overview_context(ctx: RepoContext, *, max_chars: int = 12000) -> str:
    """把 RepoContext 渲染成 Overview 模式专用的 LLM 上下文。

    Overview 的素材优先级：README > 目录结构/项目文件 > 最近 Release > wiki。
    社区讨论是编年史素材，不进入概览上下文。
    """
    parts: list[str] = [
        f"## 仓库：{ctx.full_name}",
        f"- 一句话简介：{ctx.description or '（资料未提供，请从 README 提炼）'}",
        f"- 主语言：{ctx.language}　创建于：{ctx.created_at or '未知'}",
    ]
    if ctx.stars:
        parts.append(f"- Star：{ctx.stars}")
    if ctx.topics:
        parts.append(f"- 主题标签：{', '.join(ctx.topics[:10])}")
    if ctx.contributors:
        who = "，".join(f"{c.login}（{c.contributions}）" for c in ctx.contributors[:6])
        parts.append(f"- 主要参与者：{who}")

    if ctx.readme_excerpt:
        parts.append(f"\n## README 摘录（项目自我介绍、特性与安装用法）\n{ctx.readme_excerpt}")

    if ctx.file_tree:
        parts.append(
            f"\n## 目录结构（已过滤依赖目录与构建产物，最深 {4} 层）\n{ctx.file_tree}"
        )

    if ctx.project_files:
        parts.append(f"\n## 根级项目文件摘录（安装、构建、贡献入口）\n{ctx.project_files}")

    if ctx.releases:
        parts.append("\n## 最近版本里程碑（只用于陈述版本事实）")
        for r in ctx.releases[:3]:
            line = f"- {r.tag}（{r.published_at}）{r.name}"
            if r.body:
                line += f"\n  {r.body[:120]}"
            parts.append(line)

    if ctx.wiki_excerpt:
        parts.append(f"\n## Wiki 摘录\n{ctx.wiki_excerpt}")

    return _truncate("\n".join(parts), max_chars)


def render_quickstart_context(ctx: RepoContext, *, max_chars: int = 16000) -> str:
    """把 RepoContext 渲染成 Quick Start 模式专用的 LLM 上下文。

    Quick Start 的素材优先级：贡献者入口文件（CONTRIBUTING、构建脚本与 CI 定义）
    > README > 目录结构 > 起步任务 > wiki。社区历史讨论是编年史素材，不进入
    贡献者上手剧本。
    """
    parts: list[str] = [
        f"## 仓库：{ctx.full_name}",
        f"- 一句话简介：{ctx.description or '（资料未提供，请从 README 提炼）'}",
        f"- 主语言：{ctx.language}　创建于：{ctx.created_at or '未知'}",
    ]
    if ctx.stars:
        parts.append(f"- Star：{ctx.stars}")
    if ctx.topics:
        parts.append(f"- 主题标签：{', '.join(ctx.topics[:10])}")
    if ctx.contributors:
        who = "，".join(f"{c.login}（{c.contributions}）" for c in ctx.contributors[:6])
        parts.append(f"- 起步任务里出现的维护者与参与者：{who}")

    if ctx.contributor_files:
        parts.append(
            "## 贡献者入口文件摘录（贡献指南、构建脚本与 CI 定义，提交规矩的权威来源）\n"
            f"{ctx.contributor_files}"
        )

    if ctx.readme_excerpt:
        parts.append(f"\n## README 摘录（安装、开发与运行说明）\n{ctx.readme_excerpt}")

    if ctx.file_tree:
        parts.append(f"\n## 目录结构（已过滤依赖目录与构建产物，最深 4 层）\n{ctx.file_tree}")

    if ctx.starter_issues:
        parts.append("\n## 起步任务（开放且带新人标签的真实 Issue，只能引用这里的编号）")
        for issue in ctx.starter_issues:
            labels = "、".join(issue.labels) or "（无标签）"
            parts.append(
                f"\n### #{issue.number} {issue.title}"
                f"\n标签：{labels}　发起人：{issue.author}　时间：{issue.created_at}"
                f"　评论数：{issue.comment_count}"
            )
            if issue.body:
                parts.append(f"正文：{issue.body}")
    else:
        parts.append(
            "\n## 起步任务\n（备份里没有带新人标签的开放 Issue：请改为讲解如何自己筛选可上手的任务，"
            "不要编造编号）"
        )

    if ctx.wiki_excerpt:
        parts.append(f"\n## Wiki 摘录\n{ctx.wiki_excerpt}")

    return _truncate("\n".join(parts), max_chars)


#: 各模式的第一轮上下文渲染器：模式与素材取舍一一对应，禁止在调用处散落 if/else。
_CONTEXT_RENDERERS = {
    "chronicle": render_context,
    "overview": render_overview_context,
    "quickstart": render_quickstart_context,
}


def build_prompt(
    ctx: RepoContext,
    cast: Cast,
    *,
    mode: str = DEFAULT_GAME_MODE,
    backgrounds: list[str] | None = None,
    figures: list[str] | None = None,
    bgm: list[str] | None = None,
) -> str:
    if mode not in GAME_MODES:
        raise UsageError(f"未知剧本模式：{mode}")
    template = (PROMPT_DIR / _MODE_TEMPLATES[mode]).read_text(encoding="utf-8")
    background_names = DEFAULT_BACKGROUNDS if backgrounds is None else backgrounds
    bgm_names = DEFAULT_BGM if bgm is None else bgm
    context = _CONTEXT_RENDERERS[mode](ctx)
    return (
        template.replace("{characters}", cast.render())
        .replace("{backgrounds}", "、".join(background_names) or "（无）")
        .replace("{figures}", "、".join(figures or []) or "（无）")
        .replace("{bgm}", "、".join(bgm_names) or "（无）")
        .replace("{context}", context)
    )
