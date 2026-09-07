from src.ingestion.embedder import remove_document, list_documents
from collections import defaultdict

docs = list_documents()
by_name = defaultdict(list)
for d in docs:
    name = d['source'].split('\\')[-1]
    by_name[name].append(d['doc_id'])
removed = 0
for name, grp in by_name.items():
    if len(grp) > 1:
        for did in grp[1:]:
            ok = remove_document(did)
            if ok:
                print('removed dup:', name, did)
                removed += 1
print('清理完成，删除', removed, '个重复文档')

# 验证
docs2 = list_documents()
print('剩余文档数:', len(docs2))