"""工具箱：把散落的音视频处理能力收拢成统一注册表。

每个工具是一个函数 `run(params: dict) -> dict`，params 用 MinIO 对象键传递输入，
输出写入 MinIO 后返回结果字典。前端可通过 /tools 列表 + /tools/run 通用调度，
无需为每个工具单独写接口。
"""
import logging

from services import minio_util, video, voice, wav2lip

logger = logging.getLogger(__name__)

TOOLS = {}


def register_tool(name: str, description: str, params: list):
    """注册一个工具。params 为 [{name, type, required, description}]。"""

    def deco(func):
        TOOLS[name] = {"name": name, "description": description, "params": params, "run": func}
        return func

    return deco


def list_tools() -> list:
    return [
        {"name": v["name"], "description": v["description"], "params": v["params"]}
        for v in TOOLS.values()
    ]


def run_tool(name: str, params: dict) -> dict:
    tool = TOOLS.get(name)
    if tool is None:
        raise RuntimeError(f"未知工具: {name}（可选: {', '.join(TOOLS)}）")
    return tool["run"](params or {})


# ---------- 注册工具 ----------

@register_tool("merge_video", "合并多个视频片段", [
    {"name": "inputs", "type": "array", "required": True, "description": "输入视频 MinIO 键列表"},
    {"name": "output_key", "type": "string", "required": True, "description": "输出视频 MinIO 键"},
])
def _tool_merge_video(params):
    key = video.merge_video(params["inputs"], params["output_key"])
    return {"output_key": key}


@register_tool("change_resolution", "调整视频分辨率/宽高比", [
    {"name": "input_key", "type": "string", "required": True, "description": "输入视频 MinIO 键"},
    {"name": "width", "type": "int", "required": True},
    {"name": "height", "type": "int", "required": True},
    {"name": "output_key", "type": "string", "required": True, "description": "输出视频 MinIO 键"},
])
def _tool_change_resolution(params):
    key = video.change_resolution(
        params["input_key"], params["output_key"], int(params["width"]), int(params["height"])
    )
    return {"output_key": key}


@register_tool("get_first_frame", "提取视频首帧图片", [
    {"name": "input_key", "type": "string", "required": True, "description": "输入视频 MinIO 键"},
    {"name": "output_key", "type": "string", "required": True, "description": "输出图片 MinIO 键"},
])
def _tool_get_first_frame(params):
    key = video.get_first_frame(params["input_key"], params["output_key"])
    return {"output_key": key}


@register_tool("add_captions", "给视频烧录字幕", [
    {"name": "input_key", "type": "string", "required": True, "description": "输入视频 MinIO 键"},
    {"name": "captions", "type": "array", "required": True, "description": "[{text,start,end(ms)}]"},
    {"name": "font_size", "type": "int", "required": False},
    {"name": "font_color", "type": "string", "required": False},
    {"name": "output_key", "type": "string", "required": True, "description": "输出视频 MinIO 键"},
])
def _tool_add_captions(params):
    font_size = int(params.get("font_size") or 24)
    font_color = params.get("font_color") or "white"
    key = video.add_captions(
        params["input_key"], params["output_key"], params["captions"], font_size, font_color
    )
    return {"output_key": key}


@register_tool("matting_image", "抠图（提取首帧去背景，输出透明 PNG）", [
    {"name": "input_key", "type": "string", "required": True, "description": "输入视频 MinIO 键"},
    {"name": "output_key", "type": "string", "required": True, "description": "输出 PNG MinIO 键"},
])
def _tool_matting_image(params):
    key = video.matting_image(params["input_key"], params["output_key"])
    return {"output_key": key}


@register_tool("matting_video", "抠像（逐帧去背景合成绿幕视频）", [
    {"name": "input_key", "type": "string", "required": True, "description": "输入视频 MinIO 键"},
    {"name": "output_key", "type": "string", "required": True, "description": "输出视频 MinIO 键"},
])
def _tool_matting_video(params):
    key = video.matting_video(params["input_key"], params["output_key"])
    return {"output_key": key}


@register_tool("loop_video", "把视频循环/裁剪到指定时长", [
    {"name": "input_key", "type": "string", "required": True, "description": "输入视频 MinIO 键"},
    {"name": "length_ms", "type": "int", "required": True, "description": "目标时长（毫秒）"},
    {"name": "output_key", "type": "string", "required": True, "description": "输出视频 MinIO 键"},
])
def _tool_loop_video(params):
    key = video.loop_video(params["input_key"], int(params["length_ms"]), params["output_key"])
    return {"output_key": key}


@register_tool("asr", "语音转文字（faster-whisper）", [
    {"name": "audio_key", "type": "string", "required": True, "description": "输入音频 MinIO 键"},
])
def _tool_asr(params):
    data = minio_util.download_bytes(params["audio_key"])
    text = voice.asr(data)
    return {"text": text}


@register_tool("asr_subtitles", "语音转字幕（带时间戳，输出 SRT）", [
    {"name": "audio_key", "type": "string", "required": True, "description": "输入音频 MinIO 键"},
])
def _tool_asr_subtitles(params):
    data = minio_util.download_bytes(params["audio_key"])
    segments = voice.asr_segments(data)
    return {"segments": segments, "srt": voice.srt_from_segments(segments)}


@register_tool("replace_audio", "声音替换（把视频音轨替换为合成音频，不做口型）", [
    {"name": "video_key", "type": "string", "required": True, "description": "输入视频 MinIO 键"},
    {"name": "audio_key", "type": "string", "required": True, "description": "新音频 MinIO 键"},
    {"name": "output_key", "type": "string", "required": True, "description": "输出视频 MinIO 键"},
])
def _tool_replace_audio(params):
    key = wav2lip.change_video_passthrough(params["video_key"], params["audio_key"], params["output_key"])
    return {"output_key": key}
