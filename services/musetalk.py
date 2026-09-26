"""MuseTalk 数字人口型合成（需 GPU）。

MuseTalk 是 Stable Diffusion 系模型，需要 GPU + 预训练权重，无法在本机 CPU 上运行。
本模块对接一个独立部署的 MuseTalk 推理服务（HTTP），地址见 config.MUSETALK_API。

GPU 侧服务需实现如下契约：

    POST {MUSETALK_API}/ai/changeVideo
    请求 JSON: {"video_path": <MinIO 人像视频对象键>, "audio_path": <MinIO 音频对象键>}
    响应 JSON: {"code": "0000", "output_file": <MinIO 合成视频对象键>}
              （失败时 code != "0000"，msg 说明原因）

合成结果由 GPU 侧上传到 MinIO 后返回对象键，本模块再搬运到目标对象键。
"""
import logging

import httpx

import config
from services import minio_util

logger = logging.getLogger(__name__)


def _require_service() -> str:
    if not config.MUSETALK_API:
        raise RuntimeError("MuseTalk 服务未部署（MUSETALK_API 为空，需 GPU）")
    return config.MUSETALK_API


def change_video(video_key: str, audio_key: str, output_key: str) -> str:
    """语音驱动数字人：人像视频 + 音频 -> 口型合成视频。"""
    api = _require_service()
    resp = httpx.post(
        f"{api}/ai/changeVideo",
        json={"video_path": video_key, "audio_path": audio_key},
        timeout=config.MUSETALK_TIMEOUT,
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("code") not in ("0000", 0, None):
        raise RuntimeError(f"MuseTalk 合成失败: {result.get('msg')}")
    result_key = result.get("output_file") or result.get("outputFile")
    if not result_key:
        raise RuntimeError("MuseTalk 服务未返回 output_file")
    data = minio_util.download_bytes(result_key)
    return minio_util.upload_bytes(output_key, data, "video/mp4")
