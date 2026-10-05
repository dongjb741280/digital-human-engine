# 数字人平台 Python 后端

对应 Java 侧 `yudao-module-digital` 通过 HTTP 调用的 AI 能力接口。

## 目录

```
app.py                 # FastAPI 主应用，所有 HTTP 接口
config.py              # 配置（环境变量可覆盖）
requirements.txt
services/
  llm.py               # LLM 封装（OpenAI 兼容，用于文案/PPT 生成）
  minio_util.py        # MinIO 上传/下载
  voice.py             # 语音：ASR、TTS、声音训练预处理
  video.py             # 视频：ffmpeg 合成/分辨率/字幕/首帧/抠图/图层合成
  wav2lip.py           # 口型合成（Wav2Lip，CPU 可跑）
  musetalk.py          # 口型合成（MuseTalk，远程 HTTP，需 GPU）
  ppt.py               # PPT 生成（LLM + python-pptx + Pillow）
  ppt_master.py        # ppt-master 引擎封装（Anthropic tool-use 驱动）
  image_search.py      # 百度图片搜索（关键词抓图，带缓存）
  templates/           # PPT 模板（mode1/mode2/themes.json）
scripts/
  download_backgrounds.py  # 批量下载背景图并上传 MinIO、输出入库 SQL
skills/
  ppt-master/          # vendor 的 ppt-master 引擎（/generate_ppt_master 用）
docs/                  # 设计文档（api-service-design / ppt-spec / deck-template-authoring）
```

## 启动

```bash
cd digital-human-engine
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 60013
```

> 依赖说明：`requirements.txt` 仅覆盖 HTTP/语音链路。抠图、视频、PPT 功能还依赖
> `numpy`、`av`、`opencv-python`、`Pillow`、`rembg`、`python-pptx`，需另行安装；
> 系统需有 `ffmpeg`（用于视频处理）。

## 默认配置（可用环境变量覆盖）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MINIO_ENDPOINT` | `http://localhost:9000` | MinIO 地址 |
| `MINIO_ACCESS_KEY` | `minioadmin` | MinIO 账号 |
| `MINIO_SECRET_KEY` | `minioadmin` | MinIO 密码 |
| `MINIO_BUCKET` | `aidigital` | bucket |
| `JAVA_SERVER` | `http://localhost:48083` | Java digital-server（回调用） |
| `ASR_MODEL` | `medium` | faster-whisper 模型 |
| `TTS_VOICE` | `zh-CN-XiaoxiaoNeural` | edge-tts 回退音色 |
| `MATTING_MODEL` | `u2net` | rembg 抠像模型（u2net / isnet-general-use / birefnet-general） |
| `LIPSYNC_MODEL` | `wav2lip` | 口型合成：wav2lip（CPU）/ musetalk（GPU） |
| `WAV2LIP_HOME` | 空 | Wav2Lip 仓库根目录（`LIPSYNC_MODEL=wav2lip` 时必填） |
| `WAV2LIP_PYTHON` | 空 | Wav2Lip venv 的 python（留空用 `<WAV2LIP_HOME>/.venv/bin/python`） |
| `WAV2LIP_CHECKPOINT` | `checkpoints/wav2lip_gan.pth` | Wav2Lip 权重路径 |
| `WAV2LIP_FACE_DET` | `face_detection/detection/sfd/s3fd.pth` | 人脸检测权重 |
| `WAV2LIP_BATCH_SIZE` | `16` | 推理 batch size |
| `MUSETALK_API` | 空 | MuseTalk 推理服务地址（`LIPSYNC_MODEL=musetalk` 时必填） |
| `MUSETALK_TIMEOUT` | `600` | MuseTalk 请求超时（秒） |
| `LLM_API_BASE` | `https://ai-route.huihaohealth.com` | LLM 服务（OpenAI 兼容） |
| `LLM_API_KEY` | 空 | LLM 密钥（文案/PPT 生成必填） |
| `LLM_MODEL` | `claude-opus-4-7-cc` | LLM 模型名 |
| `PPT_MASTER_ENABLED` | 空（关） | 开启 ppt-master 引擎 `/generate_ppt_master`（`=1` 开启） |
| `GPT_SOVITS_API` | `http://127.0.0.1:9880` | GPT-SoVITS 推理 API |
| `GPT_SOVITS_TIMEOUT` | `120` | GPT-SoVITS 请求超时（秒） |
| `GPT_SOVITS_GPT_WEIGHTS` | `GPT_weights_v2/pretrained.ckpt` | 预训练基础权重 |
| `GPT_SOVITS_SOVITS_WEIGHTS` | `SoVITS_weights_v2/pretrained.pth` | 预训练基础权重 |
| `WOPI_SECRET` | `dev-wopi-secret-change-me` | WOPI access_token 签名密钥（生产必须改） |
| `WOPI_HOST` | `http://localhost:60013` | CODE 服务端可达的本系统地址（Docker 内访问宿主用 `http://host.docker.internal:60013`） |

## 接口清单（与 Java 对应）

| 接口 | 方法 | 说明 | 状态 |
|---|---|---|---|
| `/ai/formatAudio` | POST | 声音训练预处理（切片→ASR→上传参考音频，zero-shot） | 真实 |
| `/tts` | GET | 声音克隆/合成（GPT-SoVITS，失败回退 edge-tts） | 真实 |
| `/conflate/makeAudio` | POST | 声音克隆 POST 变体（edge-tts） | 真实 |
| `/ai/voice2txt` | POST | 语音转文字 | 真实（faster-whisper） |
| `/ai/difyChat` | POST | Dify 兼容对话接口（复用本地 LLM） | 真实 |
| `/ai/humanInteract` | POST | 数字人互动（TTS + 口型合成） | 真实 |
| `/aiDhHuman/changeImega` | POST | 抠图（rembg 出 RGBA PNG） | 真实 |
| `/aiDhHuman/changeImegaAndViedo` | POST | 抠图 + 抠像视频（绿幕） | 真实 |
| `/ai/mergeVideo` | POST | 视频拼接合成 | 真实（ffmpeg） |
| `/ai/video_resolution` | POST | 分辨率/宽高比调整 | 真实（ffmpeg） |
| `/ai/addCaptions` | POST | 加字幕（ASS 渲染） | 真实（ffmpeg） |
| `/ai/getFirstFrame` | POST | 提取首帧 | 真实（ffmpeg） |
| `/ai/changeVideo` | POST | 语音合成数字人（口型，wav2lip/musetalk） | 真实 |
| `/ai/changeVideoSplit` | POST | 语音合成数字人-分段 | 真实 |
| `/ai/deal_video` | POST | 视频图层合成（背景+数字人+PPT 叠加） | 真实（ffmpeg） |
| `/ai/makePptVoice2Video` | POST | PPT 语音转视频（参考视频按语音时长循环） | 真实 |
| `/generate_outline` | POST | 生成课件提纲 | 真实（LLM） |
| `/generate_body` | POST | 生成课件正文 | 真实（LLM） |
| `/generate_ppt` | POST | 生成 PPT（LLM→pptx+逐页图片） | 真实 |
| `/regenerate_ppt` | POST | 按 fabric 画布 JSON 重新生成 .pptx（不调 LLM） | 真实 |
| `/generate_ppt_master` | POST | ppt-master 引擎生成 PPT（topic/sources/images/template，SVG→pptx + 母版/版式） | 真实（默认关，`PPT_MASTER_ENABLED=1` 开启） |
| `/generate_ppt_master/submit` | POST | ppt-master 异步提交（入队后台生成，返回 jobId） | 真实（同上） |
| `/generate_ppt_master/status/{jobId}` | GET | ppt-master 任务状态（queued/running/success/failed） | 真实（同上） |
| `/getppt` | GET | 模板查询 | 桩 |
| `/ppttoimage` | POST | PPT 转图片 | 桩 |
| `/ppt/open_edit` | POST | 签发 WOPI access_token，返回在线编辑入口（fileId/accessToken/wopiSrc） | 真实 |
| `/wopi/files/{fileId}` | GET/POST | WOPI host：CheckFileInfo / 锁操作（LOCK/UNLOCK/REFRESH_LOCK/GET_LOCK） | 真实 |
| `/wopi/files/{fileId}/contents` | GET/POST | WOPI host：GetFile / PutFile | 真实 |
| `/ppt/notes` | POST | 提取 .pptx 每页 speaker notes（编辑后重渲染预览时精确对应备注） | 真实 |
| `/{inter_name:path}` | GET | 通用 GET 兜底 | 桩 |

## 端口说明

Java 侧 `application-local.yaml` 里各接口指向了多个端口（6001/60013/60014/60015/60021/60022/60023/60024/60025/60027），
对应原来拆分的多个 Python 服务。当前这个 `app.py` 把所有接口放在**一个进程**里（默认 60013）。

两种方式对齐：

1. **简单**：把 Java 配置里所有 Python 接口的 IP:端口都改成同一个，如 `http://localhost:60013`。
2. **拆分**：用 nginx 按路径反代，或启动多个进程分别监听不同端口并只挂载对应路由。

## 需要外部资源的能力

这些能力依赖模型权重或外部服务，需按需接入：

- **口型合成** `LIPSYNC_MODEL`
  - `wav2lip`（默认，CPU）：克隆 [Wav2Lip](https://github.com/Rudrabha/Wav2Lip) 仓库并下载权重，配置 `WAV2LIP_HOME`。
  - `musetalk`（需 GPU）：部署 MuseTalk 推理服务，配置 `MUSETALK_API`。
  - 未接入时也可走 `wav2lip.change_video_passthrough`（只合音轨，不生成口型），用于跑通流程。
- **声音合成 / 声音复刻** `GPT_SOVITS_API`：部署 GPT-SoVITS 的 `api_v2.py`（端口 9880，zero-shot TTS，权重见 `GPT_SoVITS/configs/tts_infer.yaml`）；未接入时 `/tts` 回退 edge-tts（占位）。
- **文案/PPT 生成**：配置 `LLM_API_KEY`（OpenAI 兼容）。
- **ppt-master 引擎** `/generate_ppt_master`：复用 `LLM_API_KEY`（同一网关，Anthropic `/v1/messages`）；引擎已 vendor 到 `skills/ppt-master/`，依赖见 `skills/ppt-master/requirements.txt`。默认关闭（`PPT_MASTER_ENABLED=1` 开启）。内置命令白名单 + 路径沙箱（bash 只放行引擎脚本/只读命令，read/write 限引擎根目录内），但**非 OS 级沙箱**，仍不建议对公网开放。支持 topic-research（`web_search` DuckDuckGo + `web_fetch` 复用 `web_to_md.py`，无 source 时联网补事实缺口）。
- **PPT 转图片** `/ppttoimage`：需 LibreOffice + PDF 转图片（当前为桩）。

## 在线编辑（Collabora Online / WOPI）

用浏览器在线编辑生成后的 .pptx（像 PowerPoint 一样直接改文件），本系统实现 WOPI host，把 Collabora 的读写桥接到 MinIO。桌面版 LibreOffice 不能嵌网页，浏览器里用的是 **Collabora Online（CODE）**。

### 本地跑通（`ssl.enable=false`）

```bash
docker run -t -d -p 9980:9980 \
  -e 'aliasgroup1=http://192.168.1.4:60013' \
  -e 'username=admin' -e 'password=xxx' \
  -e 'extra_params=--o:ssl.enable=false' \
  --restart always --cap-add MKNOD \
  --name collabora-code collabora/code
```

启动本系统时设置（`WOPI_HOST` 要填 CODE 容器能访问到本系统的地址，即宿主局域网 IP，且与 `aliasgroup1` 一致）：

```bash
WOPI_HOST=http://192.168.1.4:60013 .venv/bin/uvicorn app:app --host 0.0.0.0 --port 60013
```

### 编辑流程（跨 3 个项目）

1. 前端「编辑」→ Java `/aiDhPpt/open_edit`（传 `pptId`）→ Java 查 `pptUrl` → 本系统 `POST /ppt/open_edit` 返回 `wopiSrc`。
2. 前端拼 CODE 编辑地址（`http://localhost:9980/browser/<hash>/cool.html?WOPISrc=...`）塞 iframe。
3. 编辑保存 → CODE 回调 `PUT /wopi/files/{fileId}/contents` → 覆盖 MinIO 里的 .pptx。
4. 退出编辑 → 前端调 Java `/aiDhPpt/render_preview` → Java 重渲染预览图 + 调本系统 `POST /ppt/notes` 提取每页 speaker notes → 回写。

### 说明与坑

- **aliasgroup1 端口要写字面量**：`http://192.168.1.4:60013`，不能写 `http://192.168.1.4:.*`（CODE 会把端口当数字解析，`.*` 解析失败会丢弃整条 alias）。
- **只关 ssl.enable**：不要加 `--o:ssl.termination=true`（它会让 discovery 广告 https 但容器只跑 http，浏览器报 `Failed to fetch`/白屏）。
- **CODE 未开 CORS**：浏览器不能跨域 fetch CODE 的 `/hosting/discovery`，所以前端把 edit 地址（含构建号 hash）硬编码，别走 discovery fetch。
- **网络**：浏览器要能访问 CODE；CODE 要能访问 `WOPI_HOST`（GetFile/PutFile 是 CODE 服务端发起的）。容器内 `localhost` 指向容器自己，不是宿主。
- **安全**：access_token 由 `WOPI_SECRET` HMAC 签名、短时过期、绑定 fileId+user，别把 MinIO 凭据暴露给 CODE。
- **锁/版本**：锁与版本号均为内存态（单进程），多人协作编辑依赖 LOCK/UNLOCK；重启后丢失。
- **保真度**：Collabora 编辑真实 .pptx，但与 PowerPoint 非 100% 一致；ppt-master 生成的复杂母版/SVG 可能走样。
- **两套编辑模型**：Collabora 直接改 .pptx 二进制，与现有 fabric→`/regenerate_ppt` 的「重新生成」不能混用，选一条当主路径。

## 回调

Python 处理完成后会回调 Java digital-server：

- `POST {JAVA_SERVER}/digital-api/system/voiceManager/updateVoice`（训练完成，voiceStatus=2）
- `POST {JAVA_SERVER}/digital-api/system/voiceManager/updateVoiceBypython`（克隆完成，voiceStatus=4，携带 `length` 音频时长毫秒）
- `POST {JAVA_SERVER}/digital-api/system/aiDhHuman/callBackAiDhHuman`（抠图完成/失败，status=3/4）
- `POST {JAVA_SERVER}/digital-api/system/aiDhHumanVideo/updateRecordVideo`（视频处理各步骤，execStatus=1/2）

## 背景图脚本

批量下载免费背景图并上传 MinIO、输出入库 SQL：

```bash
python scripts/download_backgrounds.py --source picsum --count 12
python scripts/download_backgrounds.py --source unsplash --keyword "minimal business background" --count 12
python scripts/download_backgrounds.py --source pexels --keyword "minimal background" --count 12
```

`unsplash` / `pexels` 分别需要 `UNSPLASH_ACCESS_KEY` / `PEXELS_API_KEY` 环境变量。
