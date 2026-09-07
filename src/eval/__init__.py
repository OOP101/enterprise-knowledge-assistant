"""Eval 评估体系（2.0）。

- 评估集：data/eval/*.json（question / gold_answer / gold_docs）
- 指标：检索命中率、精确率、LLM-as-judge 打分（正确性 / 引用）
- CLI：python -m src.eval.run
"""
