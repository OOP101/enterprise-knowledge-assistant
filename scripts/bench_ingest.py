"""入库流水线基准测试：实测单文档 加载/清洗/切片/入库 各阶段耗时。

用途：估算上千份文档的入库总时长与运行配置。
隔离性：写入独立的临时 collection / persist_dir，不影响线上向量库。

用法：
    CHROMA_COLLECTION=bench_tmp CHROMA_PERSIST_DIR=vector_db_bench \
        python scripts/bench_ingest.py [样本目录] [样本数量]
    # 结束后删除 vector_db_bench 目录即可
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLE_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "公司管理常用制度合集230份"
SAMPLE_N = int(sys.argv[2]) if len(sys.argv) > 2 else 6


def pick_samples() -> list[Path]:
    files = [p for p in SAMPLE_DIR.rglob("*") if p.suffix.lower() in (".docx", ".pdf", ".md", ".txt")]
    # 大小分层抽样：小/中/大文件都覆盖
    files.sort(key=lambda p: p.stat().st_size)
    if len(files) <= SAMPLE_N:
        return files
    step = len(files) // SAMPLE_N
    return [files[i * step] for i in range(SAMPLE_N)]


def main() -> None:
    from src.ingestion.cleaner import clean_documents
    from src.ingestion.embedder import ingest_documents
    from src.ingestion.loader import load_document
    from src.ingestion.splitter import split_documents

    samples = pick_samples()
    if not samples:
        print(f"样本目录无文档：{SAMPLE_DIR}")
        return

    print(f"样本 {len(samples)} 份（大小分层抽样），目标库：{__import__('os').environ.get('CHROMA_COLLECTION', '(未隔离!)')}")
    stats = {"load": 0.0, "clean": 0.0, "split": 0.0, "ingest": 0.0}
    total_chunks = 0
    total_chars = 0
    total_bytes = 0
    print(f"{'文件':<44}{'大小KB':>8}{'加载s':>8}{'清洗s':>8}{'切片s':>8}{'入库s':>8}{'块数':>6}")
    for p in samples:
        total_bytes += p.stat().st_size
        t0 = time.perf_counter()
        docs = load_document(p, use_ocr=False)  # OCR 单独评估，此处测常规解析
        t_load = time.perf_counter() - t0

        t0 = time.perf_counter()
        docs = clean_documents(docs)
        t_clean = time.perf_counter() - t0

        t0 = time.perf_counter()
        chunks = split_documents(docs)
        t_split = time.perf_counter() - t0

        t0 = time.perf_counter()
        ingest_documents(chunks, doc_id=f"bench_{p.stem[:20]}", kb_id="bench", filename=p.name)
        t_ingest = time.perf_counter() - t0

        stats["load"] += t_load
        stats["clean"] += t_clean
        stats["split"] += t_split
        stats["ingest"] += t_ingest
        total_chunks += len(chunks)
        total_chars += sum(len(c.page_content) for c in chunks)
        print(f"{p.name[:42]:<44}{p.stat().st_size // 1024:>8}{t_load:>8.2f}{t_clean:>8.2f}{t_split:>8.2f}{t_ingest:>8.2f}{len(chunks):>6}")

    n = len(samples)
    per_doc = sum(stats.values()) / n
    print("\n==== 单文档平均（秒） ====")
    for k, v in stats.items():
        print(f"  {k:<8}{v / n:>8.2f}")
    print(f"  合计    {per_doc:>8.2f}")
    print(f"\n平均块数/文档：{total_chunks / n:.0f}；平均正文chars/文档：{total_chars / n:.0f}")
    print(f"平均文件大小：{total_bytes / n / 1024:.0f} KB")

    print("\n==== 1000 份文档外推（串行） ====")
    print(f"  解析+清洗+切片+入库（demo embedding）：{per_doc * 1000 / 60:.0f} 分钟")
    emb_calls = total_chunks / n * 1000 / 10  # DashScope v3 单批 ≤10 条
    print(f"  若换 DashScope 真实 embedding：约 {emb_calls:.0f} 次批量调用")
    print(f"    串行(0.5s/次)≈{emb_calls * 0.5 / 60:.0f} 分钟；4 并发(受 QPS 限制)≈{emb_calls * 0.5 / 4 / 60:.0f} 分钟")
    print(f"  BM25 全量语料内存占用估算：{total_chars / n * 1000 * 2 / 1024 / 1024:.0f} MB（UTF-8 双倍系数）")


if __name__ == "__main__":
    main()
