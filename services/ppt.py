"""PPT 生成：LLM 生成幻灯片内容 -> python-pptx 生成 .pptx，Pillow 渲染幻灯片图片。

内置 4 套主题模板（配色 + 真实背景图），供 getppt 选择、generate_ppt 渲染。
背景图来自 ai-to-pptx 模板抽取，存于 services/templates/。
"""
import base64
import io
import json
import os
import re
import urllib.request

from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

from services import image_search
from services import llm

_FONT_CANDIDATES = [
    # macOS
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]

_FONT_NAME = "微软雅黑"

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")

def _load_themes():
    """从 templates/themes.json 加载主题配置（配色唯一事实源），颜色转 tuple。"""
    path = os.path.join(_TEMPLATE_DIR, "themes.json")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    themes = []
    for t in raw:
        t = dict(t)
        for key in ("accent", "title_color", "body_color", "footer_color", "cover_bg"):
            t[key] = tuple(t[key])
        themes.append(t)
    return themes


# 内置主题模板：id 与前端选中值一致，name 用于展示
# bg 为背景图文件名（相对 templates 目录），cover_bg 为无背景图时的兜底色
THEMES = _load_themes()


def _load_font(size: int):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _set_run_font(run):
    """给 run 设置中文字体（latin 与 east asian 都设，否则中文吃不到字体）。"""
    run.font.name = _FONT_NAME
    rPr = run._r.get_or_add_rPr()
    ea = rPr.find(qn("a:ea"))
    if ea is None:
        ea = rPr.makeelement(qn("a:ea"), {})
        rPr.append(ea)
    ea.set("typeface", _FONT_NAME)


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


_LAYOUTS = ("cover", "agenda", "section", "content", "image_text", "full_image", "quote", "comparison", "closing")

_LAYOUT_SCHEMA = (
    "版式字段规范（layout 九选一）：\n"
    'cover 封面: {"layout":"cover","title":"主标题","subtitle":"副标题","notes":"备注"}\n'
    'agenda 目录: {"layout":"agenda","items":["章节1","章节2"]}\n'
    'section 章节过渡: {"layout":"section","title":"章节标题"}\n'
    'content 标题要点: {"layout":"content","title":"页标题","bullets":["要点1"],"notes":"备注"}\n'
    'image_text 左图右文: {"layout":"image_text","title":"...","bullets":["..."],"image":"配图关键词","image_side":"left"}\n'
    'full_image 全图: {"layout":"full_image","title":"...","image":"配图关键词"}\n'
    'quote 金句: {"layout":"quote","text":"引文","source":"出处","image":"配图关键词"}\n'
    'comparison 对比: {"layout":"comparison","title":"...","left":{"title":"A","points":["..."]},"right":{"title":"B","points":["..."]}}\n'
    'closing 结束: {"layout":"closing","title":"谢谢/总结"}\n'
)


def generate_slides(title: str, body_text: str, system=None):
    """LLM 生成幻灯片内容，返回带 layout 字段的 9 版式结构化 JSON 数组。"""
    prompt = (
        "请把下面的课件文案整理成 PPT 幻灯片内容，只输出一个 JSON 数组（不要输出代码块或任何解释文字）。\n"
        "每个元素是一个幻灯片对象，含 layout 字段及各版式专属字段。\n"
        + _LAYOUT_SCHEMA +
        "规则：第一页用 cover，最后一页用 closing；正文内容页用 content，需要配图/强调用 image_text、full_image 或 quote，需要对比用 comparison。\n"
        "控制在 6~10 页。\n"
        f"标题：{title}\n"
        f"文案正文：\n{body_text}\n"
    )
    raw = llm.chat([{"role": "user", "content": prompt}], temperature=0.3, max_tokens=16384, system=system)
    return _normalize_slides(_extract_json(raw))


def _normalize_slides(slides):
    """规则兜底：首项强制 cover、末项强制 closing、未知/缺省 layout 回退 content。"""
    if not isinstance(slides, list) or not slides:
        raise ValueError("LLM 返回内容为空，无法生成幻灯片")
    out = []
    n = len(slides)
    for i, item in enumerate(slides):
        if not isinstance(item, dict):
            item = {"title": str(item)}
        item = dict(item)
        layout = str(item.get("layout", "")).strip()
        if layout not in _LAYOUTS:
            layout = "content"
        if i == 0:
            layout = "cover"
        elif i == n - 1:
            layout = "closing"
        item["layout"] = layout
        out.append(item)
    return out


def _resolve_slide_image(item, layout):
    """取该页配图字节；无关键词或搜图失败返回 None。"""
    keyword = str(item.get("image", "")).strip()
    if not keyword:
        return None
    if layout in ("full_image", "quote"):
        keyword = keyword + " 壁纸"
    return image_search.search(keyword)


def _add_image_placeholder(slide, x=0.9, y=1.8, w=5.2, h=4.9):
    box = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    box.fill.solid()
    box.fill.fore_color.rgb = RGBColor(235, 238, 243)
    box.line.color.rgb = RGBColor(210, 214, 220)
    return box


def _img_from_data(data, width, height, dark_overlay=False, white_overlay=False):
    """把图片字节转成裁到尺寸的 Pillow 图；失败返回 None。"""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None
    img = _fit(img, width, height)
    if dark_overlay:
        ov = Image.new("RGBA", (width, height), (0, 0, 0, 100))
        img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    elif white_overlay:
        ov = Image.new("RGBA", (width, height), (255, 255, 255, 200))
        img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    return img


def _paste_image(base, data, box):
    """把图片字节贴到 base 的 box 区域（cover 裁剪到 box 比例）；失败原样返回。"""
    try:
        sub = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return base
    bw, bh = box[2] - box[0], box[3] - box[1]
    sub = _fit(sub, bw, bh)
    base.paste(sub, (box[0], box[1]))
    return base


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
    """根据幻灯片内容（9 版式）+ 主题生成 .pptx 字节。"""
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


def _style_slide(slide, item, theme, index, total, prs):
    notes = str(item.get("notes", ""))
    if notes:
        slide.notes_slide.notes_text_frame.text = notes
    layout = item.get("layout") or "content"
    {
        "cover": _style_cover,
        "agenda": _style_agenda,
        "section": _style_section,
        "content": _style_content,
        "image_text": _style_image_text,
        "full_image": _style_full_image,
        "quote": _style_quote,
        "comparison": _style_comparison,
        "closing": _style_closing,
    }[layout](slide, item, theme, index, total, prs)


def _set_bg(slide, color):
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = RGBColor(*color)


def _add_accent_bar(slide, theme, x=0.6, y=0.55, w=0.12, h=0.8):
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    bar.fill.solid()
    bar.fill.fore_color.rgb = RGBColor(*theme["accent"])
    bar.line.fill.background()
    return bar


def _add_textbox(slide, x, y, w, h, text, theme, size=18, color=None, bold=False, align=PP_ALIGN.LEFT):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    for i, line in enumerate(str(text or "").split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        run = p.add_run()
        run.text = line
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = RGBColor(*(color if color is not None else theme["body_color"]))
        _set_run_font(run)
    return tb


def _add_bullets(slide, x, y, w, h, bullets, theme, size=18):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    for j, b in enumerate(bullets or []):
        p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
        p.space_after = Pt(12)
        run = p.add_run()
        run.text = "• " + str(b)
        run.font.size = Pt(size)
        run.font.color.rgb = RGBColor(*theme["body_color"])
        _set_run_font(run)
    return tb


def _add_title(slide, title, theme, x=0.9, y=0.5, w=11.6, h=0.9, size=28):
    _add_textbox(slide, x, y, w, h, title, theme, size=size, color=theme["title_color"], bold=True)


def _add_divider(slide, x=0.9, y=1.5, w=11.6):
    line = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Pt(1.5))
    line.fill.solid()
    line.fill.fore_color.rgb = RGBColor(220, 224, 230)
    line.line.fill.background()
    return line


def _add_footer(slide, theme, index, total):
    _add_textbox(slide, 0.9, 6.9, 11.6, 0.4, f"{index + 1} / {total}    {theme['name']}",
                 theme, size=10, color=theme["footer_color"], align=PP_ALIGN.RIGHT)


def _style_cover(slide, item, theme, index, total, prs):
    bg = _bg_path(theme)
    if bg:
        slide.shapes.add_picture(bg, 0, 0, width=prs.slide_width, height=prs.slide_height)
    else:
        _set_bg(slide, theme["cover_bg"])
    _add_accent_bar(slide, theme, x=1, y=2.9, w=0.08, h=1.6)
    _add_textbox(slide, 1.3, 2.7, 10.5, 1.5, str(item.get("title", "")), theme, size=42, color=(255, 255, 255), bold=True)
    subtitle = str(item.get("subtitle", "") or f"{theme['name']} · AI 生成")
    _add_textbox(slide, 1.32, 4.35, 10, 0.5, subtitle, theme, size=18, color=(240, 240, 240))


def _style_agenda(slide, item, theme, index, total, prs):
    _set_bg(slide, (255, 255, 255))
    _add_accent_bar(slide, theme)
    _add_title(slide, "目录", theme)
    _add_divider(slide)
    for j, it in enumerate(item.get("items", []) or []):
        _add_textbox(slide, 1.2, 1.7 + j * 0.8, 10.8, 0.7, f"{j + 1}. {it}", theme, size=20)
    _add_footer(slide, theme, index, total)


def _style_section(slide, item, theme, index, total, prs):
    bg = _bg_path(theme)
    if bg:
        slide.shapes.add_picture(bg, 0, 0, width=prs.slide_width, height=prs.slide_height)
    else:
        _set_bg(slide, theme["accent"])
    _add_accent_bar(slide, theme, x=1, y=3.2, w=0.08, h=1.0)
    _add_textbox(slide, 1.4, 3.1, 10, 1.2, str(item.get("title", "")), theme, size=36, color=(255, 255, 255), bold=True)


def _style_content(slide, item, theme, index, total, prs):
    _set_bg(slide, (255, 255, 255))
    _add_accent_bar(slide, theme)
    _add_title(slide, item.get("title", ""), theme)
    _add_divider(slide)
    _add_bullets(slide, 0.9, 1.8, 11.6, 4.9, [str(b) for b in item.get("bullets", [])], theme)
    _add_footer(slide, theme, index, total)


def _style_image_text(slide, item, theme, index, total, prs):
    _set_bg(slide, (255, 255, 255))
    _add_accent_bar(slide, theme)
    _add_title(slide, item.get("title", ""), theme)
    side = item.get("image_side") or "left"
    text_x = 6.0 if side == "left" else 0.9
    _add_bullets(slide, text_x, 1.8, 5.5, 4.9, [str(b) for b in item.get("bullets", [])], theme)
    img_x = 0.9 if side == "left" else 6.3
    data = _resolve_slide_image(item, "image_text")
    if data is None:
        _add_image_placeholder(slide, img_x, 1.8, 5.2, 4.9)
    else:
        try:
            slide.shapes.add_picture(io.BytesIO(data), Inches(img_x), Inches(1.8), Inches(5.2), Inches(4.9))
        except Exception:
            _add_image_placeholder(slide, img_x, 1.8, 5.2, 4.9)
    _add_footer(slide, theme, index, total)


def _style_full_image(slide, item, theme, index, total, prs):
    data = _resolve_slide_image(item, "full_image")
    if data is None:
        bg = _bg_path(theme)
        if bg:
            slide.shapes.add_picture(bg, 0, 0, width=prs.slide_width, height=prs.slide_height)
        else:
            _set_bg(slide, theme["accent"])
    else:
        try:
            slide.shapes.add_picture(io.BytesIO(data), 0, 0, width=prs.slide_width, height=prs.slide_height)
        except Exception:
            _set_bg(slide, theme["accent"])
    _add_textbox(slide, 0.9, 3.0, 11.6, 1.4, str(item.get("title", "")), theme, size=40, color=(255, 255, 255), bold=True)
    _add_footer(slide, theme, index, total)


def _style_quote(slide, item, theme, index, total, prs):
    data = _resolve_slide_image(item, "quote")
    if data is not None:
        try:
            slide.shapes.add_picture(io.BytesIO(data), 0, 0, width=prs.slide_width, height=prs.slide_height)
        except Exception:
            data = None
    if data is None:
        _set_bg(slide, (255, 255, 255))
    on_image = data is not None
    _add_accent_bar(slide, theme, x=0.6, y=2.4, w=0.12, h=1.6)
    _add_textbox(slide, 1.0, 2.2, 11.0, 2.0, str(item.get("text", "")), theme, size=28,
                 color=(255, 255, 255) if on_image else theme["title_color"], bold=True)
    source = item.get("source")
    if source:
        _add_textbox(slide, 1.0, 4.6, 11.0, 0.6, "—— " + str(source), theme, size=16,
                     color=(240, 240, 240) if on_image else theme["footer_color"])
    _add_footer(slide, theme, index, total)


def _style_comparison(slide, item, theme, index, total, prs):
    _set_bg(slide, (255, 255, 255))
    _add_accent_bar(slide, theme)
    _add_title(slide, item.get("title", ""), theme)
    left = item.get("left") or {}
    right = item.get("right") or {}
    _add_textbox(slide, 0.9, 1.7, 5.5, 0.7, str(left.get("title", "")), theme, size=22, color=theme["title_color"], bold=True)
    _add_bullets(slide, 0.9, 2.5, 5.5, 4.0, [str(p) for p in left.get("points", [])], theme, size=16)
    _add_textbox(slide, 6.6, 1.7, 5.5, 0.7, str(right.get("title", "")), theme, size=22, color=theme["title_color"], bold=True)
    _add_bullets(slide, 6.6, 2.5, 5.5, 4.0, [str(p) for p in right.get("points", [])], theme, size=16)
    _add_footer(slide, theme, index, total)


def _style_closing(slide, item, theme, index, total, prs):
    bg = _bg_path(theme)
    if bg:
        slide.shapes.add_picture(bg, 0, 0, width=prs.slide_width, height=prs.slide_height)
    else:
        _set_bg(slide, theme["cover_bg"])
    _add_textbox(slide, 0.9, 3.0, 11.6, 1.4, str(item.get("title", "") or "谢谢/总结"), theme, size=40, color=(255, 255, 255), bold=True)


def render_slide(slide, theme=None, index: int = 0, total: int = 1, width: int = 1280, height: int = 720) -> bytes:
    """把一页幻灯片按主题渲染成 PNG 图片（按 layout 分派 9 版式）。"""
    theme = theme or THEMES[0]
    layout = slide.get("layout") or "content"
    img = {
        "cover": _render_cover,
        "agenda": _render_agenda,
        "section": _render_section,
        "content": _render_content,
        "image_text": _render_image_text,
        "full_image": _render_full_image,
        "quote": _render_quote,
        "comparison": _render_comparison,
        "closing": _render_closing,
    }[layout](slide, theme, index, total, width, height)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _img_with_bg(theme, width, height, white_overlay=False, fallback=(255, 255, 255)):
    """加载主题背景图裁到尺寸，可选叠蒙层；无图时用纯色兜底。"""
    bg = _load_bg(theme)
    if bg is not None:
        img = _fit(bg, width, height)
        alpha = 200 if white_overlay else 100
        color = (255, 255, 255) if white_overlay else (0, 0, 0)
        ov = Image.new("RGBA", (width, height), (*color, alpha))
        img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    else:
        img = Image.new("RGB", (width, height), fallback)
    return img


def _draw_header(draw, theme, title, width):
    draw.rectangle([64, 48, 76, 128], fill=theme["accent"])
    title_font = _load_font(38)
    for line in _wrap_text(draw, str(title), title_font, width - 192):
        draw.text((96, 52), line, font=title_font, fill=theme["title_color"])
        break
    draw.line([(96, 132), (width - 64, 132)], fill=(220, 224, 230), width=3)


def _draw_bullets(draw, theme, bullets, x, y, max_width, font=None):
    font = font or _load_font(26)
    for b in bullets or []:
        lines = _wrap_text(draw, str(b), font, max_width)
        draw.ellipse([x, y + 8, x + 12, y + 20], fill=theme["accent"])
        for line in lines:
            draw.text((x + 28, y), line, font=font, fill=theme["body_color"])
            y += 40
        y += 12
    return y


def _render_cover(slide, theme, index, total, width, height):
    img = _img_with_bg(theme, width, height, fallback=theme["cover_bg"])
    draw = ImageDraw.Draw(img)
    margin = int(width * 0.09)
    title_font = _load_font(62)
    sub_font = _load_font(26)
    lines = _wrap_text(draw, str(slide.get("title", "")), title_font, width - margin * 2 - 50)
    line_h = 80
    start_y = height // 2 - 50 - (len(lines) - 1) * line_h // 2
    draw.rectangle([margin, start_y - 8, margin + 8, start_y + (len(lines) - 1) * line_h + 8], fill=theme["accent"])
    y = start_y
    for line in lines:
        draw.text((margin + 32, y), line, font=title_font, fill=(255, 255, 255), anchor="lm")
        y += line_h
    subtitle = str(slide.get("subtitle", "") or f"{theme['name']} · AI 生成")
    draw.text((margin + 34, y + 6), subtitle, font=sub_font, fill=(235, 240, 248), anchor="lm")
    return img


def _render_agenda(slide, theme, index, total, width, height):
    img = _img_with_bg(theme, width, height, white_overlay=True)
    draw = ImageDraw.Draw(img)
    _draw_header(draw, theme, "目录", width)
    font = _load_font(30)
    y = 200
    for j, it in enumerate(slide.get("items", []) or []):
        draw.text((128, y), f"{j + 1}. {it}", font=font, fill=theme["body_color"])
        y += 70
    _render_footer(draw, theme, width, height, index, total)
    return img


def _render_section(slide, theme, index, total, width, height):
    img = _img_with_bg(theme, width, height, fallback=theme["accent"])
    draw = ImageDraw.Draw(img)
    draw.rectangle([64, height // 2 - 50, 72, height // 2 + 30], fill=theme["accent"])
    title_font = _load_font(52)
    for line in _wrap_text(draw, str(slide.get("title", "")), title_font, width - 192):
        draw.text((96, height // 2 - 40), line, font=title_font, fill=(255, 255, 255), anchor="lm")
        break
    return img


def _render_content(slide, theme, index, total, width, height):
    img = _img_with_bg(theme, width, height, white_overlay=True)
    draw = ImageDraw.Draw(img)
    _draw_header(draw, theme, slide.get("title", ""), width)
    _draw_bullets(draw, theme, [str(b) for b in slide.get("bullets", [])], 100, 168, width - 200)
    _render_footer(draw, theme, width, height, index, total)
    return img


def _render_image_text(slide, theme, index, total, width, height):
    img = _img_with_bg(theme, width, height, white_overlay=True)
    draw = ImageDraw.Draw(img)
    _draw_header(draw, theme, slide.get("title", ""), width)
    side = slide.get("image_side") or "left"
    if side == "left":
        box = (64, 168, 500, height - 80)
        text_x, text_w = 560, width - 560 - 64
    else:
        box = (width - 500, 168, width - 64, height - 80)
        text_x, text_w = 100, width - 500 - 100 - 64
    data = _resolve_slide_image(slide, "image_text")
    if data is None:
        draw.rectangle(box, fill=(235, 238, 243), outline=(210, 214, 220))
    else:
        img = _paste_image(img, data, box)
    _draw_bullets(draw, theme, [str(b) for b in slide.get("bullets", [])], text_x, 168, text_w, _load_font(24))
    _render_footer(draw, theme, width, height, index, total)
    return img


def _render_full_image(slide, theme, index, total, width, height):
    data = _resolve_slide_image(slide, "full_image")
    img = _img_from_data(data, width, height, dark_overlay=True) if data is not None else None
    if img is None:
        img = _img_with_bg(theme, width, height, fallback=theme["accent"])
    draw = ImageDraw.Draw(img)
    title_font = _load_font(56)
    for line in _wrap_text(draw, str(slide.get("title", "")), title_font, width - 192):
        draw.text((96, height // 2 - 40), line, font=title_font, fill=(255, 255, 255), anchor="lm")
        break
    _render_footer(draw, theme, width, height, index, total)
    return img


def _render_quote(slide, theme, index, total, width, height):
    data = _resolve_slide_image(slide, "quote")
    img = _img_from_data(data, width, height, dark_overlay=True) if data is not None else None
    on_image = img is not None
    if img is None:
        img = _img_with_bg(theme, width, height, white_overlay=True)
    draw = ImageDraw.Draw(img)
    draw.rectangle([64, height // 2 - 80, 76, height // 2 + 60], fill=theme["accent"])
    quote_font = _load_font(40)
    text_color = (255, 255, 255) if on_image else theme["title_color"]
    source_color = (240, 240, 240) if on_image else theme["footer_color"]
    y = height // 2 - 60
    for line in _wrap_text(draw, str(slide.get("text", "")), quote_font, width - 200):
        draw.text((100, y), line, font=quote_font, fill=text_color)
        y += 60
    source = slide.get("source")
    if source:
        draw.text((100, y + 10), "—— " + str(source), font=_load_font(24), fill=source_color)
    _render_footer(draw, theme, width, height, index, total)
    return img


def _render_comparison(slide, theme, index, total, width, height):
    img = _img_with_bg(theme, width, height, white_overlay=True)
    draw = ImageDraw.Draw(img)
    _draw_header(draw, theme, slide.get("title", ""), width)
    left = slide.get("left") or {}
    right = slide.get("right") or {}
    col_font = _load_font(32)
    draw.text((100, 180), str(left.get("title", "")), font=col_font, fill=theme["title_color"])
    draw.text((width // 2 + 40, 180), str(right.get("title", "")), font=col_font, fill=theme["title_color"])
    _draw_bullets(draw, theme, [str(p) for p in left.get("points", [])], 100, 240, width // 2 - 140, _load_font(22))
    _draw_bullets(draw, theme, [str(p) for p in right.get("points", [])], width // 2 + 40, 240, width // 2 - 140, _load_font(22))
    _render_footer(draw, theme, width, height, index, total)
    return img


def _render_closing(slide, theme, index, total, width, height):
    img = _img_with_bg(theme, width, height, fallback=theme["cover_bg"])
    draw = ImageDraw.Draw(img)
    title_font = _load_font(56)
    for line in _wrap_text(draw, str(slide.get("title", "") or "谢谢/总结"), title_font, width - 192):
        draw.text((96, height // 2 - 40), line, font=title_font, fill=(255, 255, 255), anchor="lm")
        break
    return img


def _render_footer(draw, theme, width, height, index, total):
    font = _load_font(18)
    text = f"{index} / {total}    {theme['name']}"
    draw.text((width - 40, height - 36), text, font=font, fill=theme["footer_color"], anchor="rm")


# ============ fabric 画布 JSON -> .pptx ============

_PX2EMU = 9525  # 1px = 9525 EMU（1280px 对应 13.333 英寸）


def _hex_to_rgb(h):
    h = (h or "#ffffff").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (255, 255, 255)


def build_pptx_from_elements(slides_elements, theme=None) -> bytes:
    """根据前端 fabric 画布 JSON（每页元素）生成 .pptx。"""
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    for raw in slides_elements:
        data = raw
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except Exception:
                data = {}
        slide = prs.slides.add_slide(blank)
        for obj in (data or {}).get("objects", []):
            otype = obj.get("type")
            if otype == "rect" and obj.get("id") == "ppt-bg":
                slide.background.fill.solid()
                slide.background.fill.fore_color.rgb = RGBColor(*_hex_to_rgb(obj.get("fill")))
            elif otype == "textbox":
                _add_textbox_shape(slide, obj)
            elif otype == "image":
                _add_image_shape(slide, obj)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _add_textbox_shape(slide, obj):
    left = int(obj.get("left", 0) * _PX2EMU)
    top = int(obj.get("top", 0) * _PX2EMU)
    width = int((obj.get("width", 300) or 0) * (obj.get("scaleX", 1) or 1) * _PX2EMU)
    height = int((obj.get("height", 100) or 0) * (obj.get("scaleY", 1) or 1) * _PX2EMU)
    width = max(width, _PX2EMU)
    height = max(height, _PX2EMU)
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    text = obj.get("text", "") or ""
    lines = text.split("\n")
    fs = int((obj.get("fontSize", 24) or 24) * 0.75)
    color = _hex_to_rgb(obj.get("fill"))
    bold = obj.get("fontWeight") == "bold"
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        run = p.add_run()
        run.text = line
        run.font.size = Pt(fs)
        run.font.color.rgb = RGBColor(*color)
        run.font.bold = bold


def _add_image_shape(slide, obj):
    src = obj.get("src", "")
    if not src:
        return
    if src.startswith("data:image"):
        data = base64.b64decode(src.split(",", 1)[1])
    else:
        try:
            data = urllib.request.urlopen(src, timeout=15).read()
        except Exception:
            return
    left = int(obj.get("left", 0) * _PX2EMU)
    top = int(obj.get("top", 0) * _PX2EMU)
    width = int((obj.get("width", 100) or 0) * (obj.get("scaleX", 1) or 1) * _PX2EMU)
    height = int((obj.get("height", 100) or 0) * (obj.get("scaleY", 1) or 1) * _PX2EMU)
    if width <= 0 or height <= 0:
        return
    try:
        slide.shapes.add_picture(io.BytesIO(data), left, top, width, height)
    except Exception:
        return

