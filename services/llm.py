"""LLM 服务封装（OpenAI 兼容 HTTP 接口，直接 httpx 调用）。

不使用 LangChain，直接读原始响应字段：网关对推理模型会同时返回
`content`（正式答案）和 `reasoning_content`（思考过程）。claude-opus-4-7-cc
偶发会把答案只放 `reasoning_content`、`content` 为空，所以这里做兜底 + 重试。
"""
import json

import httpx

import config


def _url() -> str:
    return config.LLM_API_BASE.rstrip("/") + "/v1/chat/completions"


def _headers() -> dict:
    return {"Authorization": "Bearer " + config.LLM_API_KEY}


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(300.0, connect=10.0)


def _build_messages(messages, system=None):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role in ("assistant", "ai"):
            msgs.append({"role": "assistant", "content": content})
        else:
            msgs.append({"role": "user", "content": content})
    return msgs


def _build_payload(messages, model, temperature, max_tokens, system, stream):
    payload = {
        "model": model or config.LLM_MODEL,
        "messages": _build_messages(messages, system),
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if stream:
        payload["stream"] = True
    return payload


def _strip_think(content):
    """Qwen 推理模型会在开头返回 <think>...</think> 思考块，剥掉只留正文。"""
    content = content or ""
    if "</think>" in content:
        content = content.split("</think>", 1)[1].strip()
    return content


def _extract_content(message):
    """取正式答案，为空时回退到 reasoning_content。"""
    content = message.get("content") or ""
    if not content:
        content = message.get("reasoning_content") or ""
    return _strip_think(content)


def chat(messages, model=None, temperature=0.7, max_tokens=16384, system=None, retries=2) -> str:
    """调用 LLM 对话，返回 assistant 文本内容。"""
    if not config.LLM_API_KEY:
        raise RuntimeError("未配置 LLM_API_KEY")
    payload = _build_payload(messages, model, temperature, max_tokens, system, stream=False)
    for _ in range(retries + 1):
        resp = httpx.post(_url(), json=payload, headers=_headers(), timeout=_timeout())
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            continue
        message = choices[0].get("message") or {}
        content = _extract_content(message)
        if content:
            return content
    return ""


def chat_stream(messages, model=None, temperature=0.7, max_tokens=16384, system=None):
    """调用 LLM 流式对话，逐块 yield 正式答案内容。"""
    if not config.LLM_API_KEY:
        raise RuntimeError("未配置 LLM_API_KEY")
    payload = _build_payload(messages, model, temperature, max_tokens, system, stream=True)
    with httpx.stream("POST", _url(), json=payload, headers=_headers(), timeout=_timeout()) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content") or ""
            if content:
                yield content
