"""Isolated browser QA server. Never connects to DIDA or vendor CLIs."""
import argparse
import json
import tempfile
import time
from pathlib import Path

from dispatcher.control import Controller
from dispatcher.core import Store
from dispatcher.dashboard import start_dashboard
from dispatcher.service import Service
from dispatcher.workspace import WorkspaceManager
from dispatcher.execution_settings import resolve_execution_settings, load_zcode_catalog
from tests.test_control_api import ManagedRunner


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8877)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='dispatcher-ui-qa-') as directory:
        root = Path(directory)
        store = Store(root/'data/tasks.db')
        sources = {str(i): {'id': str(i), 'projectId': 'qa', 'title': title, 'status': 0,
                            'tags': [], 'priority': priority, 'content': '隔离验收任务；不会写入真实滴答账户。'}
                   for i, (title, priority) in enumerate([
                       ('QA · 拖动这张文档卡片分配智能体', 5),
                       ('QA · 查看交付并批准文档', 3),
                       ('QA · 查看交付并退回文档', 1),
                       ('QA · 归档历史工作区文档', 0)], 1)}

        class Source:
            def fetch_tasks(self): return list(sources.values()), []
            def get(self, task): return sources[task['id']].copy()
            def ensure_comment(self, *args): pass
            def complete(self, task): sources[task['id']]['status'] = 2

        class Runner(ManagedRunner):
            def prepare(self, row):
                path = super().prepare(row)
                (path/'routing.json').write_text(json.dumps({'executor': row.get('manual_executor') or 'codex'}))
                executor = row.get('manual_executor') or 'codex'
                settings = resolve_execution_settings(executor, service.config,
                    row.get('_agent_defaults', {}).get(executor, {}), row.get('execution_overrides', {}), row['difficulty'])
                row['_execution_settings'] = settings
                (path/'execution-settings.json').write_text(json.dumps(settings), encoding='utf-8')
                return path

        service = Service(root, {'require_human_review': True, 'model':'gpt-6.1-sol',
            '_zcode_model_catalog': load_zcode_catalog(Path(__file__).resolve().parents[1])}, store, Source(), Runner(root))
        service.sync()
        for key in ('2', '3', '4'):
            row = store.get('qa:'+key)
            store.assign(row['id'], 'codex', row['revision'], sources[key], service.config)
            service.execute(store.claim())
        row = store.get('qa:4')
        store.mark(row['id'], 'done')
        agents = [{'name': name, 'enabled': True, 'status': '隔离测试执行器'}
                  for name in ('codex', 'dsh', 'zcode', 'qoder', 'antigravity')]
        control = Controller(service, WorkspaceManager(root, store), agents)
        with start_dashboard(service.snapshot, args.port, control):
            print(f'QA fixture http://127.0.0.1:{args.port}', flush=True)
            while True:
                control.process_pending()
                service.process_writebacks()
                row = store.claim()
                if row:
                    service.execute(row)
                time.sleep(.2)


if __name__ == '__main__':
    main()
