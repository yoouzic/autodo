"""Export live CLI model metadata (or the explicit installed-package fallback)."""
import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime,timezone
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dispatcher.codebuddy import load_catalog


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--package',type=Path,default=Path(os.environ.get('APPDATA',''))/'npm/node_modules/@tencent-ai/codebuddy-code')
    parser.add_argument('--region',choices=('internal','public'),default='internal')
    parser.add_argument('--source',choices=('live','installed'),default='live')
    args=parser.parse_args()
    package=args.package.resolve()
    metadata=json.loads((package/'package.json').read_text(encoding='utf-8'))
    if metadata.get('name')!='@tencent-ai/codebuddy-code':raise ValueError('Not the official CodeBuddy CLI package')
    if args.source=='live':
        from dispatcher.codebuddy_catalog import query_models
        node=shutil.which('node')
        if not node:raise ValueError('Node.js is required for the installed CLI')
        models=query_models([node,str(package/'bin/codebuddy')])
        source={'source':'cli-acp','methods':['initialize','session/new'],'prompt_calls':0}
    else:
        product_path=package/('product.internal.json' if args.region=='internal' else 'product.json')
        raw=product_path.read_bytes(); product=json.loads(raw)
        agent=next(agent for agent in product['agents'] if agent['name']=='cli')
        allowed=agent['models']; records={row['id']:row for row in product['models']}
        models=[]
        for name in allowed:
            row=records.get(name,{})
            reasoning=row.get('reasoning') or {}
            models.append({'id':name,'label':row.get('name',name),
                           'efforts':reasoning.get('supportedEfforts',[])})
        source={'source':str(product_path),'region':args.region,'source_sha256':hashlib.sha256(raw).hexdigest()}
    output=args.root/'data/codebuddy-model-catalog.json'
    value={'package':metadata['name'],'version':metadata['version'],'queried_at':datetime.now(timezone.utc).isoformat(),
           **source,'models':models}
    # Validate using the same reader before publishing the catalogue.
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        trial=Path(directory); (trial/'data').mkdir()
        (trial/'data/codebuddy-model-catalog.json').write_text(json.dumps(value),encoding='utf-8')
        load_catalog(trial)
    output.parent.mkdir(parents=True,exist_ok=True)
    temporary=output.with_suffix('.json.tmp')
    with temporary.open('x',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2)
    os.replace(temporary,output)
    print(json.dumps({'version':metadata['version'],'models':len(models),
        'adjustable':[row for row in models if row['efforts']]},ensure_ascii=False))


if __name__=='__main__':main()
