"""LLM 辅助评测集生成脚本：用 MiMo 读制度文档，生成「事实级」检索评测集。

与 gen_eval_dataset.py（模板生成"XX的主要内容是什么"）不同，本脚本让 LLM
理解文档内容后，产出答案唯一、可精准检索的具体问题（如"试用期最长不得超过
几个月"），gold_answer 为文档原文可查的事实答案。这是业内 RAG 评测集的标准
做法，指标可比模板题显著更可信。

用法：
    python scripts/gen_eval_llm.py                 # 全量生成
    python scripts/gen_eval_llm.py --per-doc 3     # 每文档生成 3 条
    python scripts/gen_eval_llm.py --limit 5       # 只处理前 5 份（试跑）
"""
from __future__ import annotations

import argparse
import ast
import json
import logging
import re
import sys
import time
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from docx import Document as DocxDocument  # noqa: E402

# ---- MiMo 配置（从 config/model_config.json 读取，避免硬编码 API Key 入库）----
def _load_mimo_config() -> tuple[str, str, str]:
    cfg_path = PROJECT_ROOT / "config" / "model_config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            base_url = cfg.get("base_url") or "https://api.xiaomimimo.com/v1"
            api_key = cfg.get("api_key") or ""
            model = cfg.get("model") or "mimo-v2.5-pro"
            if api_key:
                return base_url, api_key, model
        except Exception as e:  # noqa: BLE001
            logger.warning("读取 model_config.json 失败：%s", e)
    # 回退到环境变量
    import os
    return (
        os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1"),
        os.getenv("MIMO_API_KEY", ""),
        os.getenv("MIMO_MODEL", "mimo-v2.5-pro"),
    )


MIMO_BASE_URL, MIMO_API_KEY, MIMO_MODEL = _load_mimo_config()

PROMPT_TEMPLATE = """你是企业知识库评测集构建专家。下面是一份公司制度的正文（已截断）。请基于正文内容，生成 {n} 个「事实级」检索评测条目。

要求（务必遵守）：
1. 问题必须**具体、答案唯一、可直接从文档检索到**。好的示例：「试用期最长不得超过几个月？」「年假累计多少天可享受？」「报销单需要哪些附件？」
2. 禁止生成宽泛问题，例如「XX的主要内容是什么？」「公司对XX有什么规定？」这类一律不要。
3. 答案必须是正文中**可查证的原文事实**，用一句话概括（尽量 30 字以内），不要编造正文没有的数字。
4. 只输出一个 JSON 数组，不要输出任何解释、markdown 代码块或多余文字。数组元素格式：
   {{"question": "...", "gold_answer": "..."}}
5. JSON 的键名和字符串值必须使用英文双引号 "，严禁使用单引号 '。

制度标题：{title}

正文：
{content}

请直接输出 JSON 数组："""


def read_doc_text(path: Path, max_chars: int = 1500) -> str:
    """读取 docx 正文，拼接段落，截断到 max_chars。"""
    try:
        doc = DocxDocument(str(path))
    except Exception as e:  # noqa: BLE001
        logger.warning("解析失败 %s: %s", path.name, e)
        return ""
    paras = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    text = "\n".join(paras)
    return text[:max_chars]


def _parse_items(content: str) -> list[dict] | None:
    """多级解析 LLM 返回：标准 JSON → ast 字面量 → 正则逐对象提取。"""
    if not content:
        return None
    # 1. 标准 JSON 数组
    try:
        items = json.loads(content)
        if isinstance(items, list):
            return items
    except Exception:  # noqa: BLE001
        pass
    # 2. ast.literal_eval（兼容单引号）
    try:
        items = ast.literal_eval(content)
        if isinstance(items, list):
            return items
    except Exception:  # noqa: BLE001
        pass
    # 3. 正则提取每个 {"question":...} 对象，逐个解析，跳过坏的（容忍末尾截断）
    items = []
    for m in re.finditer(r'\{\s*"question"\s*:', content):
        # 从当前 { 开始，找到配对的 }（简化：取到下一个 }, 或字符串结尾）
        start = content.rfind("{", 0, m.start())
        seg = content[start:]
        # 尝试逐字符找配对右花括号（考虑字符串内花括号，用简单扫描）
        depth = 0
        in_str = False
        end = -1
        for i, ch in enumerate(seg):
            if ch == '"' and (i == 0 or seg[i - 1] != "\\"):
                in_str = not in_str
            elif not in_str:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
        if end > 0:
            obj_text = seg[:end]
            for parser in (json.loads, ast.literal_eval):
                try:
                    obj = parser(obj_text)
                    if isinstance(obj, dict) and obj.get("question"):
                        items.append(obj)
                        break
                except Exception:  # noqa: BLE001
                    continue
    return items or None


def call_mimo(prompt: str, retries: int = 4) -> list[dict] | None:
    """调用 MiMo 生成评测条目，返回解析后的列表；失败重试。

    用 requests（自动跟随 307/308 重定向），urllib 对 307 不跟随导致空响应体。
    """
    import requests  # noqa: PLC0415

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                MIMO_BASE_URL + "/chat/completions",
                json={
                    "model": MIMO_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 3000,
                    "temperature": 0.4,
                },
                headers={"Authorization": "Bearer " + MIMO_API_KEY},
                timeout=90,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            # 剥掉可能的 ```json ... ``` 包裹
            if content.startswith("```"):
                content = content.strip("`")
                if content.startswith("json"):
                    content = content[4:]
            items = _parse_items(content)
            if items:
                return items
            logger.warning("第 %d 次解析为空，重试", attempt)
        except Exception as e:  # noqa: BLE001
            logger.warning("第 %d 次调用失败: %s", attempt, str(e)[:120])
            time.sleep(2 * attempt)
    return None


def build_cases(per_doc: int, limit: int = 0) -> list[dict]:
    docs_dir = PROJECT_ROOT / "data" / "docs"
    files = sorted(docs_dir.glob("*.docx"))
    logger.info("发现 %d 份 docx 文档", len(files))
    if limit:
        files = files[:limit]

    cases: list[dict] = []
    ok, skip = 0, 0
    for idx, f in enumerate(files, 1):
        title = f.stem
        text = read_doc_text(f)
        if len(text) < 30:
            skip += 1
            continue
        prompt = PROMPT_TEMPLATE.format(n=per_doc, title=title, content=text)
        items = call_mimo(prompt)
        if not items:
            skip += 1
            logger.info("[%d/%d] %s → LLM 返回空，跳过", idx, len(files), f.name)
            continue
        for it in items:
            q = it.get("question", "").strip()
            a = it.get("gold_answer", "").strip()
            if q and a:
                cases.append({
                    "question": q,
                    "gold_answer": a,
                    "gold_docs": [f.name],
                    "topic": title,
                })
        ok += 1
        logger.info("[%d/%d] %s → %d 条", idx, len(files), f.name, len(items))
        time.sleep(0.3)  # 温和限速

    logger.info("完成：%d 份成功，%d 份跳过，共 %d 条", ok, skip, len(cases))
    return cases


def main():
    parser = argparse.ArgumentParser(description="LLM 辅助生成 RAG 检索评测集")
    parser.add_argument("--per-doc", type=int, default=3, help="每份文档生成的评测条数（默认 3）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 份（试跑用，默认 0=全量）")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "data" / "eval" / "policies_eval_llm.json"), help="输出路径")
    args = parser.parse_args()

    cases = build_cases(args.per_doc, args.limit)
    out = Path(args.out)
    out.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("已写入 %s（%d 条）", out, len(cases))


if __name__ == "__main__":
    main()
