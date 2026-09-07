"""LLM 模型配置接口：模型切换、连通性测试与可用模型列表拉取。"""
from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.auth.deps import get_current_user, require_role
from src.models.llm import configure_llm, get_llm_config, get_runtime_config

router = APIRouter(prefix="/api/model", tags=["模型配置"])


class ModelConfig(BaseModel):
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class ModelListRequest(BaseModel):
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None


def _resolve_effective(provider: str | None, base_url: str | None,
                       api_key: str | None) -> tuple[str, str]:
    """按「请求传入 > 服务商预置 > 当前运行时配置」解析出 base_url 与 api_key。

    api_key 允许为空串（未传时用已存配置），便于只改 base_url 就能测试。
    """
    cfg = get_runtime_config()
    providers = get_llm_config().get("providers", {})
    eff_base = (base_url or "").strip().rstrip("/")
    if not eff_base and provider and provider in providers:
        eff_base = providers[provider]["base_url"]
    if not eff_base:
        eff_base = cfg.get("base_url", "")
    eff_key = (api_key or "").strip() or cfg.get("api_key", "")
    return eff_base, eff_key


@router.get("/config", summary="获取当前模型配置（需登录）")
def get_config(_: dict | None = Depends(get_current_user)) -> dict:
    """认证开启时需登录；API Key 已脱敏，但配置本身不对匿名暴露。"""
    return get_llm_config()


@router.post("/config", summary="更新模型配置（切换模型/填 API-KEY，仅 admin）")
def update_config(
    body: ModelConfig,
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    # 若选了预置 provider，自动填充 base_url
    providers = get_llm_config().get("providers", {})
    if body.provider and body.provider in providers:
        body.base_url = body.base_url or providers[body.provider]["base_url"]
    configure_llm(
        base_url=body.base_url,
        api_key=body.api_key,
        model=body.model,
        provider=body.provider,
    )
    return {"ok": True, "config": get_llm_config()}


@router.post("/test", summary="测试配置连通性（仅 admin，不保存）")
def test_config(
    body: ModelConfig | None = None,
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    """用「表单值 + 已存配置」合并出的临时配置发起一次真实调用。

    纯测试：不落盘、不改变当前生效配置（与保存按钮职责分离）。
    """
    from config.settings import settings

    provider = (body.provider if body else None) or None
    base_url, api_key = _resolve_effective(
        provider, body.base_url if body else None, body.api_key if body else None
    )
    model = ((body.model if body else None) or get_runtime_config().get("model", "")).strip()
    if not api_key:
        return {"ok": False, "message": "未配置 API-KEY（当前为演示模式）"}
    if not model:
        return {"ok": False, "message": "未指定模型名称"}

    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=settings.llm_temperature,
        timeout=30,
    )
    t0 = time.time()
    try:
        answer = llm.invoke("你好，请用一句话介绍你自己")
        text = getattr(answer, "content", str(answer))
        ms = int((time.time() - t0) * 1000)
        return {
            "ok": True,
            "message": f"连接成功（{ms}ms）：{str(text)[:60]}",
            "model": model,
            "latency_ms": ms,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"连接失败：{e}"}


@router.post("/models", summary="拉取服务商可用模型列表（仅 admin）")
def list_models(
    body: ModelListRequest,
    _: dict | None = Depends(require_role("admin")),
) -> dict:
    """调用服务商 OpenAI 兼容的 GET /models 接口，返回可用模型 id 列表。

    未传 base_url/api_key 时使用当前已保存配置；用于前端模型下拉建议。
    """
    base_url, api_key = _resolve_effective(body.provider, body.base_url, body.api_key)
    if not base_url:
        return {"ok": False, "models": [], "message": "缺少 base_url，请先选择服务商"}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        r = httpx.get(f"{base_url}/models", headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json().get("data", [])
        ids = sorted({m.get("id", "") for m in data if isinstance(m, dict) and m.get("id")})
        return {"ok": True, "models": ids, "message": f"获取到 {len(ids)} 个模型"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "models": [], "message": f"拉取失败：{e}"}
