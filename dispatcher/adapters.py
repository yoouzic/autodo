"""Official DIDA CLI adapter. Tokens remain owned by the official CLI."""
import json
import os
import shutil
import subprocess
import threading
from contextlib import ExitStack
from pathlib import Path


def parse_json_output(text):
    text = text.strip().lstrip('\ufeff')
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def run_process(argv, *, cwd=None, input_text=None, timeout=60, log_prefix=None, env=None, line_filter=None):
    """No shell interpretation. Kill the process tree on Windows timeout."""
    with ExitStack() as stack:
        out_path = Path(str(log_prefix) + '.events.jsonl') if log_prefix else None
        err_path = Path(str(log_prefix) + '.stderr.log') if log_prefix else None
        out_file = stack.enter_context(out_path.open('w', encoding='utf-8')) if out_path else subprocess.PIPE
        err_file = stack.enter_context(err_path.open('w', encoding='utf-8')) if err_path else subprocess.PIPE
        if line_filter is not None and not log_prefix:
            raise ValueError('Streaming filters require file-backed logs')
        proc = subprocess.Popen([str(a) for a in argv], cwd=cwd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE if line_filter is not None else out_file, stderr=err_file, encoding='utf-8', errors='replace',
                                env=(dict(os.environ, **env) if env is not None else None),
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        errors=[]
        def consume():
            try:
                for line in proc.stdout:
                    filtered=line_filter(line)
                    if filtered is not None:
                        out_file.write(filtered);out_file.flush()
            except Exception as exc:
                errors.append(type(exc).__name__)
                if os.name=='nt':subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True)
                else:proc.kill()
        reader=None
        if line_filter is not None:
            reader=threading.Thread(target=consume,daemon=True);reader.start()
        try:
            if reader is None:
                stdout, stderr = proc.communicate(input_text, timeout=timeout)
            else:
                try:
                    if input_text is not None:proc.stdin.write(input_text);proc.stdin.flush()
                except BrokenPipeError:pass
                finally:proc.stdin.close()
                proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True)
            else:
                proc.kill()
            if reader is None:proc.communicate()
            else:proc.wait(timeout=10);reader.join(timeout=5)
            raise TimeoutError(f'Process exceeded {timeout}s: {Path(str(argv[0])).name}; logs: {log_prefix}')
        finally:
            if reader is not None:
                reader.join(timeout=5)
                if not reader.is_alive():proc.stdout.close()
        if reader is not None and (reader.is_alive() or errors):
            raise RuntimeError('CLI streaming log could not be captured completely')
    if out_path:
        stdout, stderr = out_path.read_text(encoding='utf-8'), err_path.read_text(encoding='utf-8')
    if proc.returncode:
        raise RuntimeError(f'{Path(str(argv[0])).name} exited {proc.returncode}: {(stderr or stdout)[-1600:]}')
    return stdout, stderr


def dida_command(root):
    entry = Path(root)/'.tools/node_modules/@suibiji/dida-cli/dist/index.js'
    if not entry.is_file():
        raise FileNotFoundError('Official DIDA CLI missing; run scripts/setup.ps1')
    node = shutil.which('node')
    if not node:
        raise FileNotFoundError('Node.js is required')
    return [node, str(entry)]


def codex_command():
    npm_entry = Path(os.environ.get('APPDATA', ''))/'npm/node_modules/@openai/codex/bin/codex.js'
    if npm_entry.is_file() and shutil.which('node'):
        return [shutil.which('node'), str(npm_entry)]
    exe = shutil.which('codex')
    if exe and Path(exe).suffix.lower() not in ('.cmd', '.bat', '.ps1'):
        return [exe]
    raise FileNotFoundError('Codex not found; configure codex_command as an argv array')


class Dida:
    def __init__(self, argv, *, command=None):
        self.argv = argv
        self.command = command or self._call

    def _call(self, args, *, timeout=90):
        if args[:2] == ['task', 'complete']:
            out, _ = run_process(self.argv + args, timeout=timeout)
            return {'ok': True, 'message': out.strip()}
        out, _ = run_process(self.argv + args + ['--json'], timeout=timeout)
        return parse_json_output(out)

    def fetch_tasks(self):
        projects = self.command(['project', 'list'])
        if not isinstance(projects, list):
            raise ValueError('project list did not return an array')
        tasks, warnings = {}, []
        for project in projects:
            if project.get('closed') or project.get('kind') == 'NOTE':
                continue
            project_id = project.get('id')
            if not project_id:
                raise ValueError('Project without id')
            data = self.command(['project', 'data', project_id])
            if not isinstance(data, dict) or not isinstance(data.get('tasks'), list):
                raise ValueError(f'Project {project_id} returned invalid tasks')
            for task in data['tasks']:
                task = dict(task, projectId=task.get('projectId') or project_id,
                            projectName=project.get('name', project_id))
                if task.get('status', 0) == 0 and task.get('id'):
                    tasks[(task['projectId'], task['id'])] = task
        try:
            extra = self.command(['task', 'filter', '--status', '0'])
            if not isinstance(extra, list):
                raise ValueError('task filter did not return an array')
            if len(extra) >= 200:
                warnings.append('Task filter returned its 200-item limit; global coverage may be incomplete')
            for task in extra:
                if task.get('status', 0) == 0 and task.get('id') and task.get('projectId'):
                    tasks.setdefault((task['projectId'], task['id']), task)
        except (RuntimeError, ValueError, TimeoutError) as exc:
            warnings.append('Global/inbox coverage unverified; task filter query failed')
        return list(tasks.values()), warnings

    def get(self, task):
        result = self.command(['task', 'get', task['projectId'], task['id']])
        if (not isinstance(result, dict) or result.get('id') != task['id'] or
                result.get('projectId') != task['projectId']):
            raise ValueError('Unexpected task get response')
        return result

    def probe(self, task, *, timeout=8):
        """Bounded read-only reconciliation; never infer completion from absence."""
        args = ['task', 'get', task['projectId'], task['id']]
        result = self._call(args, timeout=timeout) if self.command == self._call else self.command(args)
        if (not isinstance(result, dict) or result.get('id') != task['id'] or
                result.get('projectId') != task['projectId']):
            raise ValueError('Unexpected source identity during task reconciliation')
        return result

    def complete(self, task):
        return self.command(['task', 'complete', task['projectId'], task['id']])

    def comment(self, task, text):
        return self.command(['task', 'comment', 'add', task['projectId'], task['id'], '--title', text])

    def ensure_comment(self, task, marker, text):
        comments = self.command(['task', 'comment', 'list', task['projectId'], task['id']])
        if marker not in json.dumps(comments, ensure_ascii=False):
            self.comment(task, marker + '\n' + text)
