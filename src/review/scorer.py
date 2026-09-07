"""文档质量打分器（v3.0 入库审核流）。

纯规则实现，离线可跑，无需 LLM Key。四维评估：
1. 长度合理区：过短（信息量不足）/过长（疑似异常导出）降分
2. 文本密度：有效字符（中英文/数字）占比，拦截扫描件、乱码、纯符号文档
3. 结构完整度：标题/条目结构（第X章、第X条、一、/1. 等）与段落分布
4. 清洗损失率：清洗前后字符数比值，损失过大说明解析可能失败

返回 0-100 分 + 扣分原因列表，供审核队列展示与阈值判定。
"""
from __future__ import annotations

import re

# 有效字符（中文/英文/数字）
_VALID_CHAR = re.compile(r"[\u4e00-\u9fa5a-zA-Z0-9]")
# 常见中文公文/制度标题结构
_HEADING = re.compile(
    r"^\s*(第[一二三四五六七八九十百千\d]+[章节条款部分编]|[一二三四五六七八九十]+[、.]|\d+[、..]|[（(][一二三四五六七八九十\d]+[)）])"
)


def _structure_score(text: str) -> tuple[int, list[str]]:
    """结构完整度扣分：返回 (扣分, 原因)。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    deductions = 0
    reasons: list[str] = []
    if not any(_HEADING.match(ln) for ln in lines):
        deductions += 10
        reasons.append("缺少标题/条目结构（未识别到章节条款）")
    # 长文档却几乎没有换行 → 疑似解析失败拼成一段
    non_empty = len(lines)
    if len(text) > 2000 and non_empty < 5:
        deductions += 10
        reasons.append(f"全文仅 {non_empty} 个段落但篇幅过长，疑似解析失败")
    return deductions, reasons


def _density_score(text: str) -> tuple[int, list[str]]:
    """文本密度扣分：有效字符占比过低 → 疑似扫描件/乱码。"""
    if not text:
        return 100, ["文本为空"]
    valid = len(_VALID_CHAR.findall(text))
    ratio = valid / len(text)
    if ratio < 0.15:
        return 45, [f"有效文本占比过低（{ratio:.0%}），疑似扫描件或乱码"]
    if ratio < 0.30:
        return 30, [f"有效文本占比过低（{ratio:.0%}），疑似扫描件或乱码"]
    if ratio < 0.45:
        return 10, [f"有效文本占比偏低（{ratio:.0%}），建议人工确认"]
    return 0, []


def _length_score(text: str) -> tuple[int, list[str]]:
    """长度合理区扣分。"""
    n = len(text)
    if n < 200:
        return 40, [f"文本过短（{n} 字符），信息量不足"]
    if n < 500:
        return 10, [f"文本偏短（{n} 字符），建议人工确认"]
    if n > 500_000:
        return 10, [f"文本过长（{n / 10000:.0f} 万字符），疑似异常导出"]
    return 0, []


def _loss_score(args: tuple[int | None, int]) -> tuple[int, list[str]]:
    """清洗损失率扣分：清洗后/清洗前字符数比值。"""
    pre_clean_chars, post_clean_chars = args
    if not pre_clean_chars or pre_clean_chars <= 0:
        return 0, []
    loss = 1 - post_clean_chars / pre_clean_chars
    if loss < 0:
        return 0, []
    if loss > 0.5:
        return 30, [f"清洗损失率 {loss:.0%}（>50%），解析可能失败"]
    if loss > 0.3:
        return 15, [f"清洗损失率 {loss:.0%}（>30%），建议对比原文"]
    if loss > 0.15:
        return 5, [f"清洗损失率 {loss:.0%}"]
    return 0, []


def score_document(
    text: str,
    pre_clean_chars: int | None = None,
    source: str = "",
) -> dict:
    """对清洗后的文档全文打分。

    Args:
        text: 清洗后的文档全文。
        pre_clean_chars: 清洗前的字符总数（用于计算清洗损失率，缺省跳过该维度）。
        source: 文件名（仅用于展示）。

    Returns:
        {"score": 0-100, "reasons": [扣分原因...], "details": {各维度明细}, "source": str}
    """
    text = text or ""
    deductions = 0
    reasons: list[str] = []

    for fn, arg in (
        (_length_score, text),
        (_density_score, text),
        (_structure_score, text),
        (_loss_score, (pre_clean_chars, len(text))),
    ):
        d, rs = fn(arg)
        deductions += d
        reasons.extend(rs)

    score = max(0, min(100, 100 - deductions))
    return {
        "score": score,
        "reasons": reasons,
        "details": {
            "chars": len(text),
            "pre_clean_chars": pre_clean_chars,
            "lines": len([ln for ln in text.splitlines() if ln.strip()]),
        },
        "source": source,
    }
