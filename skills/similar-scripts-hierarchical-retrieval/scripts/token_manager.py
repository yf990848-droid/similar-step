# -*- coding: utf-8 -*-
"""
token_manager.py
----------------
管理推理接口所需的 x-auth-token。

凭据策略（已简化为公共账密）：
  - 账号密码为团队公共账密，编码后写在 settings.json 的 w3_credential 字段，
    代码用固定盐解码使用。无需用户输入、无凭据文件。
  - token 刷新：签发超过 refresh_after_days(默认 2.5 天) 主动刷新；
    推理接口 401 时被动 force_refresh 重试。
  - 刷新调用 w3tokens：status=="ok" 取 result.newToken。
  - token 缓存在 .cache/token.json，跨次运行复用。
"""

import os
import json
import time
import base64
import hashlib
import logging
import threading

logger = logging.getLogger("similar_retrieval.token")

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_ROOT = os.path.dirname(_THIS_DIR)
_CACHE_DIR = os.path.join(_SKILL_ROOT, ".cache")
_TOKEN_FILE = os.path.join(_CACHE_DIR, "token.json")

_DAY_SECONDS = 24 * 3600

# 公共账密编码用的固定盐（与生成编码串时一致）
_CRED_SALT = b"similar-scripts-hierarchical-retrieval/pubcred/v1"


# --------------------------------------------------------------------------- #
# 公共账密解码（固定盐 XOR + base64，仅做混淆，避免明文直接可见）
# --------------------------------------------------------------------------- #
def _cred_key():
    return hashlib.sha256(_CRED_SALT).digest()


def _xor_crypt(data_bytes, key):
    out = bytearray(len(data_bytes))
    block = b""
    counter = 0
    for i, b in enumerate(data_bytes):
        if i % 32 == 0:
            block = hashlib.sha256(key + counter.to_bytes(4, "big")).digest()
            counter += 1
        out[i] = b ^ block[i % 32]
    return bytes(out)


def _decode_cred(enc_b64):
    key = _cred_key()
    enc = base64.b64decode(enc_b64.encode("ascii"))
    return _xor_crypt(enc, key).decode("utf-8")


def _resolve_credentials(settings):
    """从 settings.w3_credential 解码出 (account, password)。"""
    cred = settings.get("w3_credential") or {}
    acc_enc = cred.get("account_enc")
    pwd_enc = cred.get("password_enc")
    if not acc_enc or not pwd_enc:
        raise RuntimeError("settings.json 缺少 w3_credential（account_enc/password_enc）。")
    try:
        return _decode_cred(acc_enc), _decode_cred(pwd_enc)
    except Exception as e:
        raise RuntimeError("解码公共账密失败：%s" % e)


# --------------------------------------------------------------------------- #
# token 缓存读写
# --------------------------------------------------------------------------- #
def _ensure_cache_dir():
    if not os.path.isdir(_CACHE_DIR):
        os.makedirs(_CACHE_DIR, exist_ok=True)


def _load_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as e:
        logger.warning("读取缓存失败 %s：%s", path, e)
        return None


def _save_json(path, obj):
    _ensure_cache_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 主类
# --------------------------------------------------------------------------- #
class TokenManager(object):
    def __init__(self, settings):
        self.settings = settings
        self.token_api_url = settings["token_api_url"]
        self.token_headers = dict(settings.get("token_headers", {}))
        tok_cfg = settings.get("token", {})
        self.refresh_after_days = float(tok_cfg.get("refresh_after_days", 2.5))
        self.proxy = settings.get("proxy")
        self.verify_ssl = settings.get("verify_ssl", True)
        self._token = None
        self._lock = threading.RLock()
        self._last_refresh_ts = 0.0

    # ---- 对外接口 ---- #
    def get_token(self):
        """返回有效 token；按需自动刷新。线程安全。"""
        with self._lock:
            if self._token:
                return self._token
            cache = _load_json(_TOKEN_FILE)
            if cache and self._is_fresh(cache):
                self._token = cache.get("token")
                if self._token:
                    logger.info("复用本地 token（未过期）。")
                    return self._token
            return self._refresh()

    def force_refresh(self, stale_token=None):
        """供 401 被动刷新调用。线程安全 + 双重检查。"""
        with self._lock:
            if (
                stale_token is not None
                and self._token
                and self._token != stale_token
            ):
                logger.info("token 已被其他线程刷新，复用新 token。")
                return self._token
            logger.info("触发 token 强制刷新（401 或主动）。")
            self._token = None
            return self._refresh()

    # ---- 内部 ---- #
    def _is_fresh(self, cache):
        issued = cache.get("issued_at", 0)
        age_days = (time.time() - issued) / _DAY_SECONDS
        return age_days < self.refresh_after_days

    def _proxies(self):
        if self.proxy:
            return {"http": self.proxy, "https": self.proxy}
        return None

    def _refresh(self):
        """用公共账密刷新 token。"""
        if requests is None:
            raise RuntimeError("缺少 requests 库，无法刷新 token。请先 pip install requests。")
        account, password = _resolve_credentials(self.settings)
        new_token = self._call_w3tokens(account, password)
        _save_json(_TOKEN_FILE, {
            "token": new_token,
            "issued_at": time.time(),
            "account": account,
        })
        self._token = new_token
        self._last_refresh_ts = time.time()
        logger.info("token 刷新成功并已缓存。")
        return new_token

    def _call_w3tokens(self, account, password):
        body = {"account": account, "password": password}
        try:
            resp = requests.post(
                self.token_api_url,
                headers=self.token_headers,
                json=body,
                proxies=self._proxies(),
                verify=self.verify_ssl,
                timeout=30,
            )
        except requests.RequestException as e:
            raise RuntimeError("调用 w3tokens 接口网络异常：%s" % e)

        if resp.status_code != 200:
            raise RuntimeError(
                "w3tokens 返回非 200：%s %s" % (resp.status_code, resp.text[:200])
            )
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError("w3tokens 响应非 JSON：%s" % resp.text[:200])

        if data.get("status") != "ok":
            raise RuntimeError("w3tokens 刷新失败，status=%s" % data.get("status"))
        result = data.get("result") or {}
        new_token = result.get("newToken")
        if not new_token:
            raise RuntimeError("w3tokens 响应缺少 result.newToken。")
        return new_token


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # 自测：解码 settings 里的公共账密
    import config_loader
    s = config_loader.load_settings()
    acc, pwd = _resolve_credentials(s)
    print("解码公共账号:", acc)
    print("解码公共密码:", "*" * len(pwd), "(长度 %d)" % len(pwd))
