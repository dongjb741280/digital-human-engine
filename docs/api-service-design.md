# ppt-master 引擎集成技术方案（digital-human-engine）

> 把 ppt-master 的「skill 方式生成 PPT」能力，内嵌进数字人平台 Python 后端（`digital-human-engine`），通过 HTTP 接口产出原生可编辑 `.pptx`（含真实母版/版式）。
> 状态：P1 已实现并 e2e 验证；安全为「软沙箱 + 开关」，非生产级隔离。

---

## 1. 目标与边界

**要做的事**：在现有 FastAPI 后端里加一个接口，接收「主题 / 素材 / 模板 / 选项」，返回一份原生可编辑 `.pptx`（已上传 MinIO）。

**本质认知**：ppt-master 不是黑盒接口，而是「**LLM 当大脑 + Python 脚本当手脚**」：
- **智能层**（不可确定）：读 `SKILL.md` → 选题规划 → 一页一页手写 SVG。
- **确定层**（可脚本化）：`project_manager.py`、`svg_quality_checker.py`、`svg_to_pptx.py` 等。

所以「集成」不是调一个现成 `/generate` 端点，而是**把 LLM 智能层接上接口，把确定层脚本放进（软）沙箱**。

**与既有 `/generate_ppt` 的关系**：`digital-human-engine` 原有一套 `LLM → 9 版式 JSON → python-pptx` 的扁平生成（见 `docs/ppt-spec.md`，`services/ppt.py`）。本方案是**新增**一条高质量路线（SVG → `svg_to_pptx`，原生可编辑形状 + 母版/版式），不替换原接口，避免破坏 Java 侧契约。

**不在本次范围**：模型微调、自研排版引擎、替换 ppt-master 脚本层、OS 级沙箱（容器/nsjail）。

---

## 2. 已实现（P1 状态）

| 项 | 落点 |
|---|---|
| 引擎 | `skills/ppt-master/`（vendor，124MB） |
| LLM 编排层 | `services/ppt_master.py`（Claude API tool-use 循环，手写 harness） |
| HTTP 接口 | `POST /generate_ppt_master`（`app.py`） |
| 配置开关 | `PPT_MASTER_ENABLED`（`config.py`，默认关） |
| 依赖 | `anthropic` + `skills/ppt-master/requirements.txt` |
| 产物 | `projects/<slug>_*/exports/*.pptx` → MinIO `copywriting/<slug>/<slug>.pptx` |

---

## 3. 架构总览（内嵌）

```
                    ┌─────────────────────────────────────────────┐
                    │          digital-human-engine（FastAPI）     │
                    │  鉴权/限流由外层网关承担，本进程只做能力      │
                    │                                             │
                    │  POST /generate_ppt_master ──┐               │
                    │       │                      │               │
                    │       ▼                      │               │
                    │  services/ppt_master.py       │               │
                    │  · Claude API tool-use 循环   │               │
                    │  · tools: bash/read/write     │               │
                    │  · 命令白名单 + 路径沙箱       │               │
                    └──────┬───────────────────────┼───────────────┘
                           │ 调用脚本               │ 读/写文件
                           ▼                       ▼
                    ┌─────────────────────────────────────────────┐
                    │        skills/ppt-master（本地引擎）          │
                    │  project_manager / svg_quality_checker /     │
                    │  source_to_md / image_search / svg_to_pptx   │
                    └──────┬──────────────────────────────────────┘
                           │ 产物 .pptx
                           ▼
                    ┌─────────────────────────────────────────────┐
                    │            MinIO（对象存储）                  │
                    └─────────────────────────────────────────────┘
```

要点：**单进程内嵌**，无独立 worker 队列、无容器沙箱；LLM 编排层跑在本进程线程里（`asyncio.to_thread`），脚本经 `bash` 工具直跑宿主 Python（`python3` 指向本引擎 `.venv`）。

---

## 4. LLM 编排层（tool-use 循环）

采用方案 A——**Claude API 手动 tool-use 循环**（`services/ppt_master.py`），四个工具 `bash` / `read_file` / `write_file`：

- `bash`：跑引擎脚本（`project_manager.py`、`svg_quality_checker.py`、`svg_to_pptx.py` 等），`cwd` 固定引擎根，`python3` 指向 `.venv`；**经命令白名单过滤**。
- `read_file`：读 `SKILL.md` / workflows / references / 转好的 source md；**限引擎根目录内**。
- `write_file`：模型逐页手写 `svg_output/*.svg`；**限引擎根目录内**，绝不做批量生成脚本。

关键 API 形态（对应当前模型）：

- `model = config.LLM_MODEL`（默认 `claude-opus-4-7-cc`），复用 `LLM_API_KEY` / `LLM_API_BASE`（同一网关，Anthropic `/v1/messages`）。
- `thinking: {type: "adaptive"}` + `output_config: {effort: "high"}`；流式输出（`messages.stream`）。
- 串行推进：每轮只走一页或一个门禁，符合 ppt-master 的 `P01–P05 → gate → … → final gate` 节奏。
- 指令文件是稳定静态前缀，**未接 prompt caching**（省 ~90% 输入成本的后续项）。

---

## 5. 接口契约

`POST /generate_ppt_master`（同步阻塞，生成是分钟级，调用方需容忍长耗时）

**门禁**：`PPT_MASTER_ENABLED`（环境变量，默认 `""`=关）。未开启时直接返回 `{"code":"9999","msg":"ppt-master engine disabled..."}`。

**请求体**：

```json
{
  "title": "海豚的秘密",                  // 或 "topic"
  "pages": 8,                             // 4~30，默认 8
  "lang": "zh-CN",
  "canvas": "ppt169",
  "images": "none",                       // none | web
  "sources": ["projects/spec.pdf"],       // 本地路径（绝对/相对引擎根）或 URL；可空
  "template": "skills/ppt-master/templates/layouts/presentation_core"  // 可空
}
```

**响应**：

```json
{
  "code": "0000",
  "data": {
    "pptUrl": "copywriting/pptmaster_<ts>/pptmaster_<ts>.pptx",   // MinIO 对象键
    "recordDesc": "海豚的秘密.pptx",
    "summary": "...",                     // 模型收尾摘要
    "usage": {"input_tokens": 84307, "output_tokens": 46946}
  }
}
```

---

## 6. 四能力映射

| 能力 | 参数 | 行为 |
|---|---|---|
| **主题直出** | 仅 `title` | topic-only，模型凭自身知识 quick-generate |
| **文档输入** | `sources` | `source_to_md.py` 转 md（PDF/DOCX/PPTX/XLSX/网页；URL 走 `web_to_md.py`），作为权威内容源，模型不杜撰 |
| **网页搜图** | `images="web"` | `image_search.py` 搜图链 `pexels → pixabay → openverse → wikimedia`（有 key 排前）；`none` 则纯原生 SVG |
| **模板/结构化** | `template` | 传 Layout/Deck 工作区根，走 `apply-template-workspace`，产出带真实 `p:sldMaster`/`p:sldLayout` 继承的 deck；不传则 free-design 扁平页 |

**图片来源优先级**：带 source 且 source 含图时，优先用 source 提供的图（溯源 `license_tier: manual`）；`image_search.py` 只在 topic-only 或 source 无合适图时触发。

**已 e2e 验证**：主题直出（4 页）、网页搜图（Pixabay+Wikimedia）、PDF source、URL source（微信公众号）、`presentation_core`（1 母版 7 布局）、`中国电信`（2 母版 5 布局）。

---

## 7. 沙箱与安全

**三层软防护（已实现，非 OS 级沙箱）**：

| 层 | 机制 | 拦截内容 |
|---|---|---|
| 开关 | `PPT_MASTER_ENABLED` 默认关 | 未显式开启时接口不可用 |
| 命令白名单 | `_is_safe_bash_command` | 拦截 `;` `&&` `\|` `` ` `` `$()` `>` `<` 等元字符、`curl`/`wget`/`sudo` 等危险命令、`python3 -c`/`-m`、引擎外脚本路径 |
| 路径沙箱 | `_resolve_tool_path` | `read_file`/`write_file` 强制限制在引擎根目录内 |

**残余风险（必须清楚）**：
1. `bash` 仍用 `shell=True`，白名单是**进程内字符串过滤**，不是系统调用级拦截，存在绕过可能。
2. 只读命令（`cat`/`find`/`grep`）与 `source_to_md.py` 的 source 参数仍可读引擎外文件（数据外泄面）。
3. `topic`/`sources`/`URL` 用户可控且进 prompt，提示注入仍可能诱导模型做出危险工具调用。

**结论**：当前是「降低风险」不是「消除风险」。**要生产级隔离，必须套容器/nsjail/独立用户 + 出站网络白名单**，并在外层网关做鉴权限流。当前形态**不建议对公网开放**。

---

## 8. 成本模型（估算）

| 项 | 估算 |
|---|---|
| 4~10 页 deck token | 输入 ~80–200K（指令 + 上下文），输出 ~50–150K（SVG 代码） |
| 模型成本 | `claude-opus-4-7-cc`（价格以网关为准）：约 $1–5 / 份 |
| 指令前缀缓存 | 未接；命中后省 ~90% 输入成本（后续项） |
| 图片 | 网页搜图 ≈ 零成本（免费档）；AI 生成单独计费（未接） |

> 成本优化顺序：**缓存指令前缀 → effort 调优 → 按需降模型**（先测，别盲降）。

---

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| **无 OS 级沙箱 → RCE/数据外泄** | 开关默认关；套容器/nsjail 才能生产；公网必须经鉴权网关 |
| 提示注入（topic/source/URL 可控） | 输入当数据不当指令；命令白名单 + 路径沙箱兜底 |
| LLM 串行手写 SVG 慢、token 贵 | 缓存指令前缀、effort 调优、Quick 画像 |
| 跨页视觉漂移 | 大上下文 + 串行 + 定期重读 `spec_lock` |
| 单份任务失败 | Quick 无锁可重跑；`generate_ppt_master` 捕获异常返回 `code:9999` + msg |

---

## 10. 未接能力（后续）

- topic-research（`web_search`/`web_fetch`，补 source 事实缺口）
- AI 图片生成（`image_gen.py` + `IMAGE_BACKEND`/provider key）
- prompt caching（缓存 skill 指令前缀）
- 异步任务化（队列 + 轮询/SSE，替代当前同步阻塞）
- OS 级沙箱（容器/nsjail/独立用户/出站白名单）
