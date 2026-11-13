"""Validated per-agent defaults and immutable per-invocation settings.

This module exposes model metadata only. Authentication remains vendor-owned.
Unknown model IDs may be selected, but their reasoning controls stay unavailable
until the installed client advertises them or a verified catalogue includes them.
"""
import copy
import json
import re
from functools import lru_cache
from pathlib import Path

from .routing import EXECUTORS


EFFORTS = {
    'codex': ['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
    'dsh': ['off', 'low', 'high', 'max'],
    'zcode': ['none', 'off', 'low', 'medium', 'high', 'xhigh', 'max'],
    'qoder': ['low', 'medium', 'high', 'xhigh', 'max'],
    'antigravity': ['low', 'medium', 'high', 'xhigh', 'max'],
    'workbuddy': ['minimal', 'low', 'medium', 'high', 'xhigh', 'max'],
    'codebuddy': ['minimal', 'low', 'medium', 'high', 'xhigh', 'max'],
    'doubao': ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'],
}
_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}')
_GEMINI = re.compile(r'^(gemini-3\.8-flash)(?:-(low|medium|high))?$')


def load_doubao_catalog(root):
    """Public model metadata only, recorded after querying the authenticated CLI."""
    path = Path(root)/'data/doubao-model-catalog.json'
    if not path.exists(): return []
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('Doubao model catalogue is too large')
    value = json.loads(path.read_text(encoding='utf-8'))
    rows = value.get('models') if isinstance(value, dict) else None
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError('Invalid Doubao model catalogue')
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict): raise ValueError('Invalid Doubao model metadata')
        name, label, levels = row.get('id'), row.get('label'), row.get('efforts')
        if (not isinstance(name, str) or not _MODEL.fullmatch(name) or name in seen
                or not isinstance(label, str) or not label or len(label) > 256
                or not isinstance(levels, list)
                or any(not isinstance(level, str) or level not in EFFORTS['doubao'] for level in levels)):
            raise ValueError('Invalid Doubao model metadata')
        seen.add(name)
        result.append({'id': name, 'label': label, 'efforts': list(dict.fromkeys(levels))})
    return result


def load_zcode_catalog(root):
    """Read a bounded, previously queried catalogue; never provider credentials."""
    path = Path(root)/'data/zcode-model-catalog.json'
    if not path.exists():
        return []
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('ZCode model catalogue is too large')
    records = json.loads(path.read_text(encoding='utf-8')).get('models')
    if not isinstance(records, list) or len(records) > 1000:
        raise ValueError('Invalid ZCode model catalogue')
    counts = {}
    for row in records:
        name = row.get('model') if isinstance(row, dict) else None
        if isinstance(name, str): counts[name] = counts.get(name, 0) + 1
    result = []
    for row in records:
        if not isinstance(row, dict) or row.get('available') is not True:
            continue
        name = row.get('model')
        key = name if counts.get(name) == 1 else row.get('id')
        levels = row.get('efforts')
        label = row.get('label', key)
        if (not isinstance(key, str) or not _MODEL.fullmatch(key) or not isinstance(levels, list)
                or any(level not in EFFORTS['zcode'] for level in levels)
                or not isinstance(label, str) or len(label) > 256):
            raise ValueError('Invalid ZCode model metadata')
        result.append({'id': key, 'label': label, 'efforts': list(dict.fromkeys(levels))})
    return result


@lru_cache(maxsize=1)
def _codex_models():
    baseline = [dict(id=name, label=name, efforts=['low','medium','high','xhigh','max','ultra'])
                for name in ('gpt-6.1-sol', 'gpt-6-astra', 'gpt-6.1-terra', 'gpt-6-sol')]
    path = Path.home()/'.codex/models_cache.json'
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            return baseline
        value = json.loads(path.read_text(encoding='utf-8'))
        models = []
        for model in value.get('models', []):
            name = model.get('slug')
            if not isinstance(name, str) or not _MODEL.fullmatch(name):
                continue
            levels = [level.get('effort') for level in model.get('supported_reasoning_levels', [])
                      if isinstance(level, dict) and level.get('effort') in EFFORTS['codex']]
            models.append({'id': name, 'label': name, 'efforts': list(dict.fromkeys(levels))})
        return models or baseline
    except (OSError, ValueError, TypeError, AttributeError):
        return baseline


def agent_capabilities(executor, config=None):
    if executor not in EXECUTORS:
        raise ValueError('Unsupported executor')
    if executor in ('workbuddy','doubao') and (config or {}).get(executor+'_transport') == 'disabled':
        return {'model': False, 'efforts': [], 'models': [], 'note': '已停用，不能派工或配置模型与思考强度。'}
    if executor in ('workbuddy','doubao') and (config or {}).get(executor+'_transport') == 'mcp':
        label='WorkBuddy' if executor=='workbuddy' else '豆包'
        waiting=(' 空闲时分段续等，默认最长15分钟；停止后需重新连接并启动会话。续等会增加工具调用轮次。'
                 if executor=='doubao' else '')
        return {'model': False, 'efforts': [], 'models': [],
                'note': '通过已登录的 '+label+' 专用会话领任务。模型与思考强度在该会话中选择；本接口不支持单次覆盖。当前支持默认或新建工作区。'+waiting}
    models = []
    notes = {
        'codex': '档位来自本机 Codex 模型目录；未列出的模型沿用该模型默认强度。',
        'dsh': '安装版目录支持 DeepSeek Flash / V4 Pro 与 off / low / high / max；留空沿用原 headless 配置。',
        'zcode': '会话设置由本机 ZCode 确认后执行，不改动原有账户配置。',
        'qoder': '仅已核实的模型提供强度选项；其他模型沿用厂商默认。模型访问权由本机账号决定。',
        'antigravity': 'Gemini 3.8 Flash 支持低、中、高；其他已列模型使用固定思考配置。',
        'workbuddy': '复用本机 WorkBuddy 内置 CLI；模型名来自安装版帮助，访问权取决于 CLI 登录账号。各模型思考档位尚未确认，暂沿用厂商默认。',
        'codebuddy': '独立 CodeBuddy CLI；支持智能体默认和单次任务覆盖。模型与思考档位来自最近一次 CLI 目录查询；目录是查询快照，实际访问权取决于 CLI 账号。',
        'doubao': '使用豆包独立 CLI 与原生授权模式；可指定模型 ID，思考档位仅在账户模型目录确认后开放。留空使用 CLI 默认（当前帮助标注 high）。',
    }
    if executor == 'codex':
        models = copy.deepcopy(_codex_models())
    elif executor == 'dsh':
        models = [{'id': name, 'label': label, 'efforts': EFFORTS['dsh'][:]}
                  for name,label in (('deepseek-flash','DeepSeek Flash'),('deepseek-v4-pro','DeepSeek V4 Pro'))]
    elif executor == 'zcode':
        models = copy.deepcopy((config or {}).get('_zcode_model_catalog', []))
    elif executor == 'qoder':
        known = {'Qwen3.8-Max': ['low','medium','xhigh'], 'GLM-5.3': ['low','high','max'],
                 'DeepSeek-V4-Pro': ['high','max']}
        names = ['Auto','Ultimate','Fast','Efficient','Sonus','Cantus','Qwen3.8-Max','Qwen3.8-Flash',
                 'Kimi-K3.1-Pro','Kimi-K2.8-Preview','GLM-5.3','GLM-5.3-Flash','DeepSeek-V4-Pro',
                 'DeepSeek-Flash','MiniMax-M3.1-Flash']
        models = [{'id': name, 'label': name, 'efforts': known.get(name, [])} for name in names]
    elif executor == 'antigravity':
        models = [{'id':'gemini-3.8-flash','label':'Gemini 3.8 Flash','efforts':['low','medium','high']},
                  *({'id':name,'label':name,'efforts':[]} for name in
                    ('claude-sonnet-4-6','claude-opus-4-6-thinking','gpt-oss-120b-medium'))]
    elif executor == 'workbuddy':
        models=[{'id':name,'label':name,'efforts':[]} for name in
                ('auto','glm-5v-turbo','glm-5.1','glm-5.0-turbo','glm-5.0','glm-4.7',
                 'kimi-k2.5','minimax-m2.7','deepseek-v3-2-volc')]
    elif executor == 'doubao':
        models = copy.deepcopy((config or {}).get('_doubao_model_catalog', []))
    elif executor == 'codebuddy':
        models = copy.deepcopy((config or {}).get('_codebuddy_model_catalog', []))
    return {'model': True, 'efforts': EFFORTS[executor][:], 'models': models, 'note': notes[executor]}


def validate_execution_settings(executor, settings):
    if executor not in EXECUTORS:
        raise ValueError('Unsupported executor')
    if not isinstance(settings, dict) or set(settings) - {'model', 'effort'}:
        raise ValueError('Only model and effort settings are allowed')
    for key, value in settings.items():
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f'{key} must be nonempty text without surrounding spaces')
        if key == 'model' and not _MODEL.fullmatch(value):
            raise ValueError('Invalid model identifier')
        if key == 'effort' and value not in EFFORTS[executor]:
            raise ValueError(f'Unsupported {executor} reasoning effort')
    return dict(settings)


def _command_option(command, names):
    value = None
    for i, argument in enumerate(command):
        if argument in names and i+1 < len(command):
            value = command[i+1]
        elif isinstance(argument, str):
            for name in names:
                if argument.startswith(name+'='):
                    value = argument[len(name)+1:]
    return value


def strip_model_options(command, executor):
    names = {'--model', '-m'}
    if executor in ('antigravity','workbuddy','codebuddy','doubao'): names.add('--effort')
    if executor == 'qoder': names.add('--reasoning-effort')
    result, skip = [], False
    for part in command:
        if skip:
            skip = False
        elif part in names:
            skip = True
        elif not any(part.startswith(name+'=') for name in names):
            result.append(part)
    return result


def resolve_execution_settings(executor, config, defaults, overrides, difficulty):
    if executor in ('workbuddy','doubao') and config.get(executor+'_transport') == 'disabled':
        label='WorkBuddy' if executor=='workbuddy' else '豆包'
        raise ValueError(label+' 已停用，不能派工或配置模型与思考强度。')
    baseline = validate_execution_settings(executor, config.get('agent_settings', {}).get(executor, {}))
    defaults = validate_execution_settings(executor, defaults or {})
    overrides = validate_execution_settings(executor, overrides or {})
    command = config.get('executor_commands', {}).get(executor, [])
    if executor in ('workbuddy','doubao') and config.get(executor+'_transport') == 'mcp':
        if baseline or defaults or overrides:
            label='WorkBuddy' if executor=='workbuddy' else '豆包'
            raise ValueError(label+' 桌面连接不支持模型覆盖，请在客户端会话中选择模型与思考强度。')
        return {'version':1, 'executor':executor, 'model':None, 'effort':None,
                'cli_model':None, 'provider':None, 'sources':{'model':'native_session','effort':'native_session'}}
    model = _command_option(command, {'--model','-m'})
    effort = _command_option(command, {'--effort'} if executor in ('antigravity','workbuddy','codebuddy','doubao') else {'--reasoning-effort'})
    sources = {'model': 'command' if model else 'vendor', 'effort': 'command' if effort else 'vendor'}
    if executor == 'codex':
        effort = config.get('efforts', {}).get(difficulty, {'easy':'low','medium':'medium','hard':'high'}.get(difficulty,'medium'))
        model = config.get('models', {}).get(effort) or config.get('model')
        sources = {'model':'project' if model else 'vendor', 'effort':'difficulty'}
    if executor == 'antigravity' and model and (match := _GEMINI.fullmatch(model)):
        model = match[1]
        if not effort and match[2]:
            effort, sources['effort'] = match[2], 'model_variant'
    for source, layer in (('project', baseline), ('agent', defaults), ('session', overrides)):
        if 'model' in layer:
            model, sources['model'] = layer['model'], source
            # Inherit the field only when the newly selected model advertises it.
            if 'effort' not in layer:
                normalized_model = model
                if executor == 'antigravity' and (variant := _GEMINI.fullmatch(model)):
                    normalized_model = variant[1]
                    if variant[2]:
                        effort, sources['effort'] = variant[2], 'model_variant'
                new_entry = next((item for item in agent_capabilities(executor, config)['models']
                                  if item['id'] == normalized_model), None)
                if new_entry is None or effort not in new_entry['efforts']:
                    effort, sources['effort'] = None, 'vendor'
        if 'effort' in layer:
            effort, sources['effort'] = layer['effort'], source
        if executor == 'antigravity' and model and (match := _GEMINI.fullmatch(model)):
            if match[2] and 'effort' in layer and match[2] != layer['effort']:
                raise ValueError('Model variant conflicts with selected reasoning effort; choose the model family')
            model = match[1]
            if match[2] and not effort:
                effort, sources['effort'] = match[2], 'model_variant'
    # DSH's installed headless profile currently selects this model; retain
    # provider-owned selection when no model is explicitly requested.
    capability_model = model or ('deepseek-flash' if executor == 'dsh' else None)
    entry = next((item for item in agent_capabilities(executor, config)['models'] if item['id'] == capability_model), None)
    explicit_effort = sources['effort'] in ('project', 'agent', 'session')
    if effort and explicit_effort and (entry is None or effort not in entry['efforts']):
        raise ValueError('This model does not advertise the selected reasoning effort; use its default')
    cli_model = model
    provider = None
    if executor == 'dsh' and (model or effort):
        if model not in ('deepseek-flash','deepseek-v4-pro'):
            raise ValueError('DSH custom settings require a model from the verified DeepSeek provider catalogue')
        provider = 'deepseek-official'
    if executor == 'antigravity' and model == 'gemini-3.8-flash' and effort:
        cli_model = model+'-'+effort
    return {'version':1, 'executor':executor, 'model':model, 'effort':effort,
            'cli_model':cli_model, 'provider':provider, 'sources':sources}
