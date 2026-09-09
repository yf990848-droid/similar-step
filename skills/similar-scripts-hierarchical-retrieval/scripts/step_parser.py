# -*- coding: utf-8 -*-
"""
step_parser.py
--------------
按触发点正则把框架脚本切成若干步骤，并为每个步骤生成"截断到该步骤为止的上文"
（above_text），用于逐步骤调检索接口。

正则选择（方案 2）：
  候选列表 = [XML 取到的正则] + settings.fallback_trigger_regexes（按序去重）
  逐条试匹配，第一条匹配数 > 0 的即采用；全 0 抛错。
"""

import re
import logging

logger = logging.getLogger("similar_retrieval.parser")


class NoStepMatchedError(Exception):
    """所有候选正则都匹配不到任何步骤。"""


def _dedup_keep_order(items):
    seen = set()
    out = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _build_regex_candidates(xml_regex, fallback_regexes):
    return _dedup_keep_order([xml_regex] + list(fallback_regexes or []))


def _try_compile(pattern):
    try:
        return re.compile(pattern)
    except re.error as e:
        logger.warning("正则编译失败，跳过：%r（%s）", pattern, e)
        return None


def _module_docstring_end(script_text):
    r"""
    定位模块级 docstring（脚本开头、真正代码之前的第一段 \"\"\"...\"\"\" 或 '''...'''）
    的结束字符位置；没有则返回 0。

    只把"出现在任何实际代码语句之前"的三引号串视为模块 docstring，
    避免把代码里普通的多行字符串误判。允许 docstring 前有：
    注释行(#...)、编码声明、空行。
    """
    idx = 0
    n = len(script_text)
    while idx < n:
        # 跳过空白
        while idx < n and script_text[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            return 0
        # 跳过整行注释（如 # encoding:utf-8）
        if script_text[idx] == "#":
            nl = script_text.find("\n", idx)
            if nl == -1:
                return 0
            idx = nl + 1
            continue
        # 遇到三引号 → 这是模块 docstring
        for quote in ('"""', "'''"):
            if script_text.startswith(quote, idx):
                close = script_text.find(quote, idx + 3)
                if close == -1:
                    return 0  # 未闭合，放弃
                return close + 3  # docstring 结束位置
        # 遇到任何其它非空白非注释字符 → 没有模块 docstring
        return 0
    return 0


def parse_steps(script_text, xml_regex, fallback_regexes):
    """
    返回 (steps, used_regex)：
      steps = [{step_index, trigger_point, above_text}, ...]
      used_regex = 实际命中的正则字符串（供上层打日志，不进输出文件）

    above_text = 脚本从开头到"该步骤匹配结束位置"的全部文本（截断式上文）。
    """
    candidates = _build_regex_candidates(xml_regex, fallback_regexes)
    if not candidates:
        raise NoStepMatchedError("没有可用的触发点正则。")

    # 模块级 docstring 内的注释不算步骤：从 docstring 之后开始匹配
    search_start = _module_docstring_end(script_text)
    if search_start > 0:
        logger.info("跳过模块 docstring（前 %d 字符）后再匹配步骤。", search_start)

    chosen = None
    chosen_matches = None
    for pat in candidates:
        compiled = _try_compile(pat)
        if compiled is None:
            continue
        matches = list(compiled.finditer(script_text, search_start))
        if matches:
            chosen = pat
            chosen_matches = matches
            logger.info("采用正则：%r（匹配 %d 个步骤）", pat, len(matches))
            break
        else:
            logger.info("正则未命中：%r", pat)

    if chosen is None:
        raise NoStepMatchedError(
            "所有候选正则都匹配不到步骤，请检查 PDU 正则配置或脚本格式。"
        )

    steps = []
    for idx, m in enumerate(chosen_matches):
        trigger_point = m.group(0)
        end_pos = m.end()  # 截断到该步骤行结束
        # above_text 仍从【文件真正开头】截取，确保包含 CaseID/import 等头部信息，
        # 供服务端提取用例编号。
        above_text = script_text[:end_pos]
        steps.append({
            "step_index": idx,
            "trigger_point": trigger_point.rstrip("\n"),
            "above_text": above_text,
        })

    return steps, chosen


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    demo = (
        "import os\n"
        "class TestCase:\n"
        "    def setup(self):\n"
        "        self.logStep(\"1、开启同步\")\n"
        "        do_something()\n"
        "    def teardown(self):\n"
        "        self.logStep('2、关闭模拟桩')\n"
    )
    fallback = [
        "((logger\\.step\\(.*?\\))|(#\\s*.*?))\\n",
        "(self\\.logStep\\(.*?\\))\\n",
    ]
    steps, used = parse_steps(demo, None, fallback)
    print("命中正则：", used)
    for s in steps:
        print("步骤", s["step_index"], "->", s["trigger_point"],
              "| above_text 长度", len(s["above_text"]))
