"""评估接口：运行 Eval 并返回指标。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.auth.deps import require_role
from src.eval.runner import run_eval

router = APIRouter(prefix="/api/eval", tags=["评估"])


class EvalRequest(BaseModel):
    kb_id: str = "default"
    use_judge: bool = False


@router.post("/run", summary="运行评估", dependencies=[Depends(require_role("admin"))])
def eval_run(body: EvalRequest) -> dict:
    from src.core import get_container

    container = get_container()
    retriever = container.get_retriever(
        kb_id=body.kb_id, top_k=container.settings.eval_top_k
    )
    llm = container.get_llm() if body.use_judge else None
    return run_eval(retriever, llm=llm, use_judge=body.use_judge, kb_id=body.kb_id)
