import json
import sys
import urllib.request

BASE = 'http://localhost:8000'
CHUNKS = r'G:\实习\律所RAG\OHN-GraphRAG_chunks_test\chunks.jsonl'
GROUP_ID = 'ohn_legal_test'
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=1800) as resp:
        return json.loads(resp.read().decode('utf-8'))


records = []
with open(CHUNKS, encoding='utf-8') as f:
    for line in f:
        if line.strip():
            records.append(json.loads(line))
        if len(records) >= N:
            break

messages = []
for rec in records:
    meta = rec['metadata']
    messages.append(
        {
            'name': f"{meta.get('document_title', 'chunk')}#{meta.get('chunk_index', '')}",
            'content': rec['page_content'],
            'role_type': 'user',
            'role': meta.get('document_title', 'document'),
            'source_description': f"chunk_id={meta.get('chunk_id', '')}",
        }
    )

print(f'Clearing graph...')
print(post('/clear', {}))
print(f'Ingesting {len(messages)} chunks...')
result = post('/messages', {'group_id': GROUP_ID, 'messages': messages})
print(result)
