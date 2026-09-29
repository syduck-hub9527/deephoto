# deephoto · 图文 PDF RAG 智能体

基于 Deep Agents + Kimi K3(多模态)的图文交错 PDF 问答系统。上传论文/教材/手册 PDF 后,
针对文档提问,系统返回带页码引用的文字回答,并附上相关**完整原图**、图注与来源页。

实现依据:本仓库根目录《DeepAgents_图文PDF_RAG_开发文档.md》(v1.0)。

## 架构对应关系

| 文档部件 | 实现 |
| --- | --- |
| Deep Agents 问答智能体 | `agent/qa.py`:`create_deep_agent` + `search_knowledge` / `read_chunk` / `inspect_image` |
| PDF 版面解析器 | `parsing/pymupdf_parser.py`(默认),协议见 `parsing/base.py`,可换 Docling |
| 多模态模型 | Kimi K3(`llm.py`,OpenAI 兼容接口,base64 图片输入) |
| 对象存储 | `storage.py`:本地文件系统内容寻址(sha256) |
| 元数据数据库 | `db.py` + `repo.py`:SQLite(WAL),结构对应文档§4 |
| 向量 + 关键词索引 | `indexing/`:BM25(必选)+ 可选 OpenAI 兼容嵌入;稳定键幂等 |
| 业务后端 / 前端 | `api/`(FastAPI + 后台 worker)+ `web/index.html` |

## 快速开始

```bash
# 1. 安装依赖(建议 Python 3.12 虚拟环境)
pip install -r requirements.txt
pip install -e .

# 2. 配置
cp .env.example .env   # 填入 DEEPHOTO_MOONSHOT_API_KEY

# 3. 启动(手动)
python -m deephoto
# 打开 http://127.0.0.1:8000(本地单机,无鉴权)
```

首次启动后在真实样本上验证通过,建议锁定依赖版本:

```bash
pip freeze > requirements.lock.txt
```

## 配置(环境变量,前缀 `DEEPHOTO_`)

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `MOONSHOT_API_KEY` | Kimi K3 密钥(**只负责问答**,不负责图片描述) | — |
| `MOONSHOT_BASE_URL` | Kimi Code 会员路由 `https://api.kimi.com/coding/v1`;直连 API 用 `https://api.moonshot.cn/v1` | coding 路由 |
| `CHAT_MODEL` | coding 路由用 `k3`;直连 API 用 `kimi-k3`(不可混用) | `k3` |
| `CHAT_TEMPERATURE` | 采样温度 | `1` |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` | 可选嵌入端点(任意 OpenAI 兼容,如阿里云百炼 `https://dashscope.aliyuncs.com/compatible-mode/v1` + `qwen3.7-text-embedding`)。**不配置时自动降级为纯关键词检索** | 关闭 |
| `DATA_DIR` | 数据库与对象存储目录 | `./data` |
| `MAX_UPLOAD_MB` | 上传大小上限 | `100` |
| `INGESTION_VERSION` | 解析/索引版本;去重复用的判定维度之一 | `v2` |
| `MINERU_API_KEY` | MinerU 云端解析 Token(解析 **PDF** 必填;Markdown/纯文本走本地解析,不需要) | — |
| `MINERU_BASE_URL` | MinerU 服务地址 | `https://mineru.net` |
| `ALLOWED_FORMATS` | 上传格式白名单(逗号分隔);pptx/xlsx/旧版 Office/图片/HTML 的引擎将在后续版本提供,放开只会得到可读失败 | `pdf,md,txt,docx` |
| `DOCX_PARSER` | docx 解析引擎:`local`(python-docx 本地,默认)/ `mineru`(云端,耗额度,需 Token) | `local` |
| `MAX_ZIP_UNCOMPRESSED_MB` | OOXML(zip)解压总量上限(防压缩炸弹) | `500` |
| `MARKDOWN_DATA_URI_MAX_MB` | Markdown 内联图(data URI)单张上限 | `10` |
| `DESCRIPTION_ENABLED` | 图片描述专用模型开关(独立于问答/嵌入/MinerU) | **`false`** |
| `DESCRIPTION_API_KEY` / `DESCRIPTION_BASE_URL` | 描述服务独立密钥与 OpenAI 兼容 Base URL(到 `/v1` 为止;不回退使用聊天密钥) | — |
| `DESCRIPTION_MODEL` | 描述模型名 | `qwen3.8-omni-flash` |
| `DESCRIPTION_REASONING_EFFORT` | 顶层关思考参数;空字符串表示不发送该参数 | `none` |
| `DESCRIPTION_TIMEOUT_SECONDS` / `DESCRIPTION_MAX_RETRIES` / `DESCRIPTION_MAX_TOKENS` | 单次请求超时 / SDK 重试上限 / 输出上限(以顶层 `max_tokens` 发送,非 `max_completion_tokens`) | `90` / `1` / `1024` |

> ⚠️ **升级注意：图片描述默认关闭。** 早期版本复用聊天模型(Moonshot)生成图片描述;
> 从本版本起描述使用独立的 `DESCRIPTION_*` 配置,仅填了旧聊天配置的用户**不会再自动描述图片**
> (入库阶段显示"图片描述已关闭",仅以图注检索)。按上表填好密钥与地址后设
> `DESCRIPTION_ENABLED=true` 并**完整重启服务**生效。已有入库文档的描述不会因此改变;
> 需要重新生成时请换一个新的 `INGESTION_VERSION` 再重新上传(去重按内容哈希+版本判定)。
> `qwen3.8-omni-flash` 走 Chat Completions,与 realtime 型号不是同一接入方式,不要混用。

## API 摘要

- `POST /api/documents` 上传文档(PDF / Markdown / TXT / DOCX,多文件逐个上传)→ `{document_id, status, source_format}`;内容与扩展名不符/损坏 400,格式不支持或不在白名单 415
- `GET /api/documents` / `GET /api/documents/{id}` / `DELETE /api/documents/{id}`
- `POST /api/qa` `{question, document_id?}` → `{answer, citations[], images[]}`
- `GET /api/documents/{id}/images/{occ_id}`、`GET .../pages/{n}`:图片与页预览(问答响应中已生成 URL,本地部署无鉴权)

### MinerU 云端解析的 PDF 自动分页

MinerU 是唯一的 PDF 解析器(代码中写死,租户不可选择)。Token 由服务端环境变量
`DEEPHOTO_MINERU_API_KEY` 提供,不写入代码、不经过浏览器。后台上传原 PDF 前会按
**每份最多 200 页、序列化后最多 200,000,000 字节**自动拆分；拆分结果依原页序
解析并合并，空白页也会占据原页码。某一页单独导出仍超限时会给出明确错误。
整个文档最多拆成 200 份；当前每份独立申请一次上传地址，因此也符合官网
“单次最多申请 50 个上传链接”的限制。

原 PDF 的上传大小限制由 `DEEPHOTO_MAX_UPLOAD_MB` 控制，默认 100 MB；如需
处理大于 200 MB 的原件，应按服务器内存情况提高该值。这个值是**原件上传限制**，
与 MinerU 的**每份子文件限制**不同。完整解析依赖 MinerU Token，无法仅凭
分页功能跳过云端鉴权。调用前请确认[官方 API 限制](https://mineru.net/apiManage/docs)。

入库状态机:`queued → parsing → describing → indexing → ready`,失败为 `failed` 并记录错误。

### 入库耗时与进度可见

每次上传登记一条观测运行(独立 run),写入与业务库分离的 `data/progress.db`
(业务长事务会跨过云端/模型调用,独立观测库才能保证处理期间进度可读)。观测只
记录与展示,不改变业务状态;观测写入失败只告警,不影响入库。

- `GET /api/documents`:每个文档带可空 `progress` 摘要(阶段、排队/处理/总耗时、
  计数 `completed/total/succeeded/failed`、当前单项、最近事件时间、警告);
  无观测记录的旧文档为 `null`,不填造零耗时。
- `GET /api/documents/{id}`:附完整进度——各阶段/子阶段耗时、每张图片与每个
  向量批次的结果、最慢单项、降级与跳过说明(选择扩展现有详情端点,不新增子资源)。
- 前端文档列表直接显示"生成图片描述 · 已处理 3/7 · 当前：图 1.4,已等待 42 秒"
  这类一行进度,可展开阶段耗时表;轮询为单一调度器(约 3 秒),全部终态后停止;
  刷新失败保留上次数据并提示。

计时口径:排队耗时(登记→领取)与后台处理耗时分开;MinerU 的上传/等待/下载
放在子项 detail,不与解析总耗时重复求和;单图/单批计时含 SDK 内部重试
(已观测次数未知);服务重启后旧实例未完成任务标记"观测中断",业务终态优先展示。

排查瓶颈时:展开耗时看各阶段占比与最慢单项;服务日志在任务结束输出一行汇总
(排队/解析/描述/索引耗时、最慢单项、警告数),日志器命名空间 `deephoto.progress`。

## 关键设计(与文档条款对应)

- **图片资产与出现位置分离**:同一图片内容一个 `ImageAsset`,多处出现各自一条 `ImageOccurrence`(页码、图注各自保留),按内容哈希去重(§3.5)。
- **图文关联**:显式引用(`references` 0.95)> 图注配对(`caption_of` 1.0)> 位置邻近(`nearby` 0.4,仅候选);低于 0.8 不作为确定证据(§3.3)。
- **取图策略**:嵌入位图直接提取原图;矢量/混合图渲染裁剪;边界不确定整页回退并标记 `needs_review`(§3.2)。
- **检索**:BM25 负责“图 3”、术语、公式名的精确命中;配置了嵌入端点则与向量分数归一化后等权融合(§3.5)。
- **回答组装**:模型只能引用 `[chunk:ID]` / `[image:ID]` 格式、且必须是本轮工具实际返回过的 ID;引用与图片列表由后端验证组装(§5.1.7、§5.3)。
- **正文内嵌原图**:模型在解释到某图的位置插入 `[image:ID]` 锚点(独占一行),前端按锚点把已校验的图片卡片渲染在对应段落之间;未被锚点使用的图片进末尾"补充图片"区。流式期间锚点只显示中性占位,收到 `done` 完成校验后才加载原图;无效/越权 ID 不加载任何资源、不显示裸 ID。
- **权限**:所有查询按 `tenant_id` 过滤;`document_id` 归属在服务端校验,模型参数无权越权(§5.1.1)。

## 已知限制(首版)

- 多栏排版阅读顺序为近似(未做栏检测);跨页图、复杂子图依赖 `needs_review` 抽检(§3.2 已声明需额外校验)。
- 扫描版 PDF 同样整份交给 MinerU 解析;MinerU 不可用时对应文档标记 `failed`,无本地 OCR 兜底。
- 无鉴权,仅本地单机使用;如需暴露到网络,必须先接入真实身份系统(数据层的 tenant 过滤结构保留)。
- worker 为单进程内线程;多副本部署需换真实队列。
- **Kimi K3 注意点**:思考常开;其官方要求多轮/工具循环回传完整 assistant 消息(含 reasoning content)。若所用 langchain-openai 版本丢弃该字段,需在 `llm.py` 集中更换适配器。
- `inspect_image` 的多模态工具返回格式(content blocks)请按实际安装的 deepagents/langchain-openai 版本验证(文档§5.2 实现提示)。

## 测试

```bash
cd tests && python3 -m unittest discover
```

纯逻辑单测(图注识别、分词/BM25、分块、图文关联、稳定键、content_list 装配、read_chunk、内嵌图片协议)不依赖第三方包;
前端分段器(`web/segments.js`)的测试需要 node,未安装时自动跳过;
解析与端到端需安装依赖后用真实 PDF 样本验证(验收集要求见文档§6)。
