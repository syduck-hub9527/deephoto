# deephoto · 图文 PDF RAG 智能体

基于 Deep Agents + Kimi K3(多模态)的图文交错 PDF 问答系统。上传论文/教材/手册 PDF 后,
针对文档提问,系统返回带页码引用的文字回答,并附上相关**完整原图**、图注与来源页。

实现依据:本仓库根目录《DeepAgents_图文PDF_RAG_开发文档.md》(v1.0)。

## 架构对应关系

| 文档部件 | 实现 |
| --- | --- |
| Deep Agents 问答智能体 | `agent/qa.py`:`create_deep_agent` + `search_knowledge` / `inspect_image` |
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
# 打开 http://127.0.0.1:8000 ,输入令牌(默认 dev-token)
```

首次启动后在真实样本上验证通过,建议锁定依赖版本:

```bash
pip freeze > requirements.lock.txt
```

## 配置(环境变量,前缀 `DEEPHOTO_`)

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `MOONSHOT_API_KEY` | Kimi K3 密钥(必填,描述与问答需要) | — |
| `MOONSHOT_BASE_URL` | Kimi Code 会员路由 `https://api.kimi.com/coding/v1`;直连 API 用 `https://api.moonshot.cn/v1` | coding 路由 |
| `CHAT_MODEL` | coding 路由用 `k3`;直连 API 用 `kimi-k3`(不可混用) | `k3` |
| `CHAT_TEMPERATURE` | 采样温度 | `1` |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` | 可选嵌入端点(任意 OpenAI 兼容,如阿里云百炼 `https://dashscope.aliyuncs.com/compatible-mode/v1` + `qwen3.7-text-embedding`)。**不配置时自动降级为纯关键词检索** | 关闭 |
| `SECRET_KEY` | 图片短时 URL 签名密钥,生产必改 | `dev-secret-change-me` |
| `BOOTSTRAP_TOKEN` / `BOOTSTRAP_TENANT` / `BOOTSTRAP_USER` | 首次启动种入的开发用户 | `dev-token` / `default` / `admin` |
| `DATA_DIR` | 数据库与对象存储目录 | `./data` |
| `MAX_UPLOAD_MB` | 上传大小上限 | `100` |
| `INGESTION_VERSION` | 解析/索引版本;去重复用的判定维度之一 | `v1` |

## API 摘要

- `POST /api/documents` 上传 PDF → `{document_id, status: "queued"}`
- `GET /api/documents` / `GET /api/documents/{id}` / `DELETE /api/documents/{id}`
- `POST /api/qa` `{question, document_id?}` → `{answer, citations[], images[]}`
- `GET /api/documents/{id}/images/{occ_id}`、`GET .../pages/{n}`:Bearer 鉴权**或** `?expires=&sig=` 短时签名(问答响应中已生成)

入库状态机:`queued → parsing → describing → indexing → ready`,失败为 `failed` 并记录错误。

## 关键设计(与文档条款对应)

- **图片资产与出现位置分离**:同一图片内容一个 `ImageAsset`,多处出现各自一条 `ImageOccurrence`(页码、图注各自保留),按内容哈希去重(§3.5)。
- **图文关联**:显式引用(`references` 0.95)> 图注配对(`caption_of` 1.0)> 位置邻近(`nearby` 0.4,仅候选);低于 0.8 不作为确定证据(§3.3)。
- **取图策略**:嵌入位图直接提取原图;矢量/混合图渲染裁剪;边界不确定整页回退并标记 `needs_review`(§3.2)。
- **检索**:BM25 负责“图 3”、术语、公式名的精确命中;配置了嵌入端点则与向量分数归一化后等权融合(§3.5)。
- **回答组装**:模型只能引用 `[chunk:ID]` / `[image:ID]` 格式、且必须是本轮工具实际返回过的 ID;图片列表与短时签名 URL 由后端验证组装(§5.1.7、§5.3)。
- **权限**:所有查询按 `tenant_id` 过滤;`document_id` 归属在服务端校验,模型参数无权越权(§5.1.1)。

## 已知限制(首版)

- 多栏排版阅读顺序为近似(未做栏检测);跨页图、复杂子图依赖 `needs_review` 抽检(§3.2 已声明需额外校验)。
- 扫描页保存整页图像并标记待校验;未内置 OCR,可先用 K3 看图兜底。
- 鉴权为开发级 Bearer 令牌;生产应接入真实身份系统(权限边界已实现多租户隔离)。
- worker 为单进程内线程;多副本部署需换真实队列。
- **Kimi K3 注意点**:思考常开;其官方要求多轮/工具循环回传完整 assistant 消息(含 reasoning content)。若所用 langchain-openai 版本丢弃该字段,需在 `llm.py` 集中更换适配器。
- `inspect_image` 的多模态工具返回格式(content blocks)请按实际安装的 deepagents/langchain-openai 版本验证(文档§5.2 实现提示)。

## 测试

```bash
cd tests && python3 -m unittest discover
```

纯逻辑单测(图注识别、分词/BM25、分块、图文关联、稳定键、URL 签名)不依赖第三方包;
解析与端到端需安装依赖后用真实 PDF 样本验证(验收集要求见文档§6)。
