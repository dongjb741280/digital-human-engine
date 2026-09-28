"""PPT 生成：LLM 生成幻灯片内容 -> python-pptx 生成 .pptx，Pillow 渲染幻灯片图片。

内置 4 套主题模板（配色 + 真实背景图），供 getppt 选择、generate_ppt 渲染。
背景图来自 ai-to-pptx 模板抽取，存于 services/templates/。
"""
import base64
import io
import json
import os
import re

from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Inches, Pt

from services import llm

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")

# 内置主题模板：id 与前端选中值一致，name 用于展示
# bg 为背景图文件名（相对 templates 目录），cover_bg 为无背景图时的兜底色
THEMES = [
    {
        "id": "0",
        "name": "课程学习汇报",
        "accent": (241, 111, 151),
        "title_color": (190, 60, 100),
        "body_color": (47, 47, 47),
        "footer_color": (119, 132, 149),
        "cover_bg": (241, 111, 151),
        "bg": "bg_0.jpg",
    },
    {
        "id": "1",
        "name": "读书分享演示",
        "accent": (160, 182, 73),
        "title_color": (110, 130, 40),
        "body_color": (47, 47, 47),
        "footer_color": (119, 132, 149),
        "cover_bg": (160, 182, 73),
        "bg": "bg_1.jpg",
    },
    {
        "id": "2",
        "name": "蓝色通用商务",
        "accent": (90, 170, 210),
        "title_color": (40, 90, 140),
        "body_color": (47, 47, 47),
        "footer_color": (119, 132, 149),
        "cover_bg": (40, 90, 140),
        "bg": "bg_2.jpg",
    },
    {
        "id": "3",
        "name": "蓝色工作汇报总结",
        "accent": (19, 117, 252),
        "title_color": (19, 117, 252),
        "body_color": (47, 47, 47),
        "footer_color": (119, 132, 149),
        "cover_bg": (19, 117, 252),
        "bg": "bg_3.jpg",
    },
]


def _load_font(size: int):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _bg_path(theme):
    name = theme.get("bg")
    if not name:
        return None
    path = os.path.join(_TEMPLATE_DIR, name)
    return path if os.path.exists(path) else None


def _load_bg(theme):
    path = _bg_path(theme)
    if not path:
        return None
    try:
        return Image.open(path).convert("RGB")
    except Exception:
        return None


def _fit(img: Image.Image, width: int, height: int) -> Image.Image:
    """cover 模式裁剪到目标比例后缩放。"""
    ratio = width / height
    iratio = img.width / img.height
    if iratio > ratio:
        new_w = int(img.height * ratio)
        left = (img.width - new_w) // 2
        img = img.crop((left, 0, left + new_w, img.height))
    else:
        new_h = int(img.width / ratio)
        top = (img.height - new_h) // 2
        img = img.crop((0, top, img.width, top + new_h))
    return img.resize((width, height), Image.LANCZOS)


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
        "控制在 6~10 页，第一项为封面页标题。"
    )
    raw = llm.chat([{"role": "user", "content": prompt}], temperature=0.3, max_tokens=16384, system=system)
    return _extract_json(raw)


def get_template(template_id=None):
    """按 id 或名称取主题，找不到时回退到第一套。"""
    if template_id is None or template_id == "":
        return THEMES[0]
    key = str(template_id)
    for t in THEMES:
        if str(t["id"]) == key or t["name"] == template_id:
            return t
    return THEMES[0]


def list_templates():
    """返回主题列表（含缩略图 base64），供 getppt 使用。"""
    result = []
    for t in THEMES:
        png = _render_thumbnail(t)
        b64 = base64.b64encode(png).decode("ascii")
        result.append(
            {
                "id": t["id"],
                "ppt_name": t["name"],
                "subject": t["name"],
                "image": f"data:image/png;base64,{b64}",
            }
        )
    return result


def _render_thumbnail(theme, width: int = 320, height: int = 180) -> bytes:
    """生成主题缩略图（迷你封面示意）。"""
    bg = _load_bg(theme)
    if bg is not None:
        img = _fit(bg, width, height)
        draw = ImageDraw.Draw(img)
        # 标题占位 + 强调条
        draw.rectangle([16, 70, 96, 76], fill=theme["accent"])
        draw.rectangle([16, 92, 288, 112], fill=(255, 255, 255))
        draw.rectangle([24, 132, 296, 138], fill=(255, 255, 255))
    else:
        img = Image.new("RGB", (width, height), theme["cover_bg"])
        draw = ImageDraw.Draw(img)
        draw.rectangle([16, 70, 96, 76], fill=theme["accent"])
        draw.rectangle([16, 92, 288, 112], fill=(255, 255, 255))
        draw.rectangle([24, 132, 296, 138], fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _wrap_text(draw, text: str, font, max_width: int):
    lines = []
    for raw_line in text.split("\n"):
        line = ""
        for ch in raw_line:
            if draw.textlength(line + ch, font=font) <= max_width:
                line += ch
            else:
                if line:
                    lines.append(line)
                line = ch
        if line:
            lines.append(line)
    return lines or [""]


def build_pptx(slides, theme=None) -> bytes:
    """根据幻灯片内容 + 主题生成 .pptx 字节。"""
    theme = theme or THEMES[0]
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]
    total = len(slides)
    for i, item in enumerate(slides):
        slide = prs.slides.add_slide(blank)
        _style_slide(slide, item, theme, i, total, prs)
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _style_slide(slide, item, theme, index: int, total: int, prs):
    title = str(item.get("title", ""))
    bullets = [str(b) for b in item.get("bullets", [])]
    notes = str(item.get("notes", ""))
    if notes:
        slide.notes_slide.notes_text_frame.text = notes

    if index == 0:
        _style_cover(slide, title, theme, prs)
    else:
        _style_content(slide, title, bullets, theme, index, total)


def _set_bg(slide, color):
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = RGBColor(*color)


def _style_cover(slide, title, theme, prs):
    bg = _bg_path(theme)
    if bg:
        slide.shapes.add_picture(bg, 0, 0, width=prs.slide_width, height=prs.slide_height)
    else:
        _set_bg(slide, theme["cover_bg"])

    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1), Inches(2.9), Inches(0.08), Inches(1.6))
    bar.fill.solid()
    bar.fill.fore_color.rgb = RGBColor(*theme["accent"])
    bar.line.fill.background()

    tb = slide.shapes.add_textbox(Inches(1.3), Inches(2.7), Inches(10.5), Inches(1.5))
    tf = tb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    r = p.add_run()
    r.text = title
    r.font.size = Pt(42)
    r.font.bold = True
    r.font.color.rgb = RGBColor(255, 255, 255)

    stb = slide.shapes.add_textbox(Inches(1.32), Inches(4.35), Inches(10), Inches(0.5))
    stf = stb.text_frame
    stf.word_wrap = True
    sp = stf.paragraphs[0]
    sp.alignment = PP_ALIGN.LEFT
    sr = sp.add_run()
    sr.text = f"{theme['name']} · AI 生成"
    sr.font.size = Pt(18)
    sr.font.color.rgb = RGBColor(240, 240, 240)


def _style_content(slide, title, bullets, theme, index, total):
    _set_bg(slide, (255, 255, 255))

    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.6), Inches(0.55), Inches(0.12), Inches(0.8))
    bar.fill.solid()
    bar.fill.fore_color.rgb = RGBColor(*theme["accent"])
    bar.line.fill.background()

    tb = slide.shapes.add_textbox(Inches(0.9), Inches(0.5), Inches(11.6), Inches(0.9))
    tf = tb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    r = p.add_run()
    r.text = title
    r.font.size = Pt(28)
    r.font.bold = True
    r.font.color.rgb = RGBColor(*theme["title_color"])

    line = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.9), Inches(1.5), Inches(11.6), Pt(1.5))
    line.fill.solid()
    line.fill.fore_color.rgb = RGBColor(220, 224, 230)
    line.line.fill.background()

    btb = slide.shapes.add_textbox(Inches(0.9), Inches(1.8), Inches(11.6), Inches(4.9))
    btf = btb.text_frame
    btf.word_wrap = True
    for j, b in enumerate(bullets):
        para = btf.paragraphs[0] if j == 0 else btf.add_paragraph()
        para.space_after = Pt(12)
        run = para.add_run()
        run.text = "• " + b
        run.font.size = Pt(18)
        run.font.color.rgb = RGBColor(*theme["body_color"])

    fb = slide.shapes.add_textbox(Inches(0.9), Inches(6.9), Inches(11.6), Inches(0.4))
    ff = fb.text_frame
    fp = ff.paragraphs[0]
    fp.alignment = PP_ALIGN.RIGHT
    fr = fp.add_run()
    fr.text = f"{index + 1} / {total}    {theme['name']}"
    fr.font.size = Pt(10)
    fr.font.color.rgb = RGBColor(*theme["footer_color"])


def render_slide(slide, theme=None, index: int = 0, total: int = 1, width: int = 1280, height: int = 720) -> bytes:
    """把一页幻灯片按主题渲染成 PNG 图片（首页封面，其余内容页）。"""
    theme = theme or THEMES[0]
    title = str(slide.get("title", ""))
    bullets = [str(b) for b in slide.get("bullets", [])]
    if index == 0:
        img = _render_cover(theme, title, width, height)
    else:
        img = _render_content(theme, title, bullets, index, total, width, height)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _render_cover(theme, title, width, height):
    bg = _load_bg(theme)
    if bg is not None:
        img = _fit(bg, width, height)
        # 轻暗色蒙层，保留背景图细节
        ov = Image.new("RGBA", (width, height), (0, 0, 0, 100))
        img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
        draw = ImageDraw.Draw(img)
    else:
        img = Image.new("RGB", (width, height), theme["cover_bg"])
        draw = ImageDraw.Draw(img)

    margin = int(width * 0.09)
    title_font = _load_font(62)
    sub_font = _load_font(26)
    lines = _wrap_text(draw, title, title_font, width - margin * 2 - 50)
    line_h = 80
    start_y = height // 2 - 50 - (len(lines) - 1) * line_h // 2

    # 左侧竖强调条
    draw.rectangle(
        [margin, start_y - 8, margin + 8, start_y + (len(lines) - 1) * line_h + 8],
        fill=theme["accent"],
    )
    y = start_y
    for line in lines:
        draw.text((margin + 32, y), line, font=title_font, fill=(255, 255, 255), anchor="lm")
        y += line_h

    draw.text((margin + 34, y + 6), f"{theme['name']} · AI 生成", font=sub_font, fill=(235, 240, 248), anchor="lm")
    return img


def _render_content(theme, title, bullets, index, total, width, height):
    bg = _load_bg(theme)
    if bg is not None:
        img = _fit(bg, width, height)
        # 白色蒙层保证正文可读（略降透明度，让背景图透出）
        ov = Image.new("RGBA", (width, height), (255, 255, 255, 200))
        img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    else:
        img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    draw.rectangle([64, 48, 76, 128], fill=theme["accent"])
    title_font = _load_font(38)
    bullet_font = _load_font(26)
    for line in _wrap_text(draw, title, title_font, width - 192):
        draw.text((96, 52), line, font=title_font, fill=theme["title_color"])
        break
    draw.line([(96, 132), (width - 64, 132)], fill=(220, 224, 230), width=3)
    y = 168
    for b in bullets:
        lines = _wrap_text(draw, str(b), bullet_font, width - 200)
        draw.ellipse([100, y + 8, 112, y + 20], fill=theme["accent"])
        for line in lines:
            draw.text((128, y), line, font=bullet_font, fill=theme["body_color"])
            y += 40
        y += 12
    _render_footer(draw, theme, width, height, index, total)
    return img


def _render_footer(draw, theme, width, height, index, total):
    font = _load_font(18)
    text = f"{index} / {total}    {theme['name']}"
    draw.text((width - 40, height - 36), text, font=font, fill=theme["footer_color"], anchor="rm")
