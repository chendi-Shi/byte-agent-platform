"""Real embedding/RRF smoke on synthetic Markdown; no retrieval-quality claim.

Run after `ollama pull bge-m3` and `pip install -e .`:
    python examples/embedding_smoke.py --output runs/embedding-smoke
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from byte_agent.knowledge import Knowledge
from byte_agent.model import Ollama


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='bge-m3')
    parser.add_argument('--output', type=Path, default=Path('runs/embedding-smoke'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    provider = Ollama(args.model, timeout=300)
    metadata = provider.describe()
    knowledge = Knowledge(args.output / 'knowledge.sqlite')
    started = time.monotonic()
    try:
        count = knowledge.ingest(Path(__file__).resolve().parent / 'knowledge', provider.embed)
        query = 'search error rollback'
        vector = provider.embed([query])[0]
        lexical = knowledge.search(query, 3, service='search')
        hybrid = knowledge.search(query, 3, vector, service='search')
        if not vector or not count or not hybrid:
            raise ValueError('embedding smoke produced empty observations')
        report = {'completed': True, 'synthetic': True, 'at': datetime.now(timezone.utc).isoformat(),
                  'model_revision': metadata, 'chunks': count, 'dimensions': len(vector),
                  'query': query, 'service': 'search', 'lexical': lexical, 'hybrid': hybrid,
                  'wall_seconds': time.monotonic() - started,
                  'embedding_token_usage': None,
                  'scope': 'Actual embeddings and RRF execution only; no comparative quality or scale result. Chat generation options are not sent to the embedding endpoint.'}
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'completed': True, 'chunks': count, 'dimensions': len(vector), 'report': str(args.output / 'report.json')}))
    finally:
        knowledge.db.close()


if __name__ == '__main__':
    main()
