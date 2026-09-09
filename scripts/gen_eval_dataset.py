"""评测集扩充脚本：从 data/docs 的制度文档批量生成检索评测条目。

思路：
- 每份文档的文件名即主题（如 007保密协议.docx）；
- 解析 docx 抽取含制度关键词的代表性句子作为 gold_answer；
- question 由模板 + 文档主题生成，覆盖 检索/流程/责任 三类问法；
- 输出与 data/eval/sample.json 同构的 JSON，供 Eval 体系直接消费。

用法：
    python scripts/gen_eval_dataset.py                 # 生成全量
    python scripts/gen_eval_dataset.py --per-doc 1     # 每文档 1 条
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from docx import Document as DocxDocument  # noqa: E402

QUESTION_TEMPLATES = [
    "{topic}的主要内容是什么？",
    "公司关于{topic}有哪些规定？",
    "{topic}的流程是怎么走的？",
    "办理{topic}需要注意什么？",
    "公司对{topic}有什么要求和标准？",
]

STOP_TOPICS = {"通知书", "协议书模板", "申请表", "登记表", "流程图"}
STOP_CHARS = re.compile(r"[\s（）()【】\[\]、，。；：\-—0-9]+")


def extract_topic(stem: str) -> str:
    """从文件名提取主题：去掉序号和通用后缀词。"""
    t = re.sub(r"^\d+", "", stem)
    t = STOP_CHARS.sub("", t)
    for stop in ("管理制度", "管理规定", "规章制度", "管理办法", "管理规范", "工作流程", "管理制度", "细则", "制度", "规定", "流程"):
        if t.endswith(stop) and len(t) > len(stop) + 1:
            t = t[: -len(stop)]
    return t


def pick_gold_sentences(path: Path, topic: str, max_sentences: int = 3) -> list[str]:
    """抽取文档中与主题最相关的代表性句子。"""
    try:
        doc = DocxDocument(str(path))
    except Exception as e:  # noqa: BLE001
        logger.warning("解析失败 %s: %s", path.name, e)
        return []
    paras = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    if not paras:
        return []
    # 优先取标题（前几段）与含主题词/关键词的句子
    keywords = ("应当", "必须", "负责", "审批", "流程", "不得", "标准", "要求", "流程", "执行")
    scored = []
    for p in paras:
        if len(p) < 8 or len(p) > 120:
            continue
        score = 0
        if topic and topic[:4] in p:
            score += 2
        score += sum(1 for k in keywords if k in p)
        if score > 0:
            scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    seen, picked = set(), []
    for _, p in scored:
        key = p[:20]
        if key in seen:
            continue
        seen.add(key)
        picked.append(p)
        if len(picked) >= max_sentences:
            break
    return picked


def build_cases(per_doc: int) -> list[dict]:
    docs_dir = PROJECT_ROOT / "data" / "docs"
    files = sorted(docs_dir.glob("*.docx"))
    logger.info("发现 %d 份 docx 文档", len(files))
    cases: list[dict] = []
    for idx, f in enumerate(files, 1):
        topic = extract_topic(f.stem)
        if not topic or any(s in topic for s in STOP_TOPICS):
            continue
        sentences = pick_gold_sentences(f, topic)
        if not sentences:
            logger.info("[%d] %s → 未抽到有效句子，跳过", idx, f.name)
            continue
        n_q = min(per_doc, len(QUESTION_TEMPLATES))
        for qi in range(n_q):
            q = QUESTION_TEMPLATES[qi].format(topic=topic)
            cases.append({
                "question": q,
                "gold_answer": "".join(sentences[:2]),
                "gold_docs": [f.name],
                "topic": topic,
            })
    return cases


def main():
    parser = argparse.ArgumentParser(description="批量生成 RAG 检索评测集")
    parser.add_argument("--per-doc", type=int, default=1, help="每份文档生成的评测条数（默认 1）")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "data" / "eval" / "policies_eval.json"), help="输出路径")
    args = parser.parse_args()

    cases = build_cases(args.per_doc)
    out = Path(args.out)
    out.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("生成 %d 条评测 → %s", len(cases), out)


if __name__ == "__main__":
    main()
