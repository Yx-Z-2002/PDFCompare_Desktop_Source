"""Build a self-contained Windows x64 desktop executable from verified files.
Requires Go 1.25+ and a prepared Windows runtime in build_runtime/.
"""
import argparse,hashlib,json,os,pathlib,shutil,subprocess,tempfile,zipfile
ROOT=pathlib.Path(__file__).resolve().parent.parent
p=argparse.ArgumentParser();p.add_argument('--app-dir',required=True,type=pathlib.Path);p.add_argument('--entry',required=True,type=pathlib.Path);p.add_argument('--out',required=True,type=pathlib.Path);p.add_argument('--go',default=str(ROOT/'packaging/downloads/go/bin/go') if (ROOT/'packaging/downloads/go/bin/go').is_file() else 'go');p.add_argument('--runtime',default=ROOT/'build_runtime',type=pathlib.Path);a=p.parse_args()
launcher=ROOT/'packaging/launcher';out=a.out.resolve();out.parent.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='pdfcompare-bundle-') as td:
 stage=pathlib.Path(td);shutil.copytree(a.runtime,stage/'runtime');shutil.copytree(a.app_dir,stage/'app');shutil.copy2(a.entry,stage/'run_desktop.py')
 licenses=a.app_dir.parent/'licenses'
 if not licenses.exists():licenses=ROOT/'original/licenses'
 if licenses.exists():shutil.copytree(licenses,stage/'licenses')
 extra=ROOT/'packaging/licenses'
 if extra.exists():shutil.copytree(extra,stage/'licenses',dirs_exist_ok=True)
 entries=[]
 with zipfile.ZipFile(launcher/'bundle.zip','w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
  for fp in sorted(stage.rglob('*')):
   if not fp.is_file() or '__pycache__' in fp.parts or fp.suffix=='.pyc':continue
   rel=fp.relative_to(stage).as_posix();b=fp.read_bytes();z.writestr(rel,b);entries.append({'path':rel,'size':len(b),'sha256':hashlib.sha256(b).hexdigest()})
 (launcher/'manifest.json').write_text(json.dumps(entries,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
 env=dict(os.environ,GOOS='windows',GOARCH='amd64',CGO_ENABLED='0',GOAMD64='v1',GOCACHE=str(ROOT/'packaging/go-cache'),GOTOOLCHAIN='local')
 subprocess.run([a.go,'build','-buildvcs=false','-trimpath','-ldflags=-s -w -H=windowsgui','-o',str(out),'.'],cwd=launcher,env=env,check=True)
 print(json.dumps({'exe':str(out),'bytes':out.stat().st_size,'sha256':hashlib.sha256(out.read_bytes()).hexdigest(),'files':len(entries)},ensure_ascii=False))
