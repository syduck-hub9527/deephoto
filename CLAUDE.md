# CLAUDE.md

## 项目目标与最高开发原则

deephoto 是基于 deepagents 的图文文档 RAG 系统。它通过检索正文、读取完整块、查看原图回答问题，并由后端校验引用和图片出处。

**在制定和实施项目计划时，尽可能深入使用 deepagents，以及其底层 LangChain / LangGraph 已有能力。智能体循环、委派、文件路由、状态持久化、上下文管理、流式事件和工具审批，优先通过框架原生能力与扩展点实现。**

每项相关功能都应明确“使用哪项框架能力、接入哪个现有入口、如何证明运行时生效”。只导入框架、把模型调用包在函数中，或只在提示词中要求规划和委派，不算充分使用框架。

这条原则适用于项目应用架构；不意味着执行开发任务的 Claude 必须启动多个编码代理。确定性的解析、索引、数据库事务、权限检查和前端渲染继续使用普通程序，避免为使用框架而增加模型调用。

## 开始工作前

1. 阅读当前分支代码、`git status`、`git diff`、`README.md`、`requirements.txt`、`pyproject.toml` 和用户指定的开发文档。以当前代码与实测契约为准，README 可能滞后。
2. 根据任务阅读下表对应模块及测试，不先凭框架记忆设计方案。
3. 尊重用户指定的阶段、暂停点和审核顺序。某阶段有测试覆盖，不表示该阶段的 API、前端和业务流程已完成；未经当前任务授权，不自动推进后续编号文档。
4. 保留用户未提交的工作。未要求时不提交、推送、重置分支或更新生产数据。

## 当前框架版本与依据

| 依赖 | 仓库锁定版本 |
| --- | --- |
| deepagents | 0.7.19 |
| langchain | 1.4.4 |
| langgraph | 1.2.14 |
| langchain-core | 1.6.9 |
| langchain-openai | 1.7.0 |
| langgraph-checkpoint-sqlite | 3.1.1 |

Python 要求为 3.11 及以上。开发时使用安装了项目依赖的虚拟环境。

能力判断依据按顺序为：当前锁定版本源码和签名、`tests/test_deepagents_contract.py` 等可执行契约、官方文档与发布说明。官方在线文档可能对应不同版本，示例必须在当前版本上验证后采用。不要静默升级依赖来使示例运行；升级时同步两份依赖声明，并说明契约变化与迁移影响。

官方资料入口：

- https://docs.langchain.com/oss/python/deepagents/overview
- https://docs.langchain.com/oss/python/deepagents/subagents
- https://docs.langchain.com/oss/python/deepagents/backends
- https://docs.langchain.com/oss/python/deepagents/middleware
- https://docs.langchain.com/oss/python/deepagents/human-in-the-loop
- https://github.com/langchain-ai/deepagents

## 现有接入点：优先扩展，不另造一套

以下路径均相对于仓库根目录。

| 能力 | 现有入口 | 开发要求 |
| --- | --- | --- |
| 创建、调用和流式运行智能体 | `src/deephoto/agent/qa.py` | 沿用 `create_deep_agent`、`invoke` / `stream` 和现有响应组装入口 |
| 业务子智能体 | `src/deephoto/agent/subagents.py` | 扩展声明式 specs、角色工具和角色 middleware |
| 原生工具与默认 GP 控制 | `src/deephoto/agent/harness.py` | 集中管理 `HarnessProfile`，避免分散注册和全局冲突 |
| 知识库虚拟文件系统 | `src/deephoto/agent/kb_vfs.py` | 通过 `BackendProtocol` 扩展只读知识库投影 |
| 后端组合路由 | `qa.py` 的 `_agent_kwargs` | 使用 `CompositeBackend`，保持默认 `StateBackend` 的临时文件能力 |
| 会话、checkpoint 与 Store | `src/deephoto/agent/persistence.py` | 复用 `SqliteSaver` / `SqliteStore`、会话归属、租约和生命周期 |
| 已完成轮次压缩 | `src/deephoto/agent/history.py` | 维护完成轮次与待执行/待审批轮次的区别 |
| 调用限制、轨迹和工具可见性 | `src/deephoto/agent/middleware.py`、`qa.py` | 原生限制 middleware 加项目所需的轻量 hooks |
| 技能和显式偏好记忆 | `src/deephoto/agent/context.py`、`agent/assets/skills/` | 复用角色技能、只读快照和 `FreshMemoryMiddleware` |
| 证据检索和权限 | `src/deephoto/agent/knowledge.py`、`src/deephoto/security.py` | 权限由服务端执行，模型输出不能授权访问 |
| 引用与输出格式 | `qa.py` 的 `_assemble`、`agent/answer_format.py` | 沿用真实证据 ID 校验与图片锚点协议 |
| HTTP / SSE 与断连清理 | `src/deephoto/api/routes_qa.py`、`src/deephoto/web/` | 映射框架事件，保留完成校验和生成器关闭行为 |
| 模型兼容适配 | `src/deephoto/llm.py` | 集中处理模型协议差异，不在业务路径中散布 provider 补丁 |

## 制定实施计划的强制步骤

涉及智能体功能时，计划必须包含一张能力映射表：

| 需求 | 原生能力或扩展点 | 仓库接入点 | 自定义业务部分 | 验证方式 |
| --- | --- | --- | --- | --- |
| 填写具体用户行为 | 填写确切 API / middleware / backend | 填写文件与函数 | 说明原生能力未覆盖什么 | 写出可观察的运行时结果 |

先搜索框架是否已有支持，再决定编写自定义代码。优先级为：

1. 已有项目入口 + 框架原生参数或能力。
2. 框架公开 middleware、backend、subagent 和状态扩展点。
3. 必要时使用原生 LangGraph 图或预编译子智能体表达特殊流程，并接回现有系统。
4. 只有经当前版本源码和最小实验确认缺口后，才编写独立的业务适配层。

选择第 3 或第 4 项时，在计划里说明缺口、已验证的替代方案、边界和回归测试。避免直接改第三方包、依赖私有实现或复制框架循环；确需使用私有接口时，集中封装并添加版本契约。

每一步应给出具体文件、输入输出、状态变化、开关兼容性与验收条件。根据任务长度选择拆分粒度，不把简单修复扩展成整套架构重写。

## 深度使用框架的具体规则

### 规划与委派

- 多步检索、跨文档比较和图像核验应评估是否适合原生规划与子智能体委派；简单问题允许直接执行。
- **0.7.19 不默认注入 `write_todos`。** 需要运行时规划工具时，先验证显式接入 LangChain `TodoListMiddleware` 在当前栈的行为，确认工具可见性、状态字段和持久化兼容性。不要在提示词里引用尚未注册的工具。
- 优先复用 `retriever` 和 `figure_checker`。新增角色必须有明确责任、最少必要工具、自包含任务说明、输出契约和独立预算。
- 当前子智能体隔离父级历史，只接收任务说明；主智能体取得的是最后一条非空 AI 文本。委派描述必须带完整问题、必要历史事实、目标文档与候选图片 ID，不能假定子级看过父级消息。
- 原生委派可以用于互不依赖的任务；需要前一步证据的任务按依赖执行。不得为了“多代理”重复检索或重复付费调用。
- 不另写手动模型循环、JSON 路由调度器或线程池替代原生智能体委派。特殊工作流先评估 LangGraph 原生图组合。

### 文件系统与证据上下文

- 字面查找、全文通读和文件式证据浏览优先使用 `/kb/` 与原生 `ls` / `read_file` / `glob` / `grep`；语义或关键词检索继续复用 `search_knowledge`，完整块使用 `read_chunk`，原图核验使用 `inspect_image`。
- `/kb/`、`/skills/`、`/memory/` 是不同职责的挂载。新增文件资源优先实现 backend 并接入组合路由，而不是直接把宿主机目录暴露给模型。
- `CompositeBackend` 会剥除匹配的路由前缀再调用子 backend，路径处理、下载、读取窗口和错误返回均应符合锁定版本协议。
- 保留默认临时 backend 供框架转存大工具结果和摘要历史；只读业务挂载不代表整个图没有临时写入能力。
- VFS 返回的证据 ID 仍需进入 tracker。工具轨迹、技能文本、偏好和模型编造的 ID 都不能充当引用证据。

### Middleware 与调用预算

- 模型请求调整、工具观测、消息整理和运行限制优先放在 `AgentMiddleware` 扩展点，不散布在 HTTP 路由里。
- 修改 hook 时检查同步与异步路径，以及它与默认 middleware 的执行顺序、同名替换行为和状态字段的交互。
- 保留工具返回的 `ToolMessage` / `Command` 语义，不能为日志记录而扁平化或重建返回值。
- `recursion_limit` 是图步数限制，不等于模型调用数，也不是整个委派树的总预算。父级限制不会自动约束子级；主级和每个子级分别使用适当限制。
- 委派模式或 04 middleware 开启时，默认 general-purpose 必须继续禁用，防止绕过角色预算。04 关闭时保持已有兼容行为；新方案若依赖严格总预算，应明确覆盖所有执行路径。
- `HarnessProfile` 按 provider/model 匹配且进程内全局生效，排除集合重复注册会取并集。通过 `harness.py` 注册，配置变化需要重启；模型适配变化要验证 profile 实际匹配。

### 持久化、上下文与记忆

- 会话历史使用原生 checkpoint 和 `thread_id`，跨会话数据使用 Store。不要再建浏览器历史到服务端历史的双重追加机制或第二套对话日志来驱动模型。
- 保留会话 owner、文档范围、profile、状态校验和请求租约。文档可用性预检在写入 `running` 前执行；预检失败不破坏已有健康会话，不为新请求留下会话记录。
- 已完成轮次只延续用户问题和最终文本，避免下轮重放工具 JSON、base64 图片及附加载荷。当前工具循环必须保留必要原始消息与模型协议字段。
- 待执行工具、部分执行结果、待审批和待恢复状态不得裁剪。消息重排后要处理基于旧下标的摘要事件。
- 框架摘要、工具结果转存与完成轮次压缩应协同工作。当前 state 变小不代表旧 checkpoint、写入记录或文件已物理清理；磁盘回收属于独立生命周期需求。
- Skills 复用角色 `SKILL.md` 与框架渐进加载，避免把全部技能全文硬塞进每轮系统提示词。
- 长期记忆当前只保存用户显式设置的语言与详略。不得自动把问答、文档事实、凭据或任意自由文本写入偏好 Store；每轮读取最新冻结快照，避免旧 checkpoint 的记忆覆盖新偏好。

### 流式输出与人工审批

- 复用 LangGraph 流式模式和命名空间。接收子图事件使用当前版本的 `subgraphs=True`；最终回答 token 按主图命名空间筛选，避免将子级内部输出展示为最终回答。
- 保留 SSE 的错误、会话与 `done` 契约。图片加载和引用展示以服务端完成校验为准；断连必须关闭生成器并释放会话租约。
- 主图流式回答使用 `stream_mode=["messages", "values"]`：正常完成以最后一个 values 快照中的最终无工具调用 AI 消息为正文；中断时只回退到最后一次非工具调用的部分文字。按 `AIMessageChunk.id` 区分模型调用，工具调用前的文字用 `step` 事件移入执行过程，不能混入 `done.answer` 或引用校验。
- 实施人工审批功能时，优先评估 `interrupt_on` / `HumanInTheLoopMiddleware`、checkpointer 与 `Command(resume=...)`。批准、编辑、拒绝和恢复通过框架继续原线程，避免重放已经执行的副作用。
- 必须把暂停与失败区别建模，并完成归属校验、API/SSE、前端、断连与恢复测试。仓库已有 interrupt 回归测试只证明框架能力；不表示业务审批功能已实现，也不授权自行推进 06。

## 必须保持的业务边界

- 后端校验租户、文档归属和可检索状态；工具隐藏只是模型可见性控制，不是执行权限或路径 ACL。
- 模型参数不能改变请求绑定的身份。新增工具和 backend 的读、写、下载、上传路径均应校验实际权限。
- 引用和图片锚点使用真实 `[chunk:ID]` / `[image:ID]`；沿用 tracker、会话中已验证证据与当前访问权限的校验链路，不放宽为“模型说存在就存在”。
- 不混用聊天、图片描述、嵌入和解析服务的配置。模型协议兼容在 `llm.py` 集中处理，保留当前工具循环需要的多模态与 reasoning 字段。
- 观测记录有界元数据，不记录密钥、原始图片载荷或完整文档。观测不能改变业务结果。
- 03 Persistence、04 Middleware、05 Memory 已由用户决定固定开启：`load_settings()` 不再读取对应环境变量（残留只告警），`Settings` 数据类默认值保持 False 仅供测试构造最小配置；图片描述按 `DESCRIPTION_API_KEY` / `BASE_URL` 是否填写自动启用。仍可选的开关是 `QA_SUBAGENTS_ENABLED`、`QA_KB_VFS_ENABLED`、`QA_SKILLS_ENABLED`、`QA_HITL_ENABLED`，环境变量统一加 `DEEPHOTO_` 前缀，默认关闭；HITL 依赖 Persistence（已固定满足）。
- “优先使用框架”不授权擅自把开关全开、修改默认模式或扩大现有工具权限。改变默认行为须属于当前任务，并说明迁移与回退方式。

## 验证与交付

在仓库根目录、安装了依赖的 Python 环境中执行：

```bash
# 框架契约；升级或引入新框架接入方式时必须运行
python -m unittest discover -s tests -p 'test_deepagents_contract.py'

# 与任务相关的测试，示例
python -m unittest discover -s tests -p 'test_qa_repairs.py'

# 改动智能体接线、共享状态或依赖后运行全量回归
python -m unittest discover -s tests

# 修改已有文件时检查差异格式
git diff --check
```

优先用 `tests/support/fakes.py` 的 `ScriptedFakeChatModel` 驱动真实编译图；mock 业务外部依赖，不把框架行为全部 mock 掉。测试应证明用户行为和运行时边界，避免只检查“传入了某参数”。根据改动选择以下验证：

- 模型实际绑定的工具、profile 匹配及不能绕过的调用限制。
- 子智能体隔离、角色预算、返回内容、证据 tracker 和流式命名空间。
- 下一轮实际模型输入没有旧图片载荷，当前轮仍能看图；必要时用 `graph.get_state` 检查恢复后的状态，不能假定原始 checkpoint 每次都包含完整 messages。
- 预检失败前后会话元数据一致，执行失败仍按失败处理，旧 checkpoint 升级可继续使用。
- Skills / Memory / VFS 的相关开关组合、只读边界、偏好刷新和关闭开关时的兼容行为。
- 审批暂停、批准/编辑/拒绝、恢复后副作用恰好执行所需次数及断连清理。

真实模型冒烟使用测试数据副本，先确认凭据和外部调用范围。脚本必须对非空会话、回答成功、错误事件、同 ID 恢复及预期工具行为设置断言，失败返回非零退出码。**空事件列表不能算预检成功，两个 `None` 不能算同会话恢复；记录回答和引用数量不能证明旧 base64 没有进入模型请求。** 原始文档状态须按原值恢复，清理测试会话，避免修改生产数据。

交付时说明：采用的原生框架能力、改动的接入点、保留的自定义业务层、实际测试结果和未验证部分。离线图测试、真实模型验证和浏览器端到端验证分别报告，不能把脚本正常退出或日志中的成功描述当作验证通过。

本文件是持续开发约束，不是一次性阶段实施文档。框架版本、入口或状态语义变化时，同步更新本文件与对应契约测试。
