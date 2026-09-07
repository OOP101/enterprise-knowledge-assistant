"""pytest 全局夹具：测试环境强制离线演示模式。

- 清空 LLM / Embedding Key（无论 .env / model_config.json 是否配置），
  保证测试不依赖真实 API 与网络，可离线稳定复现。
- 通过修改全局 settings 单例 + LLMManager 运行时配置实现。
"""
import pytest


@pytest.fixture(autouse=True)
def force_demo_mode(monkeypatch):
    from config.settings import get_settings
    from src.models import llm as llm_mod

    s = get_settings()
    monkeypatch.setattr(s, "llm_api_key", "")
    monkeypatch.setattr(s, "embedding_api_key", "")
    # LLMManager 单例：清除运行时 Key（可能来自 model_config.json）
    monkeypatch.setattr(
        llm_mod._manager,
        "_config",
        {
            "base_url": s.llm_api_base,
            "api_key": "",
            "model": s.llm_model,
            "provider": "",
        },
    )
