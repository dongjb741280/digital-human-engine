"""WOPI 协议支撑（Collabora Online 在线编辑 .pptx）：access_token 签发/校验、文件锁、版本号。

均为内存态、单进程适用（与 app.py 现有 ppt-master 任务队列一致）。
"""
import hashlib
import hmac
import threading
import time

import config

_LOCKS = {}          # file_id -> {"lock_id": str, "expires": float}
_VERSION = {}        # file_id -> int
_MU = threading.Lock()

_LOCK_TTL = 30 * 60  # 锁 30 分钟过期


def make_token(file_id: str, user: str, ttl: int = 3600) -> str:
    """签发 access_token：`file_id|user|exp|sig`。"""
    payload = f"{file_id}|{user}|{int(time.time()) + ttl}"
    sig = hmac.new(config.WOPI_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def verify_token(token: str):
    """校验 token，返回 (file_id, user)；非法/过期抛 ValueError。"""
    try:
        payload, sig = token.rsplit("|", 1)
        file_id, user, exp = payload.rsplit("|", 2)
    except ValueError:
        raise ValueError("invalid token")
    expected = hmac.new(config.WOPI_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("bad signature")
    if int(exp) < int(time.time()):
        raise ValueError("token expired")
    return file_id, user


def current_version(file_id: str) -> str:
    with _MU:
        return str(_VERSION.get(file_id, 0))


def bump_version(file_id: str) -> str:
    with _MU:
        v = _VERSION.get(file_id, 0) + 1
        _VERSION[file_id] = v
        return str(v)


def lock(file_id: str, lock_id: str) -> str | None:
    """加锁。成功返回 None；已被他人持有返回现有 lock_id。"""
    with _MU:
        cur = _LOCKS.get(file_id)
        if cur and cur["lock_id"] != lock_id and cur["expires"] > time.time():
            return cur["lock_id"]
        _LOCKS[file_id] = {"lock_id": lock_id, "expires": time.time() + _LOCK_TTL}
        return None


def refresh_lock(file_id: str, lock_id: str) -> str | None:
    """续锁。成功返回 None；冲突返回现有 lock_id（无锁返回空串）。"""
    with _MU:
        cur = _LOCKS.get(file_id)
        if cur and cur["lock_id"] != lock_id:
            return cur["lock_id"]
        if not cur:
            return ""
        cur["expires"] = time.time() + _LOCK_TTL
        return None


def unlock(file_id: str, lock_id: str) -> str | None:
    """解锁。成功返回 None；冲突返回现有 lock_id。"""
    with _MU:
        cur = _LOCKS.get(file_id)
        if cur and cur["lock_id"] != lock_id:
            return cur["lock_id"]
        _LOCKS.pop(file_id, None)
        return None


def get_lock(file_id: str) -> str | None:
    """返回当前锁 id，无锁/过期返回 None。"""
    with _MU:
        cur = _LOCKS.get(file_id)
        if cur and cur["expires"] > time.time():
            return cur["lock_id"]
        return None
