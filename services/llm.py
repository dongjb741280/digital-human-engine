"""LLM 服务封装（基于 LangChain，OpenAI 兼容接口）。"""
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

import config


def _build_llm(model=None, temperature=0.7, max_tokens=16384, streaming=False):
    return ChatOpenAI(
        model=model or config.LLM_MODEL,
        base_url=config.LLM_API_BASE.rstrip("/") + "/v1",
        api_key=config.LLM_API_KEY,
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=streaming,
    )


def _to_messages(messages, system=None):
    msgs = []
    if system:
        msgs.append(SystemMessage(content=system))
    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role in ("assistant", "ai"):
            msgs.append(AIMessage(content=content))
        else:
            msgs.append(HumanMessage(content=content))
    return msgs


def _strip_think(content):
    """Qwen 推理模型会在开头返回 <think>...</think> 思考块，剥掉只留正文。"""
    content = content or ""
    if "</think>" in content:
        content = content.split("</think>", 1)[1].strip()
    return content


def chat(messages, model=None, temperature=0.7, max_tokens=16384, system=None) -> str:
    """调用 LLM 对话，返回 assistant 文本内容。"""
    if not config.LLM_API_KEY:
        raise RuntimeError("未配置 LLM_API_KEY")
    resp = _build_llm(model, temperature, max_tokens).invoke(_to_messages(messages, system))
    content = resp.content or ""
    if not content:
        content = resp.additional_kwargs.get("reasoning_content", "") or ""
    return _strip_think(content)


def chat_stream(messages, model=None, temperature=0.7, max_tokens=16384, system=None):
    """调用 LLM 流式对话，逐块 yield 文本内容。"""
    if not config.LLM_API_KEY:
        raise RuntimeError("未配置 LLM_API_KEY")
    llm = _build_llm(model, temperature, max_tokens, streaming=True)
    for chunk in llm.stream(_to_messages(messages, system)):
        content = chunk.content
        if content:
            yield content
