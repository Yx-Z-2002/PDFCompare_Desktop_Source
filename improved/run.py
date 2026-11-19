#!/usr/bin/env python3
"""PDFCompare native desktop entry point, including distributable self-test."""
from __future__ import annotations
import json
import logging
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'app'))


def data_directory():
    override=os.environ.get('PDF_COMPARE_HOME')
    if override:return Path(override).expanduser().resolve()
    local=os.environ.get('LOCALAPPDATA')
    return ((Path(local) if local else Path.home()/'.local'/'share')/'PDFCompareDesktop').resolve()


def main():
    data_root=data_directory()
    data_root.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(filename=str(data_root/'PDFCompare.log'),level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s',encoding='utf-8')
    # Establish DPI behavior before creating the first native window.
    if os.name=='nt':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:pass
    from desktop import DesktopApp, launch, VERSION
    if '--self-test' in sys.argv:
        import tkinter as tk
        import pdfplumber
        import pypdfium2
        from PIL import Image, ImageTk
        import extract_requirements
        index=sys.argv.index('--self-test')
        destination=Path(sys.argv[index+1]) if index+1<len(sys.argv) else data_root/'self_test.json'
        with tempfile.TemporaryDirectory(prefix='pdfcompare-test-') as temp:
            root=tk.Tk()
            root.withdraw()
            app=DesktopApp(root,Path(temp))
            root.update_idletasks()
            root.update()
            result={'ok':True,'python':sys.version,'tk':root.tk.call('info','patchlevel'),
                    'native_window':True,'tabs':len(app.tabs.tabs()),'pdfplumber':pdfplumber.__version__,
                    'app_version':VERSION,'runtime_root':str(ROOT)}
            app.close()
        destination.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return 0
    launch(data_root)
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:
        details=traceback.format_exc()
        logging.exception('Unable to start PDFCompare')
        root=data_directory()
        try:
            root.mkdir(parents=True,exist_ok=True)
            (root/'startup_error.txt').write_text(details,encoding='utf-8')
        except Exception:pass
        if '--self-test' in sys.argv:
            index=sys.argv.index('--self-test')
            if index+1<len(sys.argv):
                try:Path(sys.argv[index+1]).write_text(json.dumps({'ok':False,'error':details},ensure_ascii=False),encoding='utf-8')
                except Exception:pass
        if os.name=='nt':
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None,'启动失败，日志已保存至：\n'+str(root/'startup_error.txt')+'\n\n'+details[-1500:],'PDFCompare',0x10)
            except Exception:pass
        else:print(details,file=sys.stderr)
        raise SystemExit(1)
