#!/usr/bin/env python3
"""批量下载背景图，上传 MinIO，并输出入库 SQL。

免费图源：
  picsum   —— 无需 key，随机照片（不能指定风格，适合占位/联调）
  unsplash —— 需 UNSPLASH_ACCESS_KEY，可按关键词搜图（推荐用于「简约商务」等具体风格）
  pexels   —— 需 PEXELS_API_KEY，可按关键词搜图

用法：
  python scripts/download_backgrounds.py --source picsum --count 12
  python scripts/download_backgrounds.py --source unsplash --keyword "minimal business background" --count 12
  python scripts/download_backgrounds.py --source pexels --keyword "minimal background" --count 12

说明：
  - 下载后统一上传到 MinIO 的 asset/background/ 目录
  - 输出到 stdout 的 SQL 可直接导入 MySQL（tb_ai_dh_video_background 表）
  - 环境变量：
      UNSPLASH_ACCESS_KEY  Unsplash 访问令牌
      PEXELS_API_KEY       Pexels 访问令牌
      MINIO_ENDPOINT / MINIO_BUCKET / MINIO_ACCESS_KEY / MINIO_SECRET_KEY（默认走 config.py）
"""
import argparse
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from services import minio_util  # noqa: E402


def _http_get(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "ai-digital/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def download_picsum(count: int, width: int, height: int) -> list[tuple[str, bytes]]:
    """Picsum 随机照片（无需 key）。seed 保证每次拿到不同图片。"""
    results = []
    for i in range(count):
        url = f"https://picsum.photos/seed/digital-bg-{int(time.time())}-{i}/{width}/{height}"
        data = _http_get(url)
        results.append((f"随机背景{i + 1}", data))
        print(f"  [{i + 1}/{count}] picsum 下载完成 {len(data)} bytes", file=sys.stderr)
    return results


def download_unsplash(keyword: str, count: int, width: int, height: int, key: str) -> list[tuple[str, bytes]]:
    """Unsplash 按关键词搜图（需 UNSPLASH_ACCESS_KEY）。"""
    url = (
        "https://api.unsplash.com/search/photos"
        f"?query={urllib.parse.quote(keyword)}&per_page={count}&orientation=landscape"
    )
    headers = {"Authorization": f"Client-ID {key}"}
    import json
    data = json.loads(_http_get(url, headers))
    results = []
    for i, photo in enumerate(data.get("results", [])):
        raw = photo["urls"]["raw"] + f"&w={width}&h={height}&fit=crop"
        img = _http_get(raw)
        desc = (photo.get("description") or keyword or "背景").strip()[:40] or f"背景{i + 1}"
        results.append((desc, img))
        print(f"  [{i + 1}/{count}] unsplash 下载完成 {len(img)} bytes", file=sys.stderr)
    return results


def download_pexels(keyword: str, count: int, width: int, height: int, key: str) -> list[tuple[str, bytes]]:
    """Pexels 按关键词搜图（需 PEXELS_API_KEY）。"""
    url = f"https://api.pexels.com/v1/search?query={urllib.parse.quote(keyword)}&per_page={count}&orientation=landscape"
    headers = {"Authorization": key}
    import json
    data = json.loads(_http_get(url, headers))
    results = []
    for i, photo in enumerate(data.get("photos", [])):
        img_url = photo["src"]["large2x"]
        img = _http_get(img_url)
        alt = (photo.get("alt") or keyword or "背景").strip()[:40] or f"背景{i + 1}"
        results.append((alt, img))
        print(f"  [{i + 1}/{count}] pexels 下载完成 {len(img)} bytes", file=sys.stderr)
    return results


def build_sql(bid: str, name: str, key: str) -> str:
    base = f"{config.MINIO_ENDPOINT}/{config.MINIO_BUCKET}"
    url = f"{base}/{key}"
    return (
        f"INSERT INTO tb_ai_dh_video_background "
        f"(id, bg_name, bg_url, bg_type, bg_format, bg_share, creator, create_time, deleted) "
        f"VALUES ('{bid}', '{name}', '{url}', '0', '2', '1', '1', NOW(), 0);"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="批量下载背景图并上传 MinIO")
    parser.add_argument("--source", default="picsum", choices=["picsum", "unsplash", "pexels"])
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--keyword", default="minimal business background")
    args = parser.parse_args()

    if args.source == "picsum":
        items = download_picsum(args.count, args.width, args.height)
    elif args.source == "unsplash":
        key = os.getenv("UNSPLASH_ACCESS_KEY", "")
        if not key:
            sys.exit("错误：--source unsplash 需要环境变量 UNSPLASH_ACCESS_KEY")
        items = download_unsplash(args.keyword, args.count, args.width, args.height, key)
    elif args.source == "pexels":
        key = os.getenv("PEXELS_API_KEY", "")
        if not key:
            sys.exit("错误：--source pexels 需要环境变量 PEXELS_API_KEY")
        items = download_pexels(args.keyword, args.count, args.width, args.height, key)
    else:
        sys.exit(f"未知 source: {args.source}")

    stamp = str(int(time.time() * 1000))
    print("\n=== 上传 MinIO 并生成 SQL ===", file=sys.stderr)
    print("-- 复制以下 SQL 到 mysql 执行即可入库")
    for i, (name, data) in enumerate(items):
        ext = "jpg"
        key = f"asset/background/{stamp}_{i}.{ext}"
        minio_util.upload_bytes(key, data, "image/jpeg")
        print(build_sql(f"bg{stamp}{i}", name, key))


if __name__ == "__main__":
    main()
