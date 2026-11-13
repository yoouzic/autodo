"""Revalidate discovered folders before allowing an explicit in-place selection."""
import copy
import os
import time
from pathlib import Path

from .control_store import validate_workspace_choice
from .workspace_catalog import WorkspaceCatalog, apply_workspace_guards
from .workspace_lock import WorkspaceBusy, busy_paths


def _key(path):
    return os.path.normpath(str(path)).replace('\\','/').casefold().rstrip('/')


def _inside(path, parent):
    path,parent=_key(path),_key(parent)
    return path==parent or path.startswith(parent+'/')


def _overlaps(first, second):
    return _inside(first,second) or _inside(second,first)


class WorkspacePolicy:
    def __init__(self, root, store, config):
        self.root,self.store,self.config=Path(root).resolve(),store,config
        self.catalog=WorkspaceCatalog(self.root,store,config)
        self._cache=None

    def invalidate(self):
        self._cache=None

    def list(self, *, force=False):
        if force or self._cache is None or time.monotonic()-self._cache[0]>15:
            # Cache discovery only. Durable occupancy is re-read on every call,
            # so a newly quarantined directory cannot remain selectable for 15s.
            value=self.catalog.list(include_external=self.config.get('workspace_discovery',{}).get('native_agents',False),
                                    include_guards=False)
            self._cache=(time.monotonic(),value)
        value=copy.deepcopy(self._cache[1])
        try:
            busy=busy_paths(self.root)
        except WorkspaceBusy:
            busy=None
            value['warnings'].append('工作区占用状态暂不可确认；已有目录暂不可分配。')
        runs=self.store.list_runs()
        managed=self.root/'workspaces'
        known_live=[]
        for run in runs:
            if not run.get('run_dir'):continue
            run_dir=Path(run['run_dir'])
            if not run_dir.is_absolute():run_dir=self.root/run_dir
            if not _inside(run_dir,managed) or _key(run_dir)==_key(managed):continue
            known_live.append(Path((run.get('workspace_binding') or {}).get('workspace_path') or run_dir/'workspace'))
        private=[self.root/'data',self.root/'.git',self.root/'.tools/providers',self.root/'.tools/node_modules']
        for item in value['items']:
            if not item.get('available'):
                continue
            path=Path(item['path'])
            reason=''
            if any(part.casefold() in {'.git','.hg','.svn','.runtime','.codebuddy','.workbuddy','.ssh','.aws','.azure','.gnupg'} for part in path.parts):
                reason='不能把版本控制、认证或运行数据子目录作为工作区。'
            elif _inside(path,managed) and not any(_inside(path,live) for live in known_live):
                reason='派工台分类、任务和运行记录文件夹不能直接执行；请选择已登记的具体工作区。'
            elif busy is None:
                reason='无法确认目录是否被其他任务占用。'
            elif any(_overlaps(path,other) for other in busy):
                reason='该目录或其上下级目录正被另一个派工任务使用。'
            elif any(_overlaps(path,other) for other in private):
                reason='不能选择派工台运行数据、程序目录或包含它们的上级目录。'
            for run in runs:
                if reason: break
                if not run.get('run_dir'): continue
                run_dir=Path(run['run_dir'])
                if not run_dir.is_absolute():run_dir=self.root/run_dir
                binding=run.get('workspace_binding') or {}
                live=Path(binding.get('workspace_path') or run_dir/'workspace')
                snapshot=Path(run.get('artifact_root') or binding.get('snapshot_path') or run_dir/'workspace')
                if run.get('state') in {'running','verifying'} and _overlaps(path,live):
                    reason='来源工作区正在执行或检查，结束后才能重新使用。'
                elif _inside(run_dir,path):
                    reason='不能把运行记录目录或其上级目录作为执行工作区。'
                elif snapshot!=live and _overlaps(path,snapshot):
                    reason='这是已保留的验收快照，不能原地修改。'
                elif _inside(path,run_dir) and not _inside(path,live):
                    reason='不能选择运行日志或验收快照目录。'
                elif live==run_dir/'workspace' and _inside(path,live) and _key(path)!=_key(live):
                    reason='请选择该历史运行的完整工作区，以便先保留原始产物。'
            if reason:item.update(available=False,reason=reason)
        return apply_workspace_guards(self.root,value)

    def resolve(self, choice, *, repo=None):
        choice=validate_workspace_choice(choice)
        if choice['mode']!='existing':return None
        found=next((item for item in self.list(force=True)['items'] if item['id']==choice['workspace_id']),None)
        if found is None:
            raise ValueError('所选工作区已不在目录列表中，请刷新并重新选择。')
        if not found['available']:
            raise ValueError(found['reason'] or '所选工作区当前不可用。')
        if repo:
            related=(self.config.get('repositories') or {}).get(found.get('repo_alias'))
            if _key(found['path'])!=_key(repo) and (not related or _key(related)!=_key(repo)):
                raise ValueError('已有工作区与任务指定的仓库不一致，请选择匹配目录或调整仓库。')
        return found
