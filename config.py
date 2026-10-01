"""配置。可通过环境变量覆盖，默认对应本地开发环境。"""
import os

from dotenv import load_dotenv

load_dotenv()  # 加载 .env 中的本地密钥（LLM_API_KEY 等）

# HuggingFace 镜像（国内网络加速 faster-whisper 模型下载）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# 禁用 Xet 后端，走普通 LFS 下载（Xet 的 cas-server 在国内常 401）
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# MinIO（与 Java 侧 digital-ability.minio* 对应）
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "aidigital")

# Java digital-server 地址（用于回调 updateVoice / updateVoiceBypython）
JAVA_SERVER = os.getenv("JAVA_SERVER", "http://localhost:48083")

# ASR 模型（faster-whisper 的模型名）
ASR_MODEL = os.getenv("ASR_MODEL", "medium")

# edge-tts 音色（默认中文女声；仅作为 GPT-SoVITS 未接入时的回退）
TTS_VOICE = os.getenv("TTS_VOICE", "zh-CN-XiaoxiaoNeural")

# 抠像模型（rembg 的模型名：u2net / isnet-general-use / birefnet-general）
MATTING_MODEL = os.getenv("MATTING_MODEL", "u2net")

# 数字人口型合成模型：wav2lip（CPU 可跑，默认）| musetalk（需 GPU）
LIPSYNC_MODEL = os.getenv("LIPSYNC_MODEL", "wav2lip")

# Wav2Lip 本地推理配置（LIPSYNC_MODEL=wav2lip 时使用）
WAV2LIP_HOME = os.getenv("WAV2LIP_HOME", "")  # Wav2Lip 仓库根目录
WAV2LIP_PYTHON = os.getenv("WAV2LIP_PYTHON", "")  # Wav2Lip venv 的 python，留空则用 <WAV2LIP_HOME>/.venv/bin/python
WAV2LIP_CHECKPOINT = os.getenv("WAV2LIP_CHECKPOINT", "checkpoints/wav2lip_gan.pth")
WAV2LIP_FACE_DET = os.getenv("WAV2LIP_FACE_DET", "face_detection/detection/sfd/s3fd.pth")
WAV2LIP_BATCH_SIZE = os.getenv("WAV2LIP_BATCH_SIZE", "16")

# MuseTalk 数字人口型合成服务地址（需 GPU，本机 CPU 无法运行）。
# 仅 LIPSYNC_MODEL=musetalk 时使用；留空表示未部署。
MUSETALK_API = os.getenv("MUSETALK_API", "")
MUSETALK_TIMEOUT = float(os.getenv("MUSETALK_TIMEOUT", "600"))

# LLM 服务（OpenAI 兼容，用于文案/PPT 生成）
LLM_API_BASE = os.getenv("LLM_API_BASE", "https://ai-route.huihaohealth.com")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "claude-opus-4-7-cc")

# ppt-master 引擎（/generate_ppt_master）。给 LLM 的 bash 工具无 OS 级沙箱，默认关闭；
# 仅可信内网显式开启（PPT_MASTER_ENABLED=1）。
PPT_MASTER_ENABLED = os.getenv("PPT_MASTER_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")

# GPT-SoVITS 推理 API 地址（GPT-SoVITS 项目 api.py 启动的服务）
GPT_SOVITS_API = os.getenv("GPT_SOVITS_API", "http://127.0.0.1:9880")
# 推理超时（秒），合成较长文本需要更大值
GPT_SOVITS_TIMEOUT = float(os.getenv("GPT_SOVITS_TIMEOUT", "120"))
# GPT-SoVITS 预训练基础权重（zero-shot 推理所有声音共用，路径需 GPT-SoVITS 侧可访问）
GPT_SOVITS_GPT_WEIGHTS = os.getenv("GPT_SOVITS_GPT_WEIGHTS", "GPT_weights_v2/pretrained.ckpt")
GPT_SOVITS_SOVITS_WEIGHTS = os.getenv("GPT_SOVITS_SOVITS_WEIGHTS", "SoVITS_weights_v2/pretrained.pth")
