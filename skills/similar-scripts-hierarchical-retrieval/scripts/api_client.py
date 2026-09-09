# -*- coding: utf-8 -*-
"""
api_client.py
-------------
调用推理接口做单步骤相似检索，读取 SSE 流，汇总候选。

协议（已确认）：
  POST infer_api_url，header 带 x-auth-token，SSE 流式返回。
  - 每个事件可能带 retrieval_result（单元素列表），含 task_id → 收为候选
  - request_status == "completed" 或流结束 → 停止收集
  - 状态含 error/failed → 该步骤失败，抛错
  候选按 task_id 升序（=相关性降序），同文件多片段全保留。
  401/鉴权失败 → token_manager.force_refresh() 后重试一次。
"""

import json
import logging

logger = logging.getLogger("similar_retrieval.api")

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

# 候选只保留这 8 个字段（schema 已冻结）
_CANDIDATE_FIELDS = (
    "task_id",            # 仅内部去重/日志用，主脚本汇总时丢弃
    "similarity",         # → 相似度
    "generated_snippet",  # → 参考代码
    "trigger_point",      # → 相似步骤
)

_FAIL_TOKENS = ("error", "failed", "fail")


class StepRetrievalError(Exception):
    """单步骤检索失败。"""


class ApiClient(object):
    def __init__(self, settings, token_manager):
        if requests is None:
            raise RuntimeError("缺少 requests 库，请先 pip install requests。")
        self.settings = settings
        self.token_manager = token_manager
        self.infer_api_url = settings["infer_api_url"]
        self.fixed_request_fields = settings.get("fixed_request_fields", {})
        self.fixed_headers = settings.get("fixed_headers", {})
        req_cfg = settings.get("request", {})
        self.per_step_timeout = req_cfg.get("per_step_timeout_sec", 120)
        self.connect_timeout = req_cfg.get("connect_timeout_sec", 10)
        self.retry_on_401 = settings.get("token", {}).get("retry_on_401", True)
        self.proxy = settings.get("proxy")
        # SSL 证书验证：内网自签 CA 时可设 false 跳过，或设为 CA 证书文件路径
        self.verify_ssl = settings.get("verify_ssl", True)
        # 调试：开启后把【第一次】请求的原始响应字节 dump 到文件，用于排查 SSE 格式
        self.debug_dump_raw = settings.get("debug_dump_raw", False)
        self._dumped = False  # 仅 dump 一次

    # ------------------------------------------------------------------ #
    def retrieve_one_step(self, above_text, pdu_name, product_line):
        """对单个步骤的 above_text 做检索，返回候选列表（按 task_id 升序）。"""
        token = self.token_manager.get_token()
        try:
            return self._do_request(above_text, pdu_name, product_line, token)
        except _Unauthorized:
            if not self.retry_on_401:
                raise StepRetrievalError("鉴权失败(401)，且未开启自动刷新。")
            logger.info("遇到 401，强制刷新 token 后重试一次。")
            # 把触发 401 的旧 token 传入，force_refresh 做双重检查：
            # 若已有其他线程刷过则复用，不重复刷新。
            token = self.token_manager.force_refresh(stale_token=token)
            return self._do_request(above_text, pdu_name, product_line, token)

    # ------------------------------------------------------------------ #
    def _build_body(self, above_text, pdu_name, product_line):
        body = dict(self.fixed_request_fields)
        body["plugin_request_param"] = {
            "above_text": above_text,
            "pdu_name": pdu_name,
            "product_line": product_line,
        }
        return body

    def _build_headers(self, token):
        headers = dict(self.fixed_headers)
        headers["x-auth-token"] = token
        return headers

    def _proxies(self):
        if self.proxy:
            return {"http": self.proxy, "https": self.proxy}
        return None

    def _do_request(self, above_text, pdu_name, product_line, token):
        body = self._build_body(above_text, pdu_name, product_line)
        headers = self._build_headers(token)
        try:
            resp = requests.post(
                self.infer_api_url,
                headers=headers,
                json=body,
                stream=True,
                proxies=self._proxies(),
                verify=self.verify_ssl,
                timeout=(self.connect_timeout, self.per_step_timeout),
            )
        except requests.RequestException as e:
            raise StepRetrievalError("检索接口网络异常：%s" % e)

        if resp.status_code in (401, 403):
            resp.close()
            raise _Unauthorized()
        if resp.status_code != 200:
            text = self._safe_peek(resp)
            raise StepRetrievalError(
                "检索接口返回非 200：%s %s" % (resp.status_code, text)
            )

        # 调试：把响应的原始字节 dump 出来（追加），便于核对真实 SSE / error
        if self.debug_dump_raw:
            self._dump_raw(resp)
            # dump 后该响应体已被读完，无法再解析，直接返回空
            return []

        return self._consume_sse(resp)

    def _dump_raw(self, resp):
        import os as _os
        dump_path = _os.path.join(
            _os.path.dirname(_os.path.abspath(__file__)), "_sse_raw_dump.txt"
        )
        try:
            raw = resp.raw.read(decode_content=True)
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                text = repr(raw)
            # 追加模式：每个步骤一段，便于对比 0候选 与 error 步骤的差异
            with open(dump_path, "a", encoding="utf-8") as f:
                f.write("\n\n########## 步骤响应 dump ##########\n")
                f.write("=== status_code: %s ===\n" % resp.status_code)
                f.write("=== raw body (decoded utf-8, errors=replace) ===\n")
                f.write(text)
            logger.info("已追加 dump 原始响应到：%s", dump_path)
        except Exception as e:
            logger.error("dump 原始响应失败：%s", e)
        finally:
            resp.close()

    @staticmethod
    def _safe_peek(resp):
        try:
            return resp.text[:200]
        except Exception:
            return "<无法读取响应体>"

    # ------------------------------------------------------------------ #
    def _consume_sse(self, resp):
        """逐行读 SSE，收集候选，直到 completed / 流结束。"""
    def _consume_sse(self, resp):
        """
        读取完整响应体，按 data: 事件块解析，收集候选。

        真实 SSE 形态：多个 `data:{长JSON}` 事件，块间以空行分隔，
        最后一个事件 request_status=completed 不带候选。
        不用 iter_lines（对超长单行 + 多事件不可靠），改为整体读取后按行切。

        错误处理：某事件 request_status=error（如未达相似度阈值、用例编号提取失败）
        表示该步骤检索无有效结果，记为空候选、不终止整体流程，错误详情打日志。
        """
        candidates = []
        request_id = None
        step_error = None
        try:
            raw = resp.raw.read(decode_content=True)
        except Exception as e:
            resp.close()
            raise StepRetrievalError("读取响应流失败：%s" % e)
        finally:
            resp.close()

        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)

        for payload in self._iter_sse_events(text):
            request_id = payload.get("request_id", request_id)
            status = str(payload.get("request_status", "")).lower()

            # 收候选（任何带 retrieval_result 的事件）
            results = payload.get("retrieval_result")
            if results:
                for item in results:
                    candidates.append(self._pick_fields(item))

            # 记录错误（但不立即终止，作为"该步骤无结果"处理）
            if "error" in status or "fail" in status:
                exc = payload.get("exception") or {}
                step_error = "%s %s" % (
                    exc.get("error_code", ""), exc.get("error_msg", "")
                )

        # 同一文件可能多片段，按 task_id 升序；去重（按 task_id + snippet）
        candidates = self._dedup_candidates(candidates)
        candidates.sort(key=lambda c: (c.get("task_id") is None, c.get("task_id", 0)))

        if not candidates and step_error:
            logger.info(
                "本步骤无候选（服务端：%s，request_id=%s）",
                step_error.strip(), request_id,
            )
        else:
            logger.info(
                "本步骤收集候选 %d 个（request_id=%s）", len(candidates), request_id
            )
        return candidates

    @staticmethod
    def _iter_sse_events(text):
        """
        从完整响应文本里逐个产出事件 JSON 对象。
        按行扫描，取每个以 'data:' 开头的行，解析其后的 JSON。
        """
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or not line.startswith("data:"):
                continue
            body = line[len("data:"):].strip()
            if not body or body[0] not in "{[":
                continue
            try:
                yield json.loads(body)
            except ValueError:
                continue

    @staticmethod
    def _dedup_candidates(candidates):
        """按 (task_id, generated_snippet) 去重，保留首次出现。"""
        seen = set()
        out = []
        for c in candidates:
            key = (c.get("task_id"), c.get("generated_snippet"))
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
        return out

    @staticmethod
    def _extract_data_json(line):
        """
        解析一行。兼容两种形态：
          - SSE 标准： "data: {json}"
          - 直接就是 json 行： "{json}"
        非 JSON 行返回 None。
        """
        if line.startswith("data:"):
            line = line[len("data:"):].strip()
        if not line or line[0] not in "{[":
            return None
        try:
            return json.loads(line)
        except ValueError:
            return None

    @staticmethod
    def _pick_fields(item):
        if not isinstance(item, dict):
            return {}
        return {k: item.get(k) for k in _CANDIDATE_FIELDS}


class _Unauthorized(Exception):
    """内部信号：401/403。"""
