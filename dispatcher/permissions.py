"""Host-owned, one-run tool policy. Never changes task-result review policy."""
import copy
import json
from pathlib import Path

LABELS={'default':'沿用安全默认','workspace':'工作区访问','auto':'自动审核工具操作','full':'完全访问'}
SUPPORTED={'codex','codebuddy'}


def capabilities(executor):
    return {'profiles':list(LABELS) if executor in SUPPORTED else ['default'],
            'labels':LABELS.copy(),
            'descriptions':{
                'default':'沿用已验证的工具与隔离设置，任务结果仍需你复审。',
                'workspace':'允许工作区文件读写；仅预批准工具执行，其他操作拒绝。任务结果仍需你复审。',
                'auto':'工具请求交给执行器原生自动审核，可能被拒绝；不是无条件放行。任务结果仍需你复审。',
                'full':'取消工作区文件隔离，可能修改其他目录或执行命令；不会获得管理员权限。'+('CodeBuddy 仍保留厂商高危操作限制。' if executor=='codebuddy' else '')},
            'note':('仅本次启动生效；结果仍需你复审。完全访问不等于管理员权限。'
                    + ('CodeBuddy 完全访问仍保留厂商高危操作限制。' if executor=='codebuddy' else '')
                    if executor in SUPPORTED else '此执行器暂沿用已验证的固定权限。')}


def validate(executor, profile='default', full_access_ack=False):
    if not isinstance(profile,str) or profile not in LABELS:
        raise ValueError('无效的会话权限选项。')
    if profile!='default' and executor not in SUPPORTED:
        raise ValueError('请明确选择支持会话权限的 Codex 或 CodeBuddy。')
    if type(full_access_ack) is not bool:
        raise ValueError('完整访问确认必须是明确的勾选。')
    if profile=='full' and full_access_ack is not True:
        raise ValueError('完全访问需要为本次运行明确确认。')
    return profile


def resolve(profile, executor, *, answer=False):
    # A stored choice has already passed the command/CAS/ack boundary.
    validate(executor, profile, profile=='full')
    if answer and profile in {'auto','full'}:
        raise ValueError('纯文本任务固定只读，请使用安全默认或工作区权限。')
    return {'version':1,'executor':executor,'profile':profile,'label':LABELS[profile],
            'filesystem':'read-only' if answer else 'full' if profile=='full' else 'workspace',
            'tool_approval':'automatic_review' if profile=='auto' else 'bypass_with_vendor_limits' if profile=='full' else 'preapproved_only',
            'result_review':'human_required'}


def freeze(row,run_dir,executor):
    policy=resolve(row.get('permission_profile','default'),executor,
                   answer=row.get('_delivery_policy',{}).get('mode')=='answer')
    row['_permissions']=copy.deepcopy(policy)
    (Path(run_dir)/'permissions.json').write_text(json.dumps(policy,ensure_ascii=False,indent=2),encoding='utf-8')
    return policy


def snapshot(row,run_dir,executor):
    path=Path(run_dir)/'permissions.json'
    if '_permissions' not in row:
        if path.exists(): raise ValueError('Cannot adopt unbound permission snapshot')
        return freeze(row,run_dir,executor)
    policy=row['_permissions']
    if not path.is_file() or json.loads(path.read_text(encoding='utf-8'))!=policy or policy.get('executor')!=executor:
        raise ValueError('本轮权限记录已变化，拒绝启动。')
    return copy.deepcopy(policy)
