import json
import re
import html
import os
import time
from pathlib import Path
from datetime import datetime
import yaml
from youtube_transcript_api import YouTubeTranscriptApi
from openai import OpenAI
import yt_dlp


# 固定类别表：让归档文件夹数量可控，不至于满目散乱
CATEGORIES = [
    "心理/情绪", "健康/医学", "人际关系", "商业/创业", "科技/AI",
    "哲学/思维", "财务/投资", "科学/研究", "学习/成长", "访谈/人物", "其他",
]


def load_config(config_path="config.yaml"):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _strip_quotes(s):
    """Remove accidental surrounding quotes a user might type in the UI."""
    s = s.strip()
    for q in ("'''", '"""', '"', "'"):
        if s.startswith(q) and s.endswith(q) and len(s) > len(q) * 2:
            s = s[len(q):-len(q)].strip()
            break
    return s


def save_config(updates, config_path="config.yaml"):
    config = load_config(config_path)
    if "api_key" in updates:
        config["deepseek"]["api_key"] = updates["api_key"].strip()
    if "vault_path" in updates:
        config["obsidian"]["vault_path"] = _strip_quotes(updates["vault_path"])
    if "folder" in updates and updates["folder"]:
        config["obsidian"]["folder"] = updates["folder"].strip()
    if "chunk_minutes" in updates:
        config["processing"]["chunk_minutes"] = int(updates["chunk_minutes"])
    if "cookies_browser" in updates:
        config.setdefault("processing", {})["cookies_browser"] = updates["cookies_browser"].strip()
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False)


def extract_video_id(url):
    patterns = [
        r"(?:v=)([a-zA-Z0-9_-]{11})",
        r"(?:youtu\.be/)([a-zA-Z0-9_-]{11})",
        r"(?:embed/)([a-zA-Z0-9_-]{11})",
        r"(?:shorts/)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


# ── 抗错重试 ──────────────────────────────────────────────────────────────────

def _retry(fn, *, tries=4, base_delay=3.0, log=None, what=""):
    """对 YouTube 抓取做指数退避重试。SSLEOFError / 连接抖动重试常能成功。"""
    last = None
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:
            last = e
            msg = str(e)
            # 机器人检测 / 需要登录：重试也没用，直接抛出更友好的提示
            if "Sign in to confirm" in msg or "not a bot" in msg:
                raise RuntimeError(
                    "YouTube 要求验证（反爬）。请在设置里把「浏览器 Cookie」设为你已登录 YouTube 的浏览器"
                    "（如 chrome / safari / edge），再重试。"
                ) from e
            if attempt < tries:
                delay = base_delay * (2 ** (attempt - 1))
                if log:
                    log(f"   ⏳ {what}失败（{type(e).__name__}），{delay:.0f}s 后第 {attempt+1} 次重试…", "warn")
                time.sleep(delay)
            else:
                raise
    raise last


def _cookies_opt(config):
    """从 config 读取 cookies-from-browser 设置，返回 yt-dlp 选项片段。"""
    browser = (config.get("processing", {}) or {}).get("cookies_browser", "").strip()
    if browser and browser.lower() not in ("none", "无", "不使用"):
        return {"cookiesfrombrowser": (browser.lower(),)}
    return {}


def get_video_metadata(url, config=None, log=None):
    config = config or {}
    ydl_opts = {"quiet": True, "no_warnings": True, **_cookies_opt(config)}

    def _do():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title", "Unknown"),
                "channel": info.get("uploader", "Unknown"),
                "duration": info.get("duration", 0),
                "upload_date": info.get("upload_date", ""),
                "url": url,
                "thumbnail": info.get("thumbnail", ""),
                "video_id": info.get("id", ""),
            }

    return _retry(_do, log=log, what="获取视频信息")


def get_transcript(video_id, log=None):
    def _do():
        # v1.x: instantiate first, then call api.list()
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)

        # Prefer manual English, then auto English, then anything available
        for finder in [
            lambda tl: tl.find_manually_created_transcript(["en", "en-US", "en-GB"]),
            lambda tl: tl.find_generated_transcript(["en", "en-US", "en-GB"]),
            lambda tl: next(iter(tl)),
        ]:
            try:
                t = finder(transcript_list)
                return t.fetch(), t.language_code
            except Exception:
                continue
        raise Exception("No transcript available for this video")

    return _retry(_do, log=log, what="获取字幕")


def _entry_attr(entry, key, default=0):
    """Compat helper: supports both v0.x (dict) and v1.x (object) entries."""
    if hasattr(entry, key):
        return getattr(entry, key)
    try:
        return entry[key]
    except (KeyError, TypeError):
        return default


def chunk_transcript(transcript, chunk_seconds=600):
    if not transcript:
        return []

    chunks = []
    current = []
    chunk_start = _entry_attr(transcript[0], "start")
    boundary = chunk_start + chunk_seconds

    for entry in transcript:
        start = _entry_attr(entry, "start")
        if start >= boundary and current:
            text = " ".join(_entry_attr(e, "text", "").replace("\n", " ") for e in current)
            chunks.append({"start": chunk_start, "end": start, "text": text})
            current = [entry]
            chunk_start = start
            boundary = start + chunk_seconds
        else:
            current.append(entry)

    if current:
        last = current[-1]
        text = " ".join(_entry_attr(e, "text", "").replace("\n", " ") for e in current)
        chunks.append({
            "start": chunk_start,
            "end": _entry_attr(last, "start") + _entry_attr(last, "duration"),
            "text": text,
        })

    return chunks


def format_time(seconds):
    if not seconds:
        return "0:00"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe_filename(title):
    # 去掉文件系统非法字符，外加 URL 敏感字符（# % ?），避免文件名进 URL 时被截断/误解
    clean = re.sub(r'[<>:"/\\|?*#%\x00-\x1f]', "", title).strip(". ")
    return clean[:80] if clean else "untitled"


def _norm_facts(items):
    """把 research/examples 统一成 [{title, detail}]，兼容旧的纯字符串。"""
    out = []
    for x in items or []:
        if isinstance(x, dict):
            title = (x.get("title") or "").strip()
            detail = (x.get("detail") or "").strip()
            if title or detail:
                out.append({"title": title or detail, "detail": detail})
        elif isinstance(x, str) and x.strip():
            out.append({"title": x.strip(), "detail": ""})
    return out


# ── AI 分析 ──────────────────────────────────────────────────────────────────

def analyze_chunk(client, model, chunk, video_title, idx, total):
    start_str = format_time(chunk["start"])
    end_str = format_time(chunk["end"])
    text = chunk["text"][:5000]

    prompt = f"""请分析以下视频片段的文字稿（视频：《{video_title}》，时间段：{start_str}—{end_str}，第{idx+1}/{total}段）：

---
{text}
---

请以JSON格式返回以下字段：
- title_cn: 本段中文标题（10字以内）
- title_en: 本段英文标题（简短）
- summary_cn: 本段中文摘要（3-5句话，捕捉主要观点和论证逻辑）
- key_quotes: 最重要的原文引用（1-3条，保留英文原文，不翻译；若无则 []）
- research: 本段提到的研究、实验、调查或数据。每条是一个对象 {{"title": 一句话标题, "detail": 详细介绍}}。
  ⚠️ detail 必须只依据本段视频内容来写，把这项研究/数据说清楚：是什么、谁做的、发现了什么、用来支撑什么观点（2-4句中文）。
  绝对不要编造视频里没提到的数字、机构或结论；视频没讲清楚的部分就如实说"视频中未展开"。若本段无研究则 []。
- examples: 本段提到的例子、类比、故事或案例。每条是一个对象 {{"title": 一句话标题, "detail": 详细介绍}}。
  ⚠️ detail 同样只依据本段视频内容，把这个例子/故事讲清楚：讲了什么、说明了什么道理（2-4句中文）。不要编造。若无则 []。
- concepts: 出现的专业概念或术语（格式："中文（English）"；若无则 []）

只返回JSON，不要其他文字。"""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你是专业的内容分析师，擅长分析播客、演讲和学术对谈。你严格忠实于原文，绝不编造原文没有的信息。请严格按JSON格式输出，不要有任何前缀或说明文字。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
        max_tokens=2800,
    )

    try:
        data = json.loads(response.choices[0].message.content)
    except json.JSONDecodeError:
        return {
            "title_cn": f"第{idx+1}段", "title_en": f"Section {idx+1}",
            "summary_cn": response.choices[0].message.content[:300],
            "key_quotes": [], "research": [], "examples": [], "concepts": [],
        }

    data["research"] = _norm_facts(data.get("research"))
    data["examples"] = _norm_facts(data.get("examples"))
    return data


def generate_overall_summary(client, model, video_title, sections):
    lines = []
    for i, s in enumerate(sections):
        a = s["analysis"]
        lines.append(
            f"第{i+1}段（{format_time(s['chunk']['start'])}—{format_time(s['chunk']['end'])}）：{a.get('title_cn','')}\n{a.get('summary_cn','')}"
        )

    cat_list = "、".join(CATEGORIES)
    prompt = f"""基于视频《{video_title}》的分段摘要，生成整体分析：

{chr(10).join(lines)}

请以JSON格式返回：
- overall_summary: 整体内容概述（2-3段，中文，每段约100字）
- main_themes: 主要话题或主题（5-8条，每条一句话，中文）
- key_takeaways: 最重要的观点/启示/结论（5-10条，每条一句话，中文）
- category: 从以下固定类别里选**最贴切的一个**（只能选一个，原样返回）：{cat_list}
- tags: 3-6个内容标签（中文，简短，便于检索归类，如"原生家庭""依恋理论""慢性压力"）
- all_concepts: 汇总全部关键概念（去重，中英双语；若无则 []）

只返回JSON，不要其他文字。"""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你是专业的内容分析师。请严格按JSON格式输出。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        response_format={"type": "json_object"},
        max_tokens=2000,
    )

    try:
        data = json.loads(response.choices[0].message.content)
    except Exception:
        data = {}

    cat = (data.get("category") or "").strip()
    if cat not in CATEGORIES:
        cat = "其他"
    return {
        "overall_summary": data.get("overall_summary", ""),
        "main_themes": data.get("main_themes", []),
        "key_takeaways": data.get("key_takeaways", []),
        "category": cat,
        "tags": [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()],
        "all_concepts": data.get("all_concepts", []),
    }


# ── Markdown output ──────────────────────────────────────────────────────────

def generate_markdown(metadata, sections, overall):
    title = metadata["title"]
    date = datetime.now().strftime("%Y-%m-%d")
    dur = format_time(metadata["duration"])
    category = overall.get("category", "其他")
    tags = overall.get("tags", [])
    tag_line = ", ".join(["youtube", "video-notes", category] + tags)

    parts = [
        f'---',
        f'title: "{title.replace(chr(34), chr(39))}"',
        f'channel: "{metadata["channel"]}"',
        f'url: "{metadata["url"]}"',
        f'date: {date}',
        f'duration: "{dur}"',
        f'category: "{category}"',
        f'tags: [{tag_line}]',
        f'---',
        f'',
        f'# {title}',
        f'',
        f'> **频道**：{metadata["channel"]}  ',
        f'> **链接**：{metadata["url"]}  ',
        f'> **时长**：{dur}　**类别**：{category}',
        f'',
        f'## 整体概述',
        f'',
        overall.get("overall_summary", ""),
        f'',
        f'## 主要话题',
        f'',
    ]
    for t in overall.get("main_themes", []):
        parts.append(f"- {t}")
    parts += ["", "## 核心要点", ""]
    for t in overall.get("key_takeaways", []):
        parts.append(f"- {t}")

    if overall.get("all_concepts"):
        parts += ["", "## 关键概念", ""]
        for c in overall["all_concepts"]:
            parts.append(f"- {c}")

    parts += ["", "---", "", f"## 分段详解（共{len(sections)}段）", ""]

    for i, s in enumerate(sections):
        chunk, a = s["chunk"], s["analysis"]
        start_str = format_time(chunk["start"])
        end_str = format_time(chunk["end"])
        parts += [
            f'### {i+1}. {a.get("title_cn", "")}　`{start_str} — {end_str}`',
            f'',
            a.get("summary_cn", ""),
            f'',
        ]
        if a.get("research"):
            parts += ["**🔬 研究 / 数据**", ""]
            for r in _norm_facts(a["research"]):
                parts.append(f"- **{r['title']}**" + (f"：{r['detail']}" if r['detail'] else ""))
            parts.append("")
        if a.get("examples"):
            parts += ["**💡 例子 / 故事**", ""]
            for e in _norm_facts(a["examples"]):
                parts.append(f"- **{e['title']}**" + (f"：{e['detail']}" if e['detail'] else ""))
            parts.append("")
        if a.get("concepts"):
            parts += ["**🏷 关键概念**", ""]
            for c in a["concepts"]:
                parts.append(f"- {c}")
            parts.append("")
        if a.get("key_quotes"):
            parts += ["**English**", ""]
            for q in a["key_quotes"]:
                parts.append(f"> {q}")
            parts.append("")

    return "\n".join(parts)


# ── HTML output ───────────────────────────────────────────────────────────────

def generate_html(metadata, sections, overall):
    title = html.escape(metadata["title"])
    channel = html.escape(metadata["channel"])
    date = datetime.now().strftime("%Y-%m-%d")
    dur = format_time(metadata["duration"])
    video_url = html.escape(metadata["url"])
    video_id = html.escape(metadata.get("video_id", "") or extract_video_id(metadata["url"]) or "")
    category = html.escape(overall.get("category", "其他"))
    tags = overall.get("tags", [])

    # TOC
    toc_items = []
    for i, s in enumerate(sections):
        ts = format_time(s["chunk"]["start"])
        t_cn = html.escape(s["analysis"].get("title_cn", f"第{i+1}段"))
        toc_items.append(f'<li><a href="#sec-{i+1}"><span class="toc-ts">{ts}</span>{t_cn}</a></li>')
    toc_html = "\n".join(toc_items)

    def facts_block(items, css, label):
        items = _norm_facts(items)
        if not items:
            return ""
        rows = []
        for it in items:
            t = html.escape(it["title"])
            d = html.escape(it["detail"])
            kw = html.escape((it["title"] + " " + it["detail"])[:120])
            detail_html = f'<div class="fact-d">{d}</div>' if d else ""
            rows.append(
                f'<div class="fact-item {css}">'
                f'<div class="fact-t">{t}'
                f'<button class="dig-btn" onclick="openDig(this)" data-kw="{kw}" data-title="{t}">🔍 深挖</button>'
                f'</div>{detail_html}'
                f'<div class="dig-panel"></div></div>'
            )
        icon = "🔬 研究 / 数据" if css == "research" else "💡 例子 / 故事"
        return f'<div class="facts-lbl {css}">{icon}</div>{"".join(rows)}'

    # Section cards (style 2: detail inline)
    cards = []
    for i, s in enumerate(sections):
        chunk, a = s["chunk"], s["analysis"]
        start_str = format_time(chunk["start"])
        end_str = format_time(chunk["end"])
        ts_sec = int(chunk["start"])

        t_cn = html.escape(a.get("title_cn", ""))
        t_en = html.escape(a.get("title_en", ""))
        summary = html.escape(a.get("summary_cn", ""))

        quotes_html = "".join(
            f'<div class="quote">{html.escape(q)}</div>' for q in a.get("key_quotes", [])
        )
        concept_tags = "".join(
            f'<span class="ctag">{html.escape(c)}</span>' for c in a.get("concepts", [])
        )
        concepts_aside = (
            f'<div class="aside-lbl" style="margin-top:18px">概念</div><div class="ctags">{concept_tags}</div>'
            if concept_tags else ""
        )

        sep = "&" if "?" in metadata["url"] else "?"
        yt_link = html.escape(f'{metadata["url"]}{sep}t={ts_sec}s')

        aside = ""
        if t_en or quotes_html or concept_tags:
            aside = f"""<aside class="sec-aside">
      <div class="aside-lbl">English · 点心</div>
      <div class="aside-en-title">{t_en}</div>
      {quotes_html}
      {concepts_aside}
    </aside>"""

        cards.append(f"""
<section id="sec-{i+1}" class="sec-card">
  <div class="sec-main">
    <div class="sec-head">
      <span class="sec-num">{i+1:02d}</span>
      <div class="sec-titles"><h2>{t_cn}</h2></div>
      <a class="ts-link" href="{yt_link}" target="_blank">{start_str} → {end_str}</a>
    </div>
    <p class="sec-summary">{summary}</p>
    {facts_block(a.get("research"), "research", "研究")}
    {facts_block(a.get("examples"), "examples", "例子")}
    <div class="ask-box">
      <button class="ask-toggle" onclick="toggleAsk(this)">💬 追问这一段（基于原片字幕，由 DeepSeek 回答）</button>
      <div class="ask-body">
        <textarea class="ask-input" placeholder="例：这段里那个研究，原话是怎么说的？再详细讲讲。"></textarea>
        <button class="ask-send" onclick="sendAsk(this, {i})">发送</button>
        <div class="ask-answer"></div>
      </div>
    </div>
  </div>
  {aside}
</section>""")

    sections_html = "\n".join(cards)

    overall_text = html.escape(overall.get("overall_summary", "")).replace("\n", "<br>")
    themes_html = "".join(f"<li>{html.escape(t)}</li>" for t in overall.get("main_themes", []))
    takeway_html = "".join(f"<li>{html.escape(t)}</li>" for t in overall.get("key_takeaways", []))
    all_concepts = "".join(f'<span class="ctag">{html.escape(c)}</span>' for c in overall.get("all_concepts", []))
    concepts_card = f'<div class="ov-card full"><div class="ov-card-title">关键概念</div><div class="ctags">{all_concepts}</div></div>' if all_concepts else ""
    tags_html = "".join(f'<span class="tag-chip">{html.escape(t)}</span>' for t in tags)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --paper:#faf8f4;--card:#fffdf9;
  --ink:#2a2521;--ink-soft:#4a443c;--muted:#8a8174;
  --line:#e7e0d4;--line-soft:#efeae0;
  --navy:#1e3a5f;--navy-deep:#16304f;
  --sky:#cfe0ef;
  --amber:#9a7b3f;--amber-bg:#f6f0e3;--amber-bd:#e6d9bd;
  --olive:#5f6b43;--olive-bg:#eef0e6;--olive-bd:#d7ddc6;
}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,"PingFang SC",sans-serif;background:var(--paper);color:var(--ink);line-height:1.8}}
a{{color:var(--navy);text-decoration:none}}
a:hover{{text-decoration:underline}}
.layout{{display:flex;min-height:100vh}}
.sidebar{{width:248px;min-width:248px;background:var(--navy-deep);color:#fff;
  position:sticky;top:0;height:100vh;overflow-y:auto;display:flex;flex-direction:column;
  scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.2) transparent}}
.sb-head{{padding:22px 18px 14px;border-bottom:1px solid rgba(255,255,255,.1)}}
.sb-head h1{{font-size:13.5px;font-weight:600;line-height:1.5;color:#fff}}
.sb-meta{{font-size:11px;color:rgba(255,255,255,.5);margin-top:6px}}
.toc-lbl{{font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:rgba(255,255,255,.35);padding:14px 18px 6px}}
.toc ol{{list-style:none;padding:0 0 20px}}
.toc ol li a{{display:block;padding:6px 18px;color:rgba(255,255,255,.65);font-size:12.5px;transition:.15s}}
.toc ol li a:hover,.toc ol li a.active{{background:rgba(255,255,255,.1);color:#fff;text-decoration:none}}
.toc-ts{{font-family:ui-monospace,monospace;font-size:10px;color:var(--sky);margin-right:5px}}
.main{{flex:1;padding:42px 52px;max-width:1080px}}
.hero{{margin-bottom:34px;padding-bottom:26px;border-bottom:2px solid var(--line)}}
.hero h1{{font-size:25px;font-weight:700;color:var(--navy);line-height:1.35;margin-bottom:12px}}
.hero-meta{{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;color:var(--muted);align-items:center}}
.cat-chip{{background:var(--navy);color:#fff;font-size:12px;padding:3px 11px;border-radius:20px}}
.tag-chip{{background:var(--amber-bg);color:var(--amber);font-size:12px;padding:2px 10px;border-radius:20px;border:1px solid var(--amber-bd)}}
.ov-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:36px}}
.ov-card{{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:18px 22px}}
.ov-card.full{{grid-column:span 2}}
.ov-card-title{{font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--navy);margin-bottom:10px}}
.ov-card p{{font-size:14.5px;line-height:1.85}}
.ov-card ul{{list-style:none;padding:0}}
.ov-card ul li{{font-size:13.5px;padding:4px 0 4px 15px;position:relative}}
.ov-card ul li::before{{content:"→";position:absolute;left:0;color:var(--navy);font-size:11px}}
.ctags{{display:flex;flex-wrap:wrap;gap:6px}}
.ctag{{background:#eef2f7;color:var(--navy);font-size:12px;padding:3px 10px;border-radius:20px;border:1px solid var(--line)}}
.sec-divider{{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:20px;padding-bottom:10px;border-bottom:1px solid var(--line)}}
.sec-card{{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--navy);
  border-radius:12px;padding:26px 30px;margin-bottom:20px;
  display:grid;grid-template-columns:1fr 232px;gap:30px}}
.sec-main{{min-width:0}}
.sec-head{{display:flex;align-items:flex-start;gap:14px;margin-bottom:14px}}
.sec-num{{font-family:ui-monospace,"SF Mono",monospace;font-size:19px;font-weight:700;color:var(--navy);min-width:26px;padding-top:3px}}
.sec-titles{{flex:1}}
.sec-titles h2{{font-size:18px;font-weight:700;color:var(--navy);line-height:1.4}}
.ts-link{{font-family:ui-monospace,"SF Mono",monospace;font-size:11.5px;color:var(--navy);white-space:nowrap;padding:4px 10px;background:#eef2f7;border-radius:6px;margin-left:auto}}
.ts-link:hover{{background:var(--navy);color:#fff;text-decoration:none}}
.sec-summary{{font-size:15px;line-height:1.85;color:var(--ink);margin-bottom:6px}}
.facts-lbl{{font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;margin:18px 0 8px}}
.facts-lbl.research{{color:var(--amber)}}
.facts-lbl.examples{{color:var(--olive)}}
.fact-item{{margin-bottom:12px;padding-left:16px;position:relative}}
.fact-item::before{{content:"";position:absolute;left:0;top:9px;width:7px;height:7px;border-radius:50%}}
.fact-item.research::before{{background:var(--amber)}}
.fact-item.examples::before{{background:var(--olive)}}
.fact-t{{font-size:14px;font-weight:600;color:var(--ink);display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}}
.fact-d{{font-size:13.5px;line-height:1.8;color:var(--ink-soft);margin-top:3px}}
.dig-btn{{font-size:11px;font-weight:500;color:var(--navy);background:none;border:none;cursor:pointer;padding:0;opacity:.7}}
.dig-btn:hover{{opacity:1;text-decoration:underline}}
.dig-panel{{display:none;margin-top:8px;padding:12px 14px;background:#f3f6fa;border:1px solid var(--line);border-radius:8px;font-size:13px}}
.dig-panel.open{{display:block}}
.dig-panel h4{{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:0 0 6px}}
.dig-links{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}}
.dig-links a{{font-size:12.5px;background:#fff;border:1px solid var(--line);padding:4px 10px;border-radius:6px}}
.dig-prompt{{position:relative}}
.dig-prompt textarea{{width:100%;min-height:78px;font-family:inherit;font-size:12.5px;line-height:1.6;
  border:1px solid var(--line);border-radius:6px;padding:8px 10px;background:#fff;color:var(--ink-soft);resize:vertical}}
.copy-btn{{margin-top:6px;font-size:12px;background:var(--navy);color:#fff;border:none;padding:5px 12px;border-radius:6px;cursor:pointer}}
.sec-aside{{border-left:1px solid var(--line-soft);padding-left:22px}}
.aside-lbl{{font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:10px}}
.aside-en-title{{font-size:13px;font-style:italic;color:var(--ink-soft);margin-bottom:14px;line-height:1.5}}
.quote{{font-size:12.5px;line-height:1.7;color:var(--ink-soft);padding:8px 0 8px 12px;border-left:2px solid var(--sky);margin-bottom:10px;font-style:italic}}
.ask-box{{margin-top:18px;border-top:1px dashed var(--line);padding-top:12px}}
.ask-toggle{{font-size:12.5px;color:var(--navy);background:none;border:none;cursor:pointer;padding:0;font-weight:500}}
.ask-toggle:hover{{text-decoration:underline}}
.ask-body{{display:none;margin-top:10px}}
.ask-body.open{{display:block}}
.ask-input{{width:100%;min-height:64px;font-family:inherit;font-size:13px;line-height:1.6;border:1px solid var(--line);border-radius:8px;padding:9px 11px;background:#fff;resize:vertical}}
.ask-send{{margin-top:7px;font-size:13px;background:var(--navy);color:#fff;border:none;padding:7px 16px;border-radius:7px;cursor:pointer}}
.ask-send:disabled{{opacity:.5;cursor:wait}}
.ask-answer{{margin-top:12px;font-size:13.5px;line-height:1.85;color:var(--ink);white-space:pre-wrap}}
.ask-answer.err{{color:#b4452e}}
@media(max-width:820px){{.sidebar{{display:none}}.sec-card{{grid-template-columns:1fr}}.ov-grid{{grid-template-columns:1fr}}.ov-card.full{{grid-column:span 1}}
  .sec-aside{{border-left:none;border-top:1px solid var(--line-soft);padding-left:0;padding-top:18px}}}}
</style>
</head>
<body data-vid="{video_id}">
<div class="layout">
  <nav class="sidebar">
    <div class="sb-head">
      <h1>{title}</h1>
      <div class="sb-meta">{channel} · {dur} · {date}</div>
    </div>
    <div class="toc-lbl">目录</div>
    <nav class="toc"><ol>{toc_html}</ol></nav>
  </nav>
  <main class="main">
    <div class="hero">
      <h1>{title}</h1>
      <div class="hero-meta">
        <span class="cat-chip">{category}</span>
        {tags_html}
        <span>📺 {channel}</span>
        <span>⏱ {dur}</span>
        <span>📅 {date}</span>
        <span><a href="{video_url}" target="_blank">▶ 观看原视频</a></span>
      </div>
    </div>
    <div class="ov-grid">
      <div class="ov-card full">
        <div class="ov-card-title">整体概述</div>
        <p>{overall_text}</p>
      </div>
      <div class="ov-card">
        <div class="ov-card-title">主要话题</div>
        <ul>{themes_html}</ul>
      </div>
      <div class="ov-card">
        <div class="ov-card-title">核心要点</div>
        <ul>{takeway_html}</ul>
      </div>
      {concepts_card}
    </div>
    <div class="sec-divider">分段详解 · {len(sections)} 个片段</div>
    {sections_html}
  </main>
</div>
<script>
// 目录高亮
const secs=document.querySelectorAll('.sec-card');
const links=document.querySelectorAll('.toc a');
const obs=new IntersectionObserver(es=>{{es.forEach(e=>{{if(e.isIntersecting){{
  links.forEach(l=>l.classList.remove('active'));
  const a=document.querySelector('.toc a[href="#'+e.target.id+'"]');if(a)a.classList.add('active');}}}});}},{{threshold:0.3}});
secs.forEach(s=>obs.observe(s));

// 外部延伸："深挖"——给关键词/链接/可复制提示词，不让本应用回答
function openDig(btn){{
  const panel=btn.closest('.fact-item').querySelector('.dig-panel');
  if(panel.classList.contains('open')){{panel.classList.remove('open');return;}}
  const kw=btn.dataset.kw, t=btn.dataset.title;
  const vtitle=document.title;
  const q=encodeURIComponent(t);
  const prompt=`我在看一个 YouTube 视频《${{vtitle}}》，里面提到了「${{t}}」。\\n请你帮我详细、准确地介绍这件事的真实背景：它的来源/出处、具体内容、关键数据或结论，以及学界对它的评价或争议。如果这是一个广为流传但被误读的说法，请指出。请基于你已知的可靠知识回答，并说明确定性高低。`;
  panel.innerHTML=`
    <h4>🔎 去更可靠的地方查证</h4>
    <div class="dig-links">
      <a href="https://www.google.com/search?q=${{q}}" target="_blank">Google</a>
      <a href="https://scholar.google.com/scholar?q=${{q}}" target="_blank">Google 学术</a>
      <a href="https://en.wikipedia.org/w/index.php?search=${{q}}" target="_blank">维基百科</a>
    </div>
    <h4>📋 复制这段提示词，去问更强的 AI（Claude / GPT 等）</h4>
    <div class="dig-prompt">
      <textarea readonly>${{prompt}}</textarea>
      <button class="copy-btn" onclick="copyPrompt(this)">复制提示词</button>
    </div>`;
  panel.classList.add('open');
}}
function copyPrompt(btn){{
  const ta=btn.parentElement.querySelector('textarea');
  navigator.clipboard.writeText(ta.value).then(()=>{{btn.textContent='已复制 ✓';setTimeout(()=>btn.textContent='复制提示词',1800);}});
}}

// 视频内追问——基于该段字幕，由 DeepSeek 回答（需在应用内打开报告）
function toggleAsk(btn){{btn.nextElementSibling.classList.toggle('open');}}
async function sendAsk(btn, secIdx){{
  const body=btn.parentElement;
  const q=body.querySelector('.ask-input').value.trim();
  const ans=body.querySelector('.ask-answer');
  if(!q){{return;}}
  const vid=document.body.dataset.vid;
  btn.disabled=true;ans.className='ask-answer';ans.textContent='思考中…';
  try{{
    const res=await fetch('/ask',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{video_id:vid,section:secIdx,question:q}})}});
    const data=await res.json();
    if(data.ok){{ans.textContent=data.answer;}}
    else{{ans.className='ask-answer err';ans.textContent='出错了：'+(data.error||'未知错误')+'（提示：报告需从应用里的「HTML 报告」按钮打开，本地服务在运行时才能追问）';}}
  }}catch(e){{ans.className='ask-answer err';ans.textContent='请求失败：请确认是从应用里打开本报告（本地服务需在运行）。';}}
  btn.disabled=false;
}}
</script>
</body>
</html>"""


# ── File I/O ──────────────────────────────────────────────────────────────────

def save_to_obsidian(markdown_content, metadata, overall, config):
    raw_path = _strip_quotes(str(config["obsidian"]["vault_path"]))
    vault = Path(os.path.expanduser(raw_path))
    folder = config["obsidian"].get("folder", "YouTube笔记")
    category = overall.get("category", "其他")
    target = vault / folder / safe_filename(category)
    target.mkdir(parents=True, exist_ok=True)
    filepath = target / (safe_filename(metadata["title"]) + ".md")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(markdown_content)
    return str(filepath)


def save_html_file(html_content, metadata, config):
    output_dir = Path(config["output"].get("html_dir", "./output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = datetime.now().strftime("%Y%m%d")
    filename = f"{prefix}_{safe_filename(metadata['title'])}.html"
    filepath = output_dir / filename
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)
    return str(filepath), filename


def save_transcript_store(video_id, sections, metadata, config):
    """存一份分段字幕，供报告里的「追问这一段」回查原文。"""
    output_dir = Path(config["output"].get("html_dir", "./output"))
    tdir = output_dir / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "video_id": video_id,
        "title": metadata["title"],
        "url": metadata["url"],
        "sections": [
            {
                "idx": i,
                "start": s["chunk"]["start"],
                "end": s["chunk"]["end"],
                "title_cn": s["analysis"].get("title_cn", ""),
                "text": s["chunk"]["text"],
            }
            for i, s in enumerate(sections)
        ],
    }
    (tdir / f"{video_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── Main pipeline ─────────────────────────────────────────────────────────────

def process_video(url, config, progress_cb=None, chunk_minutes=None):
    def log(msg, level="info"):
        if progress_cb:
            progress_cb({"type": level, "message": msg})

    log("获取视频信息...")
    video_id = extract_video_id(url)
    if not video_id:
        raise ValueError(f"无法解析视频 ID：{url}")

    metadata = get_video_metadata(url, config, log)
    if not metadata.get("video_id"):
        metadata["video_id"] = video_id
    video_id = metadata["video_id"]
    log(f'✅ 视频：{metadata["title"]}')
    log(f'   频道：{metadata["channel"]}，时长：{format_time(metadata["duration"])}')

    log("获取字幕...")
    transcript, lang = get_transcript(video_id, log)
    log(f"✅ 字幕获取成功（{lang}），共 {len(transcript)} 条")

    if chunk_minutes is None:
        chunk_minutes = config["processing"].get("chunk_minutes", 10)
    chunk_minutes = int(chunk_minutes)
    chunks = chunk_transcript(transcript, chunk_seconds=chunk_minutes * 60)
    log(f"✅ 切分完成，共 {len(chunks)} 段（每段约 {chunk_minutes} 分钟）")

    client = OpenAI(
        api_key=config["deepseek"]["api_key"],
        base_url=config["deepseek"]["base_url"],
    )
    model = config["deepseek"]["model"]

    sections = []
    for i, chunk in enumerate(chunks):
        log(f"分析第 {i+1}/{len(chunks)} 段（{format_time(chunk['start'])} — {format_time(chunk['end'])}）...")
        analysis = analyze_chunk(client, model, chunk, metadata["title"], i, len(chunks))
        sections.append({"chunk": chunk, "analysis": analysis})

    log("生成整体总结...")
    overall = generate_overall_summary(client, model, metadata["title"], sections)
    log(f"✅ 整体总结完成（类别：{overall.get('category','其他')}）")

    log("生成输出文件...")
    md_content = generate_markdown(metadata, sections, overall)
    html_content = generate_html(metadata, sections, overall)

    md_path = None
    try:
        md_path = save_to_obsidian(md_content, metadata, overall, config)
        log(f"✅ Obsidian 笔记：{md_path}")
    except Exception as e:
        log(f"⚠️  Obsidian 保存失败：{e}", "warn")

    html_path, html_filename = save_html_file(html_content, metadata, config)
    log(f"✅ HTML 报告：{html_path}")

    try:
        save_transcript_store(video_id, sections, metadata, config)
    except Exception as e:
        log(f"⚠️  字幕存档失败（不影响笔记，仅影响追问功能）：{e}", "warn")

    result = {
        "title": metadata["title"],
        "channel": metadata["channel"],
        "duration": format_time(metadata["duration"]),
        "sections": len(sections),
        "md_path": md_path,
        "html_path": html_path,
        "html_filename": html_filename,
        "url": url,
        "video_id": video_id,
        "category": overall.get("category", "其他"),
        "tags": overall.get("tags", []),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "summary": overall.get("overall_summary", "")[:200],
    }

    _append_library_index(result, config)
    return result


def _append_library_index(result, config):
    """Maintain a persistent index of all processed videos."""
    output_dir = Path(config["output"].get("html_dir", "./output"))
    index_path = output_dir / "index.json"
    try:
        entries = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    except Exception:
        entries = []

    entries = [e for e in entries if e.get("url") != result["url"]]
    entries.insert(0, {
        "title": result["title"],
        "channel": result["channel"],
        "duration": result["duration"],
        "sections": result["sections"],
        "html_filename": result["html_filename"],
        "url": result["url"],
        "video_id": result.get("video_id", ""),
        "category": result.get("category", "其他"),
        "tags": result.get("tags", []),
        "date": result["date"],
        "summary": result["summary"],
    })

    index_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
