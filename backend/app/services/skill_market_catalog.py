"""技能市场精选清单：不受 GitHub 限流影响的离线兜底。

来源约束（实现时逐条核验：仓库存在、分支 main、`<subpath>/SKILL.md` 可访问且 frontmatter 含 name）：
- vikiboss/60s-skills      —— 中文生活/资讯类技能
- anthropics/skills        —— Anthropic 官方 Agent Skills 示例
- obra/superpowers         —— 软件工程工作流类技能

设计要点（勿凭直觉改动）：
1. **纯常量、不用 JSON 资源文件**：桌面版 PyInstaller 打包无需改 spec，避免漏拷。
2. **描述原文保留英文/中文原貌**：原描述带「Use when …」这类触发条件，对 AI 与用户都有信息量，
   截断到 300 字符以内即可，不要自行改写（改写会引入未经验证的能力声明）。
3. **subpath 必填**：本清单全部是多技能仓库，安装时必须带 subpath，否则
   `skill_pack_service.extract_zip` 会因「包含多个技能」拒绝提取。
4. 新增条目必须实际访问验证，不要凭印象填仓库名。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CuratedSkill:
    repo: str          # owner/name
    name: str          # SKILL.md frontmatter 的 name
    description: str
    tags: tuple[str, ...]
    subpath: str       # 仓库内的技能目录，必填


CURATED_SKILLS: tuple[CuratedSkill, ...] = (
    # ---- vikiboss/60s-skills：中文生活/资讯 ----
    CuratedSkill(
        repo="vikiboss/60s-skills", name="daily-news-60s",
        description="获取每天60秒的新闻简报，含15条必读、今日新闻和每日微语。",
        tags=("新闻", "资讯", "日报", "news"), subpath="skills/daily-news-60s",
    ),
    CuratedSkill(
        repo="vikiboss/60s-skills", name="data-query",
        description="查询各类数据：汇率、农历、历史事件、百科、油价、金价和化学元素。",
        tags=("汇率", "油价", "金价", "农历", "百科", "数据查询"), subpath="skills/data-query",
    ),
    CuratedSkill(
        repo="vikiboss/60s-skills", name="entertainment",
        description="获取娱乐内容：今日运势、英文笑话、土味情话、星座预测、KFC 疯狂星期四等。",
        tags=("娱乐", "段子", "星座", "运势"), subpath="skills/entertainment",
    ),
    CuratedSkill(
        repo="vikiboss/60s-skills", name="hot-topics",
        description="获取微博、知乎、百度、抖音、头条、B站等平台的实时热搜榜和热门话题。",
        tags=("热搜", "热榜", "微博", "知乎", "抖音", "trending"), subpath="skills/hot-topics",
    ),
    CuratedSkill(
        repo="vikiboss/60s-skills", name="media-info",
        description="获取音乐和影视榜单信息：热歌榜、飙升榜、新歌榜、电影票房、电视剧收视率。",
        tags=("音乐", "影视", "票房", "榜单"), subpath="skills/media-info",
    ),
    CuratedSkill(
        repo="vikiboss/60s-skills", name="utility-tools",
        description="实用工具集：IP 地址查询、文本翻译、二维码生成、哈希计算、网页元数据抓取、WHOIS 查询、密码生成。",
        tags=("翻译", "二维码", "IP查询", "工具", "加密"), subpath="skills/utility-tools",
    ),
    CuratedSkill(
        repo="vikiboss/60s-skills", name="weather-query",
        description="查询中国各地的实时天气、天气预报，含温度、湿度、风速、空气质量等信息。",
        tags=("天气", "天气预报", "weather"), subpath="skills/weather-query",
    ),

    # ---- anthropics/skills：官方示例 ----
    CuratedSkill(
        repo="anthropics/skills", name="algorithmic-art",
        description="Creating algorithmic art using p5.js with seeded randomness and interactive parameter exploration.",
        tags=("艺术", "生成艺术", "p5.js", "art"), subpath="skills/algorithmic-art",
    ),
    CuratedSkill(
        repo="anthropics/skills", name="brand-guidelines",
        description="Applies Anthropic's official brand colors and typography to any sort of artifact.",
        tags=("品牌", "设计", "brand"), subpath="skills/brand-guidelines",
    ),
    CuratedSkill(
        repo="anthropics/skills", name="canvas-design",
        description="Create beautiful visual art in .png and .pdf documents using design philosophy.",
        tags=("海报", "设计", "canvas", "pdf", "png"), subpath="skills/canvas-design",
    ),
    CuratedSkill(
        repo="anthropics/skills", name="doc-coauthoring",
        description="Guide users through a structured workflow for co-authoring documentation, proposals, technical specs.",
        tags=("文档", "写作", "文档协作"), subpath="skills/doc-coauthoring",
    ),
    CuratedSkill(
        repo="anthropics/skills", name="docx",
        description="Create, read, edit, or manipulate Word documents (.docx files) or Word templates (.dotx files).",
        tags=("Word", "docx", "Office", "办公"), subpath="skills/docx",
    ),
    CuratedSkill(
        repo="anthropics/skills", name="frontend-design",
        description="Guidance for distinctive, intentional visual design when building new UI or reshaping an existing one.",
        tags=("前端", "UI", "设计", "frontend"), subpath="skills/frontend-design",
    ),
    CuratedSkill(
        repo="anthropics/skills", name="mcp-builder",
        description="Guide for creating high-quality MCP (Model Context Protocol) servers.",
        tags=("MCP", "开发", "mcp-builder"), subpath="skills/mcp-builder",
    ),
    CuratedSkill(
        repo="anthropics/skills", name="pdf",
        description="Read and extract text/tables from PDFs, combine or merge multiple PDF files, and more.",
        tags=("PDF", "文档", "pdf"), subpath="skills/pdf",
    ),
    CuratedSkill(
        repo="anthropics/skills", name="pptx",
        description="Create slide decks, pitch decks, or presentations from .pptx or .potx files.",
        tags=("PPT", "pptx", "幻灯片", "Office"), subpath="skills/pptx",
    ),
    CuratedSkill(
        repo="anthropics/skills", name="skill-creator",
        description="Create new skills, modify and improve existing skills, and measure skill performance.",
        tags=("技能开发", "skill", "元技能"), subpath="skills/skill-creator",
    ),
    CuratedSkill(
        repo="anthropics/skills", name="slack-gif-creator",
        description="Knowledge and utilities for creating animated GIFs optimized for Slack.",
        tags=("GIF", "动画", "slack"), subpath="skills/slack-gif-creator",
    ),
    CuratedSkill(
        repo="anthropics/skills", name="theme-factory",
        description="Toolkit for styling artifacts with a theme: slides, docs, reports, HTML landing pages. 10 pre-set themes.",
        tags=("主题", "样式", "设计", "theme"), subpath="skills/theme-factory",
    ),
    CuratedSkill(
        repo="anthropics/skills", name="web-artifacts-builder",
        description="Suite of tools for creating elaborate, multi-component HTML artifacts using React, Tailwind CSS, shadcn/ui.",
        tags=("前端", "React", "HTML", "组件"), subpath="skills/web-artifacts-builder",
    ),
    CuratedSkill(
        repo="anthropics/skills", name="webapp-testing",
        description="Toolkit for interacting with and testing local web applications using Playwright.",
        tags=("测试", "Playwright", "浏览器自动化"), subpath="skills/webapp-testing",
    ),
    CuratedSkill(
        repo="anthropics/skills", name="xlsx",
        description="Open, read, edit, or fix an existing .xlsx/.csv spreadsheet, or build a new one.",
        tags=("Excel", "xlsx", "表格", "Office"), subpath="skills/xlsx",
    ),

    # ---- obra/superpowers：软件工程工作流 ----
    CuratedSkill(
        repo="obra/superpowers", name="brainstorming",
        description="Use before any creative work — creating features, building components, adding functionality, or planning anything new.",
        tags=("需求", "头脑风暴", "brainstorm"), subpath="skills/brainstorming",
    ),
    CuratedSkill(
        repo="obra/superpowers", name="systematic-debugging",
        description="Use when encountering any bug, test failure, or unexpected behavior, before proposing fixes.",
        tags=("调试", "debug", "排错"), subpath="skills/systematic-debugging",
    ),
    CuratedSkill(
        repo="obra/superpowers", name="test-driven-development",
        description="Use when implementing any feature or bugfix, before writing implementation code.",
        tags=("TDD", "测试驱动", "开发"), subpath="skills/test-driven-development",
    ),
    CuratedSkill(
        repo="obra/superpowers", name="writing-plans",
        description="Use when you have a spec or requirements for a multi-step task, before touching code.",
        tags=("方案", "规划", "plan"), subpath="skills/writing-plans",
    ),
    CuratedSkill(
        repo="obra/superpowers", name="verification-before-completion",
        description="Use when about to claim work is complete, fixed, or passing — requires running verification commands.",
        tags=("验证", "验收", "verification"), subpath="skills/verification-before-completion",
    ),
    CuratedSkill(
        repo="obra/superpowers", name="writing-skills",
        description="Use when creating new skills, editing existing skills, or verifying skills work before deployment.",
        tags=("技能开发", "skill"), subpath="skills/writing-skills",
    ),
)


def filter_curated(keywords: str) -> list[CuratedSkill]:
    """按关键词本地过滤精选清单（name/description/tags 子串匹配，大小写不敏感）。

    空关键词返回全部 —— 首次进入市场页即展示精选内容，不依赖网络。
    """
    kw = (keywords or "").strip().lower()
    if not kw:
        return list(CURATED_SKILLS)
    # 空格分隔的多词：任一命中即可（降低用户输入过严导致的空结果）
    words = [w for w in kw.split() if w]
    out: list[CuratedSkill] = []
    for s in CURATED_SKILLS:
        blob = (s.name + " " + s.description + " " + " ".join(s.tags)).lower()
        if any(w in blob for w in words):
            out.append(s)
    return out
