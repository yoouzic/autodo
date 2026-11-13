"""Versioned text delivery, independent of task difficulty and file delivery."""
import copy
import re

ANSWER_EXECUTORS = frozenset({'codex', 'codebuddy'})
ANSWER_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'protocol_version': {'type': 'integer', 'enum': [2]},
        'delivery_type': {'type': 'string', 'enum': ['answer']},
        'status': {'type': 'string', 'enum': ['complete', 'needs_input', 'failed']},
        'summary': {'type': 'string'}, 'answer_markdown': {'type': 'string'},
        'artifacts': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 0},
        'checks': {'type': 'array', 'items': {'type': 'string'}},
        'needs_input': {'type': 'string'},
        'evidence_refs': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'properties': {'kind': {'type': 'string', 'enum': ['task_input', 'agent_report']},
                           'label': {'type': 'string'}, 'detail': {'type': 'string'}},
            'required': ['kind', 'label', 'detail']}},
        'limitations': {'type': 'array', 'items': {'type': 'string'}},
        'input_requests': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'properties': {'id': {'type': 'string'}, 'prompt': {'type': 'string'},
                           'options': {'type': 'array', 'items': {'type': 'string'}}, 'required': {'type': 'boolean'}},
            'required': ['id', 'prompt', 'options', 'required']}},
    },
}
ANSWER_SCHEMA['required'] = list(ANSWER_SCHEMA['properties'])


def validate_mode(mode):
    if not isinstance(mode, str) or mode not in {'auto', 'answer', 'files'}:
        raise ValueError('Delivery mode must be auto, answer or files')
    return mode


def resolve_delivery(row, executor):
    mode = validate_mode(row.get('delivery_mode') or 'auto')
    if mode == 'answer' and executor not in ANSWER_EXECUTORS:
        raise ValueError('纯文本交付目前只支持 Codex 和 CodeBuddy，请选择支持的执行器。')
    if mode != 'auto':
        return {'mode': mode, 'reason': '用户指定交付形式'}
    task = row['payload']; text = str(task.get('title') or '')+'\n'+str(task.get('content') or task.get('desc') or '')
    # File requests win over conversational wording. Ambiguous tasks retain
    # the established file workflow; users can explicitly select answer.
    file_request = re.search(r'(生成|创建|修改|保存|写入|导出|交付).{0,24}(文件|代码|文档|\.\w+|PDF|Word|Excel)|\b(create|write|edit|save|export)\b.{0,40}\b(file|code|report|\.\w+)\b', text, re.I)
    answer_request = re.search(r'(无需|不用|不需要|不要).{0,8}(改|写|生成|创建)?文件|概括|解释|回答|多少额度|总结.{0,8}(点|规则)|\b(answer|explain|summarize)\b', text, re.I)
    answer = executor in ANSWER_EXECUTORS and bool(answer_request) and not file_request
    return {'mode': 'answer' if answer else 'files',
            'reason': '识别为直接回答；不修改业务文件' if answer else '保留文件交付；可手动选择纯文本'}


def validate_answer(value, *, strict=False):
    legacy_keys = set(ANSWER_SCHEMA['required']) - {'input_requests'}
    if not isinstance(value, dict) or not legacy_keys.issubset(value):
        raise ValueError('Incomplete answer result')
    if strict and set(value) - set(ANSWER_SCHEMA['required']):
        raise ValueError('Unexpected answer result fields')
    if type(value['protocol_version']) is not int or value['protocol_version'] != 2 or value['delivery_type'] != 'answer':
        raise ValueError('Invalid answer version or type')
    if value['status'] not in {'complete', 'needs_input', 'failed'} or value['artifacts'] != []:
        raise ValueError('Text answers cannot declare business files')
    for key, limit in (('summary', 4000), ('answer_markdown', 64000), ('needs_input', 8000)):
        if not isinstance(value[key], str) or len(value[key]) > limit or '\x00' in value[key]:
            raise ValueError('Invalid or oversized answer text')
    if value['status'] == 'complete' and (not value['answer_markdown'].strip() or not value['summary'].strip() or value['needs_input'].strip()):
        raise ValueError('Complete answer requires full content and no unresolved input')
    for key in ('checks', 'limitations'):
        if not isinstance(value[key], list) or len(value[key]) > 100 or any(not isinstance(s, str) or len(s) > 4000 for s in value[key]):
            raise ValueError('Invalid answer notes')
    refs = value['evidence_refs']
    if not isinstance(refs, list) or len(refs) > 50:
        raise ValueError('Invalid answer evidence')
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) != {'kind', 'label', 'detail'} or
                ref['kind'] not in {'task_input', 'agent_report'} or
                any(not isinstance(ref[k], str) or not ref[k].strip() or len(ref[k]) > 8000 for k in ('label', 'detail'))):
            raise ValueError('Untrusted or invalid evidence reference')
    questions = value.get('input_requests', [])
    if not isinstance(questions, list) or len(questions) > 20:
        raise ValueError('Invalid input questions')
    seen = set()
    for q in questions:
        if (not isinstance(q, dict) or set(q) != {'id', 'prompt', 'options', 'required'} or
                not isinstance(q['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', q['id']) or q['id'] in seen or
                not isinstance(q['prompt'], str) or not q['prompt'].strip() or len(q['prompt']) > 2000 or
                type(q['required']) is not bool or not isinstance(q['options'], list) or len(q['options']) > 8 or
                any(not isinstance(v, str) or len(v) > 200 for v in q['options'])):
            raise ValueError('Invalid input question fields')
        seen.add(q['id'])
    if value['status'] == 'complete' and questions:
        raise ValueError('Complete answer cannot have unresolved input questions')
    # Preserve old V2 receipts byte-for-byte; do not synthesize new keys.
    return copy.deepcopy({key: value[key] for key in ANSWER_SCHEMA['required'] if key in value})


def answer_projection(result, *, full=False):
    """Allowlisted delivery only; never expose host receipt or vendor logs."""
    if not isinstance(result, dict): return None
    execution = result.get('execution') or result
    if not isinstance(execution, dict) or execution.get('protocol_version') != 2: return None
    try: answer = validate_answer(execution)
    except ValueError: return None
    keys = ('delivery_type', 'status', 'summary', 'needs_input')
    projected = {k: answer[k] for k in keys}
    if full:
        projected.update({k: answer[k] for k in ('answer_markdown', 'evidence_refs', 'limitations')})
    projected['file_changes'] = result.get('file_changes') or execution.get('file_changes') or {'status': 'unverified'}
    return projected
