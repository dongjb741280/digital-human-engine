"""ppt-master 引擎封装：Claude API tool-use 循环驱动 quick-generate 流程。

移植自 ppt-master-api 的 agent.py，适配 digital-human-engine：
- LLM 走 Anthropic SDK，复用 config.LLM_API_KEY / LLM_API_BASE / LLM_MODEL（与 OpenAI 兼容网关同一套凭据）。
- 引擎（skills/ppt-master）已 vendor 到 <engine>/skills/ppt-master。
- bash 工具里 python3 指向本引擎 .venv，cwd 为引擎根目录；项目产物落在 <engine>/projects/。

特性：topic / sources（本地文件或 URL）/ images（none|web）/ template（结构化母版路线）。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

import anthropic

import config

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # digital-human-engine 根
SKILL_DIR = PROJECT_ROOT / "skills" / "ppt-master"
VENV_BIN = PROJECT_ROOT / ".venv" / "bin"

MODEL = config.LLM_MODEL
MAX_TOKENS = 32000
MAX_TURNS = 300
BASH_TIMEOUT_S = 600
READ_LIMIT = 600          # read_file 单次最多返回行数
BASH_STDOUT_CAP = 8000    # bash 单次最多返回 stdout 字符数


def _client() -> anthropic.Anthropic:
    if not config.LLM_API_KEY:
        raise RuntimeError("未配置 LLM_API_KEY")
    return anthropic.Anthropic(api_key=config.LLM_API_KEY, base_url=config.LLM_API_BASE)


TOOLS: list[dict] = [
    {
        "name": "bash",
        "description": (
            "Run a shell command. cwd is the repository root. `python3` already resolves to the "
            "project virtualenv (all ppt-master dependencies installed). Use it to run the skill's "
            "scripts: project_manager.py, icon_sync.py, text_measure.py, svg_quality_checker.py, "
            "svg_to_pptx.py, etc. Returns exit_code + stdout + stderr."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run."},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a text file. Use it to read the skill's instruction files under the skill dir "
            "(SKILL.md, workflows, references) and project files. Returns numbered lines."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path, or relative to the repo root."},
                "offset": {"type": "integer", "description": "1-based line to start at (default 1)."},
                "limit": {"type": "integer", "description": f"Max lines to return (default {READ_LIMIT})."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write a UTF-8 text file. Use it to hand-author each SVG page into the project's "
            "svg_output/ directory, one page at a time. Never use it to dump a script that "
            "batch-generates pages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path, or relative to the repo root."},
                "content": {"type": "string", "description": "Full file content."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
]

SYSTEM = """You are an autonomous agent that generates a native-editable PowerPoint (.pptx) deck using
the "PPT Master" skill package.

Absolute paths (use exactly):
- SKILL_DIR = {skill_dir}
- The skill's shell commands run with the `bash` tool; inside bash, `python3` is already the project
  virtualenv Python with every ppt-master dependency installed. cwd is the repository root.

YOUR TASK: generate a deck on the topic below using the QUICK GENERATE profile (no user interaction).

PROCEDURE:
1. read_file {skill_dir}/SKILL.md, then {skill_dir}/workflows/routing.md, then
   {skill_dir}/workflows/profiles/quick-generate.md. Follow quick-generate exactly. In every command
   you run, replace the literal `${{SKILL_DIR}}` prefix with {skill_dir}.
2. Read every other file the workflow directs you to read (the planning-capability batch, the
   execution core, the selected mode/visual-style detail files, etc.) before authoring pages.
3. Initialize the project with `python3 {skill_dir}/scripts/project_manager.py init {slug}`. The
   printed project path (under `projects/`) is the project directory; use that absolute path for
   every later command and for write_file. {source_step}{template_step}
4. Hand-author each SVG page yourself with write_file into `<project>/svg_output/`, one page at a
   time, filenames `01_*.svg`, `02_*.svg`, ... in order. NEVER write a shell/python script that
   batch-generates pages — that is forbidden by the skill and degrades quality.
5. Canvas: {canvas} (16:9 -> viewBox "0 0 1280 720"). Deck language: {lang}. Keep the deck concise
   ({pages} pages). Do NOT run topic-research and do NOT stop to ask questions; generate the content
   directly from {content_basis}. {image_policy}
6. Finish exactly as quick-generate.md section 4 prescribes: run the lockless final checker with
   `--quick-generate --canonical-authoring --stage final --json`, fix every blocking error, then
   export with `svg_to_pptx.py <project> --quick-generate --no-notes`. The final .pptx must exist
   under `<project>/exports/`.

When finished, reply with ONLY a compact summary: the absolute .pptx path, the slide count, and one
line of notes (nothing else).

Topic: {topic}
"""


def _resolve_path(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


def _resolve_tool_path(path: str) -> Path:
    """读/写工具专用：解析并限制在引擎根目录内（防读/写宿主任意路径）。"""
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    r = p.resolve()
    root = PROJECT_ROOT.resolve()
    if r != root and not r.is_relative_to(root):
        raise ValueError(f"path outside project root: {r}")
    return r


# bash 白名单：无 OS 级沙箱下的最小防线，仅放行单条、无链接/重定向/命令替换的
# 只读或引擎脚本命令。注意：这不能替代容器/nsjail，只能挡住最直接的注入与串联执行。
_ALLOWED_CMDS = {
    "python3", "python", "ls", "cat", "find", "head", "tail", "grep", "wc",
    "file", "pwd", "which", "echo", "du", "mkdir", "tree",
}
_BLOCKED_META = ("&&", "||", ";", "|", "`", "$", ">", "<", "\n", "\r", "&")


def _is_safe_bash_command(cmd: str) -> tuple[bool, str]:
    c = (cmd or "").strip()
    if not c:
        return False, "empty command"
    for meta in _BLOCKED_META:
        if meta in c:
            return False, f"blocked shell metacharacter: {meta!r}"
    tokens = c.split()
    head = tokens[0]
    if head not in _ALLOWED_CMDS:
        return False, f"command not allowed: {head!r}"
    if head in ("python3", "python"):
        if len(tokens) < 2:
            return False, "python requires a script path"
        arg = tokens[1]
        if arg in ("-c", "-m", "-i", "--version"):
            return False, f"python flag not allowed: {arg!r}"
        p = Path(arg)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        r = p.resolve()
        root = PROJECT_ROOT.resolve()
        if r != root and not r.is_relative_to(root):
            return False, f"python script outside project root: {r}"
    return True, ""


def _execute_tool(name: str, args: dict) -> str:
    if name == "bash":
        cmd = args.get("command", "")
        ok, why = _is_safe_bash_command(cmd)
        if not ok:
            return f"$ {cmd}\n[blocked] {why}"
        env = dict(os.environ)
        env["PATH"] = f"{VENV_BIN}:{env.get('PATH', '')}"
        env["SKILL_DIR"] = str(SKILL_DIR)
        try:
            r = subprocess.run(
                cmd, shell=True, cwd=PROJECT_ROOT, env=env,
                capture_output=True, text=True, timeout=BASH_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return f"$ {cmd}\nTIMEOUT after {BASH_TIMEOUT_S}s"
        out = f"$ {cmd}\nexit_code={r.returncode}\n"
        if r.stdout:
            out += f"[stdout]\n{r.stdout[:BASH_STDOUT_CAP]}"
            if len(r.stdout) > BASH_STDOUT_CAP:
                out += "\n...[stdout truncated]"
            out += "\n"
        if r.stderr:
            out += f"[stderr]\n{r.stderr[:4000]}\n"
        return out

    if name == "read_file":
        try:
            p = _resolve_tool_path(args.get("path", ""))
        except ValueError as e:
            return f"ERROR: {e}"
        if not p.exists():
            return f"ERROR: no such file: {p}"
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        offset = max(1, int(args.get("offset") or 1))
        limit = int(args.get("limit") or READ_LIMIT)
        chunk = lines[offset - 1 : offset - 1 + limit]
        numbered = "\n".join(f"{i + offset}\t{ln}" for i, ln in enumerate(chunk))
        if offset + limit < len(lines):
            numbered += f"\n[... {len(lines) - (offset - 1 + limit)} more line(s); {len(lines)} total]"
        return numbered if numbered else "(empty file)"

    if name == "write_file":
        try:
            p = _resolve_tool_path(args.get("path", ""))
        except ValueError as e:
            return f"ERROR: {e}"
        content = args.get("content", "")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {p}"

    return f"ERROR: unknown tool {name}"


def _find_pptx(slug: str) -> Path | None:
    matches = sorted(PROJECT_ROOT.glob(f"projects/{slug}_*/exports/*.pptx"))
    return matches[-1] if matches else None


def generate_deck(
    topic: str,
    slug: str,
    lang: str = "zh-CN",
    canvas: str = "ppt169",
    pages: int = 8,
    images: str = "none",
    sources: list[str] | None = None,
    template: str | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    client = _client()
    if images == "web":
        image_policy = (
            "Web image search is ENABLED: where a page benefits from photos, source them with "
            f"`python3 {SKILL_DIR}/scripts/image_search.py` per quick-generate's image-sourcing "
            "rules (zero-config Openverse/Wikimedia always work; Pexels/Pixabay activate when "
            "PEXELS_API_KEY/PIXABAY_API_KEY are set). Record provenance in image_sources.json and "
            "render any required on-slide attribution."
        )
    else:
        image_policy = (
            "Image policy: native SVG/emoji/icon visuals only — do NOT run image_search.py or "
            "image_gen.py."
        )

    abs_sources = [
        s if s.startswith(("http://", "https://")) else str(_resolve_path(s))
        for s in (sources or [])
    ]
    if abs_sources:
        src_list = "\n".join(f"   - {p}" for p in abs_sources)
        source_step = (
            "SOURCES (authoritative): convert and read them before authoring. For each source below run\n"
            f"   `python3 {SKILL_DIR}/scripts/source_to_md.py <source_path> -o <project>/sources/<name>.md`\n"
            "   (a source may be a local file or a URL; source_to_md.py auto-detects and fetches URLs).\n"
            "   Then read_file every produced .md. Facts, terminology, and structure must come from these\n"
            f"   sources, never from general knowledge.\n   Sources:\n{src_list}"
        )
        content_basis = "the provided sources only — do not invent, omit, or contradict them"
    else:
        source_step = ""
        content_basis = "your own knowledge and keep facts accurate but non-controversial"

    if template:
        abs_template = str(_resolve_path(template))
        template_step = (
            "\n   TEMPLATE (structured route): an exact template workspace root is supplied:\n"
            f"   {abs_template}\n"
            "   Follow quick-generate.md's Template branch → Direct template application: read\n"
            f"   {SKILL_DIR}/workflows/stages/apply-template-workspace.md, validate the root with\n"
            "   `svg_quality_checker.py <root>/templates --template-mode --canonical-authoring`, install\n"
            "   it into the project, and author every page carrying the Master/Layout/slot metadata its\n"
            "   design_spec requires, so the exporter produces a deck with real slide masters/layouts\n"
            "   (p:sldMaster / p:sldLayout inheritance)."
        )
    else:
        template_step = ""

    system = SYSTEM.format(
        skill_dir=SKILL_DIR, slug=slug, lang=lang, canvas=canvas, pages=pages, topic=topic,
        image_policy=image_policy, source_step=source_step, content_basis=content_basis,
        template_step=template_step,
    )
    messages: list[dict] = [{"role": "user", "content": f"生成主题《{topic}》的 PPT（{lang}，{canvas}，约 {pages} 页）。"}]

    usage = {"input_tokens": 0, "output_tokens": 0}
    last_text = ""

    for turn in range(1, MAX_TURNS + 1):
        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=system,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            resp = stream.get_final_message()

        usage["input_tokens"] += resp.usage.input_tokens
        usage["output_tokens"] += resp.usage.output_tokens
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "tool_use":
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _execute_tool(block.name, dict(block.input)),
                    })
            messages.append({"role": "user", "content": results})
        elif resp.stop_reason == "max_tokens":
            messages.append({"role": "user", "content": "Continue."})
        else:  # end_turn / stop_sequence / refusal
            last_text = next(
                (b.text for b in resp.content if b.type == "text"), ""
            )
            break

        if on_progress:
            on_progress({"turn": turn, "stop_reason": resp.stop_reason})

    pptx = _find_pptx(slug)
    return {
        "pptx_path": str(pptx) if pptx else None,
        "summary": last_text.strip(),
        "project": str(PROJECT_ROOT / "projects" / slug),
        "usage": usage,
    }
