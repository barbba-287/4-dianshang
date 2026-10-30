# P3-S3 任务文件：知识文档入库（API 方向盘 + 前端管理页）

- **版本**：v1.1 定稿（2026-10-30，Q-S3-1/2/3 已由用户拍板，本文件为实施唯一依据）
- **前置**：P3-S2 已收口（复核结论 §21，`3b9f9f2`），基线 **463 passed / 0 failed**。
- **切片判断**：模型（Document/DocumentVersion/DocumentChunk）、解析（importers pdf/docx）、
  切片（chunks 落库）、索引（vector_store + 后台 job）、检索（RagRetriever/answerer）**全已存在**。
  缺的是：**发布门控**（现状 ready 即可被检索，publish 是无调用方的死代码）、**管理 API 5 个**、**前端管理页**。
- **硬约束**：**零新表、零 migration、零 `db.py` 改动；不改 RAG 问答核心链路**（用户裁定，见 §5）。

---

## 1. 现状盘点（2026-10-30 复核方亲验，行号为当前 HEAD `3b9f9f2`）

### 1.1 数据模型（零改动，本片全部在其上做语义）

- `app/db.py:196-210` `Document`：唯一约束 **`uq_document_workspace_sha256 (workspace_id, sha256)`**
  → Document 目前是**内容寻址**的（同内容同空间必收敛到同一行）；`filename` String(512) **无唯一约束**；
  **无 created_by 列**。
- `app/db.py:213-234` `DocumentVersion`：唯一约束 `(document_id, version_no)` + `(document_id, sha256)`；
  `status` 默认 "parsing"（词典 `db.py:192`：**parsing / ready / failed**）；`published_at` DateTime 可空
  （**发布位天然存在，只是没人写**）；`storage_uri` / `error_code` / `error_message` 齐全。
- `app/db.py:237-254` `DocumentChunk`：`(document_version_id, chunk_no)` 唯一；text/token_count/页段号齐全。
- `app/db.py:153-185` `CrawlJob`：`payload_json` 为 Text（**可携带 actor 而不加列**）；唯一约束
  `(workspace_id, type, idempotency_key)`；`worker_concurrency=2`（`config.py:21`）。

### 1.2 已存在的上传链路（S3 保留其骨架，仅接桥谱系）

`app/main.py:793-903` `POST /api/documents`（**202 异步**，`knowledge.write`）：
`detect_source_type` → `enforce_size_limit`（`config.py:28` **20MB**，超限 `FILE_TOO_LARGE` → **413**）
→ `validate_content`（`upload_security.py:22-46`：**内容签名级白名单**，仅 pdf（`%PDF-` 魔数）与
docx（zip 结构校验，含成员数/解压总大小/路径穿越/加密成员拒绝））→ sha256 → `upsert_document`
（**仅按 sha 查**，`repository.py:940` 附近）→ `save_upload`（**内容寻址存盘**，入参是
`sha256_hex + ext`，原始文件名不进路径；实施方交付时须贴 `storage.py` 路径构造原文复核）
→ `upsert_document_version`（`repository.py:955-995`：同 sha 复用、否则 version_no 自增，status="parsing"）
→ `submit_document_import`（`background.py:236`）；重复上传同内容 → 伪造 succeeded job 不重复入队。

**关键语义缺口**：`upsert_document` 只按 sha 寻址 → **同文件名新内容会开新 Document，
版本链（v1/v2 同谱系）目前不存在**。这是本片要接的桥（D1）。

### 1.3 已存在的异步管线（本片只加"发布闸"，不动解析）

- `background.py:441-542` `_execute_document_import`：storage 读文件 → **sha 完整性校验**
  （不符 → `mark_version_failed(CONTENT_HASH_MISMATCH)`）→ 解析（失败 → failed + error_code）
  → 逐 chunk 落库 → 启发式商品关联 → `mark_version_ready` → **无条件 `submit_chunk_index`**。
- `background.py:567-628` `_execute_document_index`：读 chunks → `store.delete_by_version`（重索引幂等）
  → embed → `store.add(records)`。向量 payload = `{chunk_id, document_id, document_version_id,
  workspace_id, snippet, locator, version_no}`——**无发布标记**。
- 状态呈现：`GET /api/documents/{id}/versions`（`main.py:906-941`，`knowledge.read`）已返回
  status/published_at/chunk_count。**同步/异步结论：现状即异步（202 + 后台 job），大文件无 HTTP 超时问题**
  （唯一同步点是 ≤20MB 的 `await file.read()`，可接受）。

### 1.4 检索现状与发布缺口（本片核心使命）

- `main.py:947-1001` `POST /api/rag/query`：filter_payload 只有 workspace_id（+可选 product/source_type）；
- `rag/retriever.py:39-56`：payload **等值过滤**，无"仅 published"概念；
- `repository.py:1054-1068` `publish_version`：**全仓库零调用方（死代码）**，但前置校验已内置
  （status != "ready" → 拒绝）——S3 接线时**复用其语义**；
- **结论：ready（甚至 parsing 中断后 ready）即进向量库即可被检索，未发布/已下线内容与已发布内容
  在检索侧完全不可区分。** K-7 场景（未发布不可检索）现状必然失败，由 D3/D4 修复。

### 1.5 其余事实

- **前端**：`templates/customer_service.html`（95 行，客服工单页）——grep 证实**所有模板均无**
  documents / rag/query 调用；知识管理区块落此页（用户指定"客服/知识库现有页面"）。
- **归因**：Document / DocumentVersion / CrawlJob 均**无 created_by 列**；actor 只能落
  `CrawlJob.payload_json`（零 migration 的唯一归因通道）。
- **并发风险（现状已存在）**：后台 2 worker 并发跑 `document_index` job，而
  `rag/vector_store.py` 的 `InMemoryVectorStore`（add/search/delete_by_version/_flush）**无任何锁**
  （grep 证实），JSON 文件读写存在竞态。S3 引入 publish/unpublish 会提高变更频率，须一并修（D5）。
- **既有测试**：`tests/test_api_documents.py` 7 条（202/idempotent/新文档/docx chunks/415/413/404），
  使用 `wait_for_jobs(timeout=10)` 等待后台任务；113 行用例证明"不同内容**不同文件名** → 新 document"，
  与 D1 谱系设计兼容；**该 7 条本片零改动**。

---

## 2. 设计决策（已锁定项，实施方不得偏离）

### D1. 版本链（谱系）：**旧版本保留，不覆盖**；解析顺序 sha 优先、文件名次之

上传 service（新函数，见 §4）解析顺序**固定**为：

1. **sha 命中**（`(workspace_id, sha256)` 找到 Document）→ 复用该 Document 与既有 version
   （幂等重传，202，行为与现状逐字节一致）；
2. **filename 命中**（`(workspace_id, filename)` 找到 Document）→ **在该 Document 下新增
   DocumentVersion**（version_no = max+1，status="parsing"，入队解析）——这就是"同文件名再次上传
   生成新版本"的版本链；
3. 都未命中 → 新 Document + v1（现状行为）。

零 migration 约束下的三个**已知边界**（登记，不得"修复"）：
- Document 行的 `sha256`/`filename` 保留**首传值**（仅作寻址键）；版本真相在 DocumentVersion；
- 同内容先后以不同文件名上传 → 永远收敛到首个谱系（**内容优先于文件名**：同内容=同知识，
  避免向量库双份）；
- 谱系 D 的 v2 内容 C 再以**新文件名**上传 → C 会开出新 Document（其 sha 列=C）→ C 同时存在于
  两个谱系。无害（不同 version_id，向量记录 id 不冲突），接受。

### D2. 发布模型：`published_at` 是**唯一发布位**，status 词典零扩展

- **不新增状态值**：解析态沿用 `parsing/ready/failed`（`db.py:192`）；发布态 = `published_at` 非空；
  撤回 = 置回 NULL（status 仍是 ready，不回退）。前端把 parsing 显示为"解析中"。
- 发布前置：`status == "ready"`（对齐 `publish_version` 既有校验；parsing/failed → 409）。
- **同 document 至多一个已发布版本**：新发布在同一事务里清除兄弟版本的 published_at（自动降级）。
- **【用户红线 2026-10-30 15:37】superseded 标记（派生，零 migration）**：被降级版本
  （`published_at IS NULL` 且同 document 存在 `published_at` 非空的兄弟版本）在**响应层**
  标记 `superseded: true`（列表/详情/publish 响应统一由 service 层计算）；status 词典不动、
  不落库。未发布且无兄弟发布 → `superseded: false`。

### D3. 发布/撤回流程（索引生命周期 = 门控机制）

- **publish**（service 层单事务）：ready 前置 → 清同 document 所有兄弟版本 published_at →
  置本版本 published_at → commit → 入队 `document_index`（既有 job 类型）索引新版。
- **【用户红线 2026-10-30 15:37】换版发布必须同步删除旧向量**：若本次发布**顶替**了旧已发布版本，
  在 publish service 内**同步**调用 `get_vector_store().delete_by_version(旧version_id)`
  （与 unpublish 同通道，**严禁只入队异步 job 了事**——异步窗口期间旧知识仍在检索池 = 知识污染）。
  `document_unindex` job 降级为**补偿通道**：仅当同步删除抛异常时入队重试，不作为主路径。
- **unpublish**：前置 published_at 非空（否则 409）→ 同事务置 NULL → **同步**调用
  `get_vector_store().delete_by_version(version_id)`（快操作；撤回必须立即生效，安全方向宁可同步）。
- **`document_unindex` job**：`background.py` 新增类型，执行体即 `store.delete_by_version` +
  `job.cursor` 留痕；dispatch 加一个 elif 分支（只作补偿用）。
- 发布→检索生效为**最终一致**（异步索引，通常秒级）；publish 响应返回 `indexing_queued=true`，
  前端提示"检索生效中"。撤回与换版旧向量删除都是同步的，无污染窗口。

### D4. 索引门控（保证"只有发布过的版本才进 RAG 检索"）

`_execute_document_index` **顶部加发布闸**：`version.published_at is None` → 直接跳过
（`job.cursor="0"`，不写向量库）。由此得到不变量：**向量库中只存在已发布版本的 chunks**；
`rag_query` / retriever / answerer / embedder **一行不改**（D3+D4 双端保证 K-7）。
注意：现状 `_execute_document_import` 尾部的 `submit_chunk_index` **保留**（ready 后入队，
由闸门决定是否实际索引）——这样发布时只需再入队一次即可补索引，解析流程零改动。

- **【用户红线 2026-10-30 15:37】解析→索引竞态必须在执行时重查 DB**：发布闸读取的是
  **执行时刻**的 `version.published_at`（重查 DB，不是入队时刻的快照）——
  索引 job 排队期间用户 publish 了 → 重查命中 → **直接入库**；仍未发布 → **绝不入库**。
  publish 的 ready 前置使"解析进行中点击 publish"不可达；ready 与索引执行之间的竞态窗口
  由本闸覆盖。同一版本被两条路径入队索引（原解析链 + publish 链）时，靠 chunk id 确定性
  upsert 幂等吸收，不得双写。

### D5. 向量库线程安全修复（基础设施，非 RAG 语义变更）

`InMemoryVectorStore` 的 `add` / `search` / `delete_by_version` / `_flush` 加**模块级
`threading.Lock`**（现状 worker_concurrency=2 且无锁，S3 前已是竞态隐患）。检索语义、
打分、持久化格式零变化。

### D6. 权限与归因

- **零新权限点**：读 = `knowledge.read`；写/发布/撤回 = `knowledge.write`（与上传同点）。
- publish/unpublish 额外限角色（maker-checker 精神：客服可传，发布权收紧）——**默认
  `(ROLE_ADMIN, ROLE_OPERATIONS)`**（Q-S3-2 已裁定，见 §3）。
- **actor 归因**：上传/发布/撤回的 service 入队时把 `principal.subject` 写入
  `CrawlJob.payload_json["actor"]`（零 migration 唯一通道）；Document/DocumentVersion 无
  created_by 列属既有事实，本片不解锁、不伪造。

### D7. API 清单（7 端点 = 2 既有 + 5 新增）

| # | 端点 | 状态 | 说明 |
|---|---|---|---|
| 1 | `POST /api/documents` | **已有**（793-903） | 内部改调新谱系 service（D1）；对外契约不变：202 + document_id/version_id/version_no/status/job_id |
| 2 | `GET /api/documents` | 新增 | 分页列表；过滤 `filename`（模糊）/`status`/`published`（true/false/缺省全部）；每行附 latest version 摘要与 published 版本号 |
| 3 | `GET /api/documents/{document_id}` | 新增 | 单文档详情：谱系信息 + 版本链（复用 list_versions_for_document）+ published 版本号 |
| 4 | `GET /api/documents/{document_id}/versions` | **已有**（906-941） | 零改动 |
| 5 | `POST /api/documents/{document_id}/versions` | 新增 | **显式新版本上传**（multipart，同 #1 的 service，前提：filename 与该 Document 谱系一致，否则 409 `FILENAME_LINEAGE_MISMATCH`；同 sha → 幂等返回既有版本） |
| 6 | `POST /api/documents/{document_id}/versions/{version_id}/publish` | 新增 | D3；409 `INVALID_DOCUMENT_STATE`（非 ready）/ 404（版本不属该 document 或跨租户） |
| 7 | `POST /api/documents/{document_id}/versions/{version_id}/unpublish` | 新增 | D3；409 `VERSION_NOT_PUBLISHED` |

错误码进既有 `DomainError` 通道；新码（`FILENAME_LINEAGE_MISMATCH` / `INVALID_DOCUMENT_STATE` /
`VERSION_NOT_PUBLISHED` / `DOCUMENT_NOT_FOUND` 已有）的 HTTP 映射在端点内显式给定，
**不得依赖 `_warehouse_error` 式 endswith 推断**（S2 教训前置规避）。

### D8. 前端（`customer_service.html` 单页追加）

在工单面板之后新增「知识管理」`<section>`：上传表单（file input + 标题，POST #1 / #5）、
文档列表表（文件名/版本数/最新状态/发布版本/操作列）、操作按钮（发布/撤回，按角色显隐可简化为
交给后端 403 提示）、状态轮询（复用 #3/#4，提交后 `wait` 式轮询 2s 间隔至 parsing 结束）。
复用该页既有的 `api()` / `escapeHtml()` / csrf 模式；不新建页面、不动工单区块。

---

## 3. 裁定记录（2026-10-30 用户拍板，全部按推荐执行，不得再改）

### Q-S3-1 类型白名单：**已裁定维持 `{pdf, docx}`，本片不扩 txt/md**

用户原话：内容签名级校验比扩展名强得多，强行加 txt/md 需要写新的切分策略，本片不干，
**另立切片**。本片按 `{pdf, docx}` 写死；**不追加** TextParser、不动
`DOCUMENT_SOURCE_TYPES`。

### Q-S3-2 发布/撤回角色：**已裁定收紧为 `(ADMIN, OPERATIONS)`**

用户裁定：知识库的发布直接决定 AI 的输出，客服岗可以有上传权（提供子弹），但**不能有发布权**
（开保险栓）。上传保持 `knowledge.write` 现状（客服可传）；发布/撤回限
`(ADMIN, OPERATIONS)`，写死进 §4 的 knowledge_admin service 权限参数。

### Q-S3-3 大小上限：**已裁定维持既有 20MB**

用户裁定：有内容寻址和魔数校验在，这个限制足够安全。本片按 20MB 写死、
**不改** `config.py:28` 的 `upload_max_bytes`，413 语义与既有用例保持原样。

---

## 4. 改动清单（预期 6 文件 + 2 新文件）

| 文件 | 改动 | 量级 |
|---|---|---|
| `app/knowledge_admin.py`（新建） | 谱系 service（D1 三步解析）、publish/unpublish service（D3）、列表/详情聚合（D7） | ~150 行 |
| `app/main.py` | 5 新端点（D7）+ 上传端点内部改调 service（对外契约不变）+ 错误映射 | ~90 行 |
| `app/background.py` | `_execute_document_index` 顶部发布闸（D4）+ `document_unindex` 类型与执行体（D3） | ~30 行 |
| `app/rag/vector_store.py` | 模块级 Lock（D5），语义零变化 | ~10 行 |
| `app/schemas.py` | 新响应模型（列表/详情/publish/unpublish） | ~40 行 |
| `app/templates/customer_service.html` | 知识管理区块（D8） | ~40 行 |
| `tests/test_p3s3_knowledge_admin.py`（新建） | §6 用例 K-1..K-12 | 新文件 |
| `docs/review-checklist-20261011.md` | §22 台账占位 | 少量 |

**不碰**：`app/db.py`（零改动）、无 migration、`app/rag/` 其余文件（retriever/answerer/embedder/__init__）、
`app/rag_runtime.py`、`POST /api/rag/query` 端点、`app/importers/`、`app/upload_security.py`、
`tests/test_api_documents.py`（7 条既有用例零改动零删除）。

---

## 5. 守卫与禁区（违反任一条 = 复核直接打回）

1. **零新表、零 migration、零 `app/db.py` 改动**（用户裁定 6）。
2. **不改 RAG 问答核心链路**（精确定义）：`rag/retriever.py` / `rag/answerer.py` / `rag/embedder.py` /
   `rag/__init__.py` / `rag_runtime.py` / `POST /api/rag/query` 端点逐字节不变；
   `vector_store.py` 仅允许 D5 的锁；`background.py` 仅允许 D4 闸 + D3 新 job 类型。
3. **既有 7 条文档测试零改动**（`test_api_documents.py`）；全量 463 基线用例零删除。
4. 上传端点对外契约不变（202/413/415/409 语义与响应键）；幂等重传路径逐字节旧行为（D1 第 1 步）。
5. 物理存盘**必须**继续走 `save_upload`（内容寻址）+ `safe_storage_path` 校验；
   **严禁**把原始文件名/用户输入拼进存储路径（防目录穿越红线；实施方交付说明贴 `storage.py` 路径构造原文）。
6. 向量库变更只有三条通道：`document_index`（带 D4 闸）/ `document_unindex` / unpublish 的同步
   `delete_by_version`；**严禁**在端点线程直接 `store.add`。
7. 无新权限点、无 settings/environ 新开关；actor 只落 `CrawlJob.payload_json`，严禁为归因加列。

---

## 6. 验收用例（12 条，全部落 `tests/test_p3s3_knowledge_admin.py`）

复用 `test_api_documents.py` 的夹具模式（tmp_path + monkeypatch env + reload + `wait_for_jobs`）；
**必须额外设 `VECTOR_STORE_PATH` 指向 tmp**（隔离默认 `data/vectors.json`）；pdf/docx 用
reportlab / python-docx 现造。检索断言走真实 `POST /api/rag/query`（knowledge.read 账号）：

| # | 用例 | 断言要点 |
|---|---|---|
| K-1 | 正常上传回归 | 202 → parsing → wait → ready + chunk_count≥1（既有路径在新 service 下不回归） |
| K-2 | 超限拦截 | 413 `FILE_TOO_LARGE`（回归既有语义） |
| K-3 | 假后缀/假内容拦截 | ① `.exe` 直接 415 `UNSUPPORTED_TYPE`；② 内容非 `%PDF-` 的 `x.pdf` → 415 `SIGNATURE_MISMATCH` |
| K-4 | 版本演进（版本链） | 同文件名新内容 → **同 document**、version_no=2、旧 v1 保留且 status 不变；Document 行数不增 |
| K-5 | 发布后检索生效 | publish → wait → rag_query 命中该 version（citations 里 document_version_id 一致） |
| K-6 | 下线后不参与检索 | unpublish → rag_query 不再命中该 version |
| K-7 | 未发布不可检索（门控主用例） | ready 未发布 → rag_query 零命中该 version（D4 闸实锤） |
| K-8 | 发布前置 | parsing 未完成时 publish → 409 `INVALID_DOCUMENT_STATE`（可用未入队 job 的版本构造） |
| K-9 | 新发布顶替旧发布 | v1 published → publish v2 → v1.published_at 为 NULL、v2 为非 NULL；rag_query **只**命中 v2 |
| K-10 | 撤回前置 | 未发布的版本 unpublish → 409 `VERSION_NOT_PUBLISHED` |
| K-11 | 权限 | readonly 账号上传/发布 → 403；发布用 warehouse 角色账号（无 knowledge.write）→ 403 |
| K-12 | 显式版本端点 | #5 传不同 filename → 409 `FILENAME_LINEAGE_MISMATCH`；同 filename 新内容 → v2；同 sha → 幂等既有版本 |

前端 `customer_service.html` 无自动化断言；交付说明附改动片段 + 手工冒烟路径
（登录 → 客服页知识管理区块 → 上传/列表/发布/撤回四操作可见可用）。

**基线预期：463 → 475 passed / 0 failed。**

---

## 7. 已排除的选项（实施方不得重新试错）

1. **检索端加 published 过滤**（retriever/rag_query 改动）——违反禁区 2；门控在索引生命周期做（D3/D4），
   查询路径零改动且天然向后兼容。
2. **给 Document/DocumentVersion 加列**（created_by / published 布尔 / lineage_id）——零 migration 红线；
   发布位用 published_at、谱系用 filename、归因用 payload_json（D1/D2/D6）。
3. **新增状态值**（published/unpublished 进 status 词典）——published_at 已是发布位，双写必漂移。
4. **解析改同步**——现状 202 异步已消除 HTTP 超时风险；改同步是倒退。
5. **unpublish 走异步 job**——撤回必须立即生效（安全方向），同步 delete_by_version 是快操作；
   publish 才走异步（embed 大文档）。
6. **覆盖式版本链**（新版本顶掉旧行）——用户裁定保留旧版本；单发布位 + 自动降级已满足"检索只见新版"。
7. **status 改名 processing/ready/failed**——用户建议的 processing 与既有 parsing 语义相同，
   改名要动词典+全链路+既有测试，纯改名零收益；前端显示层映射"解析中"即可。
8. **TXT/MD 解析器**——Q-S3-1 已裁定不做（需新分片策略，另立切片）。

---

## 8. 交付要求

1. 完成即把交付说明写到 **`.testtmp/delivery.p3s3.md`**（落盘，防进程失联；含提交号、全量结果、
   偏差清单、`storage.py` 路径构造原文、customer_service.html 改动片段与冒烟说明）。
2. 全量回归命令（**必须剥离会话注入垫片**，R-15）：
   ```
   env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider
   ```
3. 提交信息前缀 `p3s3:`；**完成后提交并停住，不 push**（阶段收口策略：P3 三片全清后一起推）。
4. 交付说明如实列出所有偏差；遇挂死按 R-17/R-22/R-55 协议（代码写完即落盘交付说明，勿依赖进程存活）。
