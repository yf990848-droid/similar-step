---
name: similar-scripts-hierarchical-retrieval
description: 根据框架脚本调用 RAG 推理接口检索相似测试步骤代码，输出 topN 候选供生成参考
---

# 任务目标

为 `$0` 指定的目标框架脚本检索相似步骤代码，输出结果文件 `<框架脚本去扩展名>_explore.json`。

# 执行步骤

使用 Bash tool 执行主脚本，传入框架脚本完整路径，等待超时时间 15 分钟：

```bash
python .testagent/skills/similar-scripts-hierarchical-retrieval/scripts/retrieve_similar_steps.py "<框架脚本完整路径>"
```

成功时 stdout 最后一行是结果文件的绝对路径。返回该路径即可，不要总结内容。

# 约束

- **不要读取或改写框架脚本内容**，只把完整路径作为参数传给脚本。
- 脚本内部已处理配置解析、步骤切分、接口调用与认证，无需额外操作或任何凭据配置。
- 返回非 0 退出码即为检索异常，不要重试，直接返回异常信息。
- **只做检索，不生成任何测试脚本代码。**
