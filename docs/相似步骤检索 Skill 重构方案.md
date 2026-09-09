一句话目标
保持 skill 的输入输出契约不变（输入框架脚本路径 → 输出 *_explore.json）， 把内部"本地 BM25 检索"换成"调用 RAG 推理接口做逐步骤检索"。外层调用方无需改动。
目录结构
similar-scripts-hierarchical-retrieval/
├── SKILL.md              # 指挥 agent：Bash 跑主脚本，返回结果路径
├── settings.json         # 框架级配置（接口地址、固定字段、兜底正则、token策略）
├── scripts/
│   ├── retrieve_similar_steps.py  # 主入口：串起全流程，写出 explore.json
│   ├── config_loader.py           # 解析 XML + 环境变量，拿 product_line/pdu_name/正则
│   ├── step_parser.py             # 按正则切步骤，为每步算 above_text
│   ├── api_client.py              # 调接口 + 读 SSE 流 + 汇总候选
│   └── token_manager.py           # x-auth-token 缓存/刷新（密码加密落盘，自动刷新）
└── .cache/
    ├── token.json        # { token, issued_at, account }
    └── credentials.json  # 加密后的密码（仅当前用户可读）
整体流程
读 settings + 解析 XML
        ↓
确保 token 有效（2.5天主动刷新 / 401被动刷新，密码自动解密复用）
        ↓
切步骤（XML正则优先 → 匹配0则兜底正则 → 全0报错）
        ↓
每个步骤并发调接口：POST + 读SSE流 + 收候选(按task_id升序)
        ↓
汇总写出 <脚本名>_explore.json
关键决策
项	结论
接口	POST snapengine.../infer/request，SSE流式
调用粒度	每步骤截断 above_text 各调一次
请求体	固定字段 + plugin_request_param:{above_text, pdu_name, product_line}
SSE读取	带 retrieval_result 就收；completed/流关停止；含 error/failed 报错
候选排序	按 task_id 升序（=相关性降序）；同文件多片段全保留
空结果	某步骤无候选 → 记空、不报错、继续
正则	XML按pdu_name取 → 匹配0则兜底(UPCF+视频两条) → 全0报错
token刷新	result.newToken → x-auth-token；2.5天主动 + 401被动
密码存储	纯本地加密文件（本机派生密钥对称加密，自动刷新无需人工）
首次使用	交互输入账号+密码 → 密码加密落盘；之后全自动
配置来源	product_line/pdu_name 从XML取（跟随脚本业务）；URL等进settings
环境	Python 3.9，优先 requests，默认直连+可选代理
接口契约速查
推理请求体（POST，header 带 x-auth-token、caller_name:ai4xxx、agent-type:TestAgent）
{
  "language": "python",
  "msg_type": "INFERENCE",
  "plugin_request_param": {
    "above_text": "<截断到当前步骤的脚本上文>",
    "pdu_name": "分组-UPCF",
    "product_line": "云核心网产品线"
  },
  "user_rag_switch": true,
  "result_type": ["use_cache", "use_retrieval"],
  "trigger_mode": "MANUAL",
  "trigger_node": "RAG_RETRIEVAL"
}
推理响应（SSE，多个事件同一 request_id）
每个 dispatched 事件带 1 个候选（retrieval_result 单元素 + task_id）
最后 completed 事件不带候选 = 终止信号
token 接口（POST romapi.../v2/w3tokens）
header（写死，仅此接口用）： X-HW-ID: com.huawel.ipd.coretool.coremlops、 X-HW-APPKEY: 3JUnVhZLKMnI03FPOAwOxA=、Content-Type: application/json
body：{account, password}
成功 status=="ok" → 取 result.newToken
无过期字段 → 本地记 issued_at，超 2.5 天主动刷新
输出 schema
{
  "query_file": "目标框架脚本路径",
  "pdu_name": "分组-UPCF",
  "product_line": "云核心网产品线",
  "steps_count": 5,
  "duration": 12.3,
  "steps": [
    {
      "step_index": 0,
      "trigger_point": "self.logStep('1、开启同步...')",
      "candidates": [
        {
          "task_id": 0,
          "similarity": 0.9925,
          "generated_snippet": "        self.http_stack.clear()",
          "trigger_point": "关闭模拟桩",
          "test_case_name": "...",
          "test_case_number": "...",
          "file_name": "..._test.py",
          "script_url": "https://..."
        }
      ]
    }
  ]
}
候选 8 字段。
settings.json 模板
{
  "infer_api_url": "https://snapengine.cida.cce.prod-szv-y.dragon.tools.huawei.com/v1/test-agent/infer/request",
  "token_api_url": "https://romapi.huawei.com/api/ssoproxysvr/v2/w3tokens",
  "fixed_request_fields": {
    "language": "python",
    "msg_type": "INFERENCE",
    "user_rag_switch": true,
    "result_type": ["use_cache", "use_retrieval"],
    "trigger_mode": "MANUAL",
    "trigger_node": "RAG_RETRIEVAL"
  },
  "fixed_headers": {
    "Content-Type": "application/json",
    "User-Agent": "PostmanRuntime/7.51.1",
    "Accept": "text/event-stream",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "agent-type": "TestAgent",
    "caller_name": "ai4xxx"
  },
  "token_headers": {
    "X-HW-ID": "com.huawel.ipd.coretool.coremlops",
    "X-HW-APPKEY": "3JUnVhZLKMnI03FPOAwOxA=",
    "Content-Type": "application/json"
  },
  "fallback_trigger_regexes": [
    "((logger\\.step\\(.*?\\))|(#\\s*.*?))\\n",
    "(self\\.logStep\\(.*?\\))\\n"
  ],
  "token": { "refresh_after_days": 2.5, "retry_on_401": true },
  "request": { "per_step_timeout_sec": 120, "max_workers": 4, "connect_timeout_sec": 10 },
  "proxy": null,
  "xml_path": "auto"
}
密码加密说明
采用本机派生密钥的对称加密：密码不以明文存储，文件权限限当前用户。
交付物
5 个 Python 脚本 + settings.json + SKILL.md
coretest_script_generate.md 读 explore 结果那段 prompt 的改写文本
首次使用说明（账号密码交互、缓存位置、代理配置、加密强度边界）
