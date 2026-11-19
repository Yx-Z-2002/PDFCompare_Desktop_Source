PDFCompare 桌面版 2.3 重新打包说明

这份源码使用 Python 3.13、Tkinter/ttk 桌面界面和 Go 单文件启动器。
日常使用直接双击 PDFCompare_Desktop_v2_3.exe，无需安装 Python、Go 或浏览器。
以下仅用于修改源码后重新打包。

1. 安装 Python 3.13 和 Go 1.25 或更新版本（64 位）。
2. 将本源码文件夹与已交付的 PDFCompare_Desktop_v2_3.exe 放到工作目录。
3. 从已交付 exe 提取内置 Windows 运行时（只读取，不运行）：
   python packaging/extract_runtime.py --exe PDFCompare_Desktop_v2_3.exe --output build_runtime
4. 打包（根据源码目录实际名称调整 --app-dir 和 --entry）：
   python packaging/build.py --app-dir improved/app --entry improved/run.py --runtime build_runtime --out PDFCompare_Desktop_v2_3_new.exe --go go
5. 在 Windows 10/11 64 位机器运行，并执行启动自检：
   PDFCompare_Desktop_v2_3_new.exe --self-test self_test.json
   生成的 JSON 中 ok 应为 true；仍应使用实际 PDF 做业务回归。

运行机制
- Go 启动器是 Windows GUI 子系统程序，不显示命令行黑框。
- 内置资源解压到 LocalAppData/PDFCompareDesktop/runtime/<内容哈希>。
- 每次启动校验内置文件 SHA-256，损坏的缓存会重新解压。
- 启动 pythonw.exe，显示原生 Tk 窗口；不启动网页服务器或浏览器。
- 数据目录为 LocalAppData/PDFCompareDesktop/data。
- 启动器日志为 LocalAppData/PDFCompareDesktop/launcher.log。

依赖来源
- Windows Python 3.13.12 与原 PDFCompare.exe 内置 PDF 依赖保持一致。
- 新增 Tcl/Tk 8.6.15 来自 python.org 官方 Python 3.13.12 amd64/tcltk.msi。
- component_hashes.json 记录用于构建的原始组件哈希。
- 第三方许可保留在程序包 licenses/ 与 runtime/ 对应目录中。
