"""视频子系统：ffmpeg 基础操作 + 抠图/背景替换/数字人。

抠图（背景去除）用 rembg（ONNX，CPU 可跑）：
- matting_image：提取视频首帧 -> 去背景 -> RGBA PNG
- matting_video：逐帧去背景 -> 合成绿幕 -> MP4

数字人合成（MuseTalk 口型驱动）见 services/musetalk.py（需 GPU）。
"""
import io
import logging
import os
import subprocess
import tempfile

import cv2
import numpy as np
from PIL import Image
from rembg import new_session, remove

import config
from services import minio_util

logger = logging.getLogger(__name__)

_matting_session = None
_matting_session_model = None


def _get_matting_session(model_name: str):
    """懒加载并缓存 rembg 会话，避免逐帧重复加载模型。"""
    global _matting_session, _matting_session_model
    if _matting_session is None or _matting_session_model != model_name:
        _matting_session = new_session(model_name)
        _matting_session_model = model_name
    return _matting_session


def _run_ffmpeg(args: list[str]) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *args]
    logger.info("ffmpeg %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


def _with_temp(in_bytes: bytes, suffix: str):
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(in_bytes)
    tmp.close()
    return tmp.name


def _read_and_clean(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def merge_video(inputs: list[str], output_key: str) -> str:
    """合并多个视频。inputs 为 MinIO 对象键列表。"""
    local_files = []
    try:
        for key in inputs:
            local = _with_temp(minio_util.download_bytes(key), ".mp4")
            local_files.append(local)
        out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
        list_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        for f in local_files:
            list_file.write(f"file '{f}'\n")
        list_file.close()
        _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", list_file.name, "-c", "copy", out])
        data = _read_and_clean(out)
        return minio_util.upload_bytes(output_key, data, "video/mp4")
    finally:
        for f in local_files:
            if os.path.exists(f):
                os.unlink(f)


def change_resolution(input_key: str, output_key: str, width: int, height: int) -> str:
    """调整分辨率/宽高比。"""
    src = _with_temp(minio_util.download_bytes(input_key), ".mp4")
    out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
    try:
        _run_ffmpeg(["-i", src, "-vf", f"scale={width}:{height}", out])
        data = _read_and_clean(out)
        return minio_util.upload_bytes(output_key, data, "video/mp4")
    finally:
        os.unlink(src)
        if os.path.exists(out):
            os.unlink(out)


def add_captions(input_key: str, output_key: str, captions: list, font_size: int = 24, font_color: str = "white") -> str:
    """给视频加定时字幕。captions 为 [{text, start(ms), end(ms)}]，用 ASS 渲染（底部自动换行）。"""
    src = _with_temp(minio_util.download_bytes(input_key), ".mp4")
    out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
    ass = tempfile.NamedTemporaryFile(mode="w", suffix=".ass", delete=False, encoding="utf-8")
    ass.write(_build_ass(captions, font_size, font_color))
    ass.close()
    try:
        _run_ffmpeg([
            "-i", src,
            "-vf", f"subtitles='{ass.name}'",
            "-c:a", "copy", out,
        ])
        data = _read_and_clean(out)
        return minio_util.upload_bytes(output_key, data, "video/mp4")
    finally:
        os.unlink(src)
        if os.path.exists(out):
            os.unlink(out)
        if os.path.exists(ass.name):
            os.unlink(ass.name)


def _estimate_text_width(text: str, font_size: int) -> float:
    """估算字幕文本宽度（像素）：CJK 按 1 个 font_size，其余按约 0.55 个。"""
    w = 0.0
    for ch in text:
        w += font_size if ord(ch) > 0x2E80 else font_size * 0.55
    return w


def _build_ass(captions: list, font_size: int, font_color: str) -> str:
    primary = _to_ass_color(font_color)
    playres_x, playres_y = 1920, 1080
    margin_v = 40  # 与 Style 的 MarginV 一致
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {playres_x}\n"
        f"PlayResY: {playres_y}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,PingFang SC,{font_size},{primary},&H000000FF,&H00000000,&H00000000,"
        f"0,0,0,0,100,100,0,0,1,1,0,2,60,60,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    # 字幕显示区限制为屏宽 80%；超长文本在该区域内从右向左滚动
    max_width = int(playres_x * 0.8)
    left = (playres_x - max_width) // 2
    right = left + max_width
    y = playres_y - margin_v - font_size
    lines = [header]
    for cap in captions or []:
        text = str(cap.get("text", "") or "").strip()
        text = text.replace("\n", " ").replace("\r", " ")  # 合并为单行，避免多行一起滚动
        if not text:
            continue
        start = int(cap.get("start", 0) or 0)
        end = int(cap.get("end", start + 3000) or start + 3000)
        if end <= start:
            end = start + 1000
        width = _estimate_text_width(text, font_size)
        text = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
        if width > max_width:
            # 只滚动「超出部分」：文本从头显示到尾，滚动速度与朗读时长匹配
            overflow = int(width) - max_width
            text = (
                f"{{\\clip({left},0,{right},{playres_y})"
                f"\\move({left},{y},{left - overflow},{y})}}{text}"
            )
        lines.append(f"Dialogue: 0,{_fmt_ass_time(start)},{_fmt_ass_time(end)},Default,,0,0,0,,{text}")
    return "\n".join(lines)


def _fmt_ass_time(ms: int) -> str:
    ms = max(int(ms), 0)
    h = ms // 3600000
    m = (ms % 3600000) // 60000
    s = (ms % 60000) // 1000
    cs = (ms % 1000) // 10
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _to_ass_color(color: str) -> str:
    """把 'white' 或 '#RRGGBB' 转成 ASS 颜色 &H00BBGGRR。"""
    c = (color or "white").strip()
    if c.startswith("#") and len(c) == 7:
        r, g, b = c[1:3], c[3:5], c[5:7]
        return f"&H00{b}{g}{r}"
    return "&H00FFFFFF"


def get_first_frame(input_key: str, output_key: str) -> str:
    """提取视频首帧图片。"""
    src = _with_temp(minio_util.download_bytes(input_key), ".mp4")
    out = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
    try:
        _run_ffmpeg(["-i", src, "-frames:v", "1", out])
        data = _read_and_clean(out)
        return minio_util.upload_bytes(output_key, data, "image/jpeg")
    finally:
        os.unlink(src)
        if os.path.exists(out):
            os.unlink(out)


def change_image_or_matting(input_key: str, output_key: str) -> str:
    """抠图 / 背景替换（MuseTalk 桩）。

    TODO: 接入真实抠图模型（如 BiRefNet / RMBG），或 MuseTalk 数字人合成。
    这里先原样复制文件，保证流程跑通。
    """
    data = minio_util.download_bytes(input_key)
    return minio_util.upload_bytes(output_key, data)


def matting_image(input_key: str, output_key: str) -> str:
    """抠图：提取视频首帧 -> rembg 去背景 -> RGBA PNG。"""
    src = _with_temp(minio_util.download_bytes(input_key), ".mp4")
    frame = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
    try:
        _run_ffmpeg(["-i", src, "-frames:v", "1", frame])
        with open(frame, "rb") as f:
            out = remove(f.read(), session=_get_matting_session(config.MATTING_MODEL))
        return minio_util.upload_bytes(output_key, out, "image/png")
    finally:
        os.unlink(src)
        if os.path.exists(frame):
            os.unlink(frame)


def _as_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _to_object_key(path: str) -> str:
    if not path:
        return ""
    if "://" in path:
        path = path.split("/", 3)[-1] if path.count("/") >= 3 else path
    return path.lstrip("/")


def loop_video(ref_key: str, length_ms: int, output_key: str) -> str:
    """把参考视频循环/裁剪到指定时长（毫秒），输出到 MinIO output_key。"""
    ref = _with_temp(minio_util.download_bytes(ref_key), ".mp4")
    out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
    try:
        seconds = max(_as_int(length_ms), 1) / 1000.0
        _run_ffmpeg(["-stream_loop", "-1", "-i", ref, "-t", f"{seconds:.3f}", "-c:v", "copy", "-an", out])
        data = _read_and_clean(out)
        return minio_util.upload_bytes(output_key, data, "video/mp4")
    finally:
        os.unlink(ref)
        if os.path.exists(out):
            os.unlink(out)


def composite_video(background_key: str, layers: list, length_ms: int, output_key: str,
                    canvas_w: int = 1920, canvas_h: int = 1080) -> str:
    """图层合成：背景图 + 各图层按 layerOrder 依次叠加，输出 video/mp4。

    layerType == "1" 为数字人视频（带语音音轨，作为输出音轨来源）；
    "3" 为 PPT 图片，"2" 为前景装饰，"7" 为动态背景视频。没有可见图层时退回背景图视频。
    """
    length_ms = max(_as_int(length_ms), 1)
    seconds = length_ms / 1000.0

    visible = [l for l in (layers or []) if str(l.get("isShow", "1")) != "0"]
    visible.sort(key=lambda l: _as_int(l.get("layerOrder")))

    bg_path = _download(background_key)

    items = []  # (layer, local_path, is_image, is_human)
    human_audio_idx = None
    for l in visible:
        p = _download(l.get("layer_path", ""))
        if not p:
            continue
        is_image = _is_image(l.get("layer_path", ""))
        is_human = str(l.get("layerType")) == "1"
        items.append((l, p, is_image, is_human))

    if not items:
        return _image_to_video(background_key, length_ms, output_key, canvas_w, canvas_h)

    out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
    try:
        args = []
        parts = []
        # input 0: 背景
        if bg_path:
            args += ["-loop", "1", "-i", bg_path]
        else:
            args += ["-f", "lavfi", "-i", f"color=c=black:s={canvas_w}x{canvas_h}"]
        parts.append(f"[0:v]scale={canvas_w}:{canvas_h}[bg0]")
        prev = "bg0"

        idx = 1
        for l, p, is_image, is_human in items:
            layer_type = str(l.get("layerType"))
            if is_image:
                args += ["-loop", "1", "-i", p]
            else:
                args += ["-i", p]
            if layer_type == "3":
                # PPT 图层铺满画布
                boxw, boxh, boxx, boxy = canvas_w, canvas_h, 0, 0
            else:
                boxw = _as_int(l.get("boxw")) or canvas_w
                boxh = _as_int(l.get("boxh")) or canvas_h
                boxx = _as_int(l.get("boxx"))
                boxy = _as_int(l.get("boxy"))
            parts.append(f"[{idx}:v]scale={boxw}:{boxh}[L{idx}]")
            if is_human:
                # 数字人抠图：去除绿幕背景
                parts.append(f"[L{idx}]colorkey=0x00FF00:0.3:0.1[C{idx}]")
                overlay_src = f"C{idx}"
            else:
                overlay_src = f"L{idx}"
            parts.append(f"[{prev}][{overlay_src}]overlay={boxx}:{boxy}[o{idx}]")
            prev = f"o{idx}"
            if is_human:
                human_audio_idx = idx
            idx += 1

        parts.append(f"[{prev}]format=yuv420p[v]")
        args += ["-filter_complex", ";".join(parts), "-map", "[v]"]
        if human_audio_idx is not None:
            args += ["-map", f"{human_audio_idx}:a?", "-c:a", "aac"]
        args += ["-t", f"{seconds:.3f}", "-c:v", "libx264", "-pix_fmt", "yuv420p", out]

        _run_ffmpeg(args)
        data = _read_and_clean(out)
        return minio_util.upload_bytes(output_key, data, "video/mp4")
    finally:
        for p in [bg_path] + [it[1] for it in items]:
            if p and os.path.exists(p):
                os.unlink(p)
        if os.path.exists(out):
            os.unlink(out)


def _is_image(key: str) -> bool:
    ext = (os.path.splitext(key or "")[1] or "").lower()
    return ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".svg")


def _image_to_video(image_key: str, length_ms: int, output_key: str,
                    canvas_w: int = 1920, canvas_h: int = 1080) -> str:
    img = _download(image_key)
    out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
    try:
        seconds = max(_as_int(length_ms), 1) / 1000.0
        if img:
            _run_ffmpeg([
                "-loop", "1", "-i", img, "-t", f"{seconds:.3f}",
                "-vf", f"scale={canvas_w}:{canvas_h}", "-c:v", "libx264", "-pix_fmt", "yuv420p", out,
            ])
        else:
            _run_ffmpeg([
                "-f", "lavfi", "-i", f"color=c=black:s={canvas_w}x{canvas_h}",
                "-t", f"{seconds:.3f}", "-c:v", "libx264", "-pix_fmt", "yuv420p", out,
            ])
        data = _read_and_clean(out)
        return minio_util.upload_bytes(output_key, data, "video/mp4")
    finally:
        if img and os.path.exists(img):
            os.unlink(img)
        if os.path.exists(out):
            os.unlink(out)


def _download(key: str):
    key = _to_object_key(key)
    if not key:
        return None
    ext = os.path.splitext(key)[1] or ".bin"
    try:
        return _with_temp(minio_util.download_bytes(key), ext)
    except Exception:  # noqa: BLE001
        logger.warning("download failed (missing object) %s", key)
        return None


def matting_video(input_key: str, output_key: str, bg_color=(0, 255, 0)) -> str:
    """抠像视频：逐帧 rembg 去背景 -> 合成纯色(绿幕)背景 -> MP4。

    MP4 不支持 alpha 通道，故用绿幕承载透明区域，下游可 chroma-key 还原。
    逐帧 CPU 推理较慢，长视频耗时按帧数线性增长。
    """
    src = _with_temp(minio_util.download_bytes(input_key), ".mp4")
    out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
    try:
        session = _get_matting_session(config.MATTING_MODEL)
        cap = cv2.VideoCapture(src)
        if not cap.isOpened():
            raise RuntimeError("无法读取视频")
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        writer = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                buf = io.BytesIO()
                pil.save(buf, format="PNG")
                rgba = Image.open(io.BytesIO(remove(buf.getvalue(), session=session))).convert("RGBA")
                bg = Image.new("RGB", rgba.size, bg_color)
                bg.paste(rgba, mask=rgba.split()[3])
                writer.write(cv2.cvtColor(np.array(bg), cv2.COLOR_RGB2BGR))
        finally:
            cap.release()
            writer.release()
        data = _read_and_clean(out)
        return minio_util.upload_bytes(output_key, data, "video/mp4")
    finally:
        os.unlink(src)
        if os.path.exists(out):
            os.unlink(out)
