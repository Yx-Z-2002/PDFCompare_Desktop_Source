"""Native Windows desktop interface. No HTTP server, browser or WebView."""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, font as tkfont

from extract_requirements import FIELD_NAMES
import compare_matches as comparator
from service import DEFAULTS, Workspace, checked_config, extracted_records, PDFIUM_LOCK
from task_control import TaskCancelled

VERSION = '2.3'
BG = '#f2f5f7'
INK = '#19323c'
TEAL = '#087f8c'
PDF_RENDER_LOCK = PDFIUM_LOCK
FONT_FAMILY = 'Microsoft YaHei UI'
SEPARATOR_MODES = {
    'outermost': '顶部第一条 / 底部最后一条',
    'conservative': '保守识别（排除表格边框）',
}
DIFF_COLORS = {'delete': ('#ad2638', '#ffe6e9'), 'insert': ('#086b43', '#ddf4e7')}


def open_local(path: Path):
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError('文件或文件夹不存在：' + str(path))
    if os.name == 'nt':
        os.startfile(str(path))
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', str(path)])
    else:
        subprocess.Popen(['xdg-open', str(path)])


def text_box(parent, **kwargs):
    frame = ttk.Frame(parent)
    text = tk.Text(frame, wrap='word', relief='flat', borderwidth=0, padx=12,
                   pady=10, background='white', foreground=INK,
                   font=(FONT_FAMILY, 10), **kwargs)
    scroll = ttk.Scrollbar(frame, orient='vertical', command=text.yview)
    text.configure(yscrollcommand=scroll.set)
    text.pack(side='left', fill='both', expand=True)
    scroll.pack(side='right', fill='y')
    return frame, text


def put_text(widget, value):
    widget.configure(state='normal')
    widget.delete('1.0', 'end')
    widget.insert('1.0', value)
    widget.configure(state='disabled')


def put_segments(widget, segments):
    """Render raw text tags, without converting through HTML or changing text."""
    widget.configure(state='normal')
    widget.delete('1.0', 'end')
    for tag, (foreground, background) in DIFF_COLORS.items():
        widget.tag_configure(tag, foreground=foreground, background=background)
    widget.tag_configure('heading', foreground=INK, font=(FONT_FAMILY,10,'bold'))
    widget.tag_configure('note', foreground='#715c24', background='#fff8df')
    for value, tag in segments:
        widget.insert('end', value, (tag,) if tag else ())
    widget.configure(state='disabled')


class PDFPreview:
    """Render the original page; optional overlays never modify the PDF."""
    def __init__(self, owner, path: Path, number=1, *, separator_config=None, side=None):
        self.owner, self.path, self.side = owner, path, side
        self.separator_config = separator_config
        self.last_analysis = None
        self.image_size = None
        self.page = max(1, int(number))
        self.total = None
        self.scale = 1.35
        self.generation = 0
        self.events = queue.Queue()
        self.workers = []
        self.closed = False
        self.win = tk.Toplevel(owner.root)
        self.win.protocol('WM_DELETE_WINDOW', self.close)
        prefix = '页边识别预览' if separator_config is not None else '原 PDF 核对'
        self.win.title(prefix + ' · ' + (side+' · ' if side else '') + path.name)
        width=min(1050,owner.root.winfo_screenwidth()-60)
        height=min(840,owner.root.winfo_screenheight()-80)
        self.win.geometry(f'{width}x{height}')
        self.win.minsize(720, 460)
        top = ttk.Frame(self.win, padding=(10,8))
        top.pack(fill='x')
        ttk.Button(top, text='上一页', command=lambda: self.navigate(-1)).pack(side='left')
        self.page_var = tk.StringVar(value=str(self.page))
        entry = ttk.Entry(top, textvariable=self.page_var, width=7, justify='center')
        entry.pack(side='left', padx=6)
        entry.bind('<Return>', lambda event: self.jump())
        ttk.Button(top, text='跳转', command=self.jump).pack(side='left')
        ttk.Button(top, text='下一页', command=lambda: self.navigate(1)).pack(side='left', padx=6)
        ttk.Button(top, text='缩小', command=lambda: self.zoom(.8)).pack(side='left', padx=(15, 4))
        ttk.Button(top, text='放大', command=lambda: self.zoom(1.25)).pack(side='left')
        self.label = tk.StringVar(value='正在渲染…')
        ttk.Label(top, textvariable=self.label).pack(side='left', padx=14)
        self.show_mask=tk.BooleanVar(value=True)
        self.show_lines=tk.BooleanVar(value=True)
        self.edge_summary=tk.StringVar(value='按当前页的横线位置计算，不依赖页眉页脚文字是否重复。')
        if separator_config is not None:
            controls=ttk.Frame(self.win,padding=(10,0,10,5))
            controls.pack(fill='x')
            ttk.Checkbutton(controls,text='显示裁切蒙层',variable=self.show_mask,command=self.draw_overlays).pack(side='left')
            ttk.Checkbutton(controls,text='显示候选横线',variable=self.show_lines,command=self.draw_overlays).pack(side='left',padx=12)
            ttk.Button(controls,text='识别依据与排除文字',command=self.show_analysis).pack(side='right')
            ttk.Button(controls,text='按当前设置重算',command=self.reload_settings).pack(side='right',padx=6)
            ttk.Label(self.win,textvariable=self.edge_summary,style='Muted.TLabel',wraplength=max(680,width-30),justify='left').pack(fill='x',padx=12,pady=(0,7))
        panel = ttk.Frame(self.win)
        panel.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(panel, background='#d9e1e6', highlightthickness=0)
        vs = ttk.Scrollbar(panel, orient='vertical', command=self.canvas.yview)
        hs = ttk.Scrollbar(panel, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        vs.grid(row=0, column=1, sticky='ns')
        hs.grid(row=1, column=0, sticky='ew')
        panel.rowconfigure(0, weight=1)
        panel.columnconfigure(0, weight=1)
        self.canvas.bind('<MouseWheel>', lambda e: self.canvas.yview_scroll(int(-e.delta / 120), 'units'))
        self.render()
        self.win.after(80, self.poll)

    def navigate(self, offset):
        target=max(1,self.page+offset)
        if self.total:target=min(self.total,target)
        self.page=target
        self.page_var.set(str(target))
        self.render()

    def jump(self):
        try:
            number=int(self.page_var.get())
            if number<1 or (self.total and number>self.total):raise ValueError()
            self.page=number
            self.render()
        except ValueError:messagebox.showinfo('页码','请输入有效的 PDF 页码。',parent=self.win)

    def zoom(self, ratio):
        self.scale=min(3,max(.5,self.scale*ratio))
        self.render()

    def reload_settings(self):
        try:self.separator_config=self.owner.separator_preview_config(self.side)
        except Exception as exc:self.owner.error(exc);return
        self.render()

    def render(self):
        self.generation+=1
        generation,number,scale=self.generation,self.page,self.scale
        config=dict(self.separator_config) if self.separator_config is not None else None
        self.label.set('正在渲染…')
        def work():
            try:
                import pypdfium2 as pdfium
                with PDF_RENDER_LOCK:
                    if self.closed or generation!=self.generation:return
                    document=pdfium.PdfDocument(str(self.path))
                    try:
                        total=len(document)
                        page=document[min(number,total)-1]
                        try:
                            bitmap=page.render(scale=scale)
                            try:image=bitmap.to_pil().copy()
                            finally:bitmap.close()
                        finally:page.close()
                    finally:document.close()
                analysis=None
                if config is not None:
                    if self.closed or generation!=self.generation:return
                    import pdfplumber
                    from margins import separator_analysis
                    from extract_requirements import effective_page_bbox
                    with pdfplumber.open(self.path) as document:
                        page=document.pages[min(number,len(document.pages))-1]
                        visible=effective_page_bbox(page)
                        analysis=separator_analysis(page,bounds=visible,
                            zone=config['separator_zone'],min_width=config['separator_min_width'],
                            mode=config.get('separator_mode','conservative'))
                        if config['top_margin']+config['bottom_margin']>=visible[3]-visible[1]:
                            raise ValueError('手动上下裁切范围覆盖整页，请在高级设置中减小裁切值。')
                        analysis['visible_bbox']=list(visible)
                        analysis['manual_top']=config['top_margin']
                        analysis['manual_bottom']=config['bottom_margin']
                        analysis['enabled']=config['auto_separators']
                        analysis['preview_top']=max(config['top_margin'],analysis.get('top',0))
                        analysis['preview_bottom']=max(config['bottom_margin'],analysis.get('bottom',0))
                        analysis['applied_top']=max(config['top_margin'],analysis.get('top',0) if config['auto_separators'] else 0)
                        analysis['applied_bottom']=max(config['bottom_margin'],analysis.get('bottom',0) if config['auto_separators'] else 0)
                        if analysis['applied_top']+analysis['applied_bottom']>=visible[3]-visible[1]:
                            raise ValueError('手动与横线裁切合并后覆盖整页，请减小手动裁切值或调整横线识别范围。')
                        if analysis['preview_top']+analysis['preview_bottom']>=visible[3]-visible[1]:
                            analysis.setdefault('warnings',[]).append('自动建议与手动裁切重叠；请减小手动裁切值后再启用横线裁切。')
                self.events.put((generation,image,total,analysis,None))
            except Exception as exc:self.events.put((generation,None,None,None,str(exc)))
        worker=threading.Thread(target=work,daemon=True,name='pdf-preview')
        self.workers=[thread for thread in self.workers if thread.is_alive()]
        self.workers.append(worker)
        worker.start()

    def draw_overlays(self):
        self.canvas.delete('page-edge-overlay')
        analysis=self.last_analysis
        if not analysis or not self.image_size:return
        bounds=analysis['visible_bbox']
        width,height=bounds[2]-bounds[0],bounds[3]-bounds[1]
        iw,ih=self.image_size
        sx,sy=iw/width,ih/height
        tag='page-edge-overlay'
        top=max(0,min(height,float(analysis.get('preview_top',0))))
        bottom=max(0,min(height,float(analysis.get('preview_bottom',0))))
        if self.show_mask.get():
            for y0,y1,label in [(0,top*sy,f'顶部建议排除 {top:.1f} pt'),((height-bottom)*sy,ih,f'底部建议排除 {bottom:.1f} pt')]:
                if y1<=y0:continue
                self.canvas.create_rectangle(0,y0,iw,y1,fill='#e69c6d',stipple='gray50',outline='#c36835',width=2,tags=tag)
                self.canvas.create_text(10,y0+8,text=label,fill='#813812',font=(FONT_FAMILY,10,'bold'),anchor='nw',tags=tag)
        if self.show_lines.get():
            for candidate in analysis.get('candidates',[]):
                box=candidate.get('bbox')
                if not box:continue
                x0=max(0,min(iw,(box[0]-bounds[0])*sx));x1=max(0,min(iw,(box[2]-bounds[0])*sx))
                y=(candidate.get('y',(box[1]+box[3])/2)-bounds[1])*sy
                if y<0 or y>ih:continue
                accepted=candidate.get('accepted',False)
                self.canvas.create_line(x0,y,x1,y,fill='#008478' if accepted else '#bf8b1b',width=3 if accepted else 2,dash=() if accepted else (6,4),tags=tag)
        enabled='开启' if analysis.get('enabled') else '关闭'
        self.edge_summary.set(f"橙色蒙层＝建议排除区；绿色＝自动识别边界；黄色虚线＝未采用的候选线。原 PDF 保持完整。\n"
            f"{SEPARATOR_MODES.get(analysis.get('mode','conservative'),'')}；当前页自动建议：顶部 {analysis.get('top',0):.1f} pt / 底部 {analysis.get('bottom',0):.1f} pt；横线裁切开关{enabled}。"
            f"按当前设置实际裁切：顶部 {analysis.get('applied_top',0):.1f} pt / 底部 {analysis.get('applied_bottom',0):.1f} pt。"
            +('\n未找到可靠分隔线，自动识别保留原页。扫描图片中的横线不能直接识别。' if not any(c.get('accepted') for c in analysis.get('candidates',[])) else ''))

    def show_analysis(self):
        if not self.last_analysis:return
        analysis=self.last_analysis
        parts=[f'第 {self.page} 页 · 页边识别依据',
               '识别方式：'+SEPARATOR_MODES.get(analysis.get('mode','conservative'),''),
               f"自动建议：顶部 {analysis.get('top',0):.1f} pt；底部 {analysis.get('bottom',0):.1f} pt。",
               '预览只绘制蒙层，不会改写或裁切原 PDF。横线自动裁切需在主界面勾选后重新提取。']
        for candidate in analysis.get('candidates',[]):
            state='采纳' if candidate.get('accepted') else '未采纳'
            parts.append(f"\n{state} · {candidate.get('side','')} · {candidate.get('kind','')}\n{candidate.get('reason','')}")
        if not any(candidate.get('accepted') for candidate in analysis.get('candidates',[])):parts.append('\n未找到可靠分隔线，保留原页。扫描图片中的横线不能直接识别。')
        for warning in analysis.get('warnings',[]):parts.append('\n提示：'+str(warning))
        removed=analysis.get('removed_text') or {}
        for side,label in [('top','顶部建议排除文字'),('bottom','底部建议排除文字')]:
            values=removed.get(side,[]) if isinstance(removed,dict) else []
            parts.extend(['\n'+label,'\n'.join(map(str,values))[:12000] or '（没有可提取文字）'])
        win=tk.Toplevel(self.win)
        win.title('页边识别依据与排除文字')
        win.geometry(f"800x{min(680,self.owner.root.winfo_screenheight()-100)}")
        win.minsize(600,400)
        close=ttk.Button(win,text='关闭',command=win.destroy)
        close.pack(side='bottom',pady=10)
        frame,text=text_box(win)
        frame.pack(fill='both',expand=True,padx=12,pady=12)
        put_text(text,'\n'.join(parts))
        self.analysis_window=win

    def close(self):
        if self.closed:return
        self.closed=True
        self.generation+=1
        if self.win.winfo_exists():self.win.destroy()
        self.photo=None

    def poll(self):
        if self.closed or not self.win.winfo_exists():return
        try:
            while True:
                generation,image,total,analysis,error=self.events.get_nowait()
                if generation!=self.generation:continue
                if error:
                    self.label.set('预览失败')
                    messagebox.showerror('无法预览',error,parent=self.win)
                else:
                    from PIL import ImageTk
                    self.total=total
                    self.photo=ImageTk.PhotoImage(image,master=self.win)
                    self.canvas.delete('all')
                    self.canvas.create_image(0,0,anchor='nw',image=self.photo)
                    self.canvas.configure(scrollregion=(0,0,image.width,image.height))
                    self.canvas.yview_moveto(0)
                    self.last_analysis=analysis
                    self.image_size=(image.width,image.height)
                    self.draw_overlays()
                    self.label.set(f'第 {self.page} / {total} 页 · {self.scale:.0%}')
        except queue.Empty:pass
        self.win.after(80,self.poll)

class ResultTable:
    def __init__(self, owner, parent, side):
        self.owner, self.side = owner, side
        self.frame = ttk.Frame(parent, padding=(12, 10))
        self.items = []
        self.mapping = {}
        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill='x', pady=(0, 8))
        ttk.Label(toolbar, text='筛选').pack(side='left')
        self.filter = tk.StringVar(value='全部')
        options = ['全部', '已命中', '未命中', '字段未提取到 / 为空', '有核对提示'] if side in ('A','B') else ['全部', '仅差异', '一致', '编号重复']
        ttk.Combobox(toolbar, values=options, textvariable=self.filter, state='readonly', width=17).pack(side='left', padx=6)
        self.search = tk.StringVar()
        ttk.Entry(toolbar, textvariable=self.search, width=26).pack(side='left', padx=(10, 6))
        ttk.Label(toolbar, text='搜索编号、标题、描述或字段值', style='Muted.TLabel').pack(side='left')
        self.count = tk.StringVar(value='尚未提取')
        ttk.Label(toolbar, textvariable=self.count, style='Metric.TLabel').pack(side='right')
        if side == 'comparison':
            legend = ttk.Frame(self.frame)
            legend.pack(fill='x', pady=(0,6))
            ttk.Label(legend,text='红字：A 中不同或仅 A 命中的文字',foreground=DIFF_COLORS['delete'][0]).pack(side='left')
            ttk.Label(legend,text='绿字：B 中不同或仅 B 命中的文字',foreground=DIFF_COLORS['insert'][0]).pack(side='left',padx=18)
            ttk.Label(legend,text='选中条目查看逐段标色',style='Muted.TLabel').pack(side='left')
        panes = ttk.Panedwindow(self.frame, orient='vertical')
        panes.pack(fill='both', expand=True)
        tree_frame = ttk.Frame(panes)
        columns = ('id','status','pages','title','field') if side in ('A','B') else ('id','status','a','b')
        labels = ('编号','提取状态','PDF 页码','标题','查询字段原文') if side in ('A','B') else ('编号','比较结果','A 标题','B 标题')
        widths = (175,125,90,340,220) if side in ('A','B') else (180,200,350,350)
        self.tree = ttk.Treeview(tree_frame, columns=columns, show='headings', selectmode='browse', height=6)
        for col, label, width in zip(columns, labels, widths):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, minwidth=65, stretch=col not in ('id','status','pages'))
        vs = ttk.Scrollbar(tree_frame, orient='vertical', command=self.tree.yview)
        hs = ttk.Scrollbar(tree_frame, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vs.grid(row=0, column=1, sticky='ns')
        hs.grid(row=1, column=0, sticky='ew')
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree.tag_configure('warning', foreground='#985c00')
        self.tree.tag_configure('match', foreground='#0b6f60')
        self.tree.tag_configure('only_a', foreground=DIFF_COLORS['delete'][0])
        self.tree.tag_configure('only_b', foreground=DIFF_COLORS['insert'][0])
        self.tree.tag_configure('duplicate', foreground='#80409b')
        detail_frame = ttk.Frame(panes)
        detail_bar = ttk.Frame(detail_frame)
        detail_bar.pack(fill='x', pady=(8, 5))
        ttk.Label(detail_bar, text='条目详情与原文证据（本次提取）', style='Section.TLabel').pack(side='left')
        if side in ('A','B'):
            ttk.Button(detail_bar, text='查看此条目的 PDF 原页', command=self.preview).pack(side='right')
        else:
            ttk.Button(detail_bar, text='查看 B 原页', command=lambda: self.preview('B')).pack(side='right')
            ttk.Button(detail_bar, text='查看 A 原页', command=lambda: self.preview('A')).pack(side='right', padx=6)
        ttk.Button(detail_bar,text='展开全文',command=self.show_details).pack(side='right',padx=6)
        frame, self.details = text_box(detail_frame, height=5)
        frame.pack(fill='both', expand=True)
        put_text(self.details, '选中上方条目，查看完整文本、查询字段及核对提示。')
        panes.add(tree_frame, weight=3)
        panes.add(detail_frame, weight=2)
        self.panes=panes
        self.last_pane_height=0
        panes.bind('<Configure>', self.layout_panes)
        self.tree.bind('<<TreeviewSelect>>', self.selected)
        self.tree.bind('<Double-1>', lambda e: self.show_details())
        self.search.trace_add('write', lambda *args: self.refresh())
        self.filter.trace_add('write', lambda *args: self.refresh())

    def layout_panes(self,event=None):
        height=self.panes.winfo_height()
        if height>100 and height!=self.last_pane_height:
            self.last_pane_height=height
            self.panes.sashpos(0,int(height*.54))

    @staticmethod
    def status(item):
        if item.get('field_status') in ('missing','empty'):
            return '字段未提取到' if item.get('field_status') == 'missing' else '字段为空'
        return '已命中' if item.get('matched') else '未命中'

    def load(self, items):
        self.items = items
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self.mapping.clear()
        needle = self.search.get().casefold().strip()
        mode = self.filter.get()
        for i, item in enumerate(self.items):
            if self.side in ('A','B'):
                status = self.status(item)
                if mode == '已命中' and not item.get('matched'): continue
                if mode == '未命中' and status != '未命中': continue
                if mode in ('字段未提取到 / 为空','字段缺失 / 为空') and item.get('field_status') not in ('missing','empty'): continue
                if mode == '有核对提示' and not item.get('notes'): continue
                haystack = '\n'.join(str(item.get(k,'')) for k in ('item_id','title','description','field_value'))
                values = (item['item_id'], status, ', '.join(map(str,item.get('pages',[]))), item['title'], item.get('field_value','').replace('\n',' / '))
                tag = 'warning' if item.get('notes') or status in ('字段未提取到','字段为空') else ('match' if item.get('matched') else '')
            else:
                status = item.status
                if mode == '仅差异' and status == 'same': continue
                if mode == '一致' and status != 'same': continue
                if mode == '编号重复' and status != 'duplicate': continue
                haystack = item.item_id + '\n' + '\n'.join(x.title+'\n'+x.description for x in item.a+item.b)
                values = (item.item_id, comparator.STATUS[item.status], ' | '.join(x.title for x in item.a), ' | '.join(x.title for x in item.b))
                tag = status if status in ('only_a','only_b','duplicate') else ('match' if status == 'same' else 'warning')
            if needle and needle not in haystack.casefold(): continue
            iid = str(i)
            self.mapping[iid] = item
            self.tree.insert('', 'end', iid=iid, values=values, tags=(tag,))
        self.count.set(f'显示 {len(self.mapping)} / {len(self.items)} 条')
        put_text(self.details,'选中上方条目，查看完整文本、查询字段及核对提示。' if self.mapping else '当前筛选范围内没有条目。可切换为“全部”或查看诊断。')

    def selected_item(self):
        selected = self.tree.selection()
        return self.mapping.get(selected[0]) if selected else None

    def selected(self, event=None):
        item = self.selected_item()
        if item is None:
            return
        if self.side == 'comparison':
            put_segments(self.details, self.comparison_detail(item))
            return
        if self.side in ('A','B'):
            parts = [item['item_id'] + '  ·  ' + self.status(item), '标题：' + item['title'],
                'PDF 页码：' + ', '.join(map(str,item.get('pages',[]))),
                '\n具体描述\n' + (item.get('description') or '（未提取到描述）'),
                '\n查询字段：' + item.get('query_field',''), item.get('field_value','') or '（未提取到字段值）',
                '\n全部属性\n' + '\n'.join(f'{k}：{v}' for k,v in item.get('attributes',{}).items())]
            diagnostic=item.get('field_diagnostic') or {}
            if diagnostic.get('summary'):
                explanation=['\n字段提取排查',str(diagnostic['summary'])]
                labels={'cropped':'被页边裁切排除','partially_cropped':'部分进入裁切范围',
                        'repeated_removed':'被重复页边清理排除','retained':'保留的文字区域'}
                for evidence in diagnostic.get('evidence',[])[:12]:
                    explanation.append(f"第 {evidence.get('page','?')} 页 · {labels.get(evidence.get('disposition'),'原文线索')}：\n{evidence.get('text','')}")
                if diagnostic.get('evidence_count',0)>12:
                    explanation.append('更多定位线索请查看完整提取报告。')
                parts.insert(-1,'\n'.join(explanation))
            notes=[x for x in item.get('notes',[]) if x not in str(diagnostic.get('summary','')).splitlines()]
            if notes:
                parts.append('\n需核对\n' + '\n'.join('• '+x for x in notes))
            if item.get('field_evidence'):
                parts.append('\n字段定位证据\n' + json.dumps(item['field_evidence'], ensure_ascii=False, indent=2))
        put_text(self.details, '\n'.join(parts))

    def comparison_detail(self, item):
        segments = [(item.item_id + '  ·  ' + comparator.STATUS[item.status] + '\n', 'heading')]
        audit = self.owner.audit.get(item.item_id)
        if audit:
            segments.append(('\n未命中原因核对\n' + audit.get('reason','') + '\n', 'note'))
            if audit.get('field_values'):
                segments.append(('对面字段原文：' + ' / '.join(audit['field_values']) + '\n', ''))
        if item.formatting_only:
            segments.append(('按当前比较规则一致；原文空白排版差异不标为内容变化。\n', 'note'))
        if item.status == 'duplicate':
            segments.append(('同一文件中编号重复：保留全部记录，未自动配对或标记增删。\n', 'note'))
        elif item.status in ('only_a','only_b'):
            segments.append(('颜色表示仅在当前提取命中结果中出现；请结合未命中原因核对另一份原文。\n', 'note'))
        for note in item.review_notes:
            if not audit or note != audit.get('reason'):
                segments.append((str(note)+'\n', 'note'))
        diffs = {}
        if len(item.a) == len(item.b) == 1 and item.status != 'duplicate':
            for field in ('title','description'):
                if getattr(item, field+'_changed'):
                    diffs[field] = comparator.text_diff_segments(getattr(item.a[0],field), getattr(item.b[0],field))
        for index, (side, records) in enumerate((('A',item.a),('B',item.b))):
            segments.append(('\n'+side+' 原文\n', 'heading'))
            if not records:
                segments.append(('当前命中结果中没有此编号；请核对未命中及诊断记录。\n', ''))
            for record in records:
                if item.status == 'duplicate':
                    segments.append((f'【CSV 第 {record.row} 行】\n', 'note'))
                for field,label in (('title','标题'),('description','具体描述')):
                    segments.append((label+'\n', 'heading'))
                    if field in diffs:
                        segments.extend(diffs[field][index])
                    else:
                        tag = 'delete' if item.status == 'only_a' else ('insert' if item.status == 'only_b' else '')
                        segments.append((getattr(record,field), tag))
                    segments.append(('\n', ''))
        if diffs:
            segments.append(('\n差异说明（含空白变化）\n', 'heading'))
            for field,label in (('title','标题'),('description','具体描述')):
                if field in diffs:
                    segments.append((label+'：'+diffs[field][2]+'\n', 'note'))
        return segments

    def show_details(self):
        if self.selected_item() is None:return
        self.selected()
        win=tk.Toplevel(self.owner.root)
        win.title('完整条目详情 · PDFCompare')
        width=min(1000,self.owner.root.winfo_screenwidth()-100)
        height=min(720,self.owner.root.winfo_screenheight()-100)
        win.geometry(f'{width}x{height}')
        win.minsize(640,400)
        frame,text=text_box(win)
        frame.pack(fill='both',expand=True,padx=12,pady=12)
        if self.side == 'comparison':
            put_segments(text,self.comparison_detail(self.selected_item()))
        else:
            put_text(text,self.details.get('1.0','end-1c'))
        ttk.Button(win,text='关闭',command=win.destroy).pack(pady=(0,12))
        self.detail_window=win
        self.detail_text=text

    def preview(self, side=None):
        item = self.selected_item()
        if item is None:
            return
        side = side or (self.side if self.side in ('A','B') else 'A')
        if self.side in ('A','B'):
            pages = item.get('pages') or [1]
        else:
            records = [x for x in self.owner.reports.get(side,{}).get('items',[]) if x['item_id'] == item.item_id]
            if not records:
                audit=self.owner.audit.get(item.item_id,{})
                if audit.get('missing_side')==side and audit.get('pages'):
                    self.owner.preview(side,audit['pages'][0]);return
                messagebox.showinfo('原文核对', f'{side} 的提取结果中没有该编号，请在诊断页搜索原文。', parent=self.owner.root)
                return
            pages = records[0].get('pages') or [1]
        self.owner.preview(side, pages[0])


class DesktopApp:
    def __init__(self, root: tk.Tk, data_root: Path):
        self.root, self.data_root = root, data_root
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.workspace = Workspace(self.data_root)
        self.events = queue.Queue()
        self.busy = False
        self.job_id = 0
        self.cancel_event = None
        self.worker = None
        self.pending_terminal = None
        self.closing = False
        self.closed = False
        self.last_cancelled_output = None
        self.run_result = None
        self.run_paths = {}
        self.reports = {}
        self.audit = {}
        self.config_loading = True
        self.preview_windows = []
        self.advanced_window = None
        self.source_fields = {}
        self.current_diagnostics = ''
        global FONT_FAMILY
        available=set(tkfont.families(root))
        FONT_FAMILY=next((name for name in ('Microsoft YaHei UI','Microsoft YaHei','Noto Sans CJK SC','WenQuanYi Micro Hei','DejaVu Sans') if name in available),'TkDefaultFont')
        for name in ('TkDefaultFont','TkTextFont','TkMenuFont','TkHeadingFont'):
            try:tkfont.nametofont(name).configure(family=FONT_FAMILY,size=10)
            except tk.TclError:pass
        self.root.title(f'PDFCompare {VERSION} · PDF 需求提取与比较')
        width=min(1220,max(980,self.root.winfo_screenwidth()-80))
        height=min(880,max(660,self.root.winfo_screenheight()-80))
        self.root.geometry(f'{width}x{height}')
        self.root.minsize(980, 660)
        self.root.report_callback_exception=self.callback_error
        self.root.configure(background=BG)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.inputs_visible=True
        self.root.bind('<Configure>',self.window_resized)
        self._styles()
        defaults = dict(DEFAULTS)
        defaults.update(remove_repeated=False, output_root=str(Path.home()/'Documents'/'PDFCompare'), open_results=False)
        try:
            saved = json.loads((data_root/'desktop_settings.json').read_text(encoding='utf-8'))
            defaults.update({k:v for k,v in saved.items() if k in DEFAULTS and (isinstance(v,type(DEFAULTS[k])) or (isinstance(DEFAULTS[k],(int,float)) and not isinstance(DEFAULTS[k],bool) and isinstance(v,(int,float)) and not isinstance(v,bool)))})
        except (OSError, ValueError, TypeError):
            pass
        self.vars = {k:(tk.BooleanVar(value=v) if isinstance(v,bool) else tk.StringVar(value=str(v))) for k,v in defaults.items()}
        self.separator_mode_label = tk.StringVar(value=SEPARATOR_MODES.get(self.vars['separator_mode'].get(),SEPARATOR_MODES['outermost']))
        self.vars['separator_mode'].trace_add('write', self.sync_separator_mode)
        self.paths = {s:tk.StringVar() for s in ('A','B')}
        self.status = tk.StringVar(value='选择两份 PDF，设置查询条件后开始。')
        self.summary = tk.StringVar(value='未开始 · 结果将在下方原生窗口中显示')
        self.progress = tk.DoubleVar(value=0)
        self._build()
        for variable in self.vars.values(): variable.trace_add('write', self.changed)
        for variable in self.paths.values(): variable.trace_add('write', self.changed)
        self.config_loading = False
        self.sync_filter()
        self.poll_after=self.root.after(80, self.poll)

    def _styles(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', font=(FONT_FAMILY,10), foreground=INK)
        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG)
        style.configure('TCheckbutton', background=BG)
        style.configure('TLabelframe', background=BG)
        style.configure('TLabelframe.Label', background=BG, foreground=INK)
        style.configure('Header.TLabel', font=(FONT_FAMILY,21,'bold'), foreground=INK)
        style.configure('Muted.TLabel', foreground='#607480', font=(FONT_FAMILY,9))
        style.configure('Section.TLabel', font=(FONT_FAMILY,10,'bold'))
        style.configure('Metric.TLabel', foreground=TEAL, font=(FONT_FAMILY,10,'bold'))
        style.configure('TButton', padding=(11,6))
        style.configure('Accent.TButton', background=TEAL, foreground='white', padding=(17,8), font=(FONT_FAMILY,10,'bold'))
        style.map('Accent.TButton', background=[('active','#086c78'),('disabled','#a3b8bc')])
        style.configure('Stop.TButton', background='#fbe5e8', foreground='#96283a')
        style.map('Stop.TButton', background=[('active','#f5cbd2'),('disabled','#e4e8ea')], foreground=[('disabled','#77858b')])
        style.configure('TEntry', padding=5)
        style.configure('TCombobox', padding=4)
        style.configure('Treeview', rowheight=30, background='white', fieldbackground='white', borderwidth=0)
        style.configure('Treeview.Heading', background='#e4ecef', padding=7, font=(FONT_FAMILY,10,'bold'))
        style.map('Treeview', background=[('selected','#d7eeee')], foreground=[('selected','#0d4650')])
        style.configure('TNotebook', background=BG, borderwidth=0)
        style.configure('TNotebook.Tab', padding=(18,8))
        style.configure('Horizontal.TProgressbar', background=TEAL, troughcolor='#dce7eb')

    def _build(self):
        outer = ttk.Frame(self.root, padding=(22,16,22,14))
        outer.pack(fill='both', expand=True)
        header = ttk.Frame(outer)
        header.pack(fill='x', pady=(0,12))
        ttk.Label(header, text='PDF 需求提取与比较', style='Header.TLabel').pack(side='left')
        self.fold_button=ttk.Button(header,text='收起输入区',command=self.toggle_inputs)
        self.fold_button.pack(side='right',padx=(12,0))
        ttk.Label(header, text=f'桌面版 {VERSION}  /  本地处理', style='Muted.TLabel').pack(side='right', pady=(10,0))
        files = ttk.LabelFrame(outer, text='  01  选择文件与查询范围  ', padding=(12,8))
        files.pack(fill='x')
        self.files_frame=files
        files.columnconfigure(1,weight=1)
        self.path_entries = []
        self.pick_buttons = []
        for row, side in enumerate(('A','B')):
            ttk.Label(files,text=f'PDF {side}',style='Section.TLabel').grid(row=row,column=0,padx=(0,10),pady=4)
            entry = ttk.Entry(files,textvariable=self.paths[side])
            entry.grid(row=row,column=1,sticky='ew',pady=4)
            self.path_entries.append(entry)
            button = ttk.Button(files,text='选择文件…',command=lambda s=side:self.choose_pdf(s))
            button.grid(row=row,column=2,padx=(8,0),pady=4)
            self.pick_buttons.append(button)
        filters = ttk.Frame(files)
        filters.grid(row=2,column=0,columnspan=3,sticky='ew',pady=(9,2))
        filters.columnconfigure(1,weight=1)
        filters.columnconfigure(3,weight=2)
        ttk.Label(filters,text='A 查询字段').grid(row=0,column=0,padx=(0,8),sticky='w')
        ttk.Combobox(filters,textvariable=self.vars['field'],values=FIELD_NAMES,width=26).grid(row=0,column=1,sticky='ew')
        ttk.Label(filters,text='查询内容').grid(row=0,column=2,padx=(18,8))
        ttk.Entry(filters,textvariable=self.vars['query'],width=26).grid(row=0,column=3,sticky='ew')
        ttk.Checkbutton(filters,text='B 使用相同条件',variable=self.vars['same_filter'],command=self.sync_filter).grid(row=0,column=4,padx=(14,0))
        self.label_b=ttk.Label(filters,text='B 查询字段')
        self.label_b.grid(row=1,column=0,pady=(6,0),sticky='w')
        self.field_b = ttk.Combobox(filters,textvariable=self.vars['field_b'],values=FIELD_NAMES,width=26)
        self.field_b.grid(row=1,column=1,sticky='ew',pady=(6,0))
        self.query_b_label=ttk.Label(filters,text='查询内容')
        self.query_b_label.grid(row=1,column=2,padx=(18,8),pady=(6,0))
        self.query_b = ttk.Entry(filters,textvariable=self.vars['query_b'],width=26)
        self.query_b.grid(row=1,column=3,sticky='ew',pady=(6,0))
        ttk.Button(filters,text='高级设置…',command=self.advanced).grid(row=0,column=5,padx=(10,0),sticky='ew')
        edgebar=ttk.Frame(files)
        edgebar.grid(row=3,column=0,columnspan=3,sticky='ew',pady=(8,0))
        ttk.Checkbutton(edgebar,text='按横线识别页眉页脚',variable=self.vars['auto_separators']).pack(side='left')
        mode_box=ttk.Combobox(edgebar,textvariable=self.separator_mode_label,values=list(SEPARATOR_MODES.values()),state='readonly',width=29)
        mode_box.pack(side='left',padx=8)
        mode_box.bind('<<ComboboxSelected>>',self.select_separator_mode)
        self.separator_mode_box=mode_box
        self.separator_side=tk.StringVar(value='A')
        ttk.Button(edgebar,text='预览页边识别',command=self.preview_separators).pack(side='right')
        ttk.Combobox(edgebar,textvariable=self.separator_side,values=['A','B'],state='readonly',width=4).pack(side='right',padx=6)
        ttk.Label(edgebar,text='PDF').pack(side='right')
        actions = ttk.Frame(outer)
        self.actions_frame=actions
        actions.pack(fill='x',pady=(12,10))
        self.start_btn = ttk.Button(actions,text='提取并比较',style='Accent.TButton',command=lambda:self.start(True))
        self.start_btn.pack(side='left')
        self.extract_btn = ttk.Button(actions,text='仅提取',command=lambda:self.start(False))
        self.extract_btn.pack(side='left',padx=8)
        self.compare_btn = ttk.Button(actions,text='比较当前结果',command=self.recompare,state='disabled')
        self.compare_btn.pack(side='left')
        ttk.Checkbutton(actions,text='比较时忽略空白差异',variable=self.vars['ignore_whitespace']).pack(side='left',padx=12)
        ttk.Button(actions,text='打开结果目录',command=self.open_output).pack(side='right')
        ttk.Button(actions,text='导出完整表格…',command=self.export_table).pack(side='right',padx=8)
        ttk.Label(outer,textvariable=self.summary,style='Metric.TLabel').pack(fill='x',pady=(0,9))
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill='both',expand=True)
        self.tables = {}
        for side,name in [('A','A · 提取结果'),('B','B · 提取结果'),('comparison','差异比较')]:
            table=ResultTable(self,self.tabs,side)
            self.tables[side]=table
            self.tabs.add(table.frame,text=name)
        diagnosis=ttk.Frame(self.tabs,padding=12)
        self.tabs.add(diagnosis,text='诊断与溯源')
        findbar=ttk.Frame(diagnosis)
        findbar.pack(fill='x',pady=(0,8))
        ttk.Label(findbar,text='原文定位').pack(side='left')
        self.locate_text=tk.StringVar()
        ttk.Entry(findbar,textvariable=self.locate_text,width=34).pack(side='left',padx=8)
        self.locate_side=tk.StringVar(value='A')
        ttk.Combobox(findbar,textvariable=self.locate_side,values=['A','B'],state='readonly',width=4).pack(side='left')
        self.locate_btn=ttk.Button(findbar,text='搜索原文文字层',command=self.locate)
        self.locate_btn.pack(side='left',padx=8)
        ttk.Button(findbar,text='查看 PDF 原页',command=self.preview_current).pack(side='left')
        ttk.Button(findbar,text='恢复诊断报告',command=lambda:put_text(self.diagnostics,self.current_diagnostics)).pack(side='right')
        frame,self.diagnostics=text_box(diagnosis)
        frame.pack(fill='both',expand=True)
        put_text(self.diagnostics,'提取后会列出每份 PDF 的异常、识别范围与页码。\n\n“字段未提取到 / 为空”表示程序未能读出对应字段，不等于原文不存在。\n如果某条需求没有出现，可在此输入编号或关键词，搜索 PDF 的原始文字层。\n扫描页需要 OCR；本工具会标记无文字层的页面。')
        foot=ttk.Frame(outer)
        foot.pack(side='bottom',fill='x',pady=(10,0),before=self.tabs)
        self.stop_btn=ttk.Button(foot,text='中止任务',command=self.stop_task,style='Stop.TButton',state='disabled')
        self.stop_btn.pack(side='right',padx=(10,0))
        ttk.Progressbar(foot,variable=self.progress,maximum=100,length=180).pack(side='right',padx=(12,0))
        ttk.Label(foot,textvariable=self.status,style='Muted.TLabel').pack(side='left',fill='x',expand=True)

    def window_resized(self,event):
        if event.widget==self.root and self.run_result and self.root.winfo_height()<760 and self.inputs_visible:
            self.toggle_inputs()

    def toggle_inputs(self):
        if self.inputs_visible:
            self.files_frame.pack_forget()
            self.fold_button.configure(text='展开输入区')
            self.inputs_visible=False
        else:
            self.files_frame.pack(fill='x',before=self.actions_frame)
            self.fold_button.configure(text='收起输入区')
            self.inputs_visible=True

    def changed(self,*args):
        if self.config_loading:return
        if self.run_result and not self.busy:
            self.status.set('设置已修改。点击“提取并比较”重新提取；当前表格保留上次结果。')
        self.sync_filter()

    def sync_separator_mode(self,*args):
        self.separator_mode_label.set(SEPARATOR_MODES.get(self.vars['separator_mode'].get(),SEPARATOR_MODES['outermost']))

    def select_separator_mode(self,event=None):
        value=next((key for key,label in SEPARATOR_MODES.items() if label==self.separator_mode_label.get()),'outermost')
        self.vars['separator_mode'].set(value)

    def sync_filter(self):
        state='disabled' if self.vars['same_filter'].get() else 'normal'
        if hasattr(self,'field_b'):
            self.field_b.configure(state=state)
            self.query_b.configure(state=state)
            for widget in (self.label_b,self.field_b,self.query_b_label,self.query_b):
                if self.vars['same_filter'].get():widget.grid_remove()
                else:widget.grid()

    def choose_pdf(self,side):
        path=filedialog.askopenfilename(parent=self.root,title='选择 PDF '+side,filetypes=[('PDF 文件','*.pdf')])
        if path:self.paths[side].set(path)

    def read_config(self):
        result={key:var.get() for key,var in self.vars.items()}
        # Browser reports remain exportable files, but never open automatically.
        result['open_results']=False
        return checked_config(result)

    def advanced(self):
        if self.advanced_window and self.advanced_window.winfo_exists():
            self.advanced_window.lift();return
        win=tk.Toplevel(self.root)
        self.advanced_window=win
        win.title('提取参数与保存设置')
        height=min(750,self.root.winfo_screenheight()-80)
        win.geometry(f'800x{height}')
        win.minsize(700,480)
        outer=ttk.Frame(win)
        outer.pack(fill='both',expand=True)
        canvas=tk.Canvas(outer,background=BG,highlightthickness=0)
        scrollbar=ttk.Scrollbar(outer,orient='vertical',command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right',fill='y')
        canvas.pack(side='left',fill='both',expand=True)
        body=ttk.Frame(canvas,padding=20)
        body_window=canvas.create_window(0,0,window=body,anchor='nw')
        body.bind('<Configure>',lambda event:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda event:canvas.itemconfigure(body_window,width=event.width))
        win.bind('<MouseWheel>',lambda event:canvas.yview_scroll(int(-event.delta/120),'units'))
        body.columnconfigure(1,weight=1)
        ttk.Label(body,text='高级设置',style='Header.TLabel').grid(row=0,column=0,columnspan=3,sticky='w',pady=(0,12))
        row=1
        for key,label in [('output_root','结果保存位置'),('task_name','任务名称'),('id_pattern','需求编号正则')]:
            ttk.Label(body,text=label).grid(row=row,column=0,sticky='w',pady=6,padx=(0,12))
            ttk.Entry(body,textvariable=self.vars[key]).grid(row=row,column=1,columnspan=1 if key=='output_root' else 2,sticky='ew',pady=6)
            if key=='output_root':ttk.Button(body,text='选择…',command=self.choose_output).grid(row=row,column=2,padx=(8,0))
            row+=1
        ttk.Label(body,text='默认编号适用 AT-XXX-数字；非此格式请填写自定义规则。',style='Muted.TLabel').grid(row=row,column=0,columnspan=3,sticky='w',pady=(0,10));row+=1
        grid=ttk.LabelFrame(body,text='  版面参数  ',padding=12)
        grid.grid(row=row,column=0,columnspan=3,sticky='ew',pady=5);row+=1
        for index,(key,label) in enumerate([('heading_indent','标题允许缩进（pt）'),('margin_zone','页边识别区域（%）'),('repeat_ratio','重复行识别比例（%）'),('separator_zone','横线搜索区域（%）'),('separator_min_width','横线最小宽度（%）'),('top_margin_a','A 顶部裁切（pt）'),('bottom_margin_a','A 底部裁切（pt）'),('top_margin_b','B 顶部裁切（pt）'),('bottom_margin_b','B 底部裁切（pt）')]):
            r,c=divmod(index,2)
            ttk.Label(grid,text=label).grid(row=r,column=c*2,sticky='w',padx=(0,9),pady=6)
            ttk.Entry(grid,textvariable=self.vars[key],width=9).grid(row=r,column=c*2+1,sticky='ew',padx=(0,18),pady=6)
        for key,label in [('remove_repeated','删除重复页眉页脚（默认关闭；建议先保留完整内容）'),('auto_separators','按横线识别页眉页脚（先在主界面预览）'),('no_tables','仅使用文字行规则（表格识别异常时尝试）')]:
            ttk.Checkbutton(body,text=label,variable=self.vars[key]).grid(row=row,column=0,columnspan=3,sticky='w',pady=5);row+=1
        ttk.Label(body,text='横线识别方式').grid(row=row,column=0,sticky='w',pady=6)
        mode_box=ttk.Combobox(body,textvariable=self.separator_mode_label,values=list(SEPARATOR_MODES.values()),state='readonly',width=34)
        mode_box.grid(row=row,column=1,columnspan=2,sticky='w',pady=6);row+=1
        mode_box.bind('<<ComboboxSelected>>',self.select_separator_mode)
        ttk.Label(body,text='横线搜索区域：页面顶部/底部各占比；最小宽度：占可见页宽的比例。\n首末横线模式：顶部区域取第一条、底部区域取最后一条，不再排除表格边框。\n线太靠内可扩大搜索区域，线较短可降低最小宽度。先预览，再启用裁切。',style='Muted.TLabel',wraplength=710,justify='left').grid(row=row,column=0,columnspan=3,sticky='w',pady=8);row+=1
        buttons=ttk.Frame(win,padding=(20,10))
        buttons.pack(side='bottom',fill='x',before=outer)
        ttk.Button(buttons,text='导入设置 JSON…',command=self.import_settings).pack(side='left')
        ttk.Button(buttons,text='导出设置 JSON…',command=self.export_settings).pack(side='left',padx=6)
        ttk.Button(buttons,text='关闭',command=win.destroy).pack(side='right')

    def choose_output(self):
        path=filedialog.askdirectory(parent=self.advanced_window or self.root,title='选择结果保存位置')
        if path:self.vars['output_root'].set(path)

    def import_settings(self):
        path=filedialog.askopenfilename(parent=self.root,title='导入设置',filetypes=[('JSON 设置','*.json')])
        if not path:return
        try:
            data=checked_config(json.loads(Path(path).read_text(encoding='utf-8-sig')))
            self.config_loading=True
            for key,value in data.items():self.vars[key].set(value)
            self.config_loading=False
            self.changed()
        except Exception as exc:
            self.config_loading=False
            self.error(exc)

    def export_settings(self):
        path=filedialog.asksaveasfilename(parent=self.root,initialfile='PDFCompare_settings.json',defaultextension='.json',filetypes=[('JSON 设置','*.json')])
        if not path:return
        try:Path(path).write_text(json.dumps(self.read_config(),ensure_ascii=False,indent=2),encoding='utf-8')
        except Exception as exc:self.error(exc)

    def set_busy(self,busy):
        self.busy=busy
        enabled=not busy and not self.closing
        for button in [self.start_btn,self.extract_btn,*self.pick_buttons,self.locate_btn]:
            button.configure(state='normal' if enabled else 'disabled')
        self.compare_btn.configure(state='normal' if enabled and self.run_result else 'disabled')
        can_stop=busy and self.cancel_event is not None and not self.cancel_event.is_set()
        self.stop_btn.configure(state='normal' if can_stop else 'disabled')

    def begin_job(self,label,work,thread_name):
        """Run one job; Tk remains the sole owner of all UI mutations."""
        if self.busy or self.closing:return
        self.job_id+=1
        token=self.job_id
        event=threading.Event()
        self.cancel_event=event
        self.pending_terminal=None
        context={'retained_folder':None}
        def checkpoint():
            if event.is_set():raise TaskCancelled()
        def emit(message,progress=0):
            self.events.put((token,'progress',(message,progress)))
        def worker():
            try:
                checkpoint()
                kind,data=work(checkpoint,emit,context)
                # Do not check again here: a service may have just finished an
                # atomic output commit. A late stop must not hide valid results.
                self.events.put((token,kind,data))
            except TaskCancelled:
                self.events.put((token,'cancelled',context))
            except Exception as exc:
                logging.exception('Desktop background task failed')
                self.events.put((token,'error',str(exc)))
        self.worker=threading.Thread(target=worker,daemon=True,name=thread_name)
        self.progress.set(0)
        self.status.set(label)
        self.set_busy(True)
        self.worker.start()

    def stop_task(self):
        if not self.busy or self.cancel_event is None or self.cancel_event.is_set():return
        # A completed worker has a terminal event waiting for the UI; it cannot
        # be cancelled retroactively merely because polling has not run yet.
        if self.worker is not None and not self.worker.is_alive():
            self.stop_btn.configure(state='disabled')
            return
        self.cancel_event.set()
        self.stop_btn.configure(state='disabled')
        self.status.set('正在中止，等待当前处理步骤结束…')

    def start(self,compare=True):
        if self.busy or self.closing:return
        try:
            config=self.read_config()
            paths={side:Path(var.get().strip().strip('"')).expanduser().resolve() for side,var in self.paths.items()}
            for side,path in paths.items():
                if not self.paths[side].get().strip() or not path.is_file():raise ValueError('请选择有效的 PDF '+side+' 文件。')
            (self.data_root/'desktop_settings.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
        except Exception as exc:self.error(exc);return
        def work(checkpoint,emit,context):
            def update(message,progress=0):emit(message,progress*.9 if compare else progress)
            documents=[]
            for side,path in paths.items():
                checkpoint()
                update('正在读取 PDF '+side)
                with path.open('rb') as stream:
                    documents.append(self.workspace.register_document(stream,path.stat().st_size,path.name,cancel_check=checkpoint))
            result=self.workspace.extract(documents[0]['id'],documents[1]['id'],config,update,cancel_check=checkpoint)
            context['retained_folder']=result['folder']
            reports={side:json.loads((Path(result['folder'])/side/'extraction_report.json').read_text(encoding='utf-8')) for side in ('A','B')}
            if compare:
                checkpoint()
                comparisons=self.comparisons(result,config['ignore_whitespace'],cancel_check=checkpoint)
                self.workspace.compare(result['id'],config['ignore_whitespace'],
                    lambda message,progress=0:emit(message,90+progress*.1),cancel_check=checkpoint)
                # All cancellation-sensitive work is before the final commit.
            else:comparisons=[]
            return 'done',(result,paths,reports,comparisons,compare)
        self.begin_job('正在打开 PDF…',work,'desktop-extract')

    def comparisons(self,result,ignore_whitespace,*,cancel_check=None):
        folder=Path(result['folder'])
        a,b=[extracted_records(folder/side/'extraction_report.json',side,cancel_check=cancel_check) for side in ('A','B')]
        return comparator.compare(a,b,ignore_whitespace=ignore_whitespace,cancel_check=cancel_check)

    def recompare(self):
        if self.busy or self.closing or not self.run_result:return
        ignore=self.vars['ignore_whitespace'].get()
        result=self.run_result
        def work(checkpoint,emit,context):
            values=self.comparisons(result,ignore,cancel_check=checkpoint)
            self.workspace.compare(result['id'],ignore,emit,cancel_check=checkpoint)
            return 'compared',values
        self.begin_job('正在比较当前结果…',work,'desktop-compare')

    def poll(self):
        if self.closed:return
        if self.poll_after is not None:
            self.root.after_cancel(self.poll_after)
            self.poll_after=None
        try:
            while True:
                token,kind,data=self.events.get_nowait()
                if token!=self.job_id:continue
                if kind=='progress':
                    if self.busy and not self.closing and not (self.cancel_event and self.cancel_event.is_set()):
                        self.status.set(data[0]);self.progress.set(data[1])
                else:self.pending_terminal=(token,kind,data)
        except queue.Empty:pass
        # A terminal message can be queued just before the worker returns.
        # Never enable another task or dispose its workspace until it has exited.
        if self.pending_terminal is not None and (self.worker is None or not self.worker.is_alive()):
            token,kind,data=self.pending_terminal
            self.pending_terminal=None
            if token==self.job_id:
                late_stop=bool(self.cancel_event and self.cancel_event.is_set())
                self.set_busy(False)
                if self.closing:
                    self.finish_close()
                    return
                if kind=='cancelled':
                    folder=data.get('retained_folder')
                    self.last_cancelled_output=folder
                    message=('已中止当前任务；上次完成的结果保持不变。' if self.run_result else '已中止当前任务。')
                    if folder:
                        message+=' 本次已完成的提取结果保留在：'+str(folder)
                        self.current_diagnostics+='\n\n'+message
                        put_text(self.diagnostics,self.current_diagnostics)
                    self.status.set(message)
                elif kind=='error':self.error(data)
                elif kind=='done':
                    result,paths,reports,comparisons,compared=data
                    self.run_result,self.run_paths,self.reports=result,paths,reports
                    for side in ('A','B'):self.tables[side].load(reports[side]['items'])
                    self.load_audit()
                    self.tables['comparison'].load(comparisons)
                    self.populate_diagnostics()
                    self.show_summary(compared)
                    if self.root.winfo_height()<900 and self.inputs_visible:self.toggle_inputs()
                    self.progress.set(100)
                    self.status.set(('已完成；中止请求生效前结果已提交：' if late_stop else '完成。结果已保存：')+result['folder'])
                    if compared:self.tabs.select(self.tables['comparison'].frame)
                elif kind=='compared':
                    self.load_audit()
                    self.tables['comparison'].load(data)
                    self.progress.set(100)
                    self.show_summary(True)
                    self.tabs.select(self.tables['comparison'].frame)
                    self.status.set('比较已完成；中止请求生效前结果已提交。' if late_stop else '比较完成，CSV 与诊断报告已保存。')
                elif kind=='located':
                    put_text(self.diagnostics,data)
                    self.status.set('原文搜索已完成。' if not late_stop else '原文搜索在中止请求生效前已完成。')
                # run_result may have been installed by a successful first job.
                self.set_busy(False)
        if not self.closed:self.poll_after=self.root.after(80,self.poll)

    def show_summary(self,compared):
        parts=[]
        for side in ('A','B'):
            report=self.reports[side]
            count=len(report['items'])
            matched=sum(bool(i.get('matched')) for i in report['items'])
            unresolved=sum(i.get('field_status') in ('missing','empty') for i in report['items'])
            parts.append(f'{side}：识别 {count} / 命中 {matched} / 待核对 {unresolved}')
        if compared:
            values=self.tables['comparison'].items
            parts.append(f'差异 {sum(x.status!="same" for x in values)} / 比较 {len(values)}')
        self.summary.set('     ·     '.join(parts))

    def populate_diagnostics(self):
        parts=['提取诊断与溯源','比较只覆盖已识别且满足查询条件的条目。字段未提取到、扫描页、未解析编号都需要核对。',
               '这里显示逐页摘要。完整文字及定位证据保存在结果目录的 extraction_report.json。']
        for side,report in self.reports.items():
            parts.extend(['\n'+'═'*35,f'{side} · {self.run_paths[side].name}',f"PDF 页数：{report.get('page_count',0)}；识别条目：{len(report['items'])}"])
            warnings=report.get('warnings',[])
            parts.append('\n'.join('• '+str(x) for x in warnings[:150]) if warnings else '未收到提取警告。')
            if len(warnings)>150:parts.append(f'另有 {len(warnings)-150} 条提示，请查看完整报告。')
            diagnostics=report.get('diagnostics') or report.get('page_diagnostics') or []
            for page in diagnostics:
                if not isinstance(page,dict):continue
                parts.append(f"\n第 {page.get('page','?')} 页 · 状态：{page.get('status','已处理')} · 文字词数：{page.get('word_count','?')}")
                candidates=page.get('candidate_ids') or []
                if candidates:parts.append('候选编号：'+', '.join(map(str,candidates[:20]))+(' …' if len(candidates)>20 else ''))
                for warning in (page.get('warnings') or [])[:6]:parts.append('提示：'+str(warning))
                unparsed=page.get('unparsed_headings') or []
                if unparsed:parts.append('未解析编号文字：'+json.dumps(unparsed,ensure_ascii=False)[:800])
                preview=str(page.get('text_preview') or '')
                if preview:parts.append('文字片段：'+preview[:400]+(' …' if len(preview)>400 else ''))
                if sum(map(len,parts))>250000:
                    parts.append('\n页面较多，界面摘要已截短。请在结果目录查看完整诊断报告。')
                    break
            cleaning=[page for page in report.get('page_cleaning',[]) if page.get('removed_lines',0) or page.get('top_margin',0) or page.get('bottom_margin',0) or page.get('auto_detection',{}).get('enabled')]
            if cleaning:
                parts.append('\n页面裁切与清理记录（裁切不会修改原 PDF）')
                for page in cleaning[:150]:
                    auto=page.get('auto_detection') or {}
                    parts.append(f"第 {page.get('page','?')} 页：顶部排除 {page.get('top_margin',0):.1f} pt；底部排除 {page.get('bottom_margin',0):.1f} pt；排除/清理共 {page.get('removed_lines',0)} 行（裁切 {page.get('cropped_lines',0)} 行，重复行 {page.get('repeated_removed_lines',0)} 行）。")
                    if auto.get('enabled'):
                        parts.append(f"横线自动识别建议：顶部 {auto.get('top',0):.1f} pt / 底部 {auto.get('bottom',0):.1f} pt。")
                        accepted=[candidate for candidate in auto.get('candidates',[]) if candidate.get('accepted')]
                        if not accepted:parts.append('未找到可靠分隔线，本页未额外自动裁切。')
                        for candidate in accepted[:4]:parts.append('依据：'+str(candidate.get('reason','已采纳页边横线')))
                        for warning in auto.get('warnings',[])[:4]:parts.append('提示：'+str(warning))
                    removed=auto.get('removed_text') or {}
                    for edge,label in [('top','顶部排除文字'),('bottom','底部排除文字')]:
                        values=removed.get(edge,[]) if isinstance(removed,dict) else []
                        if values:parts.append(label+'：'+' / '.join(map(str,values))[:600])
                if len(cleaning)>150:parts.append('更多页面裁切记录请查看完整 extraction_report.json。')
        self.current_diagnostics='\n'.join(parts)
        put_text(self.diagnostics,self.current_diagnostics)

    def load_audit(self):
        self.audit={}
        if not self.run_result:return
        path=Path(self.run_result['folder'])/'comparison'/'comparison_audit.json'
        try:self.audit={item['item_id']:item for item in json.loads(path.read_text(encoding='utf-8'))}
        except (OSError,ValueError,KeyError):pass

    def locate(self):
        if self.busy or self.closing:return
        needle=self.locate_text.get().strip()
        side=self.locate_side.get()
        path=Path(self.paths[side].get().strip().strip('"')).expanduser().resolve() if self.paths[side].get().strip() else self.run_paths.get(side,Path(''))
        if not needle:self.error('请输入需要查找的编号或关键词。');return
        if not path.is_file():self.error('请先选择 PDF '+side+'。');return
        document_ids=self.run_result.get('document_ids',[]) if self.run_result and path==self.run_paths.get(side) else []
        def work(checkpoint,emit,context):
            if document_ids:
                document_id=document_ids[0 if side=='A' else 1]
            else:
                with path.open('rb') as stream:
                    document_id=self.workspace.register_document(stream,path.stat().st_size,path.name,cancel_check=checkpoint)['id']
            result=self.workspace.search_document(document_id,needle,emit,cancel_check=checkpoint)
            lines=[f'PDF {side} · {path.name}',f'查询：{needle}',f"找到 {len(result['hits'])} 个含关键词的页面。",result.get('note','')]
            for hit in result['hits']:
                checkpoint()
                lines.extend([f"\n第 {hit['page']} 页 · {hit.get('occurrences',1)} 处",hit['text']])
            if not result['hits']:lines.append('\n原始文字层中未找到。请检查是否为扫描文字、不同字符编码或拼写；这不能证明视觉页面中不存在。')
            if result['scanned_pages']:lines.append('\n无可提取文字的 PDF 页码：'+', '.join(map(str,result['scanned_pages'])))
            checkpoint()
            return 'located','\n'.join(lines)
        self.begin_job('正在搜索原文文字层…',work,'source-search')

    def separator_preview_config(self,side):
        side=(side or 'A').lower()
        config={'auto_separators':self.vars['auto_separators'].get(),'separator_mode':self.vars['separator_mode'].get()}
        if config['separator_mode'] not in SEPARATOR_MODES:raise ValueError('请选择有效的横线识别方式。')
        for name,key in [('separator_zone','separator_zone'),('separator_min_width','separator_min_width'),('top_margin','top_margin_'+side),('bottom_margin','bottom_margin_'+side)]:
            try:value=float(self.vars[key].get())
            except (TypeError,ValueError):raise ValueError('页边识别参数必须是数字。') from None
            if not math.isfinite(value) or value<0:raise ValueError('页边识别参数必须为非负有限数字。')
            config[name]=value
        if not 5<=config['separator_zone']<=35:raise ValueError('横线搜索区域须为 5–35%。')
        if not 30<=config['separator_min_width']<=95:raise ValueError('横线最小宽度须为 30–95%。')
        return config

    def preview_separators(self):
        side=self.separator_side.get()
        path=Path(self.paths[side].get().strip().strip('"')).expanduser()
        if not path.is_file():self.error('请先选择 PDF '+side+'。');return
        try:
            config=self.separator_preview_config(side)
            self.preview_windows.append(PDFPreview(self,path,1,separator_config=config,side=side))
        except Exception as exc:self.error(exc)

    def preview_current(self):
        side=self.locate_side.get()
        path=Path(self.paths[side].get().strip().strip('"')).expanduser()
        if not path.is_file():self.error('请先选择 PDF '+side+'。');return
        try:self.preview_windows.append(PDFPreview(self,path,1))
        except Exception as exc:self.error(exc)

    def preview(self,side,page=1):
        path=self.run_paths.get(side) or Path(self.paths[side].get().strip().strip('"'))
        if not path.is_file():self.error('找不到原 PDF 文件，请重新选择。');return
        try:self.preview_windows.append(PDFPreview(self,path,page))
        except Exception as exc:self.error(exc)

    def open_output(self):
        path=Path(self.run_result['folder']) if self.run_result else Path(self.vars['output_root'].get()).expanduser()
        try:path.mkdir(parents=True,exist_ok=True);open_local(path)
        except Exception as exc:self.error(exc)

    def export_table(self):
        if not self.run_result:self.error('请先提取 PDF。');return
        selection=self.tabs.select()
        side=next((side for side,table in self.tables.items() if str(table.frame)==selection),None)
        if side is None:
            initial='extraction_diagnostics.txt';source=None
        else:
            relative=side+'/all_items.csv' if side in ('A','B') else 'comparison/comparison.csv'
            source=Path(self.run_result['folder'])/relative
            if not source.exists():self.error('请先完成比较。');return
            initial=side+'_results.csv'
        target=filedialog.asksaveasfilename(parent=self.root,initialfile=initial,defaultextension=Path(initial).suffix,filetypes=[('结果文件','*'+Path(initial).suffix)])
        if not target:return
        try:
            if source:shutil.copyfile(source,target)
            else:Path(target).write_text(self.diagnostics.get('1.0','end-1c'),encoding='utf-8-sig')
            self.status.set('已导出：'+target)
        except Exception as exc:self.error(exc)

    def callback_error(self,error_type,value,tb):
        logging.error('Desktop callback failed',exc_info=(error_type,value,tb))
        self.error(value)

    def error(self,exc):
        self.status.set('处理未完成：'+str(exc))
        messagebox.showerror('PDFCompare',str(exc)+'\n\n如问题持续，请保留问题 PDF、查询条件和程序日志，便于复现。',parent=self.root)

    def close(self):
        if self.closed or self.closing:return
        self.closing=True
        for preview in self.preview_windows:preview.close()
        if self.busy:
            self.stop_task()
            self.status.set('正在中止当前任务，处理步骤结束后关闭窗口…')
            return
        self.finish_close()

    def finish_close(self):
        if self.closed:return
        if ((self.worker is not None and self.worker.is_alive()) or
                any(worker.is_alive() for preview in self.preview_windows for worker in preview.workers)):
            self.status.set('正在结束后台处理…')
            self.root.after(100,self.finish_close)
            return
        self.workspace.close()
        self.closed=True
        if self.poll_after is not None:
            self.root.after_cancel(self.poll_after)
            self.poll_after=None
        self.root.destroy()


def launch(data_root: Path):
    root=tk.Tk()
    DesktopApp(root,data_root)
    root.mainloop()
