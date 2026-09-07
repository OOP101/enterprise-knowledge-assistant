"""Eval CLI 入口。

用法：
    python -m src.eval.run                # 仅检索指标
    python -m src.eval.run --judge        # 加 LLM-as-judge 打分
    python -m src.eval.run --kb mykb      # 指定知识库
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, "")


def main() -> None:
    parser = argparse.ArgumentParser(description="企业知识助手 Eval 评估")
    parser.add_argument("--judge", action="store_true", help="启用 LLM-as-judge 打分")
    parser.add_argument("--kb", default="default", help="评估的知识库 ID")
    parser.add_argument("--json", dest="as_json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    from src.core import get_container
    from src.eval.runner import run_eval

    container = get_container()
    retriever = container.get_retriever(kb_id=args.kb, top_k=container.settings.eval_top_k)
    llm = container.get_llm() if args.judge else None

    result = run_eval(retriever, llm=llm, use_judge=args.judge, kb_id=args.kb)

    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print("=" * 50)
    print(f"评估样本：{result['total']}")
    print(f"命中率 hit_rate ：{result['hit_rate']}")
    print(f"精确率 precision：{result['precision']}")
    print(f"F1             ：{result['f1']}")
    print("=" * 50)
    for i, case in enumerate(result["cases"], 1):
        print(f"\n[{i}] {case['question']}")
        print(f"    hit={case['hit_rate']} precision={case['precision']} f1={case['f1']}")
        if "judge" in case:
            j = case["judge"]
            print(f"    judge 正确性={j['correctness']} 忠实度={j['faithfulness']} 评语={j['comment']}")


if __name__ == "__main__":
    main()
