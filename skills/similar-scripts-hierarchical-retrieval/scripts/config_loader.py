# -*- coding: utf-8 -*-
"""
config_loader.py
----------------
读取 settings.json 与 TestMate CodeElf 插件持久化 XML，
对外提供：product_line / pdu_name / xml_trigger_regex。

XML 中两个关键 option 的 value 是 HTML 转义后的 JSON 字符串：
  - agentPduConfigCache       -> 取当前 product_line / pdu_name
  - advancedPduProductConfig  -> 数组，按 pdu_name 匹配，取 trigger_point_regulars

设计原则：XML 缺失或解析失败都不致命，返回空值，由上层走 fallback 正则。
环境变量可覆盖：
  TESTAGENT_PRODUCT_LINE / TESTAGENT_PDU_NAME / TESTAGENT_XML_PATH
"""

import os
import re
import sys
import json
import glob
import html
import logging

logger = logging.getLogger("similar_retrieval.config")

# settings.json 位于本脚本上一级目录
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_ROOT = os.path.dirname(_THIS_DIR)
_SETTINGS_PATH = os.path.join(_SKILL_ROOT, "settings.json")


def load_settings():
    """读取 settings.json，返回 dict。文件缺失则抛出明确错误。"""
    if not os.path.isfile(_SETTINGS_PATH):
        raise FileNotFoundError("找不到 settings.json：%s" % _SETTINGS_PATH)
    with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _candidate_xml_paths(settings):
    """按优先级产出 XML 候选路径：环境变量 > settings.xml_path > 默认 AppData 路径。"""
    env_path = os.environ.get("TESTAGENT_XML_PATH")
    if env_path:
        yield env_path

    cfg_path = (settings or {}).get("xml_path")
    if cfg_path and cfg_path != "auto":
        yield cfg_path

    # 默认 Windows AppData 路径，PyCharm 版本号通配
    appdata = os.environ.get("APPDATA")
    if appdata:
        pattern = os.path.join(
            appdata, "JetBrains", "PyCharm*", "options",
            "TestMateCodeElfPersistentState.xml",
        )
        for p in sorted(glob.glob(pattern), reverse=True):  # 版本号大的优先
            yield p


def _find_xml(settings):
    for p in _candidate_xml_paths(settings):
        if p and os.path.isfile(p):
            return p
    return None


def _read_option_json(xml_text, option_name):
    """
    从 XML 文本里抽取 <option name="xxx" value="...JSON..." /> 的 value，
    HTML 反转义后 json.loads。失败返回 None。
    """
    m = re.search(
        r'<option\s+name="%s"\s+value="(.*?)"\s*/>' % re.escape(option_name),
        xml_text,
        re.S,
    )
    if not m:
        return None
    raw = html.unescape(m.group(1))
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as e:
        logger.warning("解析 option[%s] JSON 失败：%s", option_name, e)
        return None


def _extract_trigger_regex(advanced_cfg, pdu_name):
    """
    从 advancedPduProductConfig（数组）里按 pdu_name 匹配元素，
    取 regulars.regulars_fields_arr 中 name == trigger_point_regulars 的 value。
    """
    if not isinstance(advanced_cfg, list):
        return None
    # 先按 pdu_name 精确匹配；匹配不到则退而取第一个有正则的元素
    matched = None
    for item in advanced_cfg:
        if not isinstance(item, dict):
            continue
        if pdu_name and item.get("pdu_name") == pdu_name:
            matched = item
            break
    candidates = [matched] if matched else [c for c in advanced_cfg if isinstance(c, dict)]
    for item in candidates:
        regulars = (item or {}).get("regulars") or {}
        arr = regulars.get("regulars_fields_arr") or []
        for field in arr:
            if isinstance(field, dict) and field.get("name") == "trigger_point_regulars":
                val = field.get("value")
                if val:
                    return _normalize_xml_regex(val)
    return None


def _normalize_xml_regex(pattern):
    r"""
    修正 XML 取出的正则的反斜杠层数。

    TestMate 在 XML 里把正则按"再转义一层"存储：真实正则 \. \( \) \s \n
    在 XML 经 html.unescape + json.loads 后会变成 \\. \\( \\s \\n（双反斜杠）。
    Python re 需要单反斜杠，故把连续两个反斜杠折叠为一个。

    例：'((logger\\.step\\(.*?\\))|(#\\s*.*?))\\n'
      → '((logger\.step\(.*?\))|(#\s*.*?))\n'

    仅对 XML 来源的正则调用；settings 里手写的 fallback 正则已是正确层数，不处理。
    """
    if not pattern:
        return pattern
    # 将每两个连续反斜杠折叠为一个；奇数个时保留最后一个
    return pattern.replace("\\\\", "\\")


class Config(object):
    """对外的配置载体。"""

    def __init__(self, settings, product_line, pdu_name, xml_trigger_regex, xml_path):
        self.settings = settings
        self.product_line = product_line
        self.pdu_name = pdu_name
        self.xml_trigger_regex = xml_trigger_regex  # 可能为 None
        self.xml_path = xml_path  # 实际命中的 XML 路径，可能为 None


def load_config():
    """
    汇总入口：返回 Config 对象。
    任何 XML 问题都降级为"取不到"，不抛错（除非 settings.json 本身缺失）。
    """
    settings = load_settings()

    product_line = None
    pdu_name = None
    xml_trigger_regex = None

    xml_path = _find_xml(settings)
    if not xml_path:
        logger.warning("未找到 TestMate XML，将依赖环境变量与兜底正则。")
    else:
        try:
            with open(xml_path, "r", encoding="utf-8") as f:
                xml_text = f.read()
        except OSError as e:
            logger.warning("读取 XML 失败：%s", e)
            xml_text = ""

        if xml_text:
            pdu_cache = _read_option_json(xml_text, "agentPduConfigCache")
            if isinstance(pdu_cache, dict):
                product_line = pdu_cache.get("product_line")
                pdu_name = pdu_cache.get("pdu_name")

            advanced = _read_option_json(xml_text, "advancedPduProductConfig")
            xml_trigger_regex = _extract_trigger_regex(advanced, pdu_name)

    # 环境变量覆盖
    product_line = os.environ.get("TESTAGENT_PRODUCT_LINE", product_line)
    pdu_name = os.environ.get("TESTAGENT_PDU_NAME", pdu_name)

    logger.info(
        "配置载入：product_line=%s pdu_name=%s xml=%s 正则=%s",
        product_line, pdu_name, xml_path,
        "命中" if xml_trigger_regex else "无",
    )

    return Config(settings, product_line, pdu_name, xml_trigger_regex, xml_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cfg = load_config()
    print(json.dumps({
        "product_line": cfg.product_line,
        "pdu_name": cfg.pdu_name,
        "xml_trigger_regex": cfg.xml_trigger_regex,
        "xml_path": cfg.xml_path,
    }, ensure_ascii=False, indent=2))
