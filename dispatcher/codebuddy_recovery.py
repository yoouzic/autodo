"""A narrowly recoverable directory Read probe, never a generic error bypass.

The vendor terminal must already be successful. Every declared artifact must
have later successful write and read evidence and exist inside the workspace.
The result is still an executor claim: independent and human review are required.
"""
import hashlib
import json
from pathlib import Path
from . import workbuddy
from .workspace_copy import _check_components
from .workspace_runtime import validate_artifact_path


def completed_directory_probe(stdout,workspace,*,stderr='',exit_code=0):
    if exit_code!=0 or len(stdout.encode('utf-8'))>workbuddy.MAX_OUTPUT_BYTES:return None
    try:
        events=workbuddy._events(stdout)
        terminals=[e for e in events if e.get('type')=='result']
        if len(terminals)!=1 or events[-1] is not terminals[0]:return None
        terminal=terminals[0]
        if terminal.get('subtype')!='success' or terminal.get('is_error') is not False or terminal.get('permission_denials',[])!=[]:return None
        report=terminal.get('structured_output')
        if report is None:report=workbuddy._json(terminal.get('result',''))
        report=workbuddy._business_report(report)
        if report['status']!='complete' or not report['summary'].strip() or not report['checks'] or not 1<=len(report['artifacts'])<=32:return None
        root=Path(workspace).resolve();_check_components(root)
        calls={};returned=set();failures=[];written={};read={}
        for i,event in enumerate(events):
            message=event.get('message')
            content=message.get('content',[]) if isinstance(message,dict) else []
            if not isinstance(content,list):continue
            for item in content:
                if not isinstance(item,dict):continue
                if item.get('type')=='tool_use':
                    identifier=item.get('id')
                    if not isinstance(identifier,str) or identifier in calls:return None
                    calls[identifier]=item
                elif item.get('type')=='tool_result':
                    identifier=item.get('tool_use_id')
                    if not isinstance(identifier,str) or identifier not in calls or identifier in returned:return None
                    returned.add(identifier);call=calls[identifier];name=call.get('name')
                    args=call.get('input') or {};path=args.get('file_path') if isinstance(args,dict) else None
                    resolved=(root/Path(path)).resolve() if isinstance(path,str) else None
                    if item.get('is_error') is True:
                        blocks=item.get('content')
                        text=''.join(b.get('text','') for b in blocks if isinstance(b,dict) and b.get('type')=='text') if isinstance(blocks,list) else blocks
                        if (name!='Read' or resolved!=root or not isinstance(text,str) or
                            not text.startswith('<tool_use_error>Path is a directory, not a file:') or
                            'This tool can only read files, not directories.' not in text):return None
                        failures.append(i)
                    elif item.get('is_error') is False and resolved is not None:
                        if name in {'Write','Edit'}:written[resolved]=i
                        elif name=='Read':read[resolved]=i
        if not 1<=len(failures)<=3 or set(calls)!=returned:return None
        validation,count=workbuddy._noncompletion_validation_events(events)
        if count!=len(failures) or workbuddy._has_error(validation):return None
        hashes={};total=0
        for name in report['artifacts']:
            p=validate_artifact_path(root,name)
            if not p.is_file() or not 0<p.stat().st_size<=8*1024*1024:return None
            if written.get(p,-1)<=max(failures) or read.get(p,-1)<=written.get(p,-1):return None
            total+=p.stat().st_size
            if total>16*1024*1024:return None
            hashes[p.relative_to(root).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
        if len(hashes)!=len(report['artifacts']):return None
        # Re-use the complete normalizer, including clean terminal and error checks.
        result=workbuddy.normalize_result('\n'.join(json.dumps(e,ensure_ascii=False) for e in validation),stderr=stderr,exit_code=exit_code)
        return dict(result,directory_probe_recovered=True,tool_error_count=count,
                    recovered_artifact_sha256=hashes,
                    runtime_warning={'kind':'completed_directory_probe','message':'曾误用 Read 读取工作区目录；后续文件写入与回读成功，仍需独立验收和人工复审。'})
    except (ValueError,TypeError,KeyError,OSError,RecursionError):return None
