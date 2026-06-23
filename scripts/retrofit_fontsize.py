#!/usr/bin/env python3
"""一次性脚本：给 output/ 里已生成的旧报告 HTML 注入「字号」控件。

新报告由 generate_html 直接带控件；本脚本只补存量旧报告。幂等——已注入的会跳过。
注入内容与 generate_html 的实现保持一致：
  - <head> 末尾：--fs 变量 + 各阅读选择器的 calc 缩放 + .fs-ctrl 样式 + 防闪烁内联脚本
  - <body> 末尾：字号控件 DOM + setFS 脚本
旧报告的 CSS 与新模板同构（同一个 generate_html 历代产出），故一套模板可统一注入；
注入的 <style> 排在原 <style> 之后，同特异性后者胜，无需 !important。
"""
import sys
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
MARKER = 'class="fs-ctrl"'

HEAD_INJECT = """<style>
/* 字号缩放（旧报告回填，由 retrofit_fontsize.py 注入；排在原样式之后故生效） */
:root{--fs:1.12}
.sec-titles h2{font-size:calc(18px*var(--fs))}
.sec-summary,.drawer-body p{font-size:calc(15px*var(--fs))}
.ov-card p{font-size:calc(14.5px*var(--fs))}
.fact-t{font-size:calc(14px*var(--fs))}
.ov-card ul li,.fact-d,.dig-panel p,.gloss-term,.gloss-def,.ask-answer{font-size:calc(13.5px*var(--fs))}
.aside-en-title{font-size:calc(13px*var(--fs))}
.quote{font-size:calc(12.5px*var(--fs))}
.fs-ctrl{position:fixed;left:24px;bottom:24px;z-index:41;display:flex;align-items:center;gap:9px;background:var(--card,#fffdf9);border:1px solid var(--line,#e7e0d4);border-radius:24px;padding:6px 13px 6px 15px;box-shadow:0 4px 16px rgba(22,48,79,.16)}
.fs-ctrl .fs-lbl{font-size:11px;font-weight:700;letter-spacing:.06em;color:var(--muted,#8a8174)}
.fs-ctrl .fs-btns{display:flex;gap:2px}
.fs-ctrl button{border:none;background:none;color:var(--navy,#1e3a5f);cursor:pointer;width:27px;height:27px;border-radius:50%;font-weight:700;line-height:1;display:flex;align-items:center;justify-content:center;transition:.15s}
.fs-ctrl button:hover{background:#eef2f7}
.fs-ctrl button.on{background:var(--navy,#1e3a5f);color:#fff}
.fs-ctrl button.s1{font-size:11px}.fs-ctrl button.s2{font-size:13px}
.fs-ctrl button.s3{font-size:15px}.fs-ctrl button.s4{font-size:17.5px}
@media(max-width:560px){.fs-ctrl{left:12px;bottom:12px;padding:5px 11px;gap:6px}.fs-ctrl .fs-lbl{display:none}}
</style>
<script>try{var _f=[1,1.12,1.26,1.4][(parseInt(localStorage.getItem('ytd_fs'))||2)-1];if(_f)document.documentElement.style.setProperty('--fs',_f);}catch(e){}</script>
"""

BODY_INJECT = """<div class="fs-ctrl" role="group" aria-label="字号调节">
  <span class="fs-lbl">字号</span>
  <div class="fs-btns">
    <button class="s1" onclick="setFS(1)" title="标准" aria-label="标准字号">A</button>
    <button class="s2" onclick="setFS(2)" title="大" aria-label="大字号">A</button>
    <button class="s3" onclick="setFS(3)" title="特大" aria-label="特大字号">A</button>
    <button class="s4" onclick="setFS(4)" title="超大" aria-label="超大字号">A</button>
  </div>
</div>
<script>
var FS_LEVELS=[1,1.12,1.26,1.4];
function setFS(lvl){lvl=Math.max(1,Math.min(4,lvl|0));document.documentElement.style.setProperty('--fs',FS_LEVELS[lvl-1]);document.querySelectorAll('.fs-ctrl button').forEach(function(b,i){b.classList.toggle('on',i===lvl-1);});try{localStorage.setItem('ytd_fs',lvl);}catch(e){}}
(function(){var s=2;try{s=parseInt(localStorage.getItem('ytd_fs'))||2;}catch(e){}setFS(s);})();
</script>
"""


def retrofit(html: str) -> str:
    if MARKER in html:
        return html  # 已注入，跳过
    if "</head>" in html:
        html = html.replace("</head>", HEAD_INJECT + "</head>", 1)
    if "</body>" in html:
        html = html.replace("</body>", BODY_INJECT + "</body>", 1)
    elif "</html>" in html:
        html = html.replace("</html>", BODY_INJECT + "</html>", 1)
    return html


def main():
    files = sorted(p for p in OUTPUT_DIR.glob("*.html") if p.is_file())
    done = skipped = 0
    for p in files:
        src = p.read_text(encoding="utf-8")
        out = retrofit(src)
        if out == src:
            skipped += 1
            continue
        p.write_text(out, encoding="utf-8")
        done += 1
    print(f"共 {len(files)} 个报告：注入 {done}，跳过（已注入/无锚点）{skipped}")


if __name__ == "__main__":
    main()
