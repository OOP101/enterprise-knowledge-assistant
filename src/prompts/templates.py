"""提示词模板。

集中管理所有 Prompt，便于统一维护与调优。
"""
from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

# ---- QA Prompt：严格基于知识库回答（结论先行、精简） ----
QA_SYSTEM = """你是一个严谨的企业知识助手。请遵循以下要求：
1. 仅依据【参考资料】中的内容回答问题，不得编造资料中没有的信息。
2. 如果参考资料不足以回答，直接说明"资料中未找到相关内容"，并建议用户补充关键词。
3. 回答使用简体中文，**结论先行，精简为要点**：
   - 默认 3~6 条要点、总长不超过 200 字；用户明确要求"详细/展开"时才输出完整步骤。
   - 不复述用户问题，不写"根据参考资料"之类的开场白，不输出与问题无关的资料内容。
4. 步骤/流程类问题用有序列表，一步一行；适用条件、注意事项用无序列表。
5. 若参考资料之间存在冲突，以最新或标注为"现行有效"的版本为准；无法判断时提示"以公司现行文件或 HR 最新说明为准"。
6. **不要**在正文标注引用编号，**不要**输出"参考资料"文字列表——界面会单独展示来源卡片。

【参考资料】
{context}
"""

qa_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", QA_SYSTEM),
        MessagesPlaceholder(variable_name="chat_history"),
        ("human", "{question}"),
    ]
)

# ---- 对话式 QA Prompt（带历史） ----
conversational_qa_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", QA_SYSTEM),
        MessagesPlaceholder(variable_name="chat_history"),
        ("human", "{question}"),
    ]
)

# ---- 文档入库 / 摘要 Prompt ----
summarize_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "请用 3-5 句话概括以下文档内容，突出主题、适用范围与关键结论：\n\n{text}",
        ),
        ("human", "请生成摘要。"),
    ]
)

# ---- 对话压缩 / 指代消解 Prompt ----
condense_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "根据对话历史，将用户的最后一句话改写为一个独立、完整、不含指代的问题，"
            "以便用于检索。只输出改写后的问题，不要输出其他内容。",
        ),
        MessagesPlaceholder(variable_name="chat_history"),
        ("human", "{question}"),
    ]
)

# ---- 检索查询准备 Prompt（合并指代消解 + 查询改写，单次 LLM 调用降低首字延迟） ----
search_query_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "你是检索查询优化器。结合对话历史，把用户的最后一句话改写为一个**独立、完整、不含指代**的检索查询，"
            "并适当补充同义关键词（如制度用语）。只输出改写后的查询本身，一行以内，不要解释。",
        ),
        MessagesPlaceholder(variable_name="chat_history"),
        ("human", "{question}"),
    ]
)

# ---- 推荐追问生成 Prompt（回答完成后追加，驱动"猜你想问"卡片） ----
FOLLOWUP_SYSTEM = (
    "基于用户的问题和刚才的回答，预测用户最可能继续追问的 3 个问题。"
    "要求：与当前话题直接相关、口语化、每条不超过 18 个字、不带编号。"
    "只输出 JSON 数组，格式如 [\"问题1\",\"问题2\",\"问题3\"]，不要输出其他内容。"
)

# 无 LLM 时的通用追问兜底
DEFAULT_FOLLOWUPS = ["这个流程需要准备哪些材料？", "审批一般需要多久？", "办理的入口在哪里？"]

# ---- Agent 系统提示 ----
AGENT_SYSTEM = """你是企业知识助手智能体。你拥有以下工具：
{tools}

工作原则：
- 优先使用知识库检索工具回答员工关于流程、制度、SOP 的问题。
- 如果员工表达了办理/提交/跳转等行动意图，调用相应工具。
- 若工具结果不足以回答，坦诚说明，不编造。
"""

# ---- 意图分类 Prompt（2.0：LLM 辅助意图分类）----
intent_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "你是意图分类器。判断用户问题属于哪一类，只输出一个单词（query / action / list）：\n"
            "- query：询问知识、制度、流程步骤、SOP 内容\n"
            "- action：表达办理/申请/提交/发起某个流程的行动意图\n"
            "- list：询问有哪些可用流程/服务列表\n",
        ),
        ("human", "{question}"),
    ]
)

# 未配置真实 LLM 时的兜底模板（供 DemoLLM 或离线逻辑参考）
DEMO_QA_TEMPLATE = """【参考资料】
{context}

用户问题：{question}

【演示模式回答】
"""
