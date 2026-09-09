# -*- coding: utf-8 -*-
"""
retrieve_similar_steps.py
-------------------------
主入口。用法：
    python retrieve_similar_steps.py <框架脚本路径>

流程：
  载入配置 → 读脚本 → 确保 token → 切步骤 → 并发逐步骤检索 → 写 <脚本名>_explore.json
stdout 末尾打印输出文件绝对路径，供子代理回传。
"""

import os
import sys
import json
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

# 允许直接以脚本方式运行时找到同目录模块
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import config_loader
import token_manager as token_mgr
import step_parser
import api_client

logger = logging.getLogger("similar_retrieval.main")


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,  # 日志走 stderr，stdout 只留最终结果路径
    )


def _read_script(path):
    if not os.path.isfile(path):
        raise FileNotFoundError("框架脚本不存在：%s" % path)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _output_path(script_path):
    base, _ext = os.path.splitext(script_path)
    return base + "_explore.json"


def _retrieve_all_steps(client, steps, pdu_name, product_line, max_workers):
    """
    并发对每个步骤检索。任一步骤失败则整体失败（按约束终止后续）。
    返回 step_index -> candidates 的 dict。
    """
    results = {}
    errors = {}

    def _work(step):
        return step["step_index"], client.retrieve_one_step(
            step["above_text"], pdu_name, product_line
        )

    workers = max(1, min(max_workers, len(steps))) if steps else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(_work, s): s for s in steps}
        for fut in as_completed(future_map):
            step = future_map[fut]
            try:
                idx, cands = fut.result()
                results[idx] = cands
            except Exception as e:  # noqa: BLE001 收敛为整体失败
                errors[step["step_index"]] = str(e)
                logger.error("步骤 %d 检索失败：%s", step["step_index"], e)

    if errors:
        # 按约束：任一步骤失败则终止
        first_idx = sorted(errors)[0]
        raise RuntimeError(
            "步骤检索失败（共 %d 个失败），首个：步骤 %d -> %s"
            % (len(errors), first_idx, errors[first_idx])
        )
    return results


def run(script_path):
    t0 = time.time()

    cfg = config_loader.load_config()
    settings = cfg.settings

    # 若关闭 SSL 验证（内网自签场景），压掉 requests 的 InsecureRequestWarning 刷屏
    if not settings.get("verify_ssl", True):
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            logger.info("已关闭 SSL 证书验证（verify_ssl=false），仅建议用于内网自签场景。")
        except Exception:
            pass

    script_text = _read_script(script_path)

    # token（按需自动刷新；首次可能交互输入账号密码）
    tm = token_mgr.TokenManager(settings)

    # 切步骤
    steps, used_regex = step_parser.parse_steps(
        script_text,
        cfg.xml_trigger_regex,
        settings.get("fallback_trigger_regexes", []),
    )
    logger.info("命中正则(仅日志)：%r", used_regex)
    logger.info("共切出 %d 个步骤。", len(steps))

    # 预热 token：在并发开始【之前】于主线程同步取一次 token。
    # 这样首次交互输入账号密码、刷新、写缓存都在单线程里完成，
    # 后续并发的各 worker 直接复用缓存 token，不会多个线程同时弹出输入提示。
    logger.info("准备 token …")
    tm.get_token()

    # 检索
    client = api_client.ApiClient(settings, tm)
    max_workers = settings.get("request", {}).get("max_workers", 4)

    # 调试模式：串行跑所有步骤，每个响应都 dump（追加），便于对比 0候选/error
    if settings.get("debug_dump_raw", False):
        logger.info("调试模式 debug_dump_raw=true：串行请求所有步骤并 dump 原始响应。")
        # 清空旧 dump 文件
        import os as _os
        dump_path = _os.path.join(_THIS_DIR, "_sse_raw_dump.txt")
        try:
            if _os.path.exists(dump_path):
                _os.remove(dump_path)
        except OSError:
            pass
        for s in steps:
            try:
                client.retrieve_one_step(
                    s["above_text"], cfg.pdu_name, cfg.product_line
                )
            except Exception as e:  # noqa: BLE001
                logger.error("步骤 %d dump 时异常：%s", s["step_index"], e)
        logger.info("调试 dump 完成，请查看 scripts/_sse_raw_dump.txt。")
        return None

    idx_to_cands = _retrieve_all_steps(
        client, steps, cfg.pdu_name, cfg.product_line, max_workers
    )

    # 汇总为中文精简结构。候选只保留 相似步骤/相似度/参考代码，按相似度降序。
    out_steps = []
    for s in steps:
        raw_cands = idx_to_cands.get(s["step_index"], [])
        slim = []
        for c in raw_cands:
            slim.append({
                "相似步骤": c.get("trigger_point"),
                "相似度": c.get("similarity"),
                "参考代码": c.get("generated_snippet"),
            })
        # 相似度降序（None 视为最小排最后）
        slim.sort(key=lambda x: (x["相似度"] is None, -(x["相似度"] or 0)))
        out_steps.append({
            "步骤序号": s["step_index"],
            "触发点": s["trigger_point"],
            "相似候选": slim,
        })

    duration = round(time.time() - t0, 3)
    result = {
        "步骤总数": len(out_steps),
        "耗时秒": duration,
        "步骤列表": out_steps,
    }

    out_path = _output_path(script_path)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info("完成，耗时 %.2fs，输出：%s", duration, out_path)
    return os.path.abspath(out_path)


def main(argv):
    _setup_logging()
    if len(argv) < 2:
        sys.stderr.write("用法：python retrieve_similar_steps.py <框架脚本路径>\n")
        return 2
    script_path = argv[1]
    try:
        out_path = run(script_path)
    except step_parser.NoStepMatchedError as e:
        sys.stderr.write("检索异常（无法识别步骤）：%s\n" % e)
        return 3
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("检索异常：%s\n" % e)
        return 1
    # stdout 只打印结果路径，方便子代理直接取用
    if out_path:
        print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
