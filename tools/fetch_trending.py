#!/usr/bin/env python3
"""从 GitHub Trending 抓取仓库，补齐 README，并（可选）生成中文简介。

依赖：python3（仅标准库）+ 已登录的 `gh` 命令。

用法：
    python3 tools/fetch_trending.py                  # 8 种语言 + 不限语言总榜，各 今日/本周/本月
    python3 tools/fetch_trending.py --since daily    # 只抓今日榜
    python3 tools/fetch_trending.py --repos owner/repo[,owner/repo]  # 只加/刷新指定仓库，不抓 trending
    python3 tools/fetch_trending.py --only-pinned    # 只刷新 PINNED 里的仓库，不抓 trending
    python3 tools/fetch_trending.py --no-summary     # 只抓数据，不调用大模型
    python3 tools/fetch_trending.py --languages go,rust
    python3 tools/fetch_trending.py --limit 15       # 每个窗口每种语言最多取 15 个

收录语言：JavaScript / TypeScript / Python / Java / Go / Rust / C / C++。
每轮还会抓一次不限语言的总榜：总榜里属于上面八种语言的会并入对应语言文件；不属于的
只归到 data/other.json，最终只出现在 data/all.json（不在下拉里单独列出）。

每次运行都是「合并」：已在 data/<language>.json 里的仓库保留（只刷新 star、更新时间
等实时字段），新上榜且没收录过的才补 README、写简介。每个仓库用 windows 记录它在
今日 / 本周 / 本月哪个榜上出现过。

数据落在 data/<language>.json 与 data/catalog.json。

生成简介需要 DeepSeek（或任何 OpenAI 兼容）接口：
    export DEEPSEEK_API_KEY=...
    export DEEPSEEK_BASE_URL=https://api.deepseek.com   # 可选
    export DEEPSEEK_MODEL=deepseek-chat                 # 可选
没有 key 或加了 --no-summary 时，会退化为「从 README 抽取」的英文/原文摘要。
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# key = GitHub trending 路径里的 slug，value = 页面展示用的语言名
LANGUAGES = {
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "python": "Python",
    "java": "Java",
    "go": "Go",
    "rust": "Rust",
    "c": "C",
    "cpp": "C++",
}

ALL_LABEL = "全部语言"    # data/all.json 的展示名（所有语言去重汇总）
OTHER_LABEL = "其他语言"  # 总榜里不属于上面任何语言的，只进 all

# 不管有没有上 trending，都固定收录这些仓库（owner/repo，会按语言归入对应文件）
PINNED = [
    "openclaw/openclaw",
    "NousResearch/hermes-agent",
    "stablyai/orca",
    "golang/go",
    "python/cpython",
    "RustPython/RustPython",
    "react/react",
]
PINNED_SET = set(PINNED)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) github-trending-newspaper/1.0"


# ── 抓取 GitHub Trending HTML ────────────────────────────────────────────────
def fetch_html(lang: str, since: str) -> str:
    url = f"https://github.com/trending/{lang}?since={since}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read().decode("utf-8", "replace")


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _num(s: str) -> int:
    return int(re.sub(r"[^\d]", "", s) or 0)


def parse_trending(page: str) -> list[dict]:
    """把 trending 页面拆成仓库列表。"""
    out: list[dict] = []
    for block in re.split(r'<article class="Box-row">', page)[1:]:
        block = block.split("</article>", 1)[0]
        h2 = re.search(r'<h2 class="h3 lh-condensed">(.*?)</h2>', block, re.S)
        if not h2:
            continue
        link = re.search(r'href="/([^"/]+)/([^"/?]+)"', h2.group(1))
        if not link:
            continue
        owner, name = link.group(1), link.group(2)

        desc = re.search(r'<p class="col-9[^"]*"[^>]*>(.*?)</p>', block, re.S)
        lang = re.search(r'<span itemprop="programmingLanguage">([^<]+)</span>', block)
        stars = re.search(r'href="/[^"]+/stargazers"[^>]*>(.*?)</a>', block, re.S)
        forks = re.search(r'href="/[^"]+/forks"[^>]*>(.*?)</a>', block, re.S)

        out.append(
            {
                "owner": owner,
                "name": name,
                "full_name": f"{owner}/{name}",
                "url": f"https://github.com/{owner}/{name}",
                "description": _text(desc.group(1)) if desc else "",
                "language": _text(lang.group(1)) if lang else LANGUAGES.get("", ""),
                "stars": _num(_text(stars.group(1))) if stars else 0,
                "forks": _num(_text(forks.group(1))) if forks else 0,
            }
        )
    return out


# ── 用 gh 补齐元数据与 README ────────────────────────────────────────────────
def gh_json(args: list[str]) -> dict | None:
    try:
        r = subprocess.run(
            ["gh", "api", *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def gh_text(args: list[str]) -> str | None:
    try:
        r = subprocess.run(
            ["gh", "api", *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return r.stdout if r.returncode == 0 else None


def enrich(repo: dict, need_readme: bool = True) -> dict:
    full = repo["full_name"]
    meta = gh_json([f"repos/{full}"])
    if meta:
        repo["description"] = meta.get("description") or repo["description"]
        repo["stars"] = meta.get("stargazers_count", repo["stars"])
        repo["forks"] = meta.get("forks_count", repo["forks"])
        repo["language"] = meta.get("language") or repo["language"]
        repo["homepage"] = meta.get("homepage") or ""
        repo["license"] = (meta.get("license") or {}).get("spdx_id") or ""
        repo["created_at"] = meta.get("created_at") or ""
        repo["pushed_at"] = meta.get("pushed_at") or ""
        repo["updated_at"] = meta.get("updated_at") or ""
        repo["open_issues"] = meta.get("open_issues_count", 0)
        repo["topics"] = meta.get("topics") or []
        if meta.get("archived"):
            repo["archived"] = True

    if need_readme:
        readme = gh_text([f"repos/{full}/readme", "-H", "Accept: application/vnd.github.raw"])
        repo["readme"] = clean_readme(readme or "")
    return repo


def repo_stub(full: str) -> dict:
    """指定仓库的初始对象（尚未补元数据）。"""
    owner, _, name = full.partition("/")
    return {
        "owner": owner,
        "name": name,
        "full_name": full,
        "url": f"https://github.com/{full}",
        "description": "",
        "language": "",
        "stars": 0,
        "forks": 0,
        "_windows": [],
    }


def pinned_stub(full: str) -> dict:
    stub = repo_stub(full)
    stub["pinned"] = True
    return stub


# ── README 清洗 ──────────────────────────────────────────────────────────────
def clean_readme(md: str) -> str:
    """去掉徽章、图片、HTML、多余的空白与链接语法，留下可读正文。"""
    if not md:
        return ""
    s = md.replace("\r\n", "\n")

    # HTML 注释、<details>、<img>、<div> 等
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"<details.*?</details>", "", s, flags=re.S | re.I)
    s = re.sub(r"<img[^>]*>", "", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)

    # 徽章 / 图片 / 链接
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)                       # 图片
    s = re.sub(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)", "", s)       # 徽章（图片套链接）
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)                  # 链接 -> 文本
    s = re.sub(r"^\s*\[!?\[.*?$", "", s, flags=re.M)                 # 残留的坏徽章行

    # 代码块
    s = re.sub(r"```.*?```", "", s, flags=re.S)
    s = re.sub(r"~~~.*?~~~", "", s, flags=re.S)
    s = re.sub(r"`([^`]*)`", r"\1", s)

    # 强调符号
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s, flags=re.S)
    s = re.sub(r"__(.+?)__", r"\1", s, flags=re.S)
    s = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])", r"\1", s)

    # emoji（保留文字，去掉表情符号）
    s = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]", "", s)

    # 标题符号 / 引用 / 列表符号 / 表格分隔
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"^\s{0,3}>\s?", "", s, flags=re.M)
    s = re.sub(r"^\s{0,3}[-*+]\s+", "", s, flags=re.M)
    s = re.sub(r"^\s*\d+\.\s+", "", s, flags=re.M)
    s = re.sub(r"^\s*\|.*\|\s*$", "", s, flags=re.M)
    s = re.sub(r"^\s*[-=*_:]{3,}\s*$", "", s, flags=re.M)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ── 简介生成 ─────────────────────────────────────────────────────────────────
SUMMARY_PROMPT = """你是一名开源项目编辑，请用简体中文为下面这个 GitHub 仓库写一段介绍。

仓库：{full_name}
官方一句话描述：{description}
主要语言：{language}

README 内容（可能被截断）：
---
{readme}
---

要求：
1. 输出 180–300 字（中文字符）的连贯段落，直接是正文。
2. 说清楚：它是什么、解决什么问题、核心能力 / 特点、典型使用场景或技术栈。
3. 客观、具体、平实，不要营销腔，不要用「这个项目」「总之」「值得一提的是」这类空话开头。
4. 只依据上面给出的信息，不要编造版本号、性能数字或未提及的功能。
5. 不要使用 Markdown 标记、标题、列表、表情符号或换行，只输出一段文字。
6. 不要提许可证 / 开源协议（license）名称，也不要写「采用 MIT 许可证」这类话。
"""


def _fallback_summary(repo: dict) -> str:
    """没有大模型时，从 README 里抽一段原文当简介。"""
    text = re.sub(r"\s+", " ", repo.get("readme") or "").strip()
    desc = repo.get("description") or ""
    if not text:
        return desc
    chunks = re.split(r"(?<=[.。!！?？])\s+", text)
    picked, total = [], 0
    for c in chunks:
        if len(c) < 12:
            continue
        picked.append(c)
        total += len(c)
        if total >= 320:
            break
    body = " ".join(picked)[:420]
    return body or desc


def _clamp_cn(text: str, limit: int = 300) -> str:
    """控制在一段话、且不超 limit 个字符；超长时在句末截断。"""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    cut = max(head.rfind("。"), head.rfind("！"), head.rfind("？"), head.rfind("."))
    return (head[: cut + 1] if cut > limit * 0.5 else head.rstrip("，,、 ") + "。")


def _chat(prompt: str, temperature: float, max_tokens: int, attempt: int = 0) -> str | None:
    """调用 OpenAI 兼容接口，返回文本；没有 key 或调用失败时返回 None。"""
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        # 429/5xx 是临时的，退避重试；其它（如 401/402）直接失败
        if e.code in (429, 500, 502, 503) and attempt < 3:
            time.sleep(3 * (attempt + 1))
            return _chat(prompt, temperature, max_tokens, attempt + 1)
        print(f"    ! 模型调用失败: {e}", file=sys.stderr)
        return None
    except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as e:
        print(f"    ! 模型调用失败: {e}", file=sys.stderr)
        return None


def call_llm(repo: dict) -> str | None:
    prompt = SUMMARY_PROMPT.format(
        full_name=repo["full_name"],
        description=repo.get("description") or "（无）",
        language=repo.get("language") or "",
        readme=(repo.get("readme") or "")[:9000] or "（README 为空）",
    )
    text = _chat(prompt, 0.4, 700)
    return _clamp_cn(text) if text else None


def summarize(repo: dict, use_llm: bool) -> str:
    if use_llm:
        text = call_llm(repo)
        if text:
            return text
    return _fallback_summary(repo)


# ── 标签（kind + domain）─────────────────────────────────────────────────────
# 两个维度都写入仓库对象：tags / primary_tag（形式），domains / primary_domain（领域）。
TAG_KINDS = ["app", "tool", "library", "framework", "plugin", "template", "doc",
             "asset", "model", "config", "example", "other"]
TAG_DOMAINS = ["web", "mobile", "desktop", "devops", "ai", "data", "networking",
               "security", "systems", "devtools", "media", "graphics", "blockchain",
               "productivity", "learning", "embedded", "other"]

TAG_PROMPT = """你是开源仓库分类器。给下面仓库打标签。

仓库：{full_name}
官方描述：{description}
主要语言：{language}
GitHub topics：{topics}

A. kind（形式/用途），从下面选 1~3 个，第一个是最主要形式：
 app=面向最终用户、可独立运行的程序/服务/客户端
 tool=命令行工具
 library=被别的程序引用的库/SDK（你调用它）
 framework=应用框架（它调用你）
 plugin=插件/扩展/集成（依附宿主程序运行）
 template=脚手架/模板/样板工程
 doc=文档/教程/书/清单/速查表/路线图
 asset=字体/图标/主题/CSS 等资源
 model=模型权重/数据集
 config=dotfiles/环境配置/安装脚本
 example=示例/demo 代码
 other=其他

B. domain（领域），从下面选 1~3 个，第一个是最主要领域：
 {domains}

domain 判定规则（务必遵守）：
 - 填「仓库解决的问题所在的领域」，不是它用什么技术栈。
 - devtools 只给「面向开发者本人的工具」：编辑器/IDE、构建/包管理、代码生成、lint/格式化、测试、debug/profiler、通用 CLI。严禁把普通库或文档塞进 devtools 兜底。
 - 普通库/框架按其用途领域归类，而不是 devtools。
 - devops=部署/容器/K8s/CI-CD/监控/IaC/服务网格；web=网站/前端/后端 Web/CMS/API；data=数据库/存储/数据处理/分析；ai=大模型/机器学习/智能体/推理；systems=操作系统/内核/终端/shell/编译工具链/底层库/语言运行时；networking=网络协议/代理/网关/DNS/P2P/消息队列；security=安全/渗透/密钥/漏洞；media=音视频/图像/编解码/播放器；graphics=图形/渲染/3D/游戏引擎；productivity=笔记/办公/自托管日常/密码/知识管理；learning=教程/面试/算法题/课程/路线图；embedded=嵌入式/IoT/单片机/固件；other=其他。
 - 如果 kind 含 doc（文档/清单/教程），domain 优先用 learning 或它讲的领域。

参考锚点（务必与之一致）：
 lodash/lodash -> {{"kind":["library"],"primary_kind":"library","domain":["web"],"primary_domain":"web"}}
 gin-gonic/gin -> library / web
 numpy/numpy -> library / data
 junegunn/fzf -> tool / devtools
 vim/vim -> app / devtools
 jellyfin/jellyfin -> app / media
 lua/lua -> app / systems
 avelino/awesome-go -> doc / learning

只输出一行 JSON，不要解释：
{{"kind":["..."],"primary_kind":"...","domain":["..."],"primary_domain":"..."}}
"""


def _parse_tag_json(text: str | None) -> dict | None:
    if not text:
        return None
    s = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    kind = [k for k in (d.get("kind") or []) if k in TAG_KINDS]
    domain = [x for x in (d.get("domain") or []) if x in TAG_DOMAINS]
    if not kind:
        kind = [d["primary_kind"]] if d.get("primary_kind") in TAG_KINDS else ["other"]
    if not domain:
        domain = [d["primary_domain"]] if d.get("primary_domain") in TAG_DOMAINS else ["other"]
    primary_kind = d.get("primary_kind") if d.get("primary_kind") in kind else kind[0]
    primary_domain = d.get("primary_domain") if d.get("primary_domain") in domain else domain[0]
    return {"kind": kind, "primary_kind": primary_kind,
            "domain": domain, "primary_domain": primary_domain}


def _fallback_tags(repo: dict) -> dict:
    """没有大模型时，按名字/描述/topics 里的关键词粗略分类。"""
    text = " ".join([
        repo.get("full_name") or "", repo.get("description") or "",
        " ".join(repo.get("topics") or []),
    ]).lower()
    kind = "other"
    if any(w in text for w in ("awesome", "tutorial", "guide", "book", "cheatsheet", "roadmap", "curated")):
        kind = "doc"
    elif any(w in text for w in ("boilerplate", "starter", "template", "scaffold")):
        kind = "template"
    elif any(w in text for w in ("font", "icon", "theme", "css framework")):
        kind = "asset"
    elif any(w in text for w in ("command-line", "command line", "cli", "terminal")):
        kind = "tool"
    elif "framework" in text:
        kind = "framework"
    elif any(w in text for w in ("library", "sdk", "binding", "client")):
        kind = "library"
    elif any(w in text for w in ("server", "app", "platform", "editor")):
        kind = "app"
    return {"kind": [kind], "primary_kind": kind, "domain": ["other"], "primary_domain": "other"}


def classify_tags(repo: dict, use_llm: bool) -> None:
    parsed = None
    if use_llm:
        prompt = TAG_PROMPT.format(
            full_name=repo["full_name"],
            description=repo.get("description") or "（无）",
            language=repo.get("language") or "",
            topics=", ".join(repo.get("topics") or []) or "（无）",
            domains=", ".join(TAG_DOMAINS),
        )
        parsed = _parse_tag_json(_chat(prompt, 0.0, 200))
    if not parsed:
        parsed = _fallback_tags(repo)
    repo["tags"] = parsed["kind"]
    repo["primary_tag"] = parsed["primary_kind"]
    repo["domains"] = parsed["domain"]
    repo["primary_domain"] = parsed["primary_domain"]


# ── 主流程 ───────────────────────────────────────────────────────────────────
# 每次抓取是「合并」而不是「重建」：已经在列表里的仓库保留（只刷新 star / 更新时间
# 等实时字段），新上榜且没收录过的才补 README、生成简介。所以 data/<lang>.json 会
# 随天数不断长大，不绑定某一个「榜单日」。
REFRESH_FIELDS = (
    "description", "stars", "forks", "language", "homepage",
    "license", "created_at", "pushed_at", "updated_at",
    "open_issues", "archived",
)


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print(f"! {path.name} 读取失败，按空列表重建", file=sys.stderr)
        return {}


WINDOW_ORDER = ("daily", "weekly", "monthly")
WINDOW_LABEL = {"daily": "今日", "weekly": "本周", "monthly": "本月"}
SLUG_BY_LABEL = {label: slug for slug, label in LANGUAGES.items()}


def collect(slug: str | None, wins: list[str], limit: int) -> list[dict]:
    """抓某种语言（slug=None 表示不限语言的总榜）各窗口，按仓库去重后返回。"""
    tag = slug or "总榜"
    union: dict[str, dict] = {}
    for since in wins:
        where = f"{WINDOW_LABEL[since]}{'榜' if slug else '总榜'}"
        print(f"[{tag}] 拉取 {where}…", file=sys.stderr)
        for repo in parse_trending(fetch_html(slug or "", since))[:limit]:
            full = repo["full_name"]
            if full in union:
                union[full]["_windows"].append(since)
            else:
                repo["_windows"] = [since]
                union[full] = repo
    return list(union.values())


def combine(*groups: list[dict]) -> list[dict]:
    """按 full_name 合并多份仓库列表，windows 取并集。"""
    union: dict[str, dict] = {}
    for group in groups:
        for repo in group:
            full = repo["full_name"]
            if full in union:
                union[full]["_windows"] = list(
                    dict.fromkeys(union[full]["_windows"] + repo.get("_windows", []))
                )
            else:
                copy = dict(repo)
                copy["_windows"] = list(repo.get("_windows", []))
                union[full] = copy
    return list(union.values())


def windows_of(repo: dict) -> list[str]:
    seen = set(repo.pop("_windows", []))
    return [w for w in WINDOW_ORDER if w in seen]


def merge_into(
    path: Path, fresh: list[dict], label: str, wins: list[str],
    use_llm: bool, force_summary: bool = False,
) -> dict:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    today = now.date().isoformat()

    existing = load_json(path)
    old_repos = existing.get("repos", [])
    first_at = existing.get("first_at") or existing.get("generated_at") or now_iso
    known = {r["full_name"] for r in old_repos}

    new_count = sum(1 for r in fresh if r["full_name"] not in known)
    print(f"[{label}] 去重后 {len(fresh)} 个，其中新增 {new_count} 个；补齐元数据…", file=sys.stderr)

    def is_new(repo: dict) -> bool:
        return repo["full_name"] not in known

    # 已收录的仓库不需要重新抓 README / 重新写简介（force_summary 时除外）
    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        fresh = list(pool.map(lambda r: enrich(r, force_summary or is_new(r)), fresh))

    def finish(repo: dict) -> dict:
        if force_summary or not repo.get("summary"):
            repo["summary"] = summarize(repo, use_llm)
        if force_summary or not repo.get("tags"):
            classify_tags(repo, use_llm)
        repo.pop("readme", None)
        repo.pop("topics", None)
        return repo

    # 新仓库要写简介 + 标签；老仓库只在缺标签时补标签
    to_finish = fresh if force_summary else [
        r for r in fresh if is_new(r) or not r.get("tags")
    ]
    with futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(finish, to_finish))

    # 合并：老仓库保留，新仓库入列
    by = {r["full_name"]: r for r in old_repos}
    added = 0
    for repo in fresh:
        full = repo["full_name"]
        repo.pop("readme", None)
        hits = windows_of(repo)
        if full in by:
            old = by[full]
            for key in REFRESH_FIELDS:
                if key in repo:
                    old[key] = repo[key]
            old["windows"] = [w for w in WINDOW_ORDER if w in set(old.get("windows", [])) | set(hits)]
            old["last_seen"] = today
            old["seen_count"] = old.get("seen_count", 1) + 1
            old.setdefault("added_at", first_at)
            old.setdefault("summary", "")
            if force_summary and repo.get("summary"):
                old["summary"] = repo["summary"]   # 强制重写的简介要写回去
            if (force_summary or not old.get("tags")) and repo.get("tags"):
                old["tags"] = repo["tags"]
                old["primary_tag"] = repo["primary_tag"]
                old["domains"] = repo["domains"]
                old["primary_domain"] = repo["primary_domain"]
            if full in PINNED_SET:
                old["pinned"] = True
        else:
            repo["windows"] = hits
            repo["added_at"] = now_iso
            repo["last_seen"] = today
            repo["seen_count"] = 1
            if full in PINNED_SET:
                repo["pinned"] = True
            by[full] = repo
            added += 1

    repos = list(by.values())
    for repo in repos:
        repo.pop("stars_today", None)   # 不再记录/展示「今日新增 star」
        repo.pop("topics", None)        # 不再记录话题标签
        repo.pop("authors", None)       # 不再记录贡献者头像
        if not repo.get("windows") and not repo.get("pinned"):
            repo["windows"] = ["daily"]  # 早期只抓过今日榜的仓库补个默认值
    repos.sort(key=lambda r: r.get("stars", 0), reverse=True)
    return {
        "language": path.stem,
        "label": label,
        "since": list(wins),
        "generated_at": now_iso,   # 最近一次抓取
        "first_at": first_at,      # 首次收录
        "count": len(repos),
        "added": added,            # 本次新增
        "repos": repos,
    }


def write_doc(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rebuild_all(wins: list[str]) -> None:
    """把所有语言文件 + other.json 去重合并成 all.json，并重写 catalog.json。"""
    docs = []
    for slug in LANGUAGES:
        path = DATA / f"{slug}.json"
        if path.exists():
            docs.append(load_json(path))
    other_path = DATA / "other.json"
    if other_path.exists():
        docs.append(load_json(other_path))

    merged: dict[str, dict] = {}
    for d in docs:
        for repo in d["repos"]:
            merged.setdefault(repo["full_name"], repo)
    all_repos = sorted(merged.values(), key=lambda r: r.get("stars", 0), reverse=True)
    firsts = [d.get("first_at") or d.get("generated_at") for d in docs]
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_doc(DATA / "all.json", {
        "language": "all",
        "label": ALL_LABEL,
        "since": list(wins),
        "generated_at": now_iso,
        "first_at": min(firsts) if firsts else now_iso,
        "count": len(all_repos),
        "repos": all_repos,
    })
    print(f"[all] 写入 data/all.json（汇总 {len(all_repos)} 个）", file=sys.stderr)

    catalog_langs = [{"key": "all", "label": ALL_LABEL, "count": len(all_repos)}]
    for d in docs:
        if d["language"] == "other":
            continue
        catalog_langs.append({"key": d["language"], "label": d["label"], "count": d["count"]})
    write_doc(DATA / "catalog.json", {
        "title": "GitHub Trending",
        "generated_at": now_iso,
        "since": list(wins),
        "languages": catalog_langs,
    })


def retag(names: list[str], use_llm: bool) -> int:
    """给 data 里缺标签的仓库补 kind/domain 标签；names 非空时只重打这几个。"""
    wanted = set(names)
    for slug in [*LANGUAGES, "other"]:
        path = DATA / f"{slug}.json"
        if not path.exists():
            continue
        doc = load_json(path)
        repos = doc.get("repos", [])
        targets = ([r for r in repos if r["full_name"] in wanted] if wanted
                   else [r for r in repos
                         if not r.get("tags") or r.get("primary_domain") == "other"])
        if not targets:
            continue
        print(f"[{slug}] 补标签 {len(targets)} 个…", file=sys.stderr)
        with futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda r: enrich(r, need_readme=False), targets))
        with futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda r: classify_tags(r, use_llm), targets))
        for r in targets:
            r.pop("topics", None)
        repos.sort(key=lambda r: r.get("stars", 0), reverse=True)
        doc["repos"] = repos
        doc["count"] = len(repos)
        doc["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_doc(path, doc)
    rebuild_all(list(WINDOW_ORDER))
    print("标签补齐完成。", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取 GitHub Trending 并向已有列表合并")
    ap.add_argument("--languages", default="all", help="逗号分隔，默认 all")
    ap.add_argument(
        "--since", default="all", choices=["all", *WINDOW_ORDER],
        help="榜单窗口；all = 今日+本周+本月（默认）",
    )
    ap.add_argument("--limit", type=int, default=25, help="每个窗口每种语言最多取几个上榜仓库")
    ap.add_argument("--repos", default="", help="只处理这些仓库（owner/repo，逗号分隔），不抓 trending")
    ap.add_argument("--only-pinned", action="store_true", help="只处理 PINNED 里的仓库，不抓 trending")
    ap.add_argument("--no-summary", action="store_true", help="跳过中文简介生成")
    ap.add_argument("--resummarize", action="store_true", help="重写简介（只配合 --repos / --only-pinned）")
    ap.add_argument("--retag", action="store_true", help="给缺标签的仓库补 kind/domain 标签，不抓 trending")
    args = ap.parse_args()

    if args.resummarize and not (args.repos or args.only_pinned):
        print("--resummarize 需要配合 --repos 或 --only-pinned 使用", file=sys.stderr)
        return 1

    wins = list(WINDOW_ORDER) if args.since == "all" else [args.since]

    slugs = list(LANGUAGES) if args.languages == "all" else [
        s.strip() for s in args.languages.split(",") if s.strip() in LANGUAGES
    ]
    if not slugs:
        print("没有可用的语言。可选：", ", ".join(LANGUAGES), file=sys.stderr)
        return 1

    use_llm = not args.no_summary
    if use_llm and not os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        print("! 未发现 DEEPSEEK_API_KEY，退化为抽取式摘要（--no-summary 同效）", file=sys.stderr)
        use_llm = False

    DATA.mkdir(exist_ok=True)

    # ── 模式 0：只补标签（--retag），不抓 trending ──
    if args.retag:
        names = [s.strip() for s in args.repos.split(",") if s.strip()] if args.repos else []
        print(f"[retag] {'指定 ' + str(len(names)) + ' 个仓库' if names else '所有缺标签的仓库'}", file=sys.stderr)
        return retag(names, use_llm)

    # ── 模式 A：只处理指定仓库（--repos）或固定仓库（--only-pinned），不抓 trending ──
    if args.repos or args.only_pinned:
        names = [s.strip() for s in args.repos.split(",") if s.strip()] if args.repos else list(PINNED)
        bad = [n for n in names if "/" not in n]
        if bad:
            print("仓库名要写成 owner/repo：", ", ".join(bad), file=sys.stderr)
            return 1
        print(f"[指定] 只处理 {len(names)} 个仓库，不抓 trending：{', '.join(names)}", file=sys.stderr)

        fresh = [repo_stub(n) for n in names]
        with futures.ThreadPoolExecutor(max_workers=6) as pool:
            fresh = list(pool.map(lambda r: enrich(r, need_readme=False), fresh))
        for repo in fresh:
            if repo["full_name"] in PINNED_SET:
                repo["pinned"] = True

        by_lang: dict[str, list[dict]] = {}
        other: list[dict] = []
        for repo in fresh:
            slug = SLUG_BY_LABEL.get((repo.get("language") or "").strip())
            if slug:
                by_lang.setdefault(slug, []).append(repo)
            else:
                other.append(repo)

        for slug, repos in by_lang.items():
            doc = merge_into(
                DATA / f"{slug}.json", repos, LANGUAGES[slug], wins, use_llm,
                force_summary=args.resummarize,
            )
            write_doc(DATA / f"{slug}.json", doc)
            print(
                f"[{slug}] 写入 data/{slug}.json（累计 {doc['count']} 个，新增 {doc['added']} 个）",
                file=sys.stderr,
            )
        if other:
            other_doc = merge_into(DATA / "other.json", other, OTHER_LABEL, wins, use_llm)
            write_doc(DATA / "other.json", other_doc)
            print(f"[other] 写入 data/other.json（{other_doc['count']} 个）", file=sys.stderr)

        rebuild_all(wins)
        print("完成。", file=sys.stderr)
        return 0

    # ── 模式 B：正常抓 trending（各语言榜 + 总榜），顺带刷新固定仓库 ──
    overall = collect(None, wins, args.limit)

    pinned = [pinned_stub(f) for f in PINNED]
    with futures.ThreadPoolExecutor(max_workers=6) as pool:
        pinned = list(pool.map(lambda r: enrich(r, need_readme=False), pinned))
    print(f"[pinned] 固定收录 {len(pinned)} 个：{', '.join(r['full_name'] for r in pinned)}", file=sys.stderr)

    routed: dict[str, list[dict]] = {slug: [] for slug in LANGUAGES}
    other: list[dict] = []
    for repo in [*overall, *pinned]:
        slug = SLUG_BY_LABEL.get((repo.get("language") or "").strip())
        if slug in routed:
            routed[slug].append(repo)
        else:
            other.append(repo)
    print(f"[总榜] 去重后 {len(overall)} 个，与固定收录合并后按语言分流", file=sys.stderr)

    for slug in slugs:
        fresh = combine(collect(slug, wins, args.limit), routed.get(slug, []))
        doc = merge_into(DATA / f"{slug}.json", fresh, LANGUAGES[slug], wins, use_llm)
        write_doc(DATA / f"{slug}.json", doc)
        print(
            f"[{slug}] 写入 data/{slug}.json（累计 {doc['count']} 个，本次新增 {doc['added']} 个）",
            file=sys.stderr,
        )

    other_doc = merge_into(DATA / "other.json", other, OTHER_LABEL, wins, use_llm)
    write_doc(DATA / "other.json", other_doc)
    print(f"[other] 写入 data/other.json（{other_doc['count']} 个，不单独展示）", file=sys.stderr)

    rebuild_all(wins)
    print("完成。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
