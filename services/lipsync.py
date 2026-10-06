"""口型合成 provider 注册表。

统一口型合成（数字人驱动）的调度入口。新增口型模型时，只需在
`LIPSYNC_PROVIDERS` 里注册一个 `change_video(video_key, audio_key, output_key) -> str`
函数，调用方（app.py）无需改动。
"""
import logging

import config
from services import musetalk, wav2lip

logger = logging.getLogger(__name__)

# 口型合成 provider 注册表：模型名 -> change_video 函数
LIPSYNC_PROVIDERS = {
    "wav2lip": wav2lip.change_video,
    "musetalk": musetalk.change_video,
    "passthrough": wav2lip.change_video_passthrough,
}


def change_video(video_key: str, audio_key: str, output_key: str) -> str:
    """按 LIPSYNC_MODEL 调度口型合成。"""
    name = config.LIPSYNC_MODEL
    provider = LIPSYNC_PROVIDERS.get(name)
    if provider is None:
        raise RuntimeError(f"未知的口型合成模型: {name}（可选: {', '.join(LIPSYNC_PROVIDERS)}）")
    return provider(video_key, audio_key, output_key)
