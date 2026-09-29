"""百度图片在线搜索：按关键词抓一张图，带进程内缓存。

非官方接口（image.baidu.com 网页搜索），版权/稳定性/分辨率风险已接受。
返回图片字节；搜图失败返回 None，由调用方降级。
"""
import json
import urllib.parse
import urllib.request

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://image.baidu.com/",
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
    url = "https://image.baidu.com/search/acjson"
    params = {
        "tn": "resultjson_com",
        "ipn": "rj",
        "word": keyword,
        "pn": 0,
        "rn": count,
        "ie": "utf-8",
    }
    qs = urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(f"{url}?{qs}", headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return []
    start = text.find("{")
    if start == -1:
        return []
    try:
        data = json.loads(text[start:])
    except json.JSONDecodeError:
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
    return urls


def _download(url):
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read()
        return data or None
    except Exception:
        return None
