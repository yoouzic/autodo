"""Host-owned immutable answer receipts outside model-writable workspaces."""
import hashlib
import json
import os
import re
from pathlib import Path
from .result_protocol import validate_answer


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def receipt_path(root, run_id):
    from .control_store import _reject_path_links
    if not isinstance(run_id, str) or not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise ValueError('Receipt requires a valid run identity')
    path = Path(root).resolve()/'data/result-receipts'/f'{run_id}.json'
    _reject_path_links(path)
    return path


def freeze_answer(root, row, result):
    delivery = validate_answer(result)
    if delivery['status'] != 'complete' or row.get('_delivery_policy', {}).get('mode') != 'answer':
        raise ValueError('Only host-selected complete answers can be frozen')
    receipt = {'version': 2, 'task_id': row['id'], 'run_id': row['current_run_id'],
               'fingerprint': row['fingerprint'], 'input_version': 1, 'expected_delivery': 'answer',
               'delivery': delivery, 'file_changes': result.get('file_changes', {'status': 'unverified'})}
    if row.get('input_context'):
        from .task_inputs import context_digest
        receipt.update(version=3, input_version=row['input_version'], parent_run_id=row['input_context']['parent_run_id'],
                       input_context_sha256=context_digest(row['input_context']),
                       source_writeback_allowed=row['input_context']['source_writeback_allowed'])
    path = receipt_path(root, row['current_run_id']); path.parent.mkdir(parents=True, exist_ok=True)
    data = encoded(receipt)
    # An exclusive, flushed write; readers receive the receipt only after close.
    # A crash leaves an unusable receipt, never an approved partial answer.
    with path.open('xb') as target:
        target.write(data); target.flush(); os.fsync(target.fileno())
    return receipt, hashlib.sha256(data).hexdigest()


def answer_digest(result):
    try:
        receipt = result['result_receipt']
        keys = {'version', 'task_id', 'run_id', 'fingerprint', 'input_version', 'expected_delivery', 'delivery', 'file_changes'}
        if receipt.get('version') == 3:
            keys |= {'parent_run_id', 'input_context_sha256', 'source_writeback_allowed'}
            if (type(receipt.get('source_writeback_allowed')) is not bool or
                    not re.fullmatch(r'[0-9a-f]{32}', str(receipt.get('parent_run_id'))) or
                    not re.fullmatch(r'[0-9a-f]{64}', str(receipt.get('input_context_sha256')))):
                return None
        if (set(receipt) != keys or type(receipt['version']) is not int or receipt['version'] not in (2, 3)
                or type(receipt['input_version']) is not int or receipt['input_version'] < 1
                or (receipt['version'] == 2 and receipt['input_version'] != 1)
                or (receipt['version'] == 3 and receipt['input_version'] < 2) or receipt['expected_delivery'] != 'answer'
                or not all(isinstance(receipt[k], str) and receipt[k] for k in ('task_id', 'run_id', 'fingerprint'))):
            return None
        delivery = validate_answer(result['execution'])
        if receipt['file_changes'] != result['execution'].get('file_changes', {'status': 'unverified'}):
            return None
        if (delivery['status'] != 'complete' or delivery != receipt['delivery'] or result.get('status') != 'complete'
                or result.get('artifacts') != [] or result.get('sha256') != {} or not result.get('checks')):
            return None
        digest = hashlib.sha256(encoded(receipt)).hexdigest()
        return digest if result.get('review_digest') == digest == result.get('verified_digest') else None
    except (KeyError, TypeError, ValueError, RecursionError):
        return None


def check_answer_receipt(root, row):
    result = row['result']; digest = answer_digest(result)
    if not digest: raise ValueError('Answer approval content changed or is incomplete')
    receipt = result['result_receipt']
    if (receipt['task_id'] != row['id'] or receipt['run_id'] != row['current_run_id'] or receipt['fingerprint'] != row['fingerprint']):
        raise ValueError('Answer receipt belongs to a different task, input or run')
    if receipt['input_version'] != row.get('input_version', 1):
        raise ValueError('Answer belongs to a different input version')
    if receipt['version'] == 3:
        from .task_inputs import context_digest
        context = row.get('input_context') or {}
        if (context_digest(context) != receipt['input_context_sha256'] or
                context.get('parent_run_id') != receipt['parent_run_id'] or
                context.get('source_writeback_allowed') != receipt['source_writeback_allowed']):
            raise ValueError('Confirmed input context changed since verification')
    path = receipt_path(root, row['current_run_id'])
    if path.stat().st_size > 1024*1024: raise ValueError('Answer receipt exceeds limit')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest or json.loads(data) != receipt:
        raise ValueError('Frozen answer receipt changed on disk')
    return True
