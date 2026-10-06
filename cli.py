"""命令行工具：查询工具列表、调用工具，适合脚本集成。

依赖运行中的编排服务（默认 http://localhost:60013）。

用法：
  python cli.py tools
  python cli.py run get_first_frame --param '{"input_key":"video/x.mp4","output_key":"video/x-frame.jpg"}'
"""
import argparse
import json
import os
import urllib.request

BASE = os.getenv("ENGINE_API", "http://localhost:60013")


def _request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    parser = argparse.ArgumentParser(prog="cli", description="数字人引擎命令行工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tools", help="列出所有可用工具")

    run_p = sub.add_parser("run", help="调用一个工具")
    run_p.add_argument("tool", help="工具名（见 tools）")
    run_p.add_argument("--param", default="{}", help="JSON 参数字符串")

    args = parser.parse_args()

    if args.cmd == "tools":
        data = _request("GET", "/tools")
        for t in data.get("data", []):
            print(f"{t['name']}\t{t['description']}")
    elif args.cmd == "run":
        result = _request("POST", "/tools/run", {"tool": args.tool, "params": json.loads(args.param)})
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
