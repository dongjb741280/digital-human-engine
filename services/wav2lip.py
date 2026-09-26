"""Wav2Lip 数字人口型合成（CPU 可跑）。

Wav2Lip 是判别式唇形同步模型，无需 GPU，CPU 上可运行（较慢）。
依赖本地 Wav2Lip 仓库 + 权重，通过子进程调用仓库的 inference.py。

部署：
  1. git clone https://github.com/Rudrabha/Wav2Lip.git
  2. 下载权重：
     - checkpoints/wav2lip.pth（~220M，也可用 wav2lip_gan.pth 质量更好）
     - face_detection/detection/sfd/s3fd.pth（~330M 人脸检测）
  3. 配置 WAV2LIP_HOME / WAV2LIP_CHECKPOINT / WAV2LIP_FACE_DET
"""
import logging
import os
import subprocess
import tempfile

import config
from services import minio_util

logger = logging.getLogger(__name__)


def _with_temp(data: bytes, suffix: str) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(data)
    tmp.close()
    return tmp.name


def change_video(video_key: str, audio_key: str, output_key: str) -> str:
    """人像视频 + 音频 -> 口型合成视频（Wav2Lip）。"""
    if not config.WAV2LIP_HOME:
        raise RuntimeError("未配置 Wav2Lip 仓库路径（WAV2LIP_HOME）")
    video_path = _with_temp(minio_util.download_bytes(video_key), ".mp4")
    audio_path = _with_temp(minio_util.download_bytes(audio_key), ".wav")
    out_path = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
    python = config.WAV2LIP_PYTHON or os.path.join(config.WAV2LIP_HOME, ".venv", "bin", "python")
    try:
        cmd = [
            python, "inference.py",
            "--checkpoint_path", config.WAV2LIP_CHECKPOINT,
            "--face", video_path,
            "--audio", audio_path,
            "--outfile", out_path,
            "--wav2lip_batch_size", str(config.WAV2LIP_BATCH_SIZE),
        ]
        logger.info("wav2lip %s", " ".join(cmd))
        subprocess.run(cmd, cwd=config.WAV2LIP_HOME, check=True)
        with open(out_path, "rb") as f:
            data = f.read()
        return minio_util.upload_bytes(output_key, data, "video/mp4")
    finally:
        for p in (video_path, audio_path):
            if os.path.exists(p):
                os.unlink(p)
        if os.path.exists(out_path):
            os.unlink(out_path)


def change_video_passthrough(video_key: str, audio_key: str, output_key: str) -> str:
    """不跑口型合成，直接把语音音轨合成到人像视频上（快速占位，用于无 GPU 跑通流程）。"""
    video_path = _with_temp(minio_util.download_bytes(video_key), ".mp4")
    audio_path = _with_temp(minio_util.download_bytes(audio_key), ".wav")
    out_path = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
    try:
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", video_path, "-i", audio_path,
            "-c:v", "copy", "-c:a", "aac", "-shortest", out_path,
        ], check=True)
        with open(out_path, "rb") as f:
            data = f.read()
        return minio_util.upload_bytes(output_key, data, "video/mp4")
    finally:
        for p in (video_path, audio_path):
            if os.path.exists(p):
                os.unlink(p)
        if os.path.exists(out_path):
            os.unlink(out_path)
