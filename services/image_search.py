"""百度图片在线搜索：按关键词抓一张图，带进程内缓存。

非官方接口（image.baidu.com 网页搜索），版权/稳定性/分辨率风险已接受。
返回图片字节；搜图失败返回 None，由调用方降级。

注意：不要加 Referer、不要手动设置 Accept-Encoding，否则触发百度反爬（Forbid spider access）。
"""
import json

import requests

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36 Edg/118.0.2088.69",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "Connection": "keep-alive",
}

_cache = {}  # keyword -> bytes | None（None 表示已搜过且失败）


def search(keyword):
    """按关键词搜图并下载，返回 bytes；失败返回 None。带进程内缓存。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return None
    if keyword in _cache:
        return _cache[keyword]
    result = None
    for url in _search_urls(keyword):
        data = _download(url)
        if data:
            result = data
            break
    _cache[keyword] = result
    return result


def _search_urls(keyword, count=5):
    try:
        resp = requests.get(
            "https://image.baidu.com/search/acjson",
            params={"tn": "resultjson_com", "word": keyword, "pn": 0},
            headers=_HEADERS,
            timeout=8,
        )
        data = resp.json()
    except Exception:
        return []
    urls = []
    for item in data.get("data", []):
        if not isinstance(item, dict):
            continue
        # 中图优先，缩略图兜底
        for k in ("middleURL", "thumbURL", "hoverURL"):
            u = item.get(k)
            if u:
                urls.append(u)
                break
        if len(urls) >= count:
            break
    return urls


def _download(url):
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=8)
        return resp.content or None
    except Exception:
        return None
