"""Stable, readable directory names for newly prepared agent workspaces."""
import hashlib
import re
import unicodedata
from pathlib import Path

from .routing import EXECUTORS


SUMMARY_LIMIT = 36
WINDOWS_DEVICE_NAMES = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} | {
    f'{prefix}{number}' for prefix in ('COM', 'LPT') for number in range(1, 10)
}


def task_summary(task):
    """Derive a bounded Windows-safe label without changing the source task."""
    text = str(task.get('title') or '').strip()
    if not text:
        content = unicodedata.normalize('NFKC', str(task.get('content') or '')).strip()
        text = re.split(r'[。！？.!?\r\n]', content, maxsplit=1)[0]
    text = unicodedata.normalize('NFKC', text)
    text = ''.join(' ' if char in '<>:"/\\|?*' or
                   unicodedata.category(char) in ('Cc', 'Cf', 'Cs') else char
                   for char in text)
    summary = re.sub(r'\s+', ' ', text).strip('. ')[:SUMMARY_LIMIT].rstrip('. ')
    if not summary:
        return '未命名任务'
    if summary.split('.', 1)[0].rstrip().upper() in WINDOWS_DEVICE_NAMES:
        summary = ('_' + summary)[:SUMMARY_LIMIT].rstrip('. ')
    return summary


def task_key_hash(task):
    """Keep task identity stable across payload edits and repeated runs."""
    if any(not isinstance(task.get(key), str) or not task[key].strip()
           for key in ('projectId', 'id')):
        raise ValueError('Workspace layout requires nonempty projectId and id strings')
    return hashlib.sha256(f"{task['projectId']}:{task['id']}".encode('utf-8')).hexdigest()[:10]


def task_directory(root, task, executor):
    """Return a task path only when all existing layout parents stay managed."""
    if executor not in EXECUTORS:
        raise ValueError(f'Unknown workspace executor: {executor}')
    managed = Path(root).resolve()/'workspaces'
    directory = managed/executor/f'{task_summary(task)}--{task_key_hash(task)}'
    # The root is anchored before resolving links; resolving it first could
    # silently turn an external junction target into the trusted boundary.
    for candidate in (managed, directory.parent, directory, directory/'runs'):
        if not candidate.resolve().is_relative_to(managed):
            raise ValueError('Workspace layout path is outside managed workspaces')
    return directory
