"""Extract the bundled Windows runtime from a PDFCompare executable for rebuilds.
Never executes the input executable; validates archive member paths before writing.
"""
from pathlib import Path
import argparse,io,zipfile
p=argparse.ArgumentParser();p.add_argument('--exe',type=Path,required=True);p.add_argument('--output',type=Path,default=Path('build_runtime'));a=p.parse_args()
data=a.exe.read_bytes();position=len(data);archive=None
while True:
 position=data.rfind(b'PK\x05\x06',0,position)
 if position<0:break
 if position+22>len(data):continue
 end=position+22+int.from_bytes(data[position+20:position+22],'little')
 try:
  z=zipfile.ZipFile(io.BytesIO(data[:end]));names=set(z.namelist())
  if 'runtime/python313.dll' in names and 'runtime/pythonw.exe' in names:archive=z;break
 except (zipfile.BadZipFile,ValueError):pass
if archive is None:raise SystemExit('No supported PDFCompare runtime archive found.')
selected=[i for i in archive.infolist() if i.filename.startswith('runtime/') and not i.is_dir()]
a.output.mkdir(parents=True,exist_ok=True);root=a.output.resolve()
for item in selected:
 rel=item.filename.removeprefix('runtime/')
 if '\\' in rel or ':' in rel:raise SystemExit('Unsafe archive path')
 target=(root/rel).resolve()
 if root not in target.parents:raise SystemExit('Unsafe archive path')
 target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.read(item))
print(f'Extracted {len(selected)} files into {root}')
