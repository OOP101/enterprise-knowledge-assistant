"""LLM 实例化。

统一封装 LLM 的创建逻辑：
- 配置了 API Key 时使用真实 LLM（OpenAI 兼容接口，如 DeepSeek / Qwen / vLLM）
- 未配置时回退到本地规则引擎（演示模式），保证整个链路可离线运行
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from config.settings import settings

DEMO_SYSTEM_PROMPT = (
    "你是企业知识助手。请严格基于给定的参考资料回答问题，"
    "不要编造资料中不存在的信息。如果资料不足，请明确说明。"
)


class DemoLLM:
    """演示模式 LLM：基于关键词与规则匹配，保证无 API Key 时链路可用。

    回答规范：
    - 将检索到的知识**整理为步骤/流程**，而非粘贴原文。
    - 正文用上标引用编号 [1][2]，文末列出"参考资料"对应编号与文件名。
    """

    def __init__(self, context_docs: list[dict] | None = None) -> None:
        # 当前检索到的来源（[{source, content}]），用于文末引用
        self._context_docs = context_docs or []

    def set_context(self, context_docs: list[dict]) -> None:
        self._context_docs = context_docs or []

    def _sources_block(self) -> str:
        """生成文末参考资料块。"""
        if not self._context_docs:
            return ""
        lines = ["\n参考资料："]
        for i, doc in enumerate(self._context_docs, 1):
            src = doc.get("source", "未知来源")
            name = str(src).split("\\")[-1].split("/")[-1]
            lines.append(f"[{i}] {name}")
        return "\n".join(lines)

    def _respond(self, question: str, context: str = "") -> str:
        q = question.lower()

        # 1. 命中知识库：整理为流程 + 文末引用
        if context:
            steps = self._extract_steps(question, context)
            if steps:
                refs = self._sources_block()
                return steps + refs

        if any(k in q for k in ("你好", "hi", "hello", "你是谁")):
            return "你好！我是企业知识助手，可以回答关于公司 SOP、技术文档等问题。"
        if "请假" in q or "流程" in q:
            return (
                "请假流程如下：\n"
                "1. 填写《请假申请单》，在流程中心提前一天提交\n"
                "2. 直属主管审批（超 3 天需部门负责人加签）\n"
                "3. 人事部门备案并录入考勤系统\n"
                "4. 事假、病假、年假、调休均需在 OA 系统留痕\n\n"
                "注：当前未配置 LLM API Key，以上为演示模式回答。"
            )
        return "【演示模式】我已收到你的问题，但当前未配置 LLM API Key，无法给出高质量回答。请在下方设置面板中配置模型与 API Key。"

    @staticmethod
    def _extract_steps(question: str, context: str) -> str:
        """从检索上下文整理出步骤式回答。

        找出与问题关键词重合度最高的若干段落，格式化为编号流程。
        """
        q_words = {w for w in re.findall(r"[\u4e00-\u9fa5]{2,}|[a-z0-9]+", question.lower())}
        paras = [p.strip() for p in context.split("\n") if p.strip()]
        if not paras:
            return ""

        scored = []
        for p in paras:
            # 跳过 markdown 标题与纯修饰行
            if p.startswith("#"):
                continue
            p_words = {w for w in re.findall(r"[\u4e00-\u9fa5]{2,}|[a-z0-9]+", p.lower())}
            score = len(q_words & p_words)
            if score > 0:
                scored.append((score, p))
        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            return ""

        # 取最相关的段落（至多 5 段），去重并裁剪为要点
        seen, steps = set(), []
        for _, p in scored:
            if p not in seen:
                seen.add(p)
                steps.append(DemoLLM._compact(p))
            if len(steps) >= 5:
                break

        body = "根据知识库为你整理如下：\n"
        for i, s in enumerate(steps, 1):
            body += f"{i}. {s}\n"
        return body

    @staticmethod
    def _compact(text: str) -> str:
        """将段落压缩为要点：去编号前缀、去空白、限长。"""
        text = re.sub(r"^[\d\-\*\.、\s]+", "", text).strip()
        text = re.sub(r"\s+", " ", text)
        return text[:80]

    def invoke(self, messages: list[dict[str, Any]] | str, **_: Any) -> str:
        """兼容 LangChain BaseChatModel / BaseLLM 的调用签名。

        从所有消息中合并文本，分别提取问题与参考资料上下文。
        """
        if isinstance(messages, str):
            return self._respond(messages)

        # 合并所有消息 content（兼容 dict 与 LangChain 消息对象）
        parts: list[str] = []
        for msg in messages:
            if isinstance(msg, dict):
                content = msg.get("content", "")
            elif hasattr(msg, "content"):  # LangChain BaseMessage（SystemMessage/HumanMessage 等）
                content = msg.content
            else:
                continue
            if isinstance(content, list):
                content = " ".join(
                    str(p.get("text", "")) for p in content if isinstance(p, dict)
                )
            if content:
                parts.append(str(content))
        full = "\n".join(parts)

        question = self._extract_question(full)
        context = self._extract_context(full)
        return self._respond(question, context)

    @staticmethod
    def _extract_question(text: str) -> str:
        """从 prompt 中提取用户问题。

        优先匹配"用户问题/人类问题:"标记；否则取最后一段非空行
        （多轮对话中最后一条 Human 消息即当前问题）。
        """
        # 若存在独立分隔的问题行
        m = re.search(r"(?:用户问题|人类问题|Human|Question)[:：]\s*(.+)", text, re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1).strip()
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        return lines[-1] if lines else text.strip()

    @staticmethod
    def _extract_context(text: str) -> str:
        """从 prompt 中提取参考资料块（截止到问题标记或结尾）。"""
        m = re.search(
            r"【参考资料】\s*(.*?)(?=\n\n用户问题|\n用户问题:|\n问题:|\Z)",
            text, re.IGNORECASE | re.DOTALL,
        )
        if m:
            return m.group(1).strip()
        # 兼容英文标记
        m2 = re.search(
            r"(?:参考资料|Context)[:：]\s*(.*?)(?:\n\n|\n(?:用户问题|人类问题|Human|Question)|\Z)",
            text, re.IGNORECASE | re.DOTALL,
        )
        return m2.group(1).strip() if m2 else ""


class LLMManager:
    """运行时 LLM 配置管理器。

    支持在运行时切换模型 / API Key / Base URL，并持久化到 JSON 文件，
    重启后配置自动恢复。未配置有效 API Key 时回退到演示模式。
    """

    CONFIG_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "model_config.json"

    # 预置常见模型服务商（OpenAI 兼容接口）
    PROVIDERS = {
        "deepseek": {
            "name": "DeepSeek",
            "base_url": "https://api.deepseek.com/v1",
            "models": ["deepseek-chat", "deepseek-reasoner"],
        },
        "qwen": {
            "name": "通义千问 (Qwen)",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "models": ["qwen-plus", "qwen-max", "qwen-turbo"],
        },
        "openai": {
            "name": "OpenAI",
            "base_url": "https://api.openai.com/v1",
            "models": ["gpt-4o", "gpt-4o-mini"],
        },
        "moonshot": {
            "name": "Moonshot (Kimi)",
            "base_url": "https://api.moonshot.cn/v1",
            "models": ["moonshot-v1-8k", "moonshot-v1-32k"],
        },
        "mimo": {
            "name": "小米 MiMo",
            "base_url": "https://api.xiaomimimo.com/v1",
            "models": ["mimo-v2.5-pro", "mimo-v2.5"],
        },
    }

    def __init__(self) -> None:
        self._config: dict = {
            "base_url": settings.llm_api_base,
            "api_key": settings.llm_api_key,
            "model": settings.llm_model,
            "provider": "",
        }
        self._load()

    def _load(self) -> None:
        try:
            if self.CONFIG_FILE.exists():
                import json

                data = json.loads(self.CONFIG_FILE.read_text(encoding="utf-8"))
                self._config.update({k: v for k, v in data.items() if v})
        except Exception:  # noqa: BLE001
            pass

    def _save(self) -> None:
        import json

        try:
            self.CONFIG_FILE.write_text(
                json.dumps(self._config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

    def get_config(self) -> dict:
        return dict(self._config)

    @property
    def ready(self) -> bool:
        return bool(self._config.get("api_key"))

    def configure(self, *, base_url: str | None = None, api_key: str | None = None,
                  model: str | None = None, provider: str | None = None) -> dict:
        """运行时更新模型配置并持久化。

        api_key 为空串视为"保持不变"，避免前端未填写 Key 时误清空已有配置。
        """
        if base_url is not None:
            self._config["base_url"] = base_url.strip().rstrip("/")
        if api_key is not None:
            api_key = api_key.strip()
            if api_key:
                self._config["api_key"] = api_key
        if model is not None:
            self._config["model"] = model.strip()
        if provider is not None:
            self._config["provider"] = provider.strip()
        self._save()
        return self.get_config()

    def build(self, context_docs: list[dict] | None = None) -> Any:
        """基于当前配置构建 LLM 实例。"""
        cfg = self._config
        if not cfg.get("api_key"):
            return DemoLLM(context_docs=context_docs)
        try:
            from langchain_openai import ChatOpenAI

            extra_kwargs: dict = {}
            if settings.llm_thinking == "off":
                # 思考模式关闭（MiMo 实测可省约一半首字延迟，且恢复 temperature 可用）
                extra_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            return ChatOpenAI(
                model=cfg["model"],
                api_key=cfg["api_key"],
                base_url=cfg["base_url"],
                temperature=settings.llm_temperature,
                **extra_kwargs,
            )
        except ImportError:
            return DemoLLM(context_docs=context_docs)


_manager = LLMManager()


def get_llm(context_docs: list[dict] | None = None) -> Any:
    """返回全局 LLM 实例（读取运行时配置）。"""
    return _manager.build(context_docs=context_docs)


def configure_llm(**kwargs: Any) -> dict:
    """运行时配置 LLM（供 API/前端调用）。"""
    return _manager.configure(**kwargs)


def get_llm_config() -> dict:
    """获取当前 LLM 配置（不回传完整 API Key，仅返回掩码展示）。"""
    cfg = _manager.get_config()
    key = cfg.pop("api_key", "")
    cfg["api_key_masked"] = key[:6] + "****" + key[-4:] if len(key) > 10 else ("已配置" if key else "")
    cfg["api_key"] = ""  # 完整 Key 只在写入时接受，绝不回传前端/接口
    cfg["ready"] = _manager.ready
    cfg["providers"] = _manager.PROVIDERS
    return cfg


def get_runtime_config() -> dict:
    """返回运行时配置（含完整 API Key）。

    仅供服务端内部使用（连通性测试 / 模型列表拉取），绝不通过接口回传。
    """
    return _manager.get_config()


def get_demo_system_prompt() -> str:
    return DEMO_SYSTEM_PROMPT
