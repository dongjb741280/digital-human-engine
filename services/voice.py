"""语音子系统：ASR、TTS、声音训练（GPT-SoVITS 桩）。

说明：
- ASR 用 faster-whisper（CPU 可跑，中文可用）。
- TTS 用 edge-tts（免费、无需 GPU）。
- 声音训练（GPT-SoVITS）需要模型权重 + GPU，这里先返回占位模型名，
  让 Java 侧流程能跑通；接入真实 GPT-SoVITS 时替换 train_voice()。
"""
import io
import logging
import tempfile
import os
import wave

import av
import edge_tts
import httpx
import numpy as np

import config
from services import minio_util

logger = logging.getLogger(__name__)

# faster-whisper 懒加载（首次会下载模型）
_whisper_model = None
# OpenCC 懒加载（繁→简）
_opencc = None


def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(config.ASR_MODEL, device="cpu", compute_type="int8")
    return _whisper_model


def _get_opencc():
    global _opencc
    if _opencc is None:
        from opencc import OpenCC
        _opencc = OpenCC("t2s")
    return _opencc


def asr_segments(audio_bytes: bytes) -> list:
    """语音转文字，返回带时间戳的片段 [{text, start, end}]（毫秒）。

    faster-whisper 每个 segment 天然带 start/end，可直接喂给 add_captions 生成字幕。
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.write(audio_bytes)
    tmp.close()
    try:
        model = _get_whisper()
        segments, _info = model.transcribe(tmp.name, language="zh")
        cc = _get_opencc()
        result = []
        for seg in segments:
            text = cc.convert(seg.text.strip())
            if text:
                result.append({"text": text, "start": int(seg.start * 1000), "end": int(seg.end * 1000)})
        return result
    finally:
        os.unlink(tmp.name)


def asr(audio_bytes: bytes) -> str:
    """语音转文字。audio_bytes 为音频文件内容（纯文本，拼接所有片段）。"""
    return "".join(s["text"] for s in asr_segments(audio_bytes)).strip()


def srt_from_segments(segments: list) -> str:
    """把 [{text, start, end}]（毫秒）组装成 SRT 字幕文本。"""
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{_fmt_srt_time(seg['start'])} --> {_fmt_srt_time(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines)


def _fmt_srt_time(ms: int) -> str:
    ms = max(int(ms), 0)
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


async def _edge_tts(text: str, voice: str, out_path: str) -> None:
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_path)


# TTS provider 注册表：模型名 -> async 合成函数(params) -> bytes
TTS_PROVIDERS = {}


def register_tts(name: str):
    def deco(fn):
        TTS_PROVIDERS[name] = fn
        return fn
    return deco


@register_tts("gpt_sovits")
async def _gpt_sovits_tts(params: dict) -> bytes:
    """GPT-SoVITS zero-shot 合成。ref_audio_path 为 MinIO 键时先下载到本地。"""
    # 只保留 api_v2.py 认识且类型匹配的参数
    gs_params = {
        k: params[k]
        for k in (
            "text", "text_lang", "ref_audio_path", "prompt_text", "prompt_lang",
            "speed_factor", "text_split_method", "batch_size", "media_type",
        )
        if params.get(k) not in (None, "")
    }

    # ref_audio_path 是 MinIO 对象键时，下载到本地临时文件
    ref_key = gs_params.get("ref_audio_path")
    tmp_ref = None
    if ref_key and not os.path.isfile(ref_key):
        tmp_ref = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_ref.write(minio_util.download_bytes(ref_key))
        tmp_ref.close()
        gs_params["ref_audio_path"] = tmp_ref.name

    try:
        url = f"{config.GPT_SOVITS_API}/tts"
        async with httpx.AsyncClient(timeout=config.GPT_SOVITS_TIMEOUT) as client:
            resp = await client.get(url, params=gs_params)
            resp.raise_for_status()
            return resp.content
    finally:
        if tmp_ref is not None and os.path.exists(tmp_ref.name):
            os.unlink(tmp_ref.name)


@register_tts("edge_tts")
async def _edge_tts_tts(params: dict) -> bytes:
    """edge-tts 占位合成（GPT-SoVITS 未接入时的回退）。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()
    try:
        await _edge_tts(params.get("text", ""), config.TTS_VOICE, tmp.name)
        with open(tmp.name, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp.name)


async def tts(params: dict) -> bytes:
    """按 TTS_MODEL 选择合成引擎；非 edge_tts 失败时回退 edge_tts。"""
    primary = config.TTS_MODEL
    provider = TTS_PROVIDERS.get(primary)
    if provider is None:
        logger.warning("未知 TTS 模型 %s，回退 edge_tts", primary)
        provider = TTS_PROVIDERS["edge_tts"]
    try:
        return await provider(params)
    except Exception as e:  # noqa: BLE001
        if primary == "edge_tts":
            raise
        logger.warning("%s 合成失败，回退 edge_tts：%s", primary, e)
        return await TTS_PROVIDERS["edge_tts"](params)


def audio_duration_ms(audio_bytes: bytes) -> int:
    """返回音频时长（毫秒），兼容 wav/mp3；解析失败返回 0。"""
    try:
        container = av.open(io.BytesIO(audio_bytes))
        duration_us = container.duration  # AV_TIME_BASE，微秒
        container.close()
        if duration_us:
            return int(duration_us / 1000)
    except Exception as e:  # noqa: BLE001
        logger.warning("音频时长解析失败: %s", e)
    return 0


def _decode_audio_mono(audio_bytes: bytes, target_sr: int = 16000):
    """用 PyAV 解码为 target_sr 单声道 float32 数组（-1~1）。失败返回 (None, target_sr)。"""
    try:
        container = av.open(io.BytesIO(audio_bytes))
    except Exception as e:  # noqa: BLE001
        logger.warning("音频解码失败: %s", e)
        return None, target_sr
    try:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=target_sr)
        chunks = []
        for frame in container.decode(stream):
            for rframe in resampler.resample(frame):
                chunks.append(rframe.to_ndarray().reshape(-1))
        for rframe in resampler.resample(None):
            chunks.append(rframe.to_ndarray().reshape(-1))
        if not chunks:
            return None, target_sr
        y = np.concatenate(chunks).astype(np.float32) / 32768.0
        return y, target_sr
    except Exception as e:  # noqa: BLE001
        logger.warning("音频解码失败: %s", e)
        return None, target_sr
    finally:
        container.close()


def _encode_wav(y: np.ndarray, sr: int) -> bytes:
    """float32(-1~1) 数组编码为单声道 wav 字节。"""
    pcm = (np.clip(y, -1.0, 1.0) * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def _slice_reference_audio(audio_bytes: bytes) -> bytes:
    """从上传音频切出一段 3~10 秒的语音片段作为参考音频。切不出则原样返回。"""
    y, sr = _decode_audio_mono(audio_bytes)
    if y is None or len(y) < sr:  # 不足 1 秒
        return audio_bytes

    frame_len = int(sr * 0.03)  # 30ms 帧
    hop = frame_len // 2
    n = max(1, (len(y) - frame_len) // hop + 1)
    rms = np.array([np.sqrt(np.mean(y[i * hop:i * hop + frame_len] ** 2)) for i in range(n)])

    peak = float(rms.max())
    if peak < 1e-6:  # 近乎静音
        return audio_bytes

    # 活跃帧（峰值 -40dB 以上）
    active = rms > peak * 0.01
    idx = np.nonzero(active)[0]
    if idx.size == 0:
        return audio_bytes

    # 语音区 [start, end]（首尾活跃帧之间）
    start = int(idx[0]) * hop
    end = min(len(y), int(idx[-1]) * hop + frame_len)

    min_n, max_n = 3 * sr, 10 * sr
    if end - start >= min_n:
        if end - start > max_n:
            end = start + max_n  # 太长，截断到 10s
    else:
        # 语音区不足 3s，向两端对称扩展
        start = max(0, start - (min_n - (end - start)) // 2)
        end = min(len(y), start + min_n)
        if end - start < min_n:
            return audio_bytes  # 音频太短，退回原音频

    return _encode_wav(y[start:end], sr)


def train_voice(voice_id: str, sample_object_key: str) -> dict:
    """声音复刻预处理：下载音频 -> 切片 -> ASR 得 prompt_text -> 上传参考音频。

    GPT-SoVITS 采用 zero-shot 推理：不训练每声音的独立模型，而是共用预训练
    基础权重 + 「参考音频 + prompt_text」来复刻音色。参考音频会先切成 3~10 秒
    的干净片段（与 GPT-SoVITS 对参考音频时长的要求一致）。
    """
    sample = minio_util.download_bytes(sample_object_key)
    ref_audio = _slice_reference_audio(sample)
    prompt_text = asr(ref_audio) if ref_audio else ""

    ref_key = f"voice/{voice_id}/ref/ref.wav"
    minio_util.upload_bytes(ref_key, ref_audio, "audio/wav")

    logger.info("train_voice voice_id=%s prompt_text=%s ref=%s", voice_id, prompt_text, ref_key)
    return {
        "gptName": config.GPT_SOVITS_GPT_WEIGHTS,
        "sovitsName": config.GPT_SOVITS_SOVITS_WEIGHTS,
        "wavName": ref_key,
        "promptText": prompt_text,
    }
