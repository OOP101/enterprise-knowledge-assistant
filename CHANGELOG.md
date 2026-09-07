# 更新日志 (CHANGELOG)

> 本文件记录企业知识助手每个版本的变更内容。
> 版本规则：主版本.次版本.修订号（Major.Minor.Patch）
> - Major：架构级重构
> - Minor：新增功能模块
> - Patch：问题修复与优化

---

## v3.0.0 — 2026-09-05

### 入库审核流（Phase 1 后端）：AI 预筛 + 人工把关 + 双层存储

- **文档质量打分器**（`src/review/scorer.py`）：纯规则离线可跑，四维评估——长度合理区 / 文本密度（拦截扫描件、乱码）/ 结构完整度（章节条款识别）/ 清洗损失率（清洗前后字符数比值），返回 0-100 分 + 扣分原因；
- **审核状态机**（`src/review/store.py`）：`pending_score → pending_review → approved / rejected`，JSON 原子写持久化；双层存储 `tier = raw | highlight`——待审/退回文档仅留原文缓存（`data/raw_docs/`，不占索引资源），过审才晋升亮点层参与检索；
- **入库链路改造**（`upload.py` / `knowledge.py`）：上传与批量入库统一走 `process_and_ingest`——AI 打分达阈值（默认 60，`REVIEW_SCORE_THRESHOLD` 可配）直接生效，低分挂起待人工审（`REVIEW_MODE`: threshold / all / off）；
- **审核 API**（`serve/routers/review.py`）：队列（按分数升序，最差在前）、入库前后对比 diff（清洗前原文 vs 清洗后切片 + 损失率）、approve（重切片带标注入库）、reject（已生效文档同步移出索引）、annotate（chunk 级标注）、stats；
- **人工标注检索加权**（`hybrid_search.py`）：标注分片 RRF 融合后按 `score × (1 + 0.2)` 加权（`HIGHLIGHT_WEIGHT`），回答来源带 `highlighted` 徽章——人审核一次，AI 长期受益；
- 删除文档联动清理审核记录与原文缓存；新增 `tests/test_review.py` 15 个用例，**累计 67 测试全离线通过**。

---

## v2.6.0 — 2026-08-30

### 对话历史合并到智能问答 + 批量删除

- **独立「对话历史」页面下线**：侧边栏导航项与页面移除，功能整体合并进智能问答页；
- **历史抽屉**：智能问答顶栏新增「历史」按钮，左侧滑出抽屉——会话列表（标题/条数/时间，当前会话高亮）、跨会话关键词搜索（按会话去重）；
- **批量删除**：每个会话带勾选框 + 全选，点「删除所选」批量删除（确认后逐个调用 DELETE，失败计数提示）；删除当前会话自动开新会话；
- **无缝续聊**：点击任一历史会话直接载入当前聊天窗口并切换 SESSION_ID，继续提问自动追加到该会话；
- 会话列表在抽屉打开时实时刷新（新问答完成后同步更新）；52 测试通过。

---

## v2.5.1 — 2026-08-30

### 修复：流程图文档入库 + 向量库段损坏自愈

#### 1. 流程图 docx 文本提取（修复 3 个上传失败）

- **根因**：流程图节点文字在绘图文本框（`w:txbxContent`/SmartArt）里，python-docx 读不到；
- `loader._extract_drawing_text()`：正文 <50 字符时按文档序提取文本框/SmartArt 文本，Choice/Fallback 去重；
- 实测 011/152/189 三份流程图恢复入库（110~226 字符/份，节点可检索），全库另有 8 份文档受益。

#### 2. 向量库 "Error loading hnsw index" 根治

- **根因**：chromadb 1.3.x/1.5.x 在本机 Windows 后台压缩器不落盘段数据文件（只留 index_metadata.pickle），重启后加载残缺段报错；数据本体在 sqlite 写入队列中未丢失；
- **启动自愈**：`embedder.repair_chroma_segments()` 在服务启动时清理半初始化段目录，chroma 自动从队列回填（实测 2089 块全部恢复，跨进程可读）；
- **版本锁定**：`requirements.txt` 锁定 `chromadb==1.3.7` 并注明升级验证要求；
- **重建脚本重写**：批量写入（100 条/批）+ 修复空库 delete 报错 / 自锁 rmtree / 缺 sys.path 三个问题；本轮重建 205 文档 / 2089 分片 / 0 失败。

### 升级注意

- 已有环境需执行 `pip install chromadb==1.3.7`；升级 chromadb 前务必在目标机器验证段文件落盘；
- 批量上传期间不要并行跑 pytest，避免 chroma 写锁冲突。

#### 补丁：流程原文检索回退默认库（同日）

- 「查看流程原文」原先按流程定义写死的部门库检索（请假→hr、报销→finance），语料全在 default 库时必然报"暂未检索到"；
- 修复：部门库不存在或未命中时自动回退默认库（`tools.get_workflow_doc`），四个流程实测全部命中（请假→公司规章制度、报销→出差费用管理制度、入职→岗位职责说明书）；52 测试通过。

#### 补丁：对话历史不显示 AI 回答（同日）

- **根因**：后端保存的角色名是 `assistant`，前端历史详情渲染只认 `ai`，导致 AI 回答全部被跳过（存储层一直正常，纯前端展示 bug）；
- 修复：`loadSessionMessages` 兼容两种角色名；刷新页面即可生效。

---

## v2.5.0 — 2026-08-30

### 智能问答体验重构：消除卡顿 + 卡片式 UI + 精简回答 + 流程确认闭环

#### 1. 消除首字"卡住"（体验最痛点）

- **根因**：流式开始前要同步执行「指代消解 + 查询改写」两次 LLM 调用，MiMo 思考模型下单次 7~12s，首字前静默等待可达 30s；
- **MiMo 思考开关**（实测）：`thinking: {"type": "disabled"` 参数有效——辅助调用 12s → 6.8s 且无 reasoning 输出；新增 `LLM_THINKING` 配置（默认 off，设 on 恢复思考模式）；
- **查询准备合并**：指代消解 + 查询改写合并为单次 LLM 调用（`conversational_qa.prepare_query`），无历史时零 LLM 开销；
- **SSE 状态事件**：流式全程推送「理解问题 → 检索知识库 → 生成回答」阶段状态，前端实时显示阶段与耗时，不再只有三个点。
- 实测效果：多轮追问（含指代）端到端 6.9s（优化前该路径仅前置调用就需 20s+）。

#### 2. 回答精简化

- QA 提示词重写：结论先行、默认 3~6 条要点 ≤200 字、步骤用有序列表、禁复述问题/客套话；
- 去掉回答末尾的文字版"参考资料"列表（界面已有来源卡片，避免重复）。
- 实测：病假材料问题回答从大段落降为 73 字要点。

#### 3. 卡片式回答 UI

- AI 回答改为卡片：头部（模型名徽章 + 阶段状态 + 实时耗时）/ 正文（Markdown）/ 尾部（来源引用 + 评分）；流式打字期间头部显示当前阶段。

#### 4. 固定流程改走 LangChain 结构化确认链（不再一句话直接开单）

- 新增 `src/agent/workflow_chain.py`：按流程定义字段 Schema（请假/加班/报销/入职），LLM `with_structured_output` 结构化提取参数，无 LLM 时正则规则兜底；
- `/agent` 的 action 意图从"直接 launch（参数塞 note）"改为返回**草稿**；新增 `POST /api/qa/workflow/draft`（LLM 提取）与 `POST /api/qa/workflow/submit`（必填校验 + 定向提交）；
- 前端渲染**确认表单卡片**：已识别信息预填、缺失必填高亮、可修改后提交，提交后渲染工单卡；
- 实测："帮我请个年假，9月3日到9月5日，家里有事" → 类型/起止日期/事由全部自动识别 → 确认提交 → 工单 `WK-CDFCF6CA03`。

#### 其他

- 修复 `qa.py` 缺少 `HTTPException` 导入导致缺必填提交返回 500 的问题（现正确返回 400）；
- 测试 46 → 52 个全部通过（新增 workflow_chain 6 例：规则提取/必填校验/提交/未知流程）。

#### 补充：推荐追问卡片（同日）

- 回答流式完成后，后端基于本次问答追加生成 3 个最可能的追问（新 `followups` SSE 事件，LLM 失败/演示模式回退通用兜底）；
- 前端在回答卡片尾部渲染「猜你想问」圆角卡片，点击即作为新问题发送；
- 实测："加班怎么申请" → 推荐"加班申请单在哪里下载？/加班审批要多久能批下来？/加班费怎么算的？"。

#### 补充：顶栏模型快捷切换器（同日）

- 智能问答页顶栏新增模型切换器：按服务商分组展示全部预置模型，当前模型高亮；
- **同服务商切换**（如 mimo-v2.5-pro ↔ mimo-v2.5）下拉即生效，toast 提示"后续回答立即生效"；
- **跨服务商切换**引导跳转模型设置页配置对应 API Key（Key 不通用，避免静默用错 Key）；
- 非 admin 只读展示当前模型；模型设置页保存后顶栏同步刷新。

#### 补丁：配置错配防护 + 错误显式上报 + 入库耗时观测（同日）

- **修复事故**：配置被切回 deepseek-chat 但 Key 仍是小米的 → 全部回答静默为空。已在线恢复 MiMo（无需重启）；根因是"换服务商沿用旧 Key"无提示；
- **流式错误显式上报**：新增 `error` SSE 事件——LLM 调用失败（Key 不匹配/额度用尽/网络）或返回空回答时，前端在回答卡片内显示真实原因与处理指引，不再显示笼统的"无法回答"；检索失败同样上报；
- **设置页换服务商提醒**：切到新服务商且未填新 Key 时，Key 栏提示"当前保存的是原服务商的 Key，需填入新 Key 并测试后再保存"；
- **入库耗时日志**：`入库完成 … 耗时 X.XXs`，便于上传批量时观测速度；
- **千份文档容量规划**：新增 `scripts/bench_ingest.py` 基准脚本（隔离临时库可复跑），实测与外推结论写入《后续优化手册》第五节——当前配置 1000 份约 5~10 分钟，4 核 8G 足够；31 份 .doc 旧格式不在支持列表需先转换。

---

## v2.4.0 — 2026-08-30

### 新功能：LLM 服务商切换（小米 MiMo）+ 前端模型配置体验优化

#### 1. 新增小米 MiMo 服务商

- `LLMManager.PROVIDERS` 注册 `mimo`（`https://api.xiaomimimo.com/v1`，OpenAI 兼容，模型 `mimo-v2.5-pro` / `mimo-v2.5`）；
- 配置已切换至 `mimo-v2.5-pro`（`config/model_config.json` + `.env`），检索问答全链路与 SSE 流式已实测通过；
- 注意：MiMo 思考模式不支持自定义 temperature（强制 1.0），详见踩坑记录。

#### 2. 前端模型配置面板重构（在线切换模型）

- **当前生效状态展示**：面板顶部常显「当前生效：服务商 / 模型 + Key 状态」；
- **模型下拉建议**：模型名输入框接 datalist——选服务商自动填预置模型，点「拉取列表」调服务商 `/models` 接口获取该 Key 全部可用模型（新接口 `POST /api/model/models`，仅 admin）；
- **测试与保存职责分离**：`POST /api/model/test` 重构为**纯连通性测试**（不再顺手保存配置，返回响应耗时），测试通过后点「保存并生效」才落盘；保存后立即生效，无需重启服务；
- **权限**：非 admin 用户表单整体只读并提示「仅管理员可修改」；
- API Key 输入留空=沿用已保存 Key，保存成功后自动清空输入框。

---

## v2.3.1 — 2026-08-30

### 安全修复批次：落实 2026-08-27 代码评审的全部严重问题

> 对应评审报告 `code-review-report-20260827.html`；后续优化路线见 `docs/后续优化手册.md`。

#### 1. 反馈管理接口补齐鉴权（🔴 严重）

- `GET /api/qa/feedback/list`、`POST /api/qa/feedback/{id}/resolve` 补上 `Depends(require_role("admin"))`（原先匿名即可读取全部反馈并篡改状态）；
- `GET /api/qa/stats` 增加登录要求（原先匿名可读 Token 用量等运维指标）。

#### 2. 密码哈希升级 scrypt（🔴 严重）

- `src/auth/users.py`：单次 SHA-256 → **scrypt 慢哈希**（N=2^14, r=8, p=1），抗 GPU 暴力破解；
- 存量旧哈希**惰性迁移**：旧格式登录成功后自动重哈希升级，无需重置密码；
- 新增密码强度校验：注册/重置密码最少 8 位。

#### 3. 启动期安全自检（🔴 严重）

- `serve/main.py::_check_security_settings()`：认证开启 + JWT_SECRET 为默认值时**拒绝启动**；
- JWT_SECRET 含 "change-me" / ADMIN_PASSWORD 为默认 admin123 时输出强警告。

#### 4. 上传接口：事件循环防阻塞 + 大小限制（🟡 中等）

- `serve/routers/upload.py`：OCR/清洗/切片/入库等重活移入 `run_in_threadpool`，不再阻塞事件循环；
- 新增 50MB 上传上限（超限返回 413）。

#### 5. 注册接口 role 语义修正（🟡 中等）

- `POST /api/auth/register`：`role`/`extra_kbs` 仅当调用者本身是已登录 admin 时生效（保留管理端建号），公开注册一律强制 user，不再静默忽略。

#### 6. 存储原子写 + 测试

- `users.json` 改为临时文件 + `os.replace` 原子写，防进程中断损坏；
- 新增 `tests/test_security.py`（8 个安全回归用例：反馈鉴权 401/403、stats 登录、scrypt 格式、旧哈希迁移、密码强度、上传 413）；`test_regressions.py` 密码样本同步加长。
- 全量 42 个测试通过；已验证存量 admin 账号（旧 SHA-256）登录成功并自动迁移为 scrypt。

#### 7. 第二批 P0 落实（同日，依据 `docs/后续优化手册.md`）

- **存储原子写推广**：`chat_history/sessions.json`、`knowledge_bases.json`、`feedback.jsonl` 全部改为临时文件 + `os.replace` 原子写；
- **chat_history 锁粒度统一**：sessions.json 的读改写统一走用户级锁，消除消息追加触发 `_touch_session` 与会话重命名/删除并发时互相覆盖丢更新的竞态；
- **匿名接口收紧**：`GET /api/knowledge/stats`、`GET /api/model/config` 增加登录要求（原先匿名可读服务器路径与模型配置）；
- **kb_id 格式校验**：创建知识库时校验 `^[a-zA-Z0-9_-]{1,64}$`，防止畸形 ID 拼出异常 Chroma collection 名；
- 测试增至 46 个全部通过（新增：knowledge/model 鉴权、kb_id 校验、chat_history 20 线程并发写 sessions.json 不损坏）。

#### 8. 启动脚本闪退修复（同日）

- **根因**：`start.bat` 中混入了 UTF-8 中文注释（第 72 行），Windows cmd 以 GBK 解析导致脚本结构被打碎，python 被拼上乱码参数立即退出，窗口闪退。与踩坑记录问题 2 同源（此前修过，后被改回）。
- **修复**：`start.bat` 恢复纯 ASCII（已校验 0 非 ASCII 字节）；启动失败分支增加 `pause`，报错会停留显示日志尾部，不再无声闪退。
- **顺带修复**：前端反馈列表 URL 拼接错误（`/api/qa/feedback/list%26limit%3D50` → 404）改为正确的 `?limit=50`；`/api/health` 版本号同步为 2.3.1。
- **验证**：cmd 下 `start.bat` 启动 → 8008 监听 → health 返回 2.3.1 → `start.bat stop` 正常停止；全量 46 测试通过。
- **自动打开浏览器**：此前删除 main.py 的 `webbrowser.open` 后脚本一直未接管，启动后需手动输入网址。现由 `start.bat` 启动隐藏的 PowerShell 端口探测进程（轮询 TCP 端口最多 30 秒），服务就绪后自动打开默认浏览器；"服务已在运行"分支同样自动打开。浏览器行为仍统一由脚本控制，`serve/main.py` 不回加浏览器逻辑（避免双重打开）。

### 升级注意

- `.env` 中 `ADMIN_PASSWORD` 仍为默认值时，每次启动会出现安全警告，请尽快修改；
- 老用户无需任何操作，首次登录自动完成哈希迁移。

---

## v2.3.0 — 2026-08-25

### 上传文档清洗 + 同名覆盖 + 文档列表排序与更新标记

> 本次升级聚焦「入库质量」与「可维护性」：上传前对文档文本做两阶段清洗，同名文档增量覆盖，入库列表按编号排序并标记更新状态。

#### 1. 上传文档清洗（`src/ingestion/cleaner.py`）

在 `POST /api/upload` 上传入库前，对提取文本做「基础 + 结构」两阶段清洗，提升切片与检索质量：

- **基础清洗**：统一换行符、去零宽/不可见控制字符、规范化标点、去行首尾空白、压缩连续空行；
- **结构清洗**：去页眉页脚（重复短行/页码/分隔线）、去目录页（连续含「…」跳转行）、合并被硬换行拆散的中文段落、去空表格残留与 markdown 表格分隔行（`| --- |`）；
- 清洗后为空的文档直接丢弃，内容相同的相邻分片去重。

#### 2. 同名覆盖入库（`src/ingestion/embedder.py`）

- 上传同名文件时按文件名定位旧 `doc_id`，执行**分片级增量更新**（复用未变化分片、仅增删变更部分，替换整文档重建）；
- 内容完全相同时基于 `content_hash` 查重跳过，防止重复入库；
- 前端上传结果区分 `✅ 清洗入库` / `♻ 覆盖更新` / `⏭ 跳过` 三种状态。

#### 3. 已入库文档列表排序 + 更新标记

**问题**：`GET /api/knowledge/list` 按「最近更新时间」倒序，批量入库后编号顺序被打乱，且无法区分哪些是本次更新、哪些是历史旧文档。

**优化**：
- **后端**：`list_documents()` 改为**按文件名前导数字自然排序**（001 → 056，无编号排最后）；
- **前端**：「已入库文档」新增 `按编号`（默认）/ `按时间` 排序切换按钮；
- **更新标记**：最近 24 小时内更新过的文档显示 `♻ 已更新` 绿色标签，便于对照 `公司管理常用制度合集230份/` 目录核对入库完整性。

---

## v2.2.0 — 2026-08-20

## v2.2.0 — 2026-08-20

### 新功能：用户对话历史 + 个人记忆 + 追问记录

#### 对话历史管理

每个登录用户拥有独立的对话会话列表和历史记录，自动随问答保存。

**后端新增：**
- `src/memory/chat_history.py` — ChatHistoryStore，JSON+JSONL 持久化，线程安全
  - 会话 CRUD（创建/列表/重命名/删除）
  - 消息追加与查询（支持分页）
  - 追问记录标记与检索
  - 跨会话关键词搜索
  - 存储结构：`data/chat_history/{username}/sessions.json` + `{session_id}/messages.jsonl`

- `serve/routers/chat.py` — 对话历史 + 个人记忆 API 路由
  - `GET    /api/chat/sessions` — 会话列表
  - `POST   /api/chat/sessions` — 新建会话
  - `GET    /api/chat/sessions/{id}/messages` — 会话消息记录（分页）
  - `PATCH  /api/chat/sessions/{id}` — 重命名会话
  - `DELETE /api/chat/sessions/{id}` — 删除会话
  - `GET    /api/chat/sessions/{id}/followups` — 追问记录
  - `GET    /api/chat/search?q=` — 跨会话搜索

**QA 接口集成：**
- `POST /api/qa/chat` — 多轮对话自动保存到用户历史
- `POST /api/qa/chat/stream` — 流式对话自动保存（通过 wrapper 收集完整回答）
- `POST /api/qa/agent/react` — Agent 对话自动保存
- 访客模式不保存（无用户身份）

#### 个人记忆文档

用户可主动记录笔记、偏好、重要信息，结构化存储。

**后端新增：**
- `src/memory/user_memory.py` — UserMemoryStore，JSONL 持久化
  - 记忆 CRUD（添加/列表/更新/删除）
  - 标签分类与过滤
  - 关键词搜索
  - 存储结构：`data/user_memory/{username}/memories.jsonl`

**API：**
  - `GET    /api/memory` — 记忆列表（可按标签过滤）
  - `POST   /api/memory` — 添加记忆
  - `PATCH  /api/memory/{id}` — 更新记忆
  - `DELETE /api/memory/{id}` — 删除记忆
  - `GET    /api/memory/search?q=` — 搜索记忆

#### 前端新增

- **对话历史页面**：左侧会话列表 + 右侧消息详情，支持搜索/删除/追问标记
- **个人记忆页面**：卡片式记忆列表，支持添加/编辑/删除/搜索/标签
- 侧边栏新增"对话历史"和"个人记忆"两个导航项（需登录）
- 页面切换时自动加载数据
- 前端版本号 → 5.0

#### 安全

- 所有对话历史和个人记忆 API 均需登录（`get_current_user`）
- 数据按用户名隔离，用户 A 无法访问用户 B 的数据
- QA 接口保存对话时仅保存已登录用户的数据

---

---

## v2.1.1 — 2026-08-20

### 安全修复：会话记忆隔离

#### 问题

`session_id` 完全由客户端控制，未与用户身份绑定。即使开启认证（`AUTH_ENABLED=true`），用户 A 只要知道用户 B 的 `session_id`，即可通过 `/api/qa/chat`、`/api/qa/chat/stream`、`/api/qa/agent/react` 等接口读取对方的对话历史。访客模式下更可随意访问任意会话。

#### 修复

- 新增 `_bind_session(session_id, user)` 函数，将 session_id 与用户身份绑定：
  - 已登录：`session_key = "username:session_id"`，用户间完全隔离
  - 访客模式：`session_key = "anon:session_id"`（统一前缀隔离命名空间）
- 所有使用 session_id 的接口均已接入绑定：
  - `POST /api/qa/chat` — 多轮对话
  - `POST /api/qa/chat/stream` — 流式对话
  - `POST /api/qa/agent/react` — ReAct Agent
  - `POST /api/qa/clear` — 清空会话
- 返回给客户端的仍是原始 `session_id`，绑定过程对前端透明

---

## v2.1.0 — 2026-08-20

### v1.5 优化补全 + 前端全面对接

#### 新增模块

- **查询改写** (`src/retrieval/query_rewriter.py`)
  LLM 智能改写口语化问题 + 规则同义词扩展兜底，提升检索召回率
  - 示例：「咋请假」→「如何申请请假 事假 病假 年假 调休」
  - 支持配置开关 `QUERY_REWRITE=true/false`

- **上下文压缩** (`src/chains/context_compressor.py`)
  Token 超预算时对分片做 LLM 摘要或规则截断，降低约 60% Token 消耗
  - 两种模式：LLM 摘要压缩（质量优先）+ 规则截断压缩（离线兜底）
  - 默认 Token 预算 6000，可通过 `CONTEXT_TOKEN_BUDGET` 配置

- **热查询缓存** (`src/retrieval/cache.py`)
  LRU 缓存高频问题，TTL 1 小时，文档变更自动失效
  - 默认容量 100 条，可通过 `CACHE_SIZE` 配置
  - 线程安全，支持命中率统计

- **Token 用量监控** (`src/core/token_tracker.py`)
  跟踪每次 LLM 调用的 Token 消耗，超 8000 告警，按模型/接口统计
  - 记录输入/输出 Token、调用次数、模型分布
  - 提供汇总统计 API

- **用户反馈系统** (`src/eval/feedback.py`)
  1-5 分评分，低分（≤2）自动标记待优化，JSONL 持久化存储
  - 支持按状态/分数过滤查询
  - 管理员可标记低分反馈已处理

- **DOCX 表格提取** (`src/ingestion/loader.py`)
  Word 文档中的表格自动转为 Markdown 格式入库，避免表格信息丢失
  - 支持多表格提取，保留行列结构

#### 新增 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/qa/feedback | 提交回答评分反馈 |
| GET | /api/qa/feedback/list | 查询用户反馈记录（支持按状态/分数过滤） |
| POST | /api/qa/feedback/{id}/resolve | 标记低分反馈已处理 |
| GET | /api/qa/stats | 系统运行统计（缓存命中率 + Token 用量 + 反馈统计） |

#### 前端对接

- **回答评分组件**：每条 AI 回答下方显示 1-5 星评分按钮，点击即提交
- **系统统计面板**：控制台新增 4 个统计卡片（缓存命中、Token 用量、反馈均分、待优化条数）
- **反馈管理表格**：系统设置页新增反馈管理面板，支持筛选/标记已处理
- CSS 版本号 3.9 → 4.0，JS 版本号同步更新

#### 链路集成

- `ask()` 单次问答：缓存检查 → 查询改写 → 检索 → 上下文压缩 → 生成 → Token 记录 → 写缓存
- `answer_with_history()` 多轮对话：指代消解 → 查询改写 → 检索 → 上下文压缩 → 生成 → Token 记录
- 流式问答 `sendStream()`：集成查询改写 + 上下文压缩
- 文档入库/删除时自动清空查询缓存

#### 配置新增

```env
QUERY_REWRITE=true          # 查询改写开关
CONTEXT_COMPRESS=true       # 上下文压缩开关
CONTEXT_TOKEN_BUDGET=6000   # 上下文 Token 预算
CACHE_ENABLED=true           # 热查询缓存开关
CACHE_SIZE=100               # 缓存容量
CACHE_TTL=3600               # 缓存过期时间（秒）
```

#### 测试修复

- 修复 `test_regressions.py` 中 2 个预先存在的 mock 参数不匹配问题（`get_vectorstore` mock 不接受 `kb_id` 参数）

---

## v2.0.0 — 2026-08-12

### 架构级重构：可观测、可评估、可扩展的多知识库 RAG 平台

> 从 1.0 的「演示完备、离线可跑」升级为「真实能力 + 专业架构」，面向简历/面试/真实落地展示。

#### 架构升级

- **依赖注入容器** (`src/core/container.py`)
  消除 1.0 的全局单例 + 循环 import，改为组合根装配
  - 支持 `container.override(key, fake)` 注入测试替身
  - 统一管理 LLM / Embedding / VectorStore / Reranker / Retriever / Memory

- **端口与适配器** (`src/core/interfaces.py`)
  定义抽象接口：`BaseReranker` / `BaseRetrieverPort` / `SessionMemoryPort` / `VectorMemoryPort` / `LLMProvider`
  - 实现可插拔、可替换，调用方面向接口编程

- **结构化日志 + 请求追踪** (`src/core/tracing.py`)
  每个请求分配 request_id，全链路追踪

#### 检索链路升级

- **可插拔 Reranker** (`src/models/reranker.py`)
  - `BGEReranker`：在线 Qwen-Rerank 或本地 FlagEmbedding
  - `DemoReranker`：1.0 字符重合兜底
  - 通过 settings 选择 provider，无需改调用方

- **全量 BM25 检索** (`src/retrieval/hybrid_search.py`)
  - 从 1.0 的「dense 子集二次检索」升级为全量语料 BM25 分片索引
  - 按 doc_id 分片管理，大语料用分片 BM25 + 分段 IDF
  - Dense + Sparse 混合检索 + RRF 融合

#### Agent 工作流升级

- **LangGraph 多节点工作流** (`src/agent/agent.py`)
  - 从 1.0 的两节点（route→act）升级为多节点真实工作流
  - 节点：intent_classifier → knowledge (retrieve→rerank→generate) / action (工具调用→结果回填) → memory_writer → 输出
  - 工具注册表支持真实 HTTP 工具 + mock 双模式

- **ReAct Agent** (`src/agent/react_agent.py`)
  - LLM 自主决策（bind_tools），支持多轮、多工具组合
  - 未配置真实 LLM 时自动回退规则 Agent（离线可用）

#### 记忆系统升级

- **长期向量记忆** (`src/memory/vector_memory.py`)
  - 从 1.0 的「未接通」升级为全链路接通
  - 短期会话记忆 + 长期向量记忆 + 记忆摘要

- **真指代消解** (`src/chains/conversational_qa.py`)
  - LLM 将多轮对话中的指代词消解为独立问题
  - 注入长期记忆，支持上下文相关检索

#### 认证与权限

- **JWT 认证** (`src/auth/jwt.py`, `src/auth/deps.py`)
  - JWT 签发/校验 + bcrypt 密码加密
  - 支持关闭（`AUTH_ENABLED=false` 匿名演示）

- **RBAC 角色权限** (`src/auth/rbac.py`)
  - admin：可访问所有知识库
  - user：部门白名单 + 公开库 + 个人额外授权

- **多知识库管理** (`src/kb/manager.py`)
  - 知识库 CRUD + 部门白名单
  - 按用户权限过滤可见知识库

#### 评估体系

- **Eval 评估集** (`src/eval/runner.py`, `src/eval/metrics.py`)
  - 检索指标：hit_rate / precision / f1
  - LLM-as-judge 打分（可选）
  - 支持指定知识库、输出 JSON

#### 流式输出

- **真 LLM 流式** (SSE)
  - 从 1.0 的「拆字模拟」升级为真 LLM stream=True
  - SSE 推送 sources / token / done 事件

#### 前端升级

- 深色/浅色主题切换
- 控制台仪表盘（文档统计 + 已入库文档列表）
- 智能问答页（多轮对话 + 流式输出 + 参考资料引用 + 流程发起）
- 知识库管理页（CRUD + 文档入库 + 批量入库）
- 模型设置页（LLM 配置 + 测试连接）
- 用户管理页（注册 + 编辑 + 部门/授权管理）
- 系统设置页（系统信息展示）

#### 部署

- Docker Compose 部署方案
- Nginx 反向代理配置
- 环境变量管理（`.env` + `pydantic-settings`）

---

## v1.0.0 — 2026-08-05（初始版本）

### 基础 RAG 问答系统

> 演示完备、离线可跑的企业知识问答 MVP。

#### 核心功能

- **文档入库流水线** (`src/ingestion/`)
  - 支持 PDF (PyMuPDF) / Word (python-docx) / Markdown / TXT 解析
  - RecursiveCharacterTextSplitter 智能切片（chunk_size=500, overlap=50）
  - DashScope text-embedding-v3 向量化
  - ChromaDB 持久化存储

- **检索模块** (`src/retrieval/`)
  - 向量语义检索（Dense）
  - DemoReranker（字符重合度排序，离线兜底）
  - HybridRetriever（LCEL 兼容）

- **问答链路** (`src/chains/`)
  - 单次问答 `ask()`：检索 → 生成 → 带来源引用
  - 多轮对话 `answer_with_history()`：会话记忆 + 指代消解

- **LLM 工厂** (`src/models/llm.py`)
  - 真实模式：ChatOpenAI（DeepSeek / Qwen / OpenAI 兼容接口）
  - 演示模式：DemoLLM（规则引擎，无 API Key 可跑）
  - 自动降级：Key 无效/过期时自动回退演示模式

- **Agent** (`src/agent/agent.py`)
  - 意图分类：query / action / list
  - 两节点工作流：route → act
  - 工具注册表（mock 模式）

- **会话记忆** (`src/memory/conversation_memory.py`)
  - 进程内 dict 存储对话历史
  - 支持多会话隔离

- **Prompt 模板** (`src/prompts/templates.py`)
  - QA_SYSTEM：企业知识助手提示词
  - 约束 LLM 仅基于参考资料回答，附带来源引用

#### API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/qa/ask | 单次问答 |
| POST | /api/qa/chat | 多轮对话 |
| POST | /api/qa/chat/stream | 流式对话（拆字模拟） |
| POST | /api/qa/agent | Agent 问答 |
| POST | /api/qa/clear | 清空会话 |
| POST | /api/upload | 上传文档 |
| POST | /api/knowledge/ingest | 批量入库 |
| DELETE | /api/knowledge/{doc_id} | 删除文档 |
| GET | /api/knowledge/list | 文档列表 |
| GET | /api/knowledge/stats | 系统统计 |

#### 前端

- 基础 HTML 单页应用
- 问答交互界面
- 文档上传与入库
- 文档列表管理

#### 配置

- `config/settings.py`：pydantic-settings 全局配置
- `.env.example`：环境变量模板
- 无 Key 时自动进入演示模式

---

## 版本规划

### v2.2.0（规划中）

- [ ] 多轮对话支持（历史上下文记忆增强）
- [ ] 文档版本管理（更新自动重索引）
- [ ] 用户反馈闭环（低分回答自动标记优化）

### v3.0.0（远期规划）

- [ ] 多 Agent 协作（问答 Agent + 文档整理 Agent + 质检 Agent）
- [ ] 接入企业 IM（飞书/钉钉机器人）
- [ ] 知识图谱增强检索
- [ ] 支持图片/表格/流程图的智能解析
- [ ] 细粒度权限控制（按部门隔离知识库）
