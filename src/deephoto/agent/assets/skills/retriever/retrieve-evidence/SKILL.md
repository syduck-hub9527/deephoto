---
name: retrieve-evidence
description: 收集可核对的文本证据与候选图片,处理截断命中、跨块上下文和检索缺口。
---

# 检索证据

1. 根据自包含任务确认问题与 document_id;使用 search_knowledge,不足时换同义关键词。
2. 命中截断或答案跨块时用 read_chunk,必要时 neighbors=1。启用 /kb/ 时,grep 命中行号减一才是 read_file offset,需读取块头取得真实 ID。
3. 摘录与要点必须由实际工具内容支撑。记录候选 image_occurrence_id、图注和位置,不要声称已看过原图。
4. 继续使用角色提示词规定的【要点】【原文摘录】【候选图片】【缺口】格式与长度上限,不回答最终用户问题。
