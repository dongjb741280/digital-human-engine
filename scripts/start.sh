#!/usr/bin/env bash
# 一键启动数字人 Python 侧服务：GPT-SoVITS（9880）+ 编排服务（60013）
# 用法：./scripts/start.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # digital-human-engine 根目录
GSV="${GSV:-$HERE/../GPT-SoVITS}"                          # GPT-SoVITS 仓库（可用 GSV= 覆盖）

log() { echo "[start] $*"; }

wait_port() {
  local port="$1" name="$2" i
  for i in $(seq 1 120); do
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      log "$name 已在 :$port 监听"
      return 0
    fi
    sleep 1
  done
  log "等待 $name 超时（:$port 未监听）"
  return 1
}

# 1. GPT-SoVITS 推理服务（9880）
if lsof -nP -iTCP:9880 -sTCP:LISTEN >/dev/null 2>&1; then
  log "GPT-SoVITS 已在 :9880 运行，跳过"
else
  if [ ! -f "$GSV/api_v2.py" ]; then
    log "未找到 GPT-SoVITS 仓库（$GSV），跳过；可用 GSV=/path 指定"
  else
    log "启动 GPT-SoVITS api_v2 ..."
    (cd "$GSV" && nohup ./venv/bin/python api_v2.py -a 127.0.0.1 -p 9880 > /tmp/gpt_sovits.log 2>&1 &)
    wait_port 9880 "GPT-SoVITS"
  fi
fi

# 2. 编排服务（60013）
if lsof -nP -iTCP:60013 -sTCP:LISTEN >/dev/null 2>&1; then
  log "编排服务已在 :60013 运行，跳过"
else
  log "启动编排服务 uvicorn ..."
  (cd "$HERE" && nohup ./.venv/bin/uvicorn app:app --host 0.0.0.0 --port 60013 > /tmp/digital-human-engine.log 2>&1 &)
  wait_port 60013 "编排服务"
fi

log "完成。日志：/tmp/gpt_sovits.log、/tmp/digital-human-engine.log"
