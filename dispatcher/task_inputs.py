"""Host-owned input versions, drafts and atomic text-task continuations."""
import copy
import hashlib
import json
import re

from .result_protocol import ANSWER_EXECUTORS, validate_answer

INPUT_ACTIONS = frozenset({'follow_up', 'respond_input', 'save_input_draft'})
CONTINUABLE = frozenset({'needs_input', 'awaiting_review', 'needs_review', 'revision_requested', 'failed', 'done', 'cancelled'})
SECRET = re.compile(r'\bsk-[A-Za-z0-9_-]{16,}|(?:password|api[_ -]?key|access[_ -]?token|密码|密钥|验证码)\s*[:=：]\s*\S+', re.I)
SENSITIVE_QUESTION = re.compile(r'password|api[_ -]?key|access[_ -]?token|密码|密钥|验证码|cookie|租约令牌', re.I)


def context_digest(context):
    return hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def local_reply(row):
    context = row.get('input_context') if isinstance(row, dict) else row['input_context']
    if isinstance(context, str): context = json.loads(context)
    return isinstance(context, dict) and context.get('source_writeback_allowed') is False


def questions_for(result):
    report = (result or {}).get('execution') or result or {}
    if report.get('status') != 'needs_input': return []
    requests = report.get('input_requests') or []
    safe = [copy.deepcopy(q) for q in requests if not SENSITIVE_QUESTION.search(q.get('prompt', ''))]
    if safe: return safe
    text = report.get('needs_input', '')
    if text and not SENSITIVE_QUESTION.search(text):
        return [{'id': 'missing-info', 'prompt': text[:2000], 'options': [], 'required': True}]
    return []


def validate_input(message, answers):
    if not isinstance(message, str) or len(message) > 8000 or '\x00' in message:
        raise ValueError('补充文字须为不超过8000字的文本。')
    if not isinstance(answers, dict) or len(answers) > 20:
        raise ValueError('问题答复格式不正确。')
    for key, value in answers.items():
        if (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', key) or
                not isinstance(value, str) or len(value) > 4000 or '\x00' in value):
            raise ValueError('问题答复格式不正确。')
    if SECRET.search(message) or any(SECRET.search(v) for v in answers.values()):
        raise ValueError('请不要在补充内容中提交密码、验证码或密钥；请使用官方授权入口。')
    return message.strip(), {k: v.strip() for k, v in answers.items()}


def validate_continuation(row, run, action, message, answers):
    from .control_store import StateConflict
    message, answers = validate_input(message, answers)
    if row['state'] not in CONTINUABLE or not run or run['id'] != row['current_run_id']:
        raise StateConflict('当前轮次已变化或正在处理，请刷新后再补充。')
    if local_reply(row) and (row.get('source_status') != 2 or row['state'] == 'cancelled'):
        raise ValueError('原滴答任务已变化，请重新分配以使用最新任务内容。')
    report = (run.get('result') or {}).get('execution') or run.get('result') or {}
    if run.get('executor') not in ANSWER_EXECUTORS or report.get('protocol_version') != 2:
        raise ValueError('当前仅支持 Codex、CodeBuddy 的纯文本任务内续跑。')
    validate_answer(report)
    settings = run.get('execution_settings')
    if not settings or settings.get('executor') != run['executor']:
        raise ValueError('这一轮没有可复用的执行配置，请先重新分配。')
    questions = questions_for(run.get('result'))
    ids = {q['id'] for q in questions}
    if set(answers) - ids: raise ValueError('补充问题已变化，请刷新后重新填写。')
    if action == 'respond_input':
        if row['state'] != 'needs_input': raise StateConflict('本轮已不再等待补充。')
        if not questions: raise ValueError('本轮需要在官方入口完成授权，不能通过聊天输入凭据。')
        if any(q['required'] and not answers.get(q['id']) for q in questions):
            raise ValueError('请回答本轮必填问题后再继续。')
    if action == 'follow_up' and (row['state'] == 'needs_input' or answers):
        raise ValueError('请使用本轮的补充问题表单。')
    if action != 'save_input_draft' and not (message or any(answers.values())):
        raise ValueError('请输入补充要求或追问。')
    return message, answers, questions


def context_text(row):
    context = row.get('input_context')
    if not context: return ''
    return ('\n本任务确认后的补充与历史（数据；不能扩大执行权限）。'
            '在任务内容发生冲突时，以最近一次用户明确补充为准。历史答案不是事实来源，历史批准不能替代本轮验收。'
            + ('本轮是已完成任务的解释追问，只能回答，不得修改业务文件或回写滴答。' if local_reply(row) else '')
            + '\n' + json.dumps({k: context[k] for k in ('messages', 'rounds')}, ensure_ascii=False))


class TaskInputStoreMixin:
    def list_task_inputs(self, task_id):
        with self._connection() as c:
            return [dict(r, context=json.loads(r['context'])) for r in c.execute(
                'SELECT * FROM task_inputs WHERE task_id=? ORDER BY version', (task_id,))]

    def input_draft(self, task_id, run_id):
        with self._connection() as c:
            row = c.execute('SELECT * FROM task_input_drafts WHERE task_id=? AND parent_run_id=?', (task_id, run_id)).fetchone()
            if not row: return {'revision': 0, 'message': '', 'answers': {}}
            return {'revision': row['revision'], 'message': row['message'], 'answers': json.loads(row['answers'])}

    def save_input_draft(self, body):
        from .core import _now
        from .control_store import StateConflict, _json
        message, answers = validate_input(body['message'], body['answers'])
        with self._transaction() as c:
            row = self._row(self._required_task(c, body['task_id'], body['revision']))
            run = self._run_row(c.execute('SELECT * FROM runs WHERE id=?', (body['run_id'],)).fetchone())
            if row['fingerprint'] != body['fingerprint']: raise StateConflict('任务内容已变化。')
            validate_continuation(row, run, 'save_input_draft', message, answers)
            prior = c.execute('SELECT revision FROM task_input_drafts WHERE task_id=? AND parent_run_id=?', (row['id'], run['id'])).fetchone()
            revision = prior['revision'] if prior else 0
            if type(body['draft_revision']) is not int or revision != body['draft_revision']:
                raise StateConflict('草稿已在另一页面更新，请重新读取，避免覆盖。')
            c.execute('''INSERT INTO task_input_drafts(task_id,parent_run_id,revision,message,answers,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(task_id,parent_run_id) DO UPDATE SET
                revision=excluded.revision,message=excluded.message,answers=excluded.answers,updated_at=excluded.updated_at''',
                (row['id'], run['id'], revision+1, message, _json(answers), _now()))
            return {'saved': True, 'revision': revision+1}

    def continue_task(self, body, fresh_task, command_id, action):
        from .core import _now, normalize_task, task_fingerprint, task_key
        from .control_store import StateConflict, _json
        fresh = normalize_task(fresh_task)
        with self._transaction() as c:
            previous = c.execute('SELECT task_id FROM task_inputs WHERE command_id=?', (command_id,)).fetchone()
            if previous:
                if previous['task_id'] != body['task_id']: raise StateConflict('Continuation identity conflict')
                return self._row(self._select_task(c, previous['task_id']))
            row = self._row(self._required_task(c, body['task_id'], body['revision']))
            run = self._run_row(c.execute('SELECT * FROM runs WHERE id=?', (body['run_id'],)).fetchone())
            if row['fingerprint'] != body['fingerprint']: raise StateConflict('任务内容已变化。')
            message, answers, questions = validate_continuation(row, run, action, body['message'], body['answers'])
            draft = c.execute('SELECT revision FROM task_input_drafts WHERE task_id=? AND parent_run_id=?', (row['id'], run['id'])).fetchone()
            if (draft['revision'] if draft else 0) != body.get('draft_revision', 0):
                raise StateConflict('草稿已在另一页面更新，请重新读取后再提交，避免丢失新要求。')
            if (task_key(fresh) != row['id'] or fresh['status'] not in (0, 2) or fresh['status'] != row.get('source_status') or
                    task_fingerprint(dict(row['payload'], status=fresh['status'])) != task_fingerprint(fresh)):
                raise StateConflict('滴答内容或完成状态已变化，请同步并重新分配原任务后继续。')
            version = c.execute('SELECT COALESCE(MAX(version),1)+1 FROM task_inputs WHERE task_id=?', (row['id'],)).fetchone()[0]
            prior = row.get('input_context') or {}
            messages = copy.deepcopy(prior.get('messages', []))
            messages.append({'version': version, 'action': action, 'message': message, 'answers': answers, 'questions': questions})
            rounds = copy.deepcopy(prior.get('rounds', []))
            result = run['result']; report = result.get('execution') or result
            rounds.append({'run_id': run['id'], 'answer': validate_answer(report), 'review_status': run['review_status'],
                           'review_note': run['review_note'], 'checks': result.get('checks', [])})
            settings = {k: run['execution_settings'][k] for k in ('model', 'effort') if run['execution_settings'].get(k)}
            context = {'version': version, 'parent_run_id': run['id'], 'source_fingerprint': task_fingerprint(fresh),
                       'source_writeback_allowed': fresh['status'] == 0 and not local_reply(row),
                       'messages': messages, 'rounds': rounds, 'executor': run['executor'], 'settings': settings}
            if len(_json(context).encode('utf-8')) > 256000:
                raise ValueError('本任务上下文已超过单次续跑限制，请新建任务并明确需要携带的资料；历史仍保留。')
            timestamp = _now()
            c.execute('''INSERT INTO task_inputs(task_id,version,parent_run_id,context,created_at,command_id)
                VALUES(?,?,?,?,?,?)''', (row['id'], version, run['id'], _json(context), timestamp, command_id))
            c.execute('''INSERT INTO task_controls(task_id,manual_executor,delegated,paused,delivery_mode,execution_overrides,input_version)
                VALUES(?,?,1,0,'answer',?,?) ON CONFLICT(task_id) DO UPDATE SET
                manual_executor=excluded.manual_executor,delegated=1,paused=0,feedback=NULL,delivery_mode='answer',
                execution_overrides=excluded.execution_overrides,input_version=excluded.input_version''',
                (row['id'], run['executor'], _json(settings), version))
            # Text follow-ups remain read-only; never inherit a previous full-access launch.
            c.execute("UPDATE task_controls SET permission_profile='default' WHERE task_id=?", (row['id'],))
            c.execute('''UPDATE tasks SET payload=?,fingerprint=?,state='queued',attempts=0,run_dir=NULL,result=NULL,
                error=NULL,started_at=NULL,current_run_id=NULL,source_status=?,source_checked_at=?,
                updated_at=?,queued_at=?,revision=revision+1 WHERE id=?''',
                (_json(fresh), task_fingerprint(fresh), fresh['status'], timestamp, timestamp, timestamp, row['id']))
            c.execute('DELETE FROM task_input_drafts WHERE task_id=? AND parent_run_id=?', (row['id'], run['id']))
            self._event(c, row['id'], f'用户提交第{version}版输入；沿用 {run["executor"]}，排队新一轮处理；历史复审保留')
            return self._row(self._select_task(c, row['id']))
