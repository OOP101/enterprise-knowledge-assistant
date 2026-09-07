"""一次性脚本：将 data/uploads 下现有文档按部门归类，迁移到对应部门知识库。

用法：.venv\\Scripts\\python.exe scripts/migrate_kb.py
说明：只做「复制式」迁移——把每个 docx 重新加载、切片并写入目标知识库的 collection。
     通用制度仍留在 default 库；已迁移的文档会在目标库新增副本（不删除 default 原件）。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# 允许直接运行脚本（脚本在 scripts/ 下，需要项目根在 sys.path）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from src.ingestion.embedder import ingest_documents  # noqa: E402
from src.ingestion.loader import load_document  # noqa: E402
from src.ingestion.splitter import split_documents  # noqa: E402

UPLOADS = PROJECT_ROOT / "data" / "uploads"

# 文件名前缀 -> 目标知识库
# 通用制度不在映射中（保留 default 库）
KB_MAP = {
    "admin_office": ["002", "003", "005", "006", "022", "025", "027", "030", "036", "037", "038", "045"],
    "finance": ["008", "009", "017", "019", "023", "024"],
    "purchase": ["010", "011", "013", "032"],
    "warehouse": ["014", "015", "016", "033", "034"],
    "hr": ["020"],
    "safety_env": ["001", "046"],
    "vehicle": ["018"],
    "legal": ["007", "029", "039", "040"],
}

# 反向：前缀 -> kb
_PREFIX_TO_KB: dict[str, str] = {}
for kb, prefixes in KB_MAP.items():
    for p in prefixes:
        _PREFIX_TO_KB[p] = kb


def migrate() -> None:
    total_files = 0
    total_chunks = 0
    for path in sorted(UPLOADS.glob("*.docx")):
        prefix = path.name.split("-")[0].strip()[:3]
        kb = _PREFIX_TO_KB.get(prefix)
        if kb is None:
            print(f"  [skip] {path.name} 保留 default")
            continue
        try:
            docs = load_document(path, use_ocr=True)
            if not docs:
                print(f"  [fail] {path.name} 加载为空")
                continue
            chunks = split_documents(docs)
            if not chunks:
                print(f"  [fail] {path.name} 切片为空")
                continue
            result = ingest_documents(chunks, kb_id=kb)
            n = result["ingested"]
            total_files += 1
            total_chunks += n
            print(f"  [ok] {path.name} -> [{kb}] 入库 {n} 片段")
        except Exception as e:  # noqa: BLE001
            print(f"  [fail] {path.name} 迁移失败: {e}")

    print(f"\n完成：迁移 {total_files} 份文件，共 {total_chunks} 片段。")


if __name__ == "__main__":
    migrate()
