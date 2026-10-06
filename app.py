"""数字人平台 Python 后端。

实现 Java 侧通过 HTTP 调用的所有接口。启动：
    uvicorn app:app --host 0.0.0.0 --port 60013

端口需与 Java 侧 digital-ability 配置中的各 URL 端口对应。
当前单进程承载全部路径，Java 各 URL 指向不同端口也可，用 nginx 或
多个进程按需拆分为多个端口即可。
"""
import asyncio
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

import config
from services import lipsync, llm, minio_util, musetalk, ppt, ppt_master, tools, voice, video, wav2lip, wopi

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AI Digital Python Backend")

# 浏览器直连测试用（开发环境放开；生产按需收紧来源）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

JAVA_UPDATE_VOICE = "/digital-api/system/voiceManager/updateVoice"
JAVA_UPDATE_VOICE_FINAL = "/digital-api/system/voiceManager/updateVoiceBypython"
JAVA_HUMAN_CALLBACK = "/digital-api/system/aiDhHuman/callBackAiDhHuman"
JAVA_UPDATE_RECORD_VIDEO = "/digital-api/system/aiDhHumanVideo/updateRecordVideo"


def callback_to_java(path: str, payload: dict) -> None:
    """回调 Java digital-server。"""
    url = f"{config.JAVA_SERVER}{path}"
    try:
        httpx.post(url, json=payload, timeout=10)
        logger.info("callback %s ok: %s", path, payload)
    except Exception as e:  # noqa: BLE001
        logger.error("callback %s failed: %s", path, e)


# media_type -> (文件后缀, content-type)
_MEDIA_EXT = {
    "wav": ("wav", "audio/wav"),
    "mp3": ("mp3", "audio/mpeg"),
    "ogg": ("ogg", "audio/ogg"),
    "flac": ("flac", "audio/flac"),
}


@app.on_event("startup")
def _startup() -> None:
    minio_util.ensure_bucket()


# ============ 语音 ============

@app.post("/ai/formatAudio")
async def format_audio(request: Request):
    """声音训练。Java voiceSave 异步调用。"""
    body = await request.json()
    voice_id = body.get("voiceId", "")
    file_org_path = body.get("fileOrgPath", "")
    # fileOrgPath 可能是 MinIO 完整 URL，截取对象键
    if "://" in file_org_path:
        file_org_path = file_org_path.split("/", 3)[-1] if file_org_path.count("/") >= 3 else file_org_path
    try:
        result = voice.train_voice(voice_id, file_org_path)
    except Exception as e:  # noqa: BLE001
        logger.exception("format_audio error")
        return {"code": "9999", "msg": str(e)}

    # 回调 Java：训练完成，状态 2
    callback_to_java(JAVA_UPDATE_VOICE, {
        "voiceId": voice_id,
        "voiceStatus": "2",
        "gptName": result["gptName"],
        "sovitsName": result["sovitsName"],
        "wavName": result["wavName"],
        "promptText": result["promptText"],
    })
    return {"code": "0000"}


@app.get("/tts")
async def tts(request: Request):
    """声音克隆 / 合成。透传 GPT-SoVITS 参数，转发到 GPT-SoVITS 推理 API。

    Java callVoiceClone 会携带 text、ref_audio_path、prompt_text、
    sovits_weights_path、gpt_weights_path、speed_factor 等参数。
    """
    params = dict(request.query_params)
    voice_id = params.pop("voiceId", "")
    voice_type = params.pop("voice_type", "1")
    params.pop("bucket_name", None)  # 仅 Java 侧使用，不透传给 GPT-SoVITS

    ext, content_type = _MEDIA_EXT.get(params.get("media_type", "wav"), ("wav", "audio/wav"))

    try:
        audio = await voice.tts(params)
        if voice_type == "2":
            # ppt 播报合成语音: voiceId = batch_pptId_pptNum_videoId
            parts = voice_id.split("_")
            if len(parts) >= 4:
                object_key = f"video/{parts[-1]}/{parts[0]}/voice/{parts[-2]}.{ext}"
            else:
                object_key = f"voice/{voice_id}/tts/{voice_id}.{ext}"
        else:
            object_key = f"voice/{voice_id}/tts/{voice_id}.{ext}"
        minio_util.upload_bytes(object_key, audio, content_type)
    except Exception as e:  # noqa: BLE001
        logger.exception("tts error")
        return {"code": "9999", "msg": str(e)}

    # 回调 Java：最终状态 4，携带 sample 路径与时长（毫秒）
    callback_to_java(JAVA_UPDATE_VOICE_FINAL, {
        "voiceId": voice_id,
        "voiceStatus": "4",
        "voiceSampleUrl": object_key,
        "voiceType": voice_type,
        "length": voice.audio_duration_ms(audio),
    })
    return {"code": "0000", "outputFile": object_key}


@app.post("/ai/difyChat")
async def dify_chat(request: Request):
    """Dify 兼容接口：供智能体 agent_api_url 指向本地，复用本地 LLM 生成答案。

    Java AiagentServiceImpl.question() 以 Dify 格式调用（inputs/query/response_mode/...），
    响应按 blocking 模式返回 {"answer": "..."}。
    """
    body = await request.json()
    query = body.get("query", "")
    inputs = body.get("inputs") or {}
    response_mode = body.get("response_mode", "blocking")
    system = ""
    if isinstance(inputs, dict):
        system = inputs.get("sys.query") or inputs.get("agentRole") or inputs.get("agent_role") or ""

    if response_mode == "streaming":
        def gen():
            try:
                for chunk in llm.chat_stream([{"role": "user", "content": query}], system=system or None):
                    yield f'data: {{"answer": {json.dumps(chunk, ensure_ascii=False)}}}\n\n'
            except Exception as e:  # noqa: BLE001
                logger.exception("difyChat stream error")

        return StreamingResponse(gen(), media_type="text/event-stream")

    try:
        answer = llm.chat([{"role": "user", "content": query}], system=system or None)
        return {"answer": answer}
    except Exception as e:  # noqa: BLE001
        logger.exception("difyChat error")
        return {"answer": ""}


@app.post("/ai/humanInteract")
async def human_interact(request: Request):
    """数字人互动口型（本地简化版）：文本 → TTS 合成语音 → 口型合成，返回合成视频地址。

    真实的口型+流媒体推送依赖远程数字人服务，这里接回本地做 TTS + 口型这一环。
    请求体与 Java sendMsgForHuman 对齐：{text, type, interrupt}。
    """
    body = await request.json()
    text = body.get("text", "")
    if not text:
        return {"answer": ""}
    try:
        # 1. TTS：合成语音（无参考音色时走 edge-tts 回退）
        audio = await voice.tts({"text": text})
        stamp = str(int(time.time() * 1000))
        voice_key = f"video/interact/voice/{stamp}.wav"
        minio_util.upload_bytes(voice_key, audio, "audio/wav")

        # 2. 口型合成：参考数字人视频 + 语音（passthrough，快速；真实口型可切 LIPSYNC_MODEL=wav2lip）
        ref_key = "1790222776175122/viedo/original/1790222776175122.mp4"
        out_key = f"video/interact/lipsync/{stamp}.mp4"
        await asyncio.to_thread(wav2lip.change_video_passthrough, ref_key, voice_key, out_key)

        return {
            "answer": f"{config.MINIO_ENDPOINT}/{config.MINIO_BUCKET}/{out_key}",
            "audioUrl": f"{config.MINIO_ENDPOINT}/{config.MINIO_BUCKET}/{voice_key}",
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("humanInteract error")
        return {"answer": ""}


@app.post("/conflate/makeAudio")
async def conflate_make_audio(request: Request):
    """声音克隆 POST 变体（保留兼容）。"""
    body = await request.json()
    if not isinstance(body, dict):
        return {"code": "9999"}
    voice_id = body.get("voiceId", body.get("voice_id", ""))
    text = body.get("text", body.get("voice_refer_conent", ""))
    voice_type = body.get("voice_type", body.get("voiceType", "1"))
    try:
        audio = await voice.tts({"text": text})
        object_key = f"voiceSample/{voice_id}_voice.mp3"
        minio_util.upload_bytes(object_key, audio, "audio/mpeg")
    except Exception as e:  # noqa: BLE001
        logger.exception("makeAudio error")
        return {"code": "9999", "msg": str(e)}
    callback_to_java(JAVA_UPDATE_VOICE_FINAL, {
        "voiceId": voice_id,
        "voiceStatus": "4",
        "voiceSampleUrl": object_key,
        "voiceType": voice_type,
    })
    return {"code": "0000", "outputFile": object_key}


@app.post("/ai/voice2txt")
async def voice2txt(request: Request):
    """语音转文字。"""
    body = await request.json()
    minio_path = body.get("minioPath", body.get("minio_path", ""))
    if "://" in minio_path:
        minio_path = minio_path.split("/", 3)[-1] if minio_path.count("/") >= 3 else minio_path
    try:
        data = minio_util.download_bytes(minio_path)
        text = voice.asr(data)
        return {"code": "0000", "text": text}
    except Exception as e:  # noqa: BLE001
        logger.exception("voice2txt error")
        return {"code": "9999", "msg": str(e)}


# ============ 视频 ============

@app.post("/aiDhHuman/changeImega")
async def change_imega(request: Request):
    """抠图（humanBg=0 保留背景，仅出图；桩）。

    remote_path / remote_image_path / file_name 均为 MinIO 对象键，
    由 Java 侧迁移 SFTP->MinIO 后传入。
    """
    body = await request.json()
    vid_id = str(body.get("id", ""))
    remote_path = body.get("remote_path", "")  # {id}/viedo/original
    file_name = body.get("file_name", "")  # {id}.mp4
    remote_image_path = body.get("remote_image_path", "")  # {id}/image
    video_key = f"{remote_path}/{file_name}"
    image_key = f"{remote_image_path}/{vid_id}_rgba.png"
    try:
        video.matting_image(video_key, image_key)
    except Exception as e:  # noqa: BLE001
        logger.exception("change_imega error")
        callback_to_java(JAVA_HUMAN_CALLBACK, {"id": vid_id, "operType": "2", "status": "3"})
        return {"code": "9999", "msg": str(e)}
    callback_to_java(JAVA_HUMAN_CALLBACK, {"id": vid_id, "operType": "2", "status": "4"})
    return {"code": "0000"}


@app.post("/aiDhHuman/changeImegaAndViedo")
async def change_imega_and_viedo(request: Request):
    """抠图 + 抠像视频（humanBg=1 去除背景；桩）。"""
    body = await request.json()
    vid_id = str(body.get("id", ""))
    remote_path = body.get("remote_path", "")  # {id}/viedo/original
    file_name = body.get("file_name", "")  # {id}.mp4
    remote_image_path = body.get("remote_image_path", "")  # {id}/image
    remote_viedo_path = body.get("remote_viedo_path", "")  # {id}/viedo/generatePath
    video_key = f"{remote_path}/{file_name}"
    image_key = f"{remote_image_path}/{vid_id}_rgba.png"
    gen_video_key = f"{remote_viedo_path}/{file_name}"
    try:
        video.matting_image(video_key, image_key)
        video.matting_video(video_key, gen_video_key)
    except Exception as e:  # noqa: BLE001
        logger.exception("change_imega_and_viedo error")
        callback_to_java(JAVA_HUMAN_CALLBACK, {"id": vid_id, "operType": "1", "status": "3"})
        return {"code": "9999", "msg": str(e)}
    callback_to_java(JAVA_HUMAN_CALLBACK, {"id": vid_id, "operType": "1", "status": "4"})
    return {"code": "0000"}


@app.post("/ai/mergeVideo")
async def merge_video(request: Request):
    """视频合成：把每段合成视频拼接成最终视频。"""
    body = await request.json()
    inputs = body.get("videoPaths", body.get("videoList", []))
    output = body.get("output_file", body.get("mergeOutputPath", "merge/out.mp4"))
    batch_video_id = body.get("batch_video_id", "")
    if not inputs:
        callback_to_java(JAVA_UPDATE_RECORD_VIDEO, {
            "video_type": "3", "execStatus": "2", "batch_video_id": batch_video_id, "mergeVideoOutPutDir": "",
        })
        return {"code": "9999", "msg": "missing videoPaths"}
    try:
        key = await asyncio.to_thread(video.merge_video, inputs, output)
        callback_to_java(JAVA_UPDATE_RECORD_VIDEO, {
            "video_type": "3", "execStatus": "1", "batch_video_id": batch_video_id, "mergeVideoOutPutDir": key,
        })
        return {"code": "0000", "outputFile": key}
    except Exception as e:  # noqa: BLE001
        logger.exception("mergeVideo error")
        callback_to_java(JAVA_UPDATE_RECORD_VIDEO, {
            "video_type": "3", "execStatus": "2", "batch_video_id": batch_video_id, "mergeVideoOutPutDir": "",
        })
        return {"code": "9999", "msg": str(e)}


@app.post("/ai/video_resolution")
async def video_resolution(request: Request):
    """分辨率/宽高比调整。"""
    body = await request.json()
    src = body.get("videoPath", body.get("inputPath", ""))
    dst = body.get("outputPath", body.get("videoOutPath", "resolution/out.mp4"))
    width = int(body.get("width", 1920))
    height = int(body.get("height", 1080))
    batch_video_id = body.get("voiceId", body.get("batch_video_id", ""))
    try:
        key = await asyncio.to_thread(video.change_resolution, src, dst, width, height)
        callback_to_java(JAVA_UPDATE_RECORD_VIDEO, {
            "video_type": "4", "execStatus": "1", "voiceId": batch_video_id, "videoOutPath": key,
        })
        return {"code": "0000", "outputFile": key}
    except Exception as e:  # noqa: BLE001
        logger.exception("video_resolution error")
        callback_to_java(JAVA_UPDATE_RECORD_VIDEO, {
            "video_type": "4", "execStatus": "2", "voiceId": batch_video_id, "videoOutPath": "",
        })
        return {"code": "9999", "msg": str(e)}


@app.post("/ai/addCaptions")
async def add_captions(request: Request):
    """加字幕。"""
    body = await request.json()
    src = body.get("minioVideoPath", body.get("videoPath", ""))
    captions = body.get("captions", [])
    batch_video_id = body.get("videoId", body.get("batch_video_id", ""))
    parts = batch_video_id.split("_")
    dst = body.get("captionsOutPath") or f"video/{parts[-1]}/{parts[0]}/final.mp4"
    try:
        font_size = int(body.get("fontSize") or 24)
    except (TypeError, ValueError):
        font_size = 24
    font_color = body.get("fontColor") or "white"
    try:
        key = await asyncio.to_thread(video.add_captions, src, dst, captions, font_size, font_color)
        callback_to_java(JAVA_UPDATE_RECORD_VIDEO, {
            "video_type": "5", "execStatus": "1", "batch_video_id": batch_video_id, "captionsOutPath": key,
        })
        return {"code": "0000", "outputFile": key}
    except Exception as e:  # noqa: BLE001
        logger.exception("addCaptions error")
        callback_to_java(JAVA_UPDATE_RECORD_VIDEO, {
            "video_type": "5", "execStatus": "2", "batch_video_id": batch_video_id, "captionsOutPath": "",
        })
        return {"code": "9999", "msg": str(e)}


@app.post("/ai/getFirstFrame")
async def get_first_frame(request: Request):
    """提取首帧。"""
    body = await request.json()
    src = body.get("videoPath", body.get("inputPath", ""))
    batch_video_id = body.get("voiceId", body.get("batch_video_id", ""))
    parts = batch_video_id.split("_")
    dst = body.get("firstFrameOutPath", f"video/{parts[-1]}/{parts[0]}/first_frame.jpg")
    try:
        key = await asyncio.to_thread(video.get_first_frame, src, dst)
        callback_to_java(JAVA_UPDATE_RECORD_VIDEO, {
            "video_type": "6", "execStatus": "1", "batch_video_id": batch_video_id, "firstFrameOutPath": key,
        })
        return {"code": "0000", "outputFile": key}
    except Exception as e:  # noqa: BLE001
        logger.exception("getFirstFrame error")
        callback_to_java(JAVA_UPDATE_RECORD_VIDEO, {
            "video_type": "6", "execStatus": "2", "batch_video_id": batch_video_id, "firstFrameOutPath": "",
        })
        return {"code": "9999", "msg": str(e)}


@app.post("/ai/changeVideo")
async def change_video(request: Request):
    """语音合成数字人视频（口型合成，LIPSYNC_MODEL 可配 wav2lip/musetalk）。"""
    body = await request.json()
    batch_pptid_pagenum = body.get("batch_pptid_pagenum", body.get("batchPptidPagenum", ""))
    # 视频 = 目录 + 文件名（Java 侧拆分传入）
    video_dir = body.get("human_generate_url", "")
    video_name = body.get("human_video_name", "")
    video_key = (video_dir.rstrip("/") + "/" + video_name) if video_name else body.get("video_path", "")
    audio_key = body.get("ppt_voice_url", body.get("audio_path", ""))
    result_dir = body.get("result_dir", "").rstrip("/")
    output_name = body.get("output_vid_name", f"{batch_pptid_pagenum}.mp4")
    output_key = f"{result_dir}/{output_name}" if result_dir else output_name
    try:
        key = await asyncio.to_thread(lipsync.change_video, video_key, audio_key, output_key)
        callback_to_java(JAVA_UPDATE_RECORD_VIDEO, {
            "video_type": "1", "execStatus": "1", "batchPptidPagenum": batch_pptid_pagenum, "voiceAddVideoOutPutDir": key,
        })
        return {"code": "0000", "outputFile": key}
    except Exception as e:  # noqa: BLE001
        logger.exception("changeVideo error")
        callback_to_java(JAVA_UPDATE_RECORD_VIDEO, {
            "video_type": "1", "execStatus": "2", "batchPptidPagenum": batch_pptid_pagenum, "voiceAddVideoOutPutDir": "",
        })
        return {"code": "9999", "msg": str(e)}


@app.post("/ai/changeVideoSplit")
async def change_video_split(request: Request):
    """语音合成数字人视频-分段（口型合成，LIPSYNC_MODEL 可配 wav2lip/musetalk）。"""
    body = await request.json()
    video_key = body.get("video_path") or body.get("human_generate_url", "")
    audio_key = body.get("audio_path") or body.get("ppt_voice_url", "")
    output_key = body.get("output_path") or body.get("output_vid_name", "")
    try:
        key = await asyncio.to_thread(lipsync.change_video, video_key, audio_key, output_key)
        return {"code": "0000", "outputFile": key}
    except Exception as e:  # noqa: BLE001
        logger.exception("changeVideoSplit error")
        return {"code": "9999", "msg": str(e)}


@app.post("/ai/deal_video")
async def deal_video(request: Request):
    """视频图层合成：背景图 + 数字人视频叠加。"""
    body = await request.json()
    voice_id = body.get("voiceId", "")
    video_out_path = body.get("videoOutPath", "")
    layers = body.get("layerList", [])
    voice_length = body.get("voiceLength", 0)
    background_key = body.get("ppt_background_path", "")
    canvas_w = int(body.get("canvas_width") or 1920)
    canvas_h = int(body.get("canvas_height") or 1080)
    try:
        key = await asyncio.to_thread(video.composite_video, background_key, layers, voice_length, video_out_path, canvas_w, canvas_h)
        callback_to_java(JAVA_UPDATE_RECORD_VIDEO, {
            "video_type": "2", "execStatus": "1", "voiceId": voice_id, "videoOutPath": key,
        })
        return {"code": "0000", "outputFile": key}
    except Exception as e:  # noqa: BLE001
        logger.exception("deal_video error")
        callback_to_java(JAVA_UPDATE_RECORD_VIDEO, {
            "video_type": "2", "execStatus": "2", "voiceId": voice_id, "videoOutPath": "",
        })
        return {"code": "9999", "msg": str(e)}


@app.post("/ai/makePptVoice2Video")
async def make_ppt_voice2video(request: Request):
    """PPT 语音转视频：把参考数字人视频按每段语音时长循环/裁剪成多段。"""
    body = await request.json()
    video_dir = body.get("humanGenerateUrl", "").rstrip("/")
    file_name = body.get("fileName", "")
    ref_key = f"{video_dir}/{file_name}" if video_dir else file_name
    lengths = body.get("pptvoiceLengths", [])
    ppt_nums = body.get("pptNums", [])
    ppt_batch = body.get("pptBatch", "")
    video_id = body.get("videoId", "")
    ppt_id = body.get("pptId", "")
    out_dir = body.get("outPutPath", "voicesplithuman/").rstrip("/")

    keys = []
    ids = []
    try:
        for i, ms in enumerate(lengths):
            ppt_num = ppt_nums[i] if i < len(ppt_nums) else i
            identifier = f"{ppt_batch}_{ppt_id}_{video_id}_{ppt_num}"
            key = f"{out_dir}/{ppt_num}.mp4"
            await asyncio.to_thread(video.loop_video, ref_key, int(ms), key)
            keys.append(key)
            ids.append(identifier)
        callback_to_java(JAVA_UPDATE_RECORD_VIDEO, {
            "video_type": "7",
            "execStatus": "1",
            "batch_pptid_videoid_list": ",".join(ids),
            "out_Put_Path_file": ",".join(keys),
        })
        return {"code": "0000", "outputFiles": keys}
    except Exception as e:  # noqa: BLE001
        logger.exception("makePptVoice2Video error")
        callback_to_java(JAVA_UPDATE_RECORD_VIDEO, {
            "video_type": "7",
            "execStatus": "2",
            "batch_pptid_videoid_list": "",
            "out_Put_Path_file": "",
        })
        return {"code": "9999", "msg": str(e)}


# ============ 文案 / PPT 生成（桩，需接真实 LLM/agent 服务） ============

@app.get("/getppt")
async def get_ppt():
    """模板查询：返回内置主题模板列表（含缩略图）。"""
    return {"code": "0000", "data": ppt.list_templates()}


@app.post("/generate_outline")
async def generate_outline(request: Request):
    """生成提纲（LLM）。"""
    body = await request.json()
    title = body.get("title", "")
    requirement = body.get("requirement", "")
    prompt = (
        f"请为以下主题生成一份课件提纲，用「一、二、三…」分级编号，覆盖引言、主体、总结。\n"
        f"标题：{title}\n"
        f"主题描述：{requirement}\n"
        f"只输出提纲本身，不要额外解释。"
    )
    try:
        text = llm.chat([{"role": "user", "content": prompt}], system=body.get("agentRole"))
        return {"code": "0000", "data": {"text": text, "conversation_id": "llm"}}
    except Exception as e:  # noqa: BLE001
        logger.exception("generate_outline error")
        return {"code": "9999", "msg": str(e)}


@app.post("/generate_body")
async def generate_body(request: Request):
    """生成课件/文案正文（LLM）。"""
    body = await request.json()
    title = body.get("title", "")
    outline = body.get("outline", "")
    prompt = (
        f"请根据以下提纲写一份完整的课件文案，逐节展开、内容详实。\n"
        f"标题：{title}\n"
        f"提纲：\n{outline}\n"
        f"只输出文案正文，不要额外解释。"
    )
    try:
        text = llm.chat([{"role": "user", "content": prompt}], system=body.get("agentRole"))
        return {"code": "0000", "data": {"text": text, "conversation_id": "llm"}}
    except Exception as e:  # noqa: BLE001
        logger.exception("generate_body error")
        return {"code": "9999", "msg": str(e)}


@app.post("/generate_ppt")
async def generate_ppt(request: Request):
    """生成 PPT：LLM 生成幻灯片 -> python-pptx 生成 .pptx -> 上传 MinIO。PNG 预览由服务端 LibreOffice 渲染。"""
    body = await request.json()
    title = body.get("title", "")
    text = body.get("text") or body.get("content", "")
    theme = ppt.get_template(body.get("pptId") or body.get("templateId"))
    try:
        slides = ppt.generate_slides(title, text, system=body.get("agentRole"))
        pptx_bytes = ppt.build_pptx(slides, theme)
        ppt_id = str(int(time.time() * 1000))
        record_desc = f"{title or '课件'}.pptx"
        ppt_key = f"copywriting/{ppt_id}/{ppt_id}.pptx"
        minio_util.upload_bytes(
            ppt_key, pptx_bytes,
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
        # 每页 PNG 预览改由服务端 LibreOffice 从 .pptx 渲染（renderPptToImages），这里不再用 Pillow 逐页出图，只返回备注 map。
        notes_map = {str(i): str(s.get("notes", "")) for i, s in enumerate(slides)}
        return {
            "code": "0000",
            "data": {
                "pptUrl": ppt_key,
                "recordDesc": record_desc,
                "images": [],
                "notesMap": notes_map,
                "slides": slides,
            },
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("generate_ppt error")
        return {"code": "9999", "msg": str(e)}


# ============ ppt-master 异步任务队列（内存态，单进程） ============

_PPT_MASTER_JOBS: dict[str, dict] = {}
_PPT_MASTER_JOBS_LOCK = threading.Lock()


def _ppt_master_job_set(job_id: str, **fields) -> None:
    with _PPT_MASTER_JOBS_LOCK:
        _PPT_MASTER_JOBS.setdefault(job_id, {}).update(fields)


def _ppt_master_job_get(job_id: str) -> dict:
    with _PPT_MASTER_JOBS_LOCK:
        return dict(_PPT_MASTER_JOBS.get(job_id, {}))


def _generate_ppt_master_body(body: dict, on_progress=None) -> dict:
    """核心：调 ppt_master 生成 deck → 上传 MinIO，返回 data dict（同步阻塞）。"""
    title = body.get("title") or body.get("topic") or ""
    pages = max(4, min(int(body.get("pages") or 8), 30))
    images = body.get("images") or "none"
    sources = body.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    template = body.get("template") or None
    lang = body.get("lang") or "zh-CN"
    canvas = body.get("canvas") or "ppt169"
    slug = "pptmaster_" + str(int(time.time() * 1000)) + "_" + uuid.uuid4().hex[:6]
    result = ppt_master.generate_deck(
        title, slug, lang, canvas, pages, images, sources, template,
        on_progress=on_progress,
    )
    pptx_path = result.get("pptx_path")
    if not pptx_path:
        raise RuntimeError("生成未产出 .pptx")
    with open(pptx_path, "rb") as f:
        pptx_bytes = f.read()
    ppt_key = f"copywriting/{slug}/{slug}.pptx"
    minio_util.upload_bytes(
        ppt_key, pptx_bytes,
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    return {
        "pptUrl": ppt_key,
        "recordDesc": f"{title}.pptx",
        "summary": result.get("summary", ""),
        "usage": result.get("usage", {}),
        "notesMap": _extract_project_notes(pptx_path),
    }


def _extract_project_notes(pptx_path: str) -> dict:
    """从项目 notes/ 目录读取每页讲解词（total_md_split.py 产出的按页 .md）。

    页号按文件名（与 SVG 同名、零填充）排序，返回 {str(页码): 讲解词}。
    """
    project_dir = Path(pptx_path).parent.parent
    notes_dir = project_dir / "notes"
    if not notes_dir.is_dir():
        return {}
    notes_map: dict[str, str] = {}
    files = sorted(nf for nf in notes_dir.glob("*.md") if nf.name != "total.md")
    for i, nf in enumerate(files):
        notes_map[str(i)] = nf.read_text(encoding="utf-8", errors="replace").strip()
    return notes_map


def _run_ppt_master_job(job_id: str, body: dict) -> None:
    """后台线程执行生成，更新任务状态/进度。"""
    _ppt_master_job_set(job_id, status="running")
    try:
        data = _generate_ppt_master_body(
            body, on_progress=lambda p: _ppt_master_job_set(job_id, progress=p)
        )
        _ppt_master_job_set(job_id, status="success", result=data)
    except Exception as e:  # noqa: BLE001
        logger.exception("ppt-master job %s failed", job_id)
        _ppt_master_job_set(job_id, status="failed", error=str(e))


def _validate_ppt_master_request(body: dict) -> dict:
    title = body.get("title") or body.get("topic") or ""
    if not title:
        return {"code": "9999", "msg": "missing title/topic"}
    return {}


@app.post("/generate_ppt_master")
async def generate_ppt_master(request: Request):
    """ppt-master 引擎生成 PPT：Claude API tool-use 循环 → SVG → svg_to_pptx（原生可编辑 + 母版/版式）。

    ⚠️ 无沙箱：给 LLM 的 bash 工具用 shell=True 直跑宿主命令、无白名单。topic/sources/URL
    用户可控且会进 prompt，存在提示注入 → 远程命令执行风险。仅限可信内网/受控调用。

    默认关闭：需设环境变量 PPT_MASTER_ENABLED=1 才会启用。

    入参：{ title|topic, pages, images: none|web, sources: [路径或 URL], template: 模板根路径,
           lang, canvas }；产出 .pptx 上传 MinIO 后返回 pptUrl。

    同步阻塞版（生成是分钟级）。前端优先用 submit + status 异步接口。
    """
    if not config.PPT_MASTER_ENABLED:
        return {"code": "9999", "msg": "ppt-master engine disabled (set PPT_MASTER_ENABLED=1 to enable)"}
    body = await request.json()
    err = _validate_ppt_master_request(body)
    if err:
        return err
    try:
        data = await asyncio.to_thread(_generate_ppt_master_body, body)
        return {"code": "0000", "data": data}
    except Exception as e:  # noqa: BLE001
        logger.exception("generate_ppt_master error")
        return {"code": "9999", "msg": str(e)}


@app.post("/generate_ppt_master/submit")
async def generate_ppt_master_submit(request: Request):
    """ppt-master 异步提交：入队后台生成，立即返回 jobId。"""
    if not config.PPT_MASTER_ENABLED:
        return {"code": "9999", "msg": "ppt-master engine disabled (set PPT_MASTER_ENABLED=1 to enable)"}
    body = await request.json()
    err = _validate_ppt_master_request(body)
    if err:
        return err
    job_id = uuid.uuid4().hex
    _ppt_master_job_set(job_id, status="queued", created=time.time(), progress=None, result=None, error=None)
    threading.Thread(target=_run_ppt_master_job, args=(job_id, body), daemon=True).start()
    return {"code": "0000", "data": {"jobId": job_id}}


@app.get("/generate_ppt_master/status/{job_id}")
async def generate_ppt_master_status(job_id: str):
    """ppt-master 任务状态查询：queued/running/success/failed + progress + result。"""
    job = _ppt_master_job_get(job_id)
    if not job:
        return {"code": "9999", "msg": "job not found"}
    return {"code": "0000", "data": job}


@app.post("/regenerate_ppt")
async def regenerate_ppt(request: Request):
    """按前端 fabric 画布 JSON 重新生成 .pptx（不调 LLM）。"""
    body = await request.json()
    title = body.get("title", "")
    theme = ppt.get_template(body.get("pptId") or body.get("templateId"))
    slides_elements = body.get("slides") or []
    try:
        pptx_bytes = ppt.build_pptx_from_elements(slides_elements, theme)
        ppt_id = str(int(time.time() * 1000))
        ppt_key = f"copywriting/{ppt_id}/{ppt_id}.pptx"
        minio_util.upload_bytes(
            ppt_key, pptx_bytes,
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
        return {"code": "0000", "data": {"pptUrl": ppt_key, "recordDesc": f"{title or '课件'}.pptx"}}
    except Exception as e:  # noqa: BLE001
        logger.exception("regenerate_ppt error")
        return {"code": "9999", "msg": str(e)}


@app.post("/ppttoimage")
async def ppt_to_image(request: Request):
    """PPT 转图片（桩）。真实实现需 LibreOffice + pdf 转图片。"""
    _ = await request.json()
    return {"code": "0000", "images": []}


@app.post("/ppt/notes")
async def ppt_notes(request: Request):
    """提取 .pptx 每页 speaker notes（供编辑后重渲染预览时精确对应备注）。"""
    body = await request.json()
    ppt_url = body.get("pptUrl") or body.get("ppt_key")
    if not ppt_url:
        return {"code": "9999", "msg": "缺少 pptUrl"}
    try:
        pptx_bytes = minio_util.download_bytes(ppt_url)
        notes = ppt.extract_notes(pptx_bytes)
        return {"code": "0000", "data": {"notes": notes}}
    except Exception as e:  # noqa: BLE001
        logger.exception("ppt_notes error")
        return {"code": "9999", "msg": str(e)}


# ============ WOPI host（Collabora Online 在线编辑 .pptx） ============

_WOPI_PPTX_CT = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _wopi_verify(file_id: str, access_token: str):
    """校验 access_token，返回 (file_id, user)。"""
    try:
        token_file, user = wopi.verify_token(access_token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    if token_file != file_id:
        raise HTTPException(status_code=401, detail="token file mismatch")
    return token_file, user


@app.post("/ppt/open_edit")
async def ppt_open_edit(request: Request):
    """前端"在线编辑"入口：签发 access_token，返回 WOPI 编辑所需信息。"""
    body = await request.json()
    ppt_key = body.get("pptUrl") or body.get("ppt_key") or body.get("key")
    if not ppt_key:
        return {"code": "9999", "msg": "缺少 pptUrl"}
    user = str(body.get("user") or body.get("userId") or "user")
    token = wopi.make_token(ppt_key, user)
    return {
        "code": "0000",
        "data": {
            "fileId": ppt_key,
            "accessToken": token,
            "wopiSrc": f"{config.WOPI_HOST}/wopi/files/{ppt_key}?access_token={token}",
        },
    }


@app.get("/wopi/files/{file_id:path}/contents")
async def wopi_get_file(file_id: str, access_token: str = ""):
    """GetFile：下发 .pptx 二进制。"""
    file_id, _ = _wopi_verify(file_id, access_token)
    data = minio_util.download_bytes(file_id)
    return Response(content=data, media_type=_WOPI_PPTX_CT)


@app.post("/wopi/files/{file_id:path}/contents")
async def wopi_put_file(file_id: str, request: Request, access_token: str = ""):
    """PutFile：保存编辑后的 .pptx 回 MinIO。"""
    file_id, _ = _wopi_verify(file_id, access_token)
    lock_id = request.headers.get("X-WOPI-Lock", "")
    current = wopi.get_lock(file_id)
    if current and current != lock_id:
        return Response(status_code=409, headers={"X-WOPI-Lock": current})
    data = await request.body()
    minio_util.upload_bytes(file_id, data, _WOPI_PPTX_CT)
    return Response(headers={"X-WOPI-ItemVersion": wopi.bump_version(file_id)})


@app.post("/wopi/files/{file_id:path}")
async def wopi_file_ops(file_id: str, request: Request, access_token: str = ""):
    """锁操作（X-WOPI-Override: LOCK/UNLOCK/REFRESH_LOCK/GET_LOCK）。"""
    file_id, _ = _wopi_verify(file_id, access_token)
    override = request.headers.get("X-WOPI-Override", "")
    lock_id = request.headers.get("X-WOPI-Lock", "")

    if override == "LOCK":
        if not lock_id:
            return Response(status_code=400)
        conflict = wopi.lock(file_id, lock_id)
        if conflict is not None:
            return Response(status_code=409, headers={"X-WOPI-Lock": conflict})
        return Response(status_code=200)

    if override == "UNLOCK":
        conflict = wopi.unlock(file_id, lock_id)
        if conflict is not None:
            return Response(status_code=409, headers={"X-WOPI-Lock": conflict})
        return Response(status_code=200)

    if override == "REFRESH_LOCK":
        conflict = wopi.refresh_lock(file_id, lock_id)
        if conflict is not None:
            return Response(status_code=409, headers={"X-WOPI-Lock": conflict})
        return Response(status_code=200)

    if override == "GET_LOCK":
        current = wopi.get_lock(file_id)
        if current is None:
            return Response(status_code=409, headers={"X-WOPI-Lock": ""})
        return Response(status_code=200, headers={"X-WOPI-Lock": current})

    return Response(status_code=400)


@app.get("/wopi/files/{file_id:path}")
async def wopi_check_file_info(file_id: str, access_token: str = ""):
    """CheckFileInfo：返回文件元信息。"""
    file_id, user = _wopi_verify(file_id, access_token)
    info = minio_util.stat(file_id)
    return {
        "BaseFileName": file_id.rsplit("/", 1)[-1],
        "Size": info.size,
        "OwnerId": user,
        "UserId": user,
        "UserFriendlyName": user,
        "UserCanWrite": True,
        "SupportsUpdate": True,
        "SupportsLocks": True,
        "SupportsGetLock": True,
        "SupportsRename": False,
        "SupportsDeleteFile": False,
        "Version": wopi.current_version(file_id),
    }


@app.get("/tools")
async def tools_list():
    """工具箱：列出所有可用工具及其参数。"""
    return {"code": "0000", "data": tools.list_tools()}


@app.post("/tools/run")
async def tools_run(request: Request):
    """工具箱：通用调度执行一个工具。body: {"tool": 工具名, "params": {...}}"""
    body = await request.json()
    name = body.get("tool", body.get("name", ""))
    params = body.get("params", {})
    try:
        result = await asyncio.to_thread(tools.run_tool, name, params)
        return {"code": "0000", "data": result}
    except Exception as e:  # noqa: BLE001
        logger.exception("tool run error: %s", name)
        return {"code": "9999", "msg": str(e)}


@app.get("/{inter_name:path}")
async def ppt_generic_get(inter_name: str):
    """abigetpptUrl 下的通用 GET 接口（桩）。"""
    return {"code": "0000", "data": None}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "60013")))
