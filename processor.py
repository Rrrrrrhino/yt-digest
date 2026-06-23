import json
import re
import html
import os
import time
import urllib.request
from pathlib import Path
from datetime import datetime
import yaml
from openai import OpenAI
import yt_dlp
from concurrent.futures import ThreadPoolExecutor, as_completed


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
    """返回 yt-dlp 的 cookie 选项片段。

    优先用项目目录下导出的 cookies.txt（最稳——不受 Chrome 是否在运行/cookie 库被锁/
    新版 macOS 应用绑定加密影响）；没有该文件时，回落到实时读取浏览器 cookie。
    """
    cookie_file = os.path.join(os.path.dirname(__file__), "cookies.txt")
    if os.path.exists(cookie_file):
        return {"cookiefile": cookie_file}
    browser = (config.get("processing", {}) or {}).get("cookies_browser", "").strip()
    if browser and browser.lower() not in ("none", "无", "不使用"):
        return {"cookiesfrombrowser": (browser.lower(),)}
    return {}


# yt-dlp 提取选项：我们只要「元数据 + 字幕」，从不下载视频本体。
# skip_download + ignore_no_formats_error 让 yt-dlp 走轻量提取路径、不去解析可下载格式——
# 这一步正是会撞上 YouTube 新 player/PO-token 反爬的地方（否则报
# "Requested format is not available" 或 "Sign in to confirm you're not a bot"）。
#
# player_client=['tv','web']：2026 起 YouTube 对 web/ios/android client 的字幕轨上了 PO-token
# 限制——这些 client 常返回「时长=0、automatic_captions 全空」的残缺信息（即"明明有自动字幕却报无字幕"）。
# 实测「tv」(客厅端) client 不受此限，能完整返回时长 + 全部自动字幕（含英文）；web 留作兜底。
_SAFE_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "ignore_no_formats_error": True,
    "extractor_args": {"youtube": {"player_client": ["tv", "web"]}},
}

_EN_LANGS = ("en", "en-US", "en-GB", "en-orig")
_CAPTION_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def extract_video_info(url, config=None, log=None):
    """一次 extract_info 同时拿到元数据和字幕轨，是 metadata / transcript 的共同数据源。"""
    config = config or {}
    ydl_opts = {**_SAFE_YDL_OPTS, **_cookies_opt(config)}

    def _do():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    return _retry(_do, log=log, what="获取视频信息")


def metadata_from_info(info, url):
    return {
        "title": info.get("title", "Unknown"),
        "channel": info.get("uploader", "Unknown"),
        "duration": info.get("duration", 0) or 0,
        "upload_date": info.get("upload_date", ""),
        "url": url,
        "thumbnail": info.get("thumbnail", ""),
        "video_id": info.get("id", ""),
    }


def get_video_metadata(url, config=None, log=None):
    """保留旧入口：单独取元数据时仍可用。"""
    return metadata_from_info(extract_video_info(url, config, log), url)


def _fetch_caption_text(track_url):
    req = urllib.request.Request(track_url, headers={"User-Agent": _CAPTION_UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "ignore")


def _parse_json3(raw):
    """YouTube json3 字幕 → [{text, start, duration}]，过滤掉无文本的滚动/空事件。"""
    data = json.loads(raw)
    out = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if not text:
            continue
        out.append({
            "text": text,
            "start": ev.get("tStartMs", 0) / 1000.0,
            "duration": ev.get("dDurationMs", 0) / 1000.0,
        })
    return out


_VTT_TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})")


def _parse_vtt(raw):
    """vtt/srv 兜底解析（极少用到——YouTube 基本都提供 json3）。"""
    def _to_s(t):
        h, m, s, ms = (int(x) for x in t)
        return h * 3600 + m * 60 + s + ms / 1000.0

    out = []
    for block in re.split(r"\n\s*\n", raw):
        if "-->" not in block:
            continue
        times = _VTT_TS.findall(block)
        if len(times) < 2:
            continue
        start, end = _to_s(times[0]), _to_s(times[1])
        text_lines = [l for l in block.splitlines()
                      if "-->" not in l and not l.strip().isdigit() and l.strip()]
        text = re.sub(r"<[^>]+>", "", " ".join(text_lines)).strip()
        if text:
            out.append({"text": text, "start": start, "duration": max(0.0, end - start)})
    return out


def get_transcript_from_info(info, config=None, log=None):
    """从 extract_info 的结果里取字幕。优先 手动英文 > 自动英文 > 任意可用。"""
    subs = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}

    def _pick(src, langs):
        for lang in langs:
            if lang in src:
                return lang, src[lang]
        return None, None

    lang, track = _pick(subs, _EN_LANGS)
    if not track:
        lang, track = _pick(autos, _EN_LANGS)
    if not track and subs:
        lang, track = next(iter(subs.items()))
    if not track and autos:
        lang, track = next(iter(autos.items()))

    if not track:
        if not _cookies_opt(config or {}):
            raise RuntimeError(
                "没取到字幕。多半是因为没设置浏览器 Cookie——YouTube 现在要求登录态才返回字幕。"
                "请在设置里把「浏览器 Cookie」设为你已登录 YouTube 的浏览器（chrome/safari/edge）后重试。"
            )
        raise RuntimeError("该视频没有可用字幕（既无人工字幕，也无自动字幕）。")

    fmt = (next((f for f in track if f.get("ext") == "json3"), None)
           or next((f for f in track if f.get("ext") in ("srv3", "srv1", "vtt")), None)
           or track[0])

    raw = _retry(lambda: _fetch_caption_text(fmt["url"]), log=log, what="获取字幕")
    entries = _parse_json3(raw) if (fmt.get("ext") == "json3" or raw.lstrip().startswith("{")) else _parse_vtt(raw)
    if not entries:
        raise RuntimeError("字幕内容为空或解析失败。")
    return entries, lang


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
    """把 research/examples 统一成 [{title, detail, dig}]，兼容旧的纯字符串。"""
    out = []
    for x in items or []:
        if isinstance(x, dict):
            title = (x.get("title") or "").strip()
            detail = (x.get("detail") or "").strip()
            dig = (x.get("dig") or "").strip()
            if title or detail:
                out.append({"title": title or detail, "detail": detail, "dig": dig})
        elif isinstance(x, str) and x.strip():
            out.append({"title": x.strip(), "detail": "", "dig": ""})
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
- summary_cn: 本段中文内容简介（6-10 句话、写成一两段连贯的中文，尽量详尽地把这一段视频讲了什么完整呈现出来：主要观点、论证逻辑、是怎么一步步展开的、提到的关键人物/例子/结论。目标是让没看视频的人光读这段简介也能比较充分地了解本段内容。只依据本段视频，不编造视频没提到的东西）
- key_quotes: 最重要的原文引用（1-3条，保留英文原文，不翻译；若无则 []）
- research: 本段提到的研究、实验、调查或数据。每条是一个对象 {{"title": 一句话标题, "detail": 详细介绍, "dig": 原文整理还原}}。
  ⚠️ detail：只依据本段视频内容，把这项研究/数据说清楚——是什么、谁做的（若有提到）、关键发现或数据、用来支撑什么观点、得出什么结论（3-6句中文，尽量详尽具体，但不堆砌）。
  ⚠️ dig：一段更长的「原文整理还原」（5-10句中文）——把原片在这一段里到底是怎么聊到这个点的，用流畅自然的中文复述讲解出来：它是怎么被引出的、主讲人如何展开、举了什么、强调了什么、最后落到什么结论。不要逐字照搬对话（原文可能是问答/口语，照搬会很生硬），而是把原文的意思、层次、语气尽量精彩详尽地传达出来，读起来像一段优秀的讲解。
  以上全部只依据本段视频内容，绝不编造视频没提到的数字、机构或结论；视频没讲清楚的部分就如实说"视频中未展开"。若本段无研究则 []。
- examples: 本段提到的例子、类比、故事或案例。每条是一个对象 {{"title": 一句话标题, "detail": 详细介绍, "dig": 原文整理还原}}。
  要求同上：detail 用 3-6 句把例子/故事讲清楚（讲了什么、说明了什么道理）；dig 用 5-10 句流畅还原原片是怎么讲这个例子/故事的。只依据本段视频内容，不要编造。若无则 []。
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
        max_tokens=8000,  # v4-pro 是推理模型，reasoning 约占 2000-2500 token，须给正文留足余量否则 JSON 会被截断
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


# 分段分析并行路数。各段相互独立，可并发跑以缩短长视频的总时长；
# 过高可能触发 DeepSeek 限流（尤其 v4-pro 这类推理模型并发额度有限），3-4 较稳。
_ANALYZE_CONCURRENCY = 4


def _analyze_chunk_safe(client, model, chunk, video_title, idx, total, log=None):
    """analyze_chunk + 重试，且**绝不抛异常**——单段持续失败只返回占位，不拖垮整篇视频。
    返回 (analysis_dict, ok_bool)。"""
    try:
        a = _retry(
            lambda: analyze_chunk(client, model, chunk, video_title, idx, total),
            tries=3, base_delay=4, log=log, what=f"分析第{idx + 1}段",
        )
        return a, True
    except Exception as e:
        return {
            "title_cn": f"第{idx + 1}段", "title_en": f"Section {idx + 1}",
            "summary_cn": f"（本段分析失败，已跳过：{e}）",
            "key_quotes": [], "research": [], "examples": [], "concepts": [],
        }, False


def _analyze_all_chunks(client, model, chunks, video_title, log=None):
    """并行分析所有分段（结果按原顺序回填）。单段失败只占位、不影响其余段。"""
    total = len(chunks)
    sections = [None] * total
    workers = max(1, min(_ANALYZE_CONCURRENCY, total))
    if log:
        log(f"开始分析 {total} 段（并行 {workers} 路）...")
    done = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(_analyze_chunk_safe, client, model, chunks[i], video_title, i, total, log): i
            for i in range(total)
        }
        for fut in as_completed(futs):
            i = futs[fut]
            analysis, ok = fut.result()
            sections[i] = {"chunk": chunks[i], "analysis": analysis}
            done += 1
            if not ok:
                failed += 1
            if log:
                ck = chunks[i]
                # 完成计数做进度（前端按「分析第 N/总 段」推进度条）；并行下按完成数递增、保证单调
                log(f"分析第 {done}/{total} 段完成（{format_time(ck['start'])}—{format_time(ck['end'])}）...")
    if failed and log:
        log(f"⚠️  有 {failed} 段分析失败、已用占位（不影响其余段与整体总结）。", "warn")
    return sections


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
- title_cn: 给这支视频起一个简洁有信息量的**中文标题**（≤28字，准确反映主题，可意译、不必直译原标题；若原标题本就是中文则精炼保留）
- overall_summary: 整体内容概述（2-3段，中文，每段约100字）
- main_themes: 主要话题或主题（5-8条，每条一句话，中文）
- key_takeaways: 最重要的观点/启示/结论（5-10条，每条一句话，中文）
- category: 从以下固定类别里选**最贴切的一个**（只能选一个，原样返回）：{cat_list}
- tags: 3-6个内容标签（中文，简短，便于检索归类，如"原生家庭""依恋理论""慢性压力"）
- all_concepts: 汇总全部关键概念（去重，中英双语；若无则 []）
- glossary: 从概念里精选**最核心的 6-10 个**做成术语表，每个一条 {{"term": 与概念列表一致的词, "def": 一句话大白话定义}}。def 必须说明**这个视频里是怎么用/讲这个概念的**（它的作用、和主旨的关系），20-45字，只依据视频内容、绝不引入视频之外的知识；若视频没真正解释某概念就别选它。若无则 []

只返回JSON，不要其他文字。"""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你是专业的内容分析师。请严格按JSON格式输出。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        response_format={"type": "json_object"},
        max_tokens=6000,  # v4-pro reasoning 余量（见 analyze_chunk 说明）
    )

    try:
        data = json.loads(response.choices[0].message.content)
    except Exception:
        data = {}

    cat = (data.get("category") or "").strip()
    if cat not in CATEGORIES:
        cat = "其他"
    glossary = []
    for g in (data.get("glossary") or []):
        if isinstance(g, dict):
            term = str(g.get("term", "")).strip()
            gdef = str(g.get("def", "")).strip()
            if term and gdef:
                glossary.append({"term": term, "def": gdef})
    return {
        "title_cn": str(data.get("title_cn", "")).strip(),
        "overall_summary": data.get("overall_summary", ""),
        "main_themes": data.get("main_themes", []),
        "key_takeaways": data.get("key_takeaways", []),
        "category": cat,
        "tags": [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()],
        "all_concepts": data.get("all_concepts", []),
        "glossary": glossary,
    }


def generate_full_digest(client, model, video_title, sections, overall):
    """精读速览：一篇连贯流畅、自成一体、几百到上千字的整理，远短于逐段详解但本身精彩。"""
    blocks = []
    for i, s in enumerate(sections):
        a = s["analysis"]
        parts = [f"【第{i+1}段 {a.get('title_cn','')}】{a.get('summary_cn','')}"]
        for r in _norm_facts(a.get("research")):
            parts.append(f"· 研究：{r['title']}——{r['detail']}")
        for e in _norm_facts(a.get("examples")):
            parts.append(f"· 例子：{e['title']}——{e['detail']}")
        blocks.append("\n".join(parts))
    body = "\n\n".join(blocks)[:12000]
    takeaways = "；".join(overall.get("key_takeaways", []))

    prompt = f"""下面是 YouTube 视频《{video_title}》的分段要点（含每段摘要、研究、例子）。
请基于这些内容，写一篇**连贯流畅、自成一体的中文「全文整理 / 精读速览」**：

要求：
- 篇幅约 600–1500 字，远短于逐段详解，但本身要是一篇精彩、有信息量、结构清晰的整理，让人读完就能把握整支视频在讲什么、怎么论证、给出了哪些关键洞见。
- 用自然的叙述把全片脉络串起来（提出什么问题 → 怎么展开 → 关键证据/例子 → 结论与启示），不要写成生硬的要点罗列，也不要分点编号。
- 只依据下面提供的内容，绝不编造视频之外的事实或数字。
- 分 4–7 个自然段，段落之间逻辑顺承。

视频核心要点：{takeaways}

分段内容：
{body}

直接输出整理正文，不要加标题，也不要任何前后说明文字。"""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你是顶尖的内容编辑，擅长把长视频整理成既精炼又精彩、忠实原意的导读文章。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.5,
        max_tokens=7000,  # 长篇导读 + v4-pro reasoning 余量（见 analyze_chunk 说明）
    )
    return (response.choices[0].message.content or "").strip()


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

# ── 报告页线性图标（深藏青描边，替代 emoji；与输入页同一套视觉语言）─────────────
def _ric(body):
    return ('<svg class="ric" viewBox="0 0 20 20" fill="none" stroke="currentColor" '
            'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">' + body + '</svg>')

IC_TV       = _ric('<rect x="2.5" y="5" width="15" height="9.4" rx="1.8"/><path d="M6.7 5l3.3-2.4L13.3 5"/>')
IC_CLOCK    = _ric('<circle cx="10" cy="10" r="6.8"/><path d="M10 6V10l2.8 1.6"/>')
IC_DOC      = _ric('<path d="M5.5 3.5h6L15 7v9.5H5.5z"/><path d="M11 3.5V7h3.5"/><path d="M7.8 10.5h4.4M7.8 13h4.4"/>')
IC_CAL      = _ric('<rect x="3.8" y="5" width="12.4" height="11" rx="1.6"/><path d="M3.8 8.3h12.4"/><path d="M7.5 3.5v3M12.5 3.5v3"/>')
IC_PLAY     = _ric('<circle cx="10" cy="10" r="7"/><path d="M8.3 6.7l4.8 3.3-4.8 3.3z" fill="currentColor" stroke="none"/>')
IC_RESEARCH = _ric('<path d="M8 3.5h4M8.6 3.5v3.8L5.2 13.7A1.4 1.4 0 006.5 16h7A1.4 1.4 0 0014.8 13.7L11.4 7.3V3.5"/><path d="M7 12.2h6"/>')
IC_EXAMPLE  = _ric('<path d="M10 3.2A4.7 4.7 0 006.1 10.8c.7.8 1.2 1.5 1.3 2.6h5.2c.1-1.1.6-1.8 1.3-2.6A4.7 4.7 0 0010 3.2z"/><path d="M8.3 16h3.4M8.8 13.4h2.4"/>')
IC_DIG      = _ric('<circle cx="9" cy="9" r="5"/><path d="M12.8 12.8L16 16"/>')
IC_BOOK     = _ric('<path d="M10 5.5C8.3 4.4 5.8 4.4 4 5v9.2c1.8-.6 4.3-.6 6 .5 1.7-1.1 4.2-1.1 6-.5V5c-1.8-.6-4.3-.6-6 .5z"/><path d="M10 5.5v9.2"/>')
IC_CHAT     = _ric('<path d="M4 5.5h12A1.5 1.5 0 0117.5 7v5A1.5 1.5 0 0116 13.5H9l-3.5 3v-3H4A1.5 1.5 0 012.5 12V7A1.5 1.5 0 014 5.5z"/>')
IC_COPY     = _ric('<rect x="7" y="7" width="9" height="9" rx="1.6"/><path d="M4.5 12.5V4.5h8"/>')
IC_FOLDER   = _ric('<path d="M3.3 6h4.2l1.4 1.8h7.8v7.7H3.3z"/>')


def generate_html(metadata, sections, overall):
    title = html.escape(metadata["title"])
    title_cn = html.escape((overall.get("title_cn") or "").strip())
    disp_title = title_cn or title          # 优先中文标题；原标题作副标题展示
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
            dig = it.get("dig", "")
            detail_html = f'<div class="fact-d">{d}</div>' if d else ""
            if dig:
                paras = "".join(
                    f"<p>{html.escape(p.strip())}</p>"
                    for p in re.split(r"\n+", dig) if p.strip()
                )
                dig_btn = f'<button class="dig-btn" onclick="toggleDig(this)">{IC_DIG} 深挖</button>'
                dig_panel = (
                    '<div class="dig-panel">'
                    f'<h4>{IC_BOOK} 原文是怎么聊这一段的（整理还原）</h4>'
                    f'{paras}</div>'
                )
            else:
                dig_btn = ""
                dig_panel = ""
            rows.append(
                f'<div class="fact-item {css}">'
                f'<div class="fact-t">{t}{dig_btn}</div>'
                f'{detail_html}{dig_panel}</div>'
            )
        icon = f"{IC_RESEARCH} 研究 / 数据" if css == "research" else f"{IC_EXAMPLE} 例子 / 故事"
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
        if quotes_html or concept_tags:
            aside = f"""<aside class="sec-aside">
      <div class="aside-lbl">English · 点心</div>
      {quotes_html}
      {concepts_aside}
    </aside>"""

        # 追问快捷按钮：以本段各「点」命名，点一下生成可复制的外部 AI 提示词
        pt_titles = [r["title"] for r in _norm_facts(a.get("research"))] + \
                    [e["title"] for e in _norm_facts(a.get("examples"))]
        quick_btns = "".join(
            f'<button class="qbtn" data-point="{html.escape(p)}" onclick="quickFill(this)">{html.escape(p)}</button>'
            for p in pt_titles
        )
        quick_html = (
            '<div class="ask-quick-lbl">想深入某个点？点一下，自动生成可复制的提示词去问外部 AI（Claude / GPT 等）：</div>'
            f'<div class="ask-quick">{quick_btns}</div>'
        ) if quick_btns else ""

        cards.append(f"""
<section id="sec-{i+1}" class="sec-card">
  <div class="sec-main">
    <div class="sec-head">
      <span class="sec-num">{i+1:02d}</span>
      <div class="sec-titles"><h2>{t_cn}</h2>{f'<div class="sec-title-en">{t_en}</div>' if t_en else ''}</div>
      <a class="ts-link" href="{yt_link}" target="_blank">{start_str} → {end_str}</a>
    </div>
    <p class="sec-summary">{summary}</p>
    {facts_block(a.get("research"), "research", "研究")}
    {facts_block(a.get("examples"), "examples", "例子")}
    <div class="ask-box">
      <button class="ask-toggle" onclick="toggleAsk(this)">{IC_CHAT} 追问这一段</button>
      <div class="ask-body">
        {quick_html}
        <textarea class="ask-input" placeholder="例：这段里那个研究，原话是怎么说的？再详细讲讲。（由本视频助手基于字幕回答）"></textarea>
        <div class="ask-row">
          <button class="ask-send" onclick="sendAsk(this, {i})">发送给本视频助手（基于字幕回答）</button>
          <button class="ask-copy" onclick="copyAsk(this)">{IC_COPY} 复制提示词</button>
          <span class="ask-hint">自由提问由本地助手按字幕回答；复制的提示词拿去问外部 AI</span>
        </div>
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
    # 关键概念 → 折叠「术语表」：每词一句视频内定义，点词跳到首次出现的段落
    def _concept_core(x):
        return re.split(r"[（(]", str(x))[0].strip()

    concept_first = {}
    for i, sec in enumerate(sections):
        for c in (sec["analysis"].get("concepts") or []):
            core = _concept_core(c)
            if core and core not in concept_first:
                concept_first[core] = i + 1

    glossary = overall.get("glossary", []) or []
    if glossary:
        rows = []
        for g in glossary:
            term = g.get("term", "")
            core = _concept_core(term)
            sec_n = concept_first.get(core)
            if sec_n is None and core:
                for k, v in concept_first.items():
                    if core in k or k in core:
                        sec_n = v
                        break
            if sec_n:
                term_el = f'<a class="gloss-term" href="#sec-{sec_n}">{html.escape(term)}</a>'
            else:
                term_el = f'<span class="gloss-term">{html.escape(term)}</span>'
            rows.append(f'<div class="gloss-row">{term_el}<span class="gloss-def">{html.escape(g.get("def",""))}</span></div>')
        concepts_card = (
            '<div class="ov-card full gloss-card">'
            '<button class="gloss-bar" onclick="toggleGloss(this)">'
            f'<span class="gloss-bar-t">{IC_BOOK} 术语表 · {len(glossary)} 个核心概念</span>'
            '<span class="gloss-bar-x">点击展开 ▾</span></button>'
            f'<div class="gloss-body">{"".join(rows)}</div></div>'
        )
    else:
        all_concepts = "".join(f'<span class="ctag">{html.escape(c)}</span>' for c in overall.get("all_concepts", []))
        concepts_card = f'<div class="ov-card full"><div class="ov-card-title">关键概念</div><div class="ctags">{all_concepts}</div></div>' if all_concepts else ""
    tags_html = "".join(f'<span class="tag-chip">{html.escape(t)}</span>' for t in tags)

    # 全文整理（精读速览）——处理时预生成、内嵌，分享出去也能看
    digest_text = (overall.get("digest") or "").strip()
    if digest_text:
        digest_paras = "".join(
            f"<p>{html.escape(p.strip())}</p>"
            for p in re.split(r"\n+", digest_text) if p.strip()
        )
        digest_fab = f'<button class="digest-fab" onclick="openDrawer()">{IC_BOOK} 精炼版本</button>'
        digest_drawer = f"""
<div class="drawer-mask" onclick="closeDrawer()"></div>
<aside class="drawer" id="digest-drawer" aria-hidden="true">
  <div class="drawer-head">
    <div><h3>{IC_BOOK} 精炼版本</h3><div class="dh-sub">基于本视频内容梳理的连贯长文，比分段更快读完</div></div>
    <button class="drawer-close" onclick="closeDrawer()" aria-label="关闭">×</button>
  </div>
  <div class="drawer-body">{digest_paras}</div>
</aside>"""
    else:
        digest_fab = ""
        digest_drawer = ""

    # 右下浮动「打开笔记库」：点击在访达里定位本文 HTML，方便随手分享（只在应用内打开、本地服务在跑时有效）
    reveal_fab = (
        '<button class="reveal-fab" onclick="revealSelf(this)" aria-label="打开笔记库" title="在访达里定位本文，方便分享">'
        f'<span class="rf-ic">{IC_FOLDER}</span><span class="rf-tx">打开笔记库</span></button>'
    )

    # #2 报告 hero 缩略图（真实 YouTube 封面，16:9；拉不到时优雅隐藏）
    hero_thumb = (
        f'<div class="hero-thumb"><img src="https://i.ytimg.com/vi/{video_id}/mqdefault.jpg" '
        f'alt="" loading="lazy" onerror="this.parentNode.style.display=\'none\'">'
        f'<a class="ht-play" href="{video_url}" target="_blank" aria-label="观看原视频">{IC_PLAY}</a></div>'
    ) if video_id else ""

    # 中文标题为主时，把原始（多为英文）标题作副标题展示，便于对照
    hero_orig = f'<div class="hero-orig">{title}</div>' if (title_cn and title_cn != title) else ""

    # 笔记字数：统计这篇笔记里中文/英文实际内容的字数（只数 CJK 汉字 + 英文单词，标点空白不计）
    def _count_words(text):
        if not text:
            return 0
        s = str(text)
        cjk = len(re.findall(r"[一-鿿]", s))
        en = len(re.findall(r"[A-Za-z]+", s))
        return cjk + en

    _wc_parts = [overall.get("overall_summary", ""), overall.get("digest", "")]
    _wc_parts += list(overall.get("main_themes", []) or [])
    _wc_parts += list(overall.get("key_takeaways", []) or [])
    for _g in (overall.get("glossary", []) or []):
        _wc_parts += [_g.get("term", ""), _g.get("def", "")]
    for _s in sections:
        _a = _s["analysis"]
        _wc_parts += [_a.get("title_cn", ""), _a.get("summary_cn", "")]
        for _f in _norm_facts(_a.get("research")) + _norm_facts(_a.get("examples")):
            _wc_parts += [_f.get("title", ""), _f.get("detail", ""), _f.get("dig", "")]
    word_count = sum(_count_words(p) for p in _wc_parts)
    wc_str = f"{word_count:,}"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{disp_title}</title>
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
  --read:"LXGW WenKai","霞鹜文楷",-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;
  --fs:1.12; /* 阅读正文字号缩放：右下「字号」控件调节、localStorage 记忆，默认比原来稍大一档 */
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
/* 术语表（折叠） */
.gloss-card{{padding:0;overflow:hidden}}
.gloss-bar{{width:100%;display:flex;align-items:center;justify-content:space-between;gap:12px;
  padding:14px 22px;background:#eef2f7;border:none;cursor:pointer;text-align:left}}
.gloss-bar-t{{font-size:13px;font-weight:700;color:var(--navy)}}
.gloss-bar-x{{font-size:12px;color:var(--muted)}}
.gloss-body{{display:none;padding:2px 22px 12px}}
.gloss-card.open .gloss-body{{display:block}}
.gloss-row{{display:flex;gap:16px;padding:12px 0;border-top:1px solid var(--line-soft)}}
.gloss-term{{font-size:13.5px;font-weight:700;color:var(--navy);min-width:140px;flex-shrink:0;line-height:1.6}}
a.gloss-term:hover{{text-decoration:underline}}
.gloss-def{{font-size:13.5px;line-height:1.75;color:var(--ink-soft)}}
@media(max-width:560px){{.gloss-row{{flex-direction:column;gap:4px}}.gloss-term{{min-width:0}}}}
.sec-divider{{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:20px;padding-bottom:10px;border-bottom:1px solid var(--line)}}
.sec-card{{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--navy);
  border-radius:12px;padding:26px 30px;margin-bottom:20px;
  display:grid;grid-template-columns:1fr 232px;gap:30px}}
.sec-main{{min-width:0}}
.sec-head{{display:flex;align-items:flex-start;gap:14px;margin-bottom:14px}}
.sec-num{{font-family:ui-monospace,"SF Mono",monospace;font-size:19px;font-weight:700;color:var(--navy);min-width:26px;padding-top:3px}}
.sec-titles{{flex:1}}
.sec-titles h2{{font-size:18px;font-weight:700;color:var(--navy);line-height:1.4}}
.sec-title-en{{font-size:13px;font-weight:400;font-style:italic;color:var(--muted);line-height:1.4;margin-top:3px}}
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
.dig-btn{{font-size:11.5px;font-weight:600;color:var(--navy);background:#eef2f7;border:1px solid var(--line);
  cursor:pointer;padding:2px 10px;border-radius:20px;transition:.15s;white-space:nowrap}}
.dig-btn:hover{{background:var(--navy);color:#fff}}
.dig-panel{{display:none;margin-top:9px;padding:13px 16px;background:#f3f6fa;border:1px solid var(--line);
  border-left:3px solid var(--navy);border-radius:8px}}
.dig-panel.open{{display:block}}
.dig-panel h4{{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--navy);margin:0 0 7px;font-weight:700}}
.dig-panel p{{font-size:13.5px;line-height:1.85;color:var(--ink-soft);margin:0 0 7px}}
.dig-panel p:last-child{{margin-bottom:0}}
.sec-aside{{border-left:1px solid var(--line-soft);padding-left:22px}}
.aside-lbl{{font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:10px}}
.aside-en-title{{font-size:13px;font-style:italic;color:var(--ink-soft);margin-bottom:14px;line-height:1.5}}
.quote{{font-size:12.5px;line-height:1.7;color:var(--ink-soft);padding:8px 0 8px 12px;border-left:2px solid var(--sky);margin-bottom:10px;font-style:italic}}
.ask-box{{margin-top:18px;border-top:1px dashed var(--line);padding-top:12px}}
.ask-toggle{{font-size:12.5px;color:var(--navy);background:none;border:none;cursor:pointer;padding:0;font-weight:500}}
.ask-toggle:hover{{text-decoration:underline}}
.ask-body{{display:none;margin-top:10px}}
.ask-body.open{{display:block}}
.ask-quick-lbl{{font-size:12px;color:var(--muted);margin-bottom:7px}}
.ask-quick{{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:12px}}
.qbtn{{font-size:12px;color:var(--ink-soft);background:var(--amber-bg);border:1px solid var(--amber-bd);
  padding:4px 11px;border-radius:20px;cursor:pointer;transition:.15s;max-width:100%;text-align:left;line-height:1.4}}
.qbtn:hover{{background:var(--amber);color:#fff;border-color:var(--amber)}}
.ask-input{{width:100%;min-height:64px;font-family:inherit;font-size:13px;line-height:1.6;border:1px solid var(--line);border-radius:8px;padding:9px 11px;background:#fff;resize:vertical}}
.ask-row{{display:flex;flex-wrap:wrap;align-items:center;gap:9px;margin-top:8px}}
.ask-send{{font-size:13px;background:var(--navy);color:#fff;border:none;padding:7px 16px;border-radius:7px;cursor:pointer}}
.ask-send:disabled{{opacity:.5;cursor:wait}}
.ask-copy{{font-size:13px;background:#eef2f7;color:var(--navy);border:1px solid var(--line);padding:7px 14px;border-radius:7px;cursor:pointer}}
.ask-copy:hover{{background:var(--navy);color:#fff}}
.ask-hint{{font-size:11.5px;color:var(--muted);flex:1;min-width:160px}}
.ask-answer{{margin-top:12px;font-size:13.5px;line-height:1.85;color:var(--ink);white-space:pre-wrap}}
.ask-answer.err{{color:#b4452e}}
/* 全文整理：右侧抽屉 */
.digest-fab{{position:fixed;top:22px;right:22px;z-index:40;background:var(--navy);color:#fff;border:none;
  font-size:13.5px;font-weight:600;padding:10px 18px;border-radius:24px;cursor:pointer;
  box-shadow:0 4px 16px rgba(22,48,79,.28);transition:.15s}}
.digest-fab:hover{{background:var(--navy-deep);box-shadow:0 6px 20px rgba(22,48,79,.38)}}
/* 右下「打开笔记库」浮钮：默认圆形只露图标，悬停展开文字 */
.reveal-fab{{position:fixed;bottom:24px;right:24px;z-index:40;display:flex;align-items:center;
  background:var(--navy);color:#fff;border:none;border-radius:26px;padding:0;height:52px;cursor:pointer;
  box-shadow:0 4px 16px rgba(22,48,79,.32);transition:.2s;overflow:hidden}}
.reveal-fab .rf-ic{{width:52px;height:52px;display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0}}
.reveal-fab .rf-tx{{font-size:13.5px;font-weight:600;white-space:nowrap;max-width:0;opacity:0;transition:.2s;overflow:hidden}}
.reveal-fab:hover{{background:var(--navy-deep);box-shadow:0 6px 22px rgba(22,48,79,.42)}}
.reveal-fab:hover .rf-tx{{max-width:140px;opacity:1;padding-right:20px}}
.rf-toast{{position:fixed;bottom:90px;right:24px;z-index:70;background:var(--navy-deep);color:#fff;
  font-size:13px;padding:9px 15px;border-radius:9px;box-shadow:0 4px 16px rgba(0,0,0,.22);
  opacity:0;transform:translateY(8px);transition:.3s;max-width:300px}}
.rf-toast.show{{opacity:1;transform:translateY(0)}}
.drawer-mask{{position:fixed;inset:0;background:rgba(20,28,40,.32);z-index:50;opacity:0;pointer-events:none;transition:.25s}}
.drawer-mask.open{{opacity:1;pointer-events:auto}}
.drawer{{position:fixed;top:0;right:0;height:100vh;width:min(560px,92vw);background:var(--card);z-index:60;
  box-shadow:-8px 0 32px rgba(20,28,40,.18);transform:translateX(100%);transition:transform .28s cubic-bezier(.4,0,.2,1);
  display:flex;flex-direction:column}}
.drawer.open{{transform:translateX(0)}}
.drawer-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;
  padding:20px 26px;border-bottom:1px solid var(--line);background:var(--navy-deep);color:#fff}}
.drawer-head h3{{font-size:15px;font-weight:600;color:#fff}}
.drawer-head .dh-sub{{font-size:11.5px;color:rgba(255,255,255,.55);margin-top:3px}}
.drawer-close{{background:rgba(255,255,255,.12);color:#fff;border:none;width:30px;height:30px;border-radius:50%;
  cursor:pointer;font-size:16px;line-height:1;flex-shrink:0}}
.drawer-close:hover{{background:rgba(255,255,255,.25)}}
.drawer-body{{flex:1;overflow-y:auto;padding:28px 30px}}
.drawer-body p{{font-size:15px;line-height:1.95;color:var(--ink);margin-bottom:16px;text-align:justify}}
@media(max-width:820px){{.sidebar{{display:none}}.sec-card{{grid-template-columns:1fr}}.ov-grid{{grid-template-columns:1fr}}.ov-card.full{{grid-column:span 1}}
  .sec-aside{{border-left:none;border-top:1px solid var(--line-soft);padding-left:0;padding-top:18px}}}}
/* ── 视觉升级：线性图标 / hero 缩略图 / 霞鹜文楷正文 ── */
.ric{{width:14px;height:14px;vertical-align:-2px;flex-shrink:0}}
.hm{{display:inline-flex;align-items:center;gap:5px}}
.hero-orig{{font-size:13.5px;color:var(--muted);font-weight:400;margin-top:6px;line-height:1.45}}
.hero{{display:flex;gap:30px;align-items:flex-start}}
.hero-text{{flex:1;min-width:0}}
.hero-thumb{{position:relative;flex-shrink:0;width:208px}}
.hero-thumb img{{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:11px;border:1px solid var(--line);display:block}}
.ht-play{{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#fff;opacity:.92;filter:drop-shadow(0 1px 4px rgba(0,0,0,.5))}}
.ht-play:hover{{opacity:1}}
.ht-play .ric{{width:40px;height:40px}}
.facts-lbl{{display:flex;align-items:center;gap:6px}}
.facts-lbl .ric,.gloss-bar-t .ric,.dig-btn .ric,.ask-toggle .ric,.ask-copy .ric,.digest-fab .ric,.drawer-head h3 .ric{{width:15px;height:15px}}
.dig-panel h4 .ric{{width:13px;height:13px}}
.reveal-fab .rf-ic .ric{{width:21px;height:21px}}
.gloss-bar-t,.dig-btn,.ask-toggle,.ask-copy,.digest-fab,.drawer-head h3{{display:inline-flex;align-items:center;gap:6px}}
/* 正文走霞鹜文楷；UI 标签 / 序号 / 时间戳保持原样以维持层级 */
.ov-card p,.ov-card ul li,.sec-summary,.fact-t,.fact-d,.dig-panel p,.gloss-def,.drawer-body p,.quote{{font-family:var(--read)}}
@media(max-width:820px){{.hero{{flex-direction:column-reverse;gap:18px}}.hero-thumb{{width:100%;max-width:360px}}}}
/* ── 阅读字号缩放（--fs）：只缩放正文/详解/术语/引用/精炼版与分段标题，标签·序号·侧栏·chip 不动以保层级 ── */
.sec-titles h2{{font-size:calc(18px*var(--fs))}}
.sec-summary,.drawer-body p{{font-size:calc(15px*var(--fs))}}
.ov-card p{{font-size:calc(14.5px*var(--fs))}}
.fact-t{{font-size:calc(14px*var(--fs))}}
.ov-card ul li,.fact-d,.dig-panel p,.gloss-term,.gloss-def,.ask-answer{{font-size:calc(13.5px*var(--fs))}}
.aside-en-title{{font-size:calc(13px*var(--fs))}}
.quote{{font-size:calc(12.5px*var(--fs))}}
/* 左下「字号」浮控（避开右上「精炼版」/右下「笔记库」浮钮） */
.fs-ctrl{{position:fixed;left:24px;bottom:24px;z-index:41;display:flex;align-items:center;gap:9px;
  background:var(--card);border:1px solid var(--line);border-radius:24px;padding:6px 13px 6px 15px;
  box-shadow:0 4px 16px rgba(22,48,79,.16)}}
.fs-ctrl .fs-lbl{{font-size:11px;font-weight:700;letter-spacing:.06em;color:var(--muted)}}
.fs-ctrl .fs-btns{{display:flex;gap:2px}}
.fs-ctrl button{{border:none;background:none;color:var(--navy);cursor:pointer;width:27px;height:27px;
  border-radius:50%;font-weight:700;line-height:1;display:flex;align-items:center;justify-content:center;transition:.15s}}
.fs-ctrl button:hover{{background:#eef2f7}}
.fs-ctrl button.on{{background:var(--navy);color:#fff}}
.fs-ctrl button.s1{{font-size:11px}}.fs-ctrl button.s2{{font-size:13px}}
.fs-ctrl button.s3{{font-size:15px}}.fs-ctrl button.s4{{font-size:17.5px}}
@media(max-width:560px){{.fs-ctrl{{left:12px;bottom:12px;padding:5px 11px;gap:6px}}.fs-ctrl .fs-lbl{{display:none}}}}
</style>
<script>try{{var _f=[1,1.12,1.26,1.4][(parseInt(localStorage.getItem('ytd_fs'))||2)-1];if(_f)document.documentElement.style.setProperty('--fs',_f);}}catch(e){{}}</script>
</head>
<body data-vid="{video_id}">
{digest_fab}
{reveal_fab}
<div class="fs-ctrl" role="group" aria-label="字号调节">
  <span class="fs-lbl">字号</span>
  <div class="fs-btns">
    <button class="s1" onclick="setFS(1)" title="标准" aria-label="标准字号">A</button>
    <button class="s2" onclick="setFS(2)" title="大" aria-label="大字号">A</button>
    <button class="s3" onclick="setFS(3)" title="特大" aria-label="特大字号">A</button>
    <button class="s4" onclick="setFS(4)" title="超大" aria-label="超大字号">A</button>
  </div>
</div>
<div class="layout">
  <nav class="sidebar">
    <div class="sb-head">
      <h1>{disp_title}</h1>
      <div class="sb-meta">{channel} · {dur} · {date}</div>
    </div>
    <div class="toc-lbl">目录</div>
    <nav class="toc"><ol>{toc_html}</ol></nav>
  </nav>
  <main class="main">
    <div class="hero">
      <div class="hero-text">
        <h1>{disp_title}</h1>
        {hero_orig}
        <div class="hero-meta">
          <span class="cat-chip">{category}</span>
          {tags_html}
          <span class="hm">{IC_TV} {channel}</span>
          <span class="hm">{IC_CLOCK} {dur}</span>
          <span class="hm">{IC_DOC} 全文约 {wc_str} 字</span>
          <span class="hm">{IC_CAL} {date}</span>
          <a class="hm" href="{video_url}" target="_blank">{IC_PLAY} 观看原视频</a>
        </div>
      </div>
      {hero_thumb}
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
{digest_drawer}
<script>
// 字号缩放：4 档（标准/大/特大/超大），localStorage 记忆，默认「大」
const FS_LEVELS=[1,1.12,1.26,1.4];
function setFS(lvl){{
  lvl=Math.max(1,Math.min(4,lvl|0));
  document.documentElement.style.setProperty('--fs',FS_LEVELS[lvl-1]);
  document.querySelectorAll('.fs-ctrl button').forEach((b,i)=>b.classList.toggle('on',i===lvl-1));
  try{{localStorage.setItem('ytd_fs',lvl);}}catch(e){{}}
}}
(function(){{var s=2;try{{s=parseInt(localStorage.getItem('ytd_fs'))||2;}}catch(e){{}}setFS(s);}})();

// 目录高亮
const secs=document.querySelectorAll('.sec-card');
const links=document.querySelectorAll('.toc a');
const obs=new IntersectionObserver(es=>{{es.forEach(e=>{{if(e.isIntersecting){{
  links.forEach(l=>l.classList.remove('active'));
  const a=document.querySelector('.toc a[href="#'+e.target.id+'"]');if(a)a.classList.add('active');}}}});}},{{threshold:0.3}});
secs.forEach(s=>obs.observe(s));

// 右下浮钮：在访达里定位本文 HTML（需在应用内打开、本地服务在运行）
function rfToast(msg){{
  const t=document.createElement('div');t.className='rf-toast';t.textContent=msg;
  document.body.appendChild(t);requestAnimationFrame(()=>t.classList.add('show'));
  setTimeout(()=>{{t.classList.remove('show');setTimeout(()=>t.remove(),320);}},2200);
}}
function revealSelf(btn){{
  const fn=decodeURIComponent((location.pathname.split('/').pop()||''));
  if(!fn.toLowerCase().endsWith('.html')||location.protocol==='file:'){{
    rfToast('请从应用「🌐 报告」按钮打开本文，才能在访达里定位');return;
  }}
  fetch('/reveal',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{filename:fn}})}})
    .then(r=>r.json()).then(d=>rfToast(d.ok?'已在访达里定位本文，可拖动分享 ✓':('定位失败：'+(d.message||''))))
    .catch(()=>rfToast('定位失败：本地服务未运行'));
}}

// 术语表：展开/收起
function toggleGloss(btn){{
  const card=btn.closest('.gloss-card');card.classList.toggle('open');
  const x=btn.querySelector('.gloss-bar-x');
  if(x)x.textContent=card.classList.contains('open')?'收起 ▴':'点击展开 ▾';
}}

// 深挖：展开/收起预生成的「原文整理还原」（内嵌，分享出去也能看）
function toggleDig(btn){{
  const panel=btn.closest('.fact-item').querySelector('.dig-panel');
  if(panel)panel.classList.toggle('open');
}}

// 全文整理抽屉
function openDrawer(){{
  document.querySelector('.drawer-mask').classList.add('open');
  document.getElementById('digest-drawer').classList.add('open');
  document.getElementById('digest-drawer').setAttribute('aria-hidden','false');
}}
function closeDrawer(){{
  document.querySelector('.drawer-mask').classList.remove('open');
  document.getElementById('digest-drawer').classList.remove('open');
  document.getElementById('digest-drawer').setAttribute('aria-hidden','true');
}}
document.addEventListener('keydown',e=>{{if(e.key==='Escape')closeDrawer();}});

// 追问快捷按钮：把某个「点」自动填成可复制的外部 AI 提示词
function quickFill(btn){{
  const body=btn.closest('.ask-body');
  const ta=body.querySelector('.ask-input');
  const point=btn.dataset.point;
  const vtitle=document.title;
  ta.value=`我在看一个 YouTube 视频《${{vtitle}}》，里面提到了「${{point}}」。\\n请你详细、准确地介绍这件事的真实背景：它的来源/出处、具体内容、关键数据或结论，以及学界对它的评价或争议。如果这是一个广为流传但被误读的说法，请指出。请基于你已知的可靠知识回答，并说明确定性高低。`;
  ta.focus();
}}
function copyAsk(btn){{
  const ta=btn.closest('.ask-body').querySelector('.ask-input');
  if(!ta.value.trim()){{btn.textContent='先写点问题 ✕';setTimeout(()=>btn.textContent='📋 复制提示词',1500);return;}}
  navigator.clipboard.writeText(ta.value).then(()=>{{btn.textContent='已复制 ✓';setTimeout(()=>btn.textContent='📋 复制提示词',1800);}});
}}

// 视频内追问——基于该段字幕，由本地助手（DeepSeek）回答（需在应用内打开报告）
function toggleAsk(btn){{btn.nextElementSibling.classList.toggle('open');}}
async function sendAsk(btn, secIdx){{
  const body=btn.closest('.ask-body');
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

    # 一次 extract_info 同时供元数据和字幕用，避免对同一视频抓两遍
    info = extract_video_info(url, config, log)
    metadata = metadata_from_info(info, url)
    if not metadata.get("video_id"):
        metadata["video_id"] = video_id
    video_id = metadata["video_id"]
    log(f'✅ 视频：{metadata["title"]}')
    log(f'   频道：{metadata["channel"]}，时长：{format_time(metadata["duration"])}')

    log("获取字幕...")
    # 偶发：YouTube 对同一请求时好时坏，有时返回的 info 里字幕轨是空的（即「明明有字幕却报无字幕」）。
    # 这等价于用户手动再点一次就好——所以这里空了就重新抓 info 再试几次，自动化掉这个手动重试。
    transcript, lang = None, None
    for attempt in range(1, 4):
        try:
            transcript, lang = get_transcript_from_info(info, config, log)
            break
        except RuntimeError as e:
            # Cookie 类问题重试也没用；最后一次也别再吞，直接抛出友好提示。
            if "Cookie" in str(e) or attempt == 3:
                raise
            log(f"   ⏳ 这次没拿到字幕（{e}）。重新抓取后第 {attempt + 1} 次尝试…", "warn")
            time.sleep(4)
            info = extract_video_info(url, config, log)
            new_meta = metadata_from_info(info, url)
            if new_meta.get("video_id"):  # 用新抓到的（通常更完整的）元数据
                metadata = new_meta
                video_id = metadata["video_id"]
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

    # 并行分析各段（独立调用，互不依赖）——长视频提速的关键。单段失败只占位、不拖垮整篇。
    sections = _analyze_all_chunks(client, model, chunks, metadata["title"], log)

    log("生成整体总结...")
    overall = generate_overall_summary(client, model, metadata["title"], sections)
    log(f"✅ 整体总结完成（类别：{overall.get('category','其他')}）")

    log("生成全文整理（精读速览）...")
    try:
        overall["digest"] = generate_full_digest(client, model, metadata["title"], sections, overall)
        log("✅ 全文整理完成")
    except Exception as e:
        overall["digest"] = ""
        log(f"⚠️  全文整理生成失败（不影响其余内容）：{e}", "warn")

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
        "title_cn": overall.get("title_cn", ""),
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
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
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
        "title_cn": result.get("title_cn", ""),
        "channel": result["channel"],
        "duration": result["duration"],
        "sections": result["sections"],
        "html_filename": result["html_filename"],
        "url": result["url"],
        "video_id": result.get("video_id", ""),
        "category": result.get("category", "其他"),
        "tags": result.get("tags", []),
        "date": result["date"],
        "created_at": result.get("created_at", ""),
        "summary": result["summary"],
    })

    index_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def update_meta(url, new_category, new_tags, config):
    """编辑某条笔记的类别 / 标签：更新 index.json、搬动 Obsidian md 到新类别子文件夹、
    重写 md frontmatter、尽力同步 HTML 报告里的类别/标签 chip。返回 (ok, message)。"""
    output_dir = Path(config["output"].get("html_dir", "./output"))
    index_path = output_dir / "index.json"
    try:
        entries = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    except Exception:
        entries = []

    entry = next((e for e in entries if e.get("url") == url), None)
    if entry is None:
        return False, "未找到这条记录"

    new_category = (new_category or "").strip() or entry.get("category", "其他")
    if isinstance(new_tags, str):
        new_tags = [t.strip() for t in re.split(r"[,，、]", new_tags) if t.strip()]
    new_tags = [str(t).strip() for t in (new_tags or []) if str(t).strip()]

    old_category = entry.get("category", "其他")
    title = entry.get("title", "")

    # 1) 搬动 + 重写 Obsidian md
    try:
        raw_path = _strip_quotes(str(config["obsidian"]["vault_path"]))
        vault = Path(os.path.expanduser(raw_path))
        folder = config["obsidian"].get("folder", "YouTube笔记")
        fname = safe_filename(title) + ".md"
        old_md = vault / folder / safe_filename(old_category) / fname
        new_dir = vault / folder / safe_filename(new_category)
        new_md = new_dir / fname
        md_text = None
        if old_md.exists():
            md_text = old_md.read_text(encoding="utf-8")
        elif new_md.exists():
            md_text = new_md.read_text(encoding="utf-8")
        if md_text is not None:
            # 重写 frontmatter 的 category / tags 行
            tag_line = ", ".join(["youtube", "video-notes", new_category] + new_tags)
            md_text = re.sub(r'(?m)^category:.*$', f'category: "{new_category}"', md_text, count=1)
            md_text = re.sub(r'(?m)^tags:.*$', f'tags: [{tag_line}]', md_text, count=1)
            md_text = re.sub(r'(?m)^(> \*\*时长\*\*：.*\*\*类别\*\*)：.*$', rf'\1：{new_category}', md_text, count=1)
            new_dir.mkdir(parents=True, exist_ok=True)
            new_md.write_text(md_text, encoding="utf-8")
            if old_md.exists() and old_md.resolve() != new_md.resolve():
                old_md.unlink()
    except Exception as e:
        # 笔记搬动失败不阻断索引更新
        pass

    # 2) 尽力同步 HTML 报告里的类别/标签 chip
    try:
        html_file = output_dir / entry.get("html_filename", "")
        if html_file.exists():
            htxt = html_file.read_text(encoding="utf-8")
            htxt = re.sub(
                r'(<span class="cat-chip">).*?(</span>)',
                lambda m: m.group(1) + html.escape(new_category) + m.group(2),
                htxt, count=1,
            )
            new_tag_html = "".join(
                f'<span class="tag-chip">{html.escape(t)}</span>' for t in new_tags
            )
            # 把第一段连续的 tag-chip 整体替换掉
            htxt = re.sub(
                r'(?:<span class="tag-chip">.*?</span>\s*)+',
                new_tag_html, htxt, count=1,
            )
            html_file.write_text(htxt, encoding="utf-8")
    except Exception:
        pass

    # 3) 更新索引
    entry["category"] = new_category
    entry["tags"] = new_tags
    index_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return True, "已更新"


def _trash_manifest_path(output_dir):
    return output_dir / "_trash" / "_trash.json"


def _read_trash_manifest(output_dir):
    p = _trash_manifest_path(output_dir)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except Exception:
        return []


def _write_trash_manifest(output_dir, records):
    p = _trash_manifest_path(output_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_entry(url, config):
    """软删除一篇笔记：HTML 报告移到 output/_trash/，Obsidian md 移到 vault 的 _trash/，
    从 index.json 移除条目，并在回收站清单里登记（可一键恢复）。返回 (ok, message)。"""
    output_dir = Path(config["output"].get("html_dir", "./output"))
    index_path = output_dir / "index.json"
    try:
        entries = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    except Exception:
        entries = []

    entry = next((e for e in entries if e.get("url") == url), None)
    if entry is None:
        return False, "未找到这条记录"

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    html_trash_name = ""
    md_trash_name = ""

    # 1) HTML 报告 → output/_trash/
    try:
        html_file = output_dir / entry.get("html_filename", "")
        if html_file.exists():
            trash = output_dir / "_trash"
            trash.mkdir(parents=True, exist_ok=True)
            html_trash_name = f"{stamp}_{html_file.name}"
            html_file.rename(trash / html_trash_name)
    except Exception:
        pass

    # 2) Obsidian md → vault/<folder>/_trash/
    try:
        raw_path = _strip_quotes(str(config["obsidian"]["vault_path"]))
        vault = Path(os.path.expanduser(raw_path))
        folder = config["obsidian"].get("folder", "YouTube笔记")
        fname = safe_filename(entry.get("title", "")) + ".md"
        base = vault / folder
        md = base / safe_filename(entry.get("category", "其他")) / fname
        if not md.exists():
            # 类别可能改过，全目录找一下
            found = list(base.rglob(fname))
            md = found[0] if found else md
        if md.exists():
            mtrash = base / "_trash"
            mtrash.mkdir(parents=True, exist_ok=True)
            md_trash_name = f"{stamp}_{fname}"
            md.rename(mtrash / md_trash_name)
    except Exception:
        pass

    # 3) 从索引移除
    entries = [e for e in entries if e.get("url") != url]
    index_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    # 4) 登记到回收站清单（用于列出与恢复）
    try:
        records = _read_trash_manifest(output_dir)
        records.insert(0, {
            "id": stamp,
            "deleted_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "title": entry.get("title", ""),
            "category": entry.get("category", "其他"),
            "html_trash": html_trash_name,
            "md_trash": md_trash_name,
            "entry": entry,
        })
        _write_trash_manifest(output_dir, records)
    except Exception:
        pass

    return True, "已移入回收站"


def list_trash(config):
    """列出回收站里的笔记：清单里登记的（可一键恢复）+ 文件夹里未登记的旧文件（仅提示手动恢复）。"""
    output_dir = Path(config["output"].get("html_dir", "./output"))
    trash = output_dir / "_trash"
    records = _read_trash_manifest(output_dir)
    out = []
    known = set()
    for r in records:
        known.add(r.get("html_trash", ""))
        out.append({
            "id": r.get("id", ""),
            "deleted_at": r.get("deleted_at", ""),
            "title": r.get("title", "") or r.get("entry", {}).get("title", ""),
            "category": r.get("category", ""),
            "orphan": False,
        })
    # 文件夹里有、但清单没登记的旧版删除文件
    if trash.exists():
        for f in sorted(trash.glob("*.html"), reverse=True):
            if f.name in known:
                continue
            disp = re.sub(r"^\d{8}_\d{6}_", "", f.name)
            disp = re.sub(r"\.html$", "", disp)
            out.append({
                "id": "orphan:" + f.name,
                "deleted_at": "",
                "title": disp,
                "category": "",
                "orphan": True,
            })
    return out


def restore_entry(trash_id, config):
    """从回收站恢复一篇笔记：HTML 移回 output/、md 移回 vault 类别目录、重新写入 index.json。
    返回 (ok, message)。orphan（旧版删除，无清单记录）的只能去文件夹手动恢复。"""
    output_dir = Path(config["output"].get("html_dir", "./output"))
    index_path = output_dir / "index.json"

    # orphan：旧版删除、无清单记录 —— 尽力把 HTML 报告移回 output/ 并重建一条最简索引
    if str(trash_id).startswith("orphan:"):
        fname = str(trash_id)[len("orphan:"):]
        src = output_dir / "_trash" / fname
        if not src.exists():
            return False, "回收站里找不到这个文件"
        restored_name = re.sub(r"^\d{8}_\d{6}_", "", fname)  # 去掉删除时间戳前缀
        dest = output_dir / restored_name
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                restored_name = f"{Path(restored_name).stem}_restored{Path(restored_name).suffix}"
                dest = output_dir / restored_name
            src.rename(dest)
        except Exception as e:
            return False, f"恢复失败：{e}"
        try:
            entries = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        except Exception:
            entries = []
        title = re.sub(r"\.html$", "", restored_name)
        if not any(e.get("html_filename") == restored_name for e in entries):
            entries.insert(0, {
                "title": title,
                "channel": "",
                "duration": "",
                "sections": 0,
                "html_filename": restored_name,
                "url": "",
                "video_id": "",
                "category": "其他",
                "tags": [],
                "date": "",
                "created_at": "",
                "summary": "（旧文件恢复，无原始分类/简介信息）",
            })
            index_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        return True, "已恢复报告到文章库（旧文件无原始分类信息）"

    records = _read_trash_manifest(output_dir)
    rec = next((r for r in records if r.get("id") == trash_id), None)
    if rec is None:
        return False, "回收站里找不到这条"

    entry = rec.get("entry") or {}

    # 1) HTML 移回 output/
    try:
        if rec.get("html_trash"):
            src = output_dir / "_trash" / rec["html_trash"]
            if src.exists():
                (output_dir).mkdir(parents=True, exist_ok=True)
                src.rename(output_dir / entry.get("html_filename", rec["html_trash"]))
    except Exception:
        pass

    # 2) md 移回 vault/<folder>/<类别>/
    try:
        if rec.get("md_trash"):
            raw_path = _strip_quotes(str(config["obsidian"]["vault_path"]))
            vault = Path(os.path.expanduser(raw_path))
            folder = config["obsidian"].get("folder", "YouTube笔记")
            mtrash = vault / folder / "_trash" / rec["md_trash"]
            if mtrash.exists():
                fname = safe_filename(entry.get("title", "")) + ".md"
                dest_dir = vault / folder / safe_filename(entry.get("category", "其他"))
                dest_dir.mkdir(parents=True, exist_ok=True)
                mtrash.rename(dest_dir / fname)
    except Exception:
        pass

    # 3) 重新写入索引（去重 + 放回最前）
    try:
        entries = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    except Exception:
        entries = []
    if entry:
        entries = [e for e in entries if e.get("url") != entry.get("url")]
        entries.insert(0, entry)
        index_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    # 4) 从回收站清单移除
    records = [r for r in records if r.get("id") != trash_id]
    _write_trash_manifest(output_dir, records)
    return True, "已恢复到文章库"
