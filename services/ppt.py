"""PPT 生成：LLM 生成幻灯片内容 -> python-pptx 生成 .pptx，Pillow 渲染幻灯片图片。"""
import io
import json
import re

from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation

from services import llm

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]


def _load_font(size: int):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _extract_json(raw: str):
    """从 LLM 输出里提取 JSON 数组（容忍 markdown 代码块/前后缀）。"""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if m:
        raw = m.group(0)
    if not raw:
        raise ValueError("LLM 返回内容为空，无法生成幻灯片")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError(f"LLM 返回不是合法 JSON: {raw[:200]}")


def generate_slides(title: str, body_text: str, system=None):
    """LLM 生成幻灯片内容，返回 [{title, bullets, notes}, ...]。"""
    prompt = (
        "请把下面的课件文案整理成 PPT 幻灯片内容，只输出一个 JSON 数组（不要输出代码块或任何解释文字），"
        '每项格式：{"title": "页标题", "bullets": ["要点1", "要点2"], "notes": "讲解备注"}。\n'
        f"标题：{title}\n"
        f"文案正文：\n{body_text}\n"
        "控制在 6~10 页。"
    )
    raw = llm.chat([{"role": "user", "content": prompt}], temperature=0.3, max_tokens=16384, system=system)
    return _extract_json(raw)


def build_pptx(slides) -> bytes:
    """根据幻灯片内容生成 .pptx 字节。"""
    prs = Presentation()
    for item in slides:
        slide = prs.slides.add_slide(prs.slide_layouts[1])  # 标题 + 内容
        slide.shapes.title.text = str(item.get("title", ""))
        body = slide.placeholders[1].text_frame
        body.clear()
        for b in item.get("bullets", []):
            p = body.add_paragraph()
            p.text = str(b)
        notes = item.get("notes", "")
        if notes:
            slide.notes_slide.notes_text_frame.text = str(notes)
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def render_slide(title: str, bullets, width: int = 1280, height: int = 720) -> bytes:
    """把一页幻灯片渲染成 PNG 图片（白底 + 标题 + 要点）。"""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    title_font = _load_font(40)
    bullet_font = _load_font(26)
    draw.text((64, 40), str(title), fill=(20, 20, 20), font=title_font)
    draw.line([(64, 108), (width - 64, 108)], fill=(200, 200, 200), width=2)
    y = 132
    for b in bullets:
        draw.text((64, y), "• " + str(b), fill=(60, 60, 60), font=bullet_font)
        y += 52
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
