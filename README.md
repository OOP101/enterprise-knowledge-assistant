# 企业知识助手

将 SOP、技术文档转化为可查询的知识库，员工提问即可秒级获取标准答案。2.0 升级为可观测、可评估、可扩展的多知识库 RAG 平台；3.0 新增入库审核流（AI 预筛打分 + 人工把关）；4.0 完成 UI「科技蓝」改版（默认浅色主题，P1 已合入）。

> 📌 **完整项目文档**（背景/架构/进度/踩坑/数据口径）见 [`docs/项目文档.md`](docs/项目文档.md)。
> 📌 **开发手册**（全程关键节点开发日志，11 篇）见 [`docs/开发手册/`](docs/开发手册/README.md)。
> 📌 **版本变更**见 [`CHANGELOG.md`](CHANGELOG.md)。

## 界面预览

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/home.png" alt="控制台"><br><sub><b>控制台</b> — 202 文档 / 2086 分片 / Token 用量与缓存命中</sub></td>
    <td width="50%"><img src="docs/screenshots/chat.png" alt="智能问答"><br><sub><b>智能问答</b> — 引用溯源（点击查看原文）+ 阶段耗时 3.9s</sub></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/knowledge-bases.png" alt="知识库管理"><br><sub><b>知识库管理</b> — 8 部门库 + 默认库，按部门授权</sub></td>
    <td><img src="docs/screenshots/model-switch.png" alt="模型热切换"><br><sub><b>模型热切换</b> — DeepSeek / Qwen / GPT / Kimi / MiMo 分组，会话内直接切换</sub></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/system-test.png" alt="系统测试"><br><sub><b>系统测试</b> — 意图路由与 ReAct 工具调用轨迹可视化</sub></td>
    <td><img src="docs/screenshots/model-settings.png" alt="模型设置"><br><sub><b>模型设置</b> — LLM 与 Embedding 运行时配置，保存即生效</sub></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/users.png" alt="用户管理"><br><sub><b>用户管理</b> — JWT 认证 + 部门白名单三级权限</sub></td>
    <td><img src="docs/screenshots/settings.png" alt="系统设置"><br><sub><b>系统设置</b> — 系统运行信息与用户反馈管理</sub></td>
  </tr>
</table>

<sub>入库审核工作台（前端 P2 规划中）等其余页面截图见 [`docs/screenshots/`](docs/screenshots/)。</sub>

## 核心亮点

- **混合检索**：Dense 向量 + 全量 BM25 双路召回 → RRF 融合（K=60）→ 可插拔重排（Qwen / BGE / Demo 兜底）
- **流式问答**：SSE 全程阶段推送（理解问题 → 检索 → 生成），多轮追问端到端 **6.9s**（实测）
- **入库审核流**：四维规则打分（0-100）+ 审核状态机 + 双层存储，人工标注分片检索加权 ×1.2
- **多知识库 × RBAC**：8 部门 + default 共 9 库；JWT 认证 + 部门白名单三级权限
- **格式覆盖**：PDF / Word(.docx·.doc) / Excel(.xlsx) / MD / TXT；不支持的格式（.xls/.wps）入库时显式上报，不静默跳过
- **冷启动 2.4s**（实测，原 ~17s）；**75 个测试全离线通过**（无需 API Key / 向量库 / 本地模型）

## 技术栈

| 层级 | 选型 |
|------|------|
| 编排框架 | LangChain v0.3+ / LangGraph |
| Web 服务 | FastAPI |
| 向量数据库 | ChromaDB（开发）→ Milvus（生产） |
| LLM | 阿里云百炼 `deepseek-v3`（主，OpenAI 兼容，运行时热切换）/ 任意 OpenAI 兼容接口 |
| Embedding | 可插拔：DashScope text-embedding-v3 / 本地 BGE / Demo 兜底 |
| 重排序 | 可插拔：Qwen-Rerank / BGE 本地 / Demo 兜底 |
| 认证 | JWT + RBAC |
| 文档处理 | PyMuPDF / python-docx(.docx) / olefile(.doc) / zipfile(.xlsx) |

## 快速开始

```bash
# 1. 安装依赖
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
# ⚠ requirements 锁定 langchain 0.3.x 依赖矩阵，请勿单独升级 langchain/langgraph/langchain-openai
#   （1.x 漂移会导致 LLM 静默回退演示模式）

# 2. 配置环境变量
cp .env.example .env
# 填写 LLM_API_KEY 与 EMBEDDING_API_KEY；留空自动进入演示模式（规则引擎离线可跑）

# 3. 启动服务
uvicorn serve.main:app --reload --port 8008
# 或 Windows 直接双击 start.bat（纯 ASCII，自动探测端口并打开浏览器）
```

访问 http://localhost:8008/docs 查看 Swagger 接口文档。

## 入库文档

将文档放入 `data/docs/`（支持 PDF / Word `docx·doc` / Excel `xlsx` / MD / TXT），调用 `POST /api/knowledge/ingest` 批量入库，或 `POST /api/upload` 上传。上传同名文档自动覆盖更新，低质文档会被审核流挂起待人工审。

## 测试

```bash
pytest tests/
```

75 个用例全离线可跑（FakeCollection / FakeVS 替代真实 ChromaDB，无需 API Key）。
