"""Validate Windows GUI PE headers and all files in the embedded bundle."""
from pathlib import Path
import argparse,hashlib,io,json,struct,zipfile
p=argparse.ArgumentParser();p.add_argument('exe',type=Path);p.add_argument('--manifest',type=Path,default=Path(__file__).parent/'launcher/manifest.json');a=p.parse_args()
b=a.exe.read_bytes();assert b[:2]==b'MZ';off=struct.unpack_from('<I',b,0x3c)[0];assert b[off:off+4]==b'PE\0\0';machine=struct.unpack_from('<H',b,off+4)[0];magic=struct.unpack_from('<H',b,off+24)[0];subsystem=struct.unpack_from('<H',b,off+24+68)[0];assert machine==0x8664 and magic==0x20b and subsystem==2
pos=b.rfind(b'PK\x05\x06');assert pos>=0;end=pos+22+int.from_bytes(b[pos+20:pos+22],'little');z=zipfile.ZipFile(io.BytesIO(b[:end]));entries=json.loads(a.manifest.read_text());assert set(z.namelist())=={e['path'] for e in entries}
for e in entries:
 content=z.read(e['path']);assert len(content)==e['size'] and hashlib.sha256(content).hexdigest()==e['sha256'],e['path']
required=['run_desktop.py','app/desktop.py','runtime/pythonw.exe','runtime/python313.dll','runtime/_tkinter.pyd','runtime/tcl86t.dll','runtime/tk86t.dll','runtime/Lib/tkinter/__init__.py','runtime/tcl/tcl8.6/init.tcl','runtime/tcl/tk8.6/tk.tcl','runtime/tcl/tcl8.6/license.terms','runtime/tcl/tk8.6/license.terms']
for r in required:assert r in z.namelist(),r
assert b'Lib\n' in z.read('runtime/python313._pth')
assert b'server' not in z.read('run_desktop.py').split(b'def main():',1)[1].split(b"if __name__",1)[0]
print(json.dumps({'ok':True,'platform':'Windows x64','pe_subsystem':'GUI (2)','bytes':len(b),'sha256':hashlib.sha256(b).hexdigest(),'verified_bundle_files':len(entries),'runtime':'CPython 3.13.12 / Tcl Tk 8.6.15','windows_execution_tested':False},indent=2))
