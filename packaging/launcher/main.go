package main

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"unsafe"
)

//go:embed bundle.zip
var bundle []byte

//go:embed manifest.json
var manifestBytes []byte

type Entry struct {
	Path   string `json:"path"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
}

var user32 = syscall.NewLazyDLL("user32.dll")
var kernel32 = syscall.NewLazyDLL("kernel32.dll")

func wide(s string) *uint16 { p, _ := syscall.UTF16PtrFromString(s); return p }
func alert(s string) {
	user32.NewProc("MessageBoxW").Call(0, uintptr(unsafe.Pointer(wide(s))), uintptr(unsafe.Pointer(wide("PDFCompare 桌面版"))), 0x10)
}
func cacheRoot() string {
	base := os.Getenv("LOCALAPPDATA")
	if base == "" {
		base = os.Getenv("TEMP")
	}
	if base == "" {
		base = os.TempDir()
	}
	return filepath.Join(base, "PDFCompareDesktop")
}
func validate(root string, entries []Entry) bool {
	for _, e := range entries {
		p := filepath.Join(root, filepath.FromSlash(e.Path))
		f, err := os.Open(p)
		if err != nil {
			return false
		}
		st, err := f.Stat()
		if err != nil || st.Size() != e.Size {
			f.Close()
			return false
		}
		h := sha256.New()
		_, err = io.Copy(h, f)
		f.Close()
		if err != nil || hex.EncodeToString(h.Sum(nil)) != e.SHA256 {
			return false
		}
	}
	return true
}
func prepare(base string) (string, error) {
	digest := sha256.Sum256(bundle)
	version := hex.EncodeToString(digest[:])[:20]
	root := filepath.Join(base, "runtime", version)
	var entries []Entry
	if err := json.Unmarshal(manifestBytes, &entries); err != nil {
		return "", err
	}
	if len(entries) == 0 {
		return "", fmt.Errorf("empty bundle manifest")
	}
	mutexName := wide("Local\\PDFCompareDesktop.Extract." + version)
	h, _, err := kernel32.NewProc("CreateMutexW").Call(0, 0, uintptr(unsafe.Pointer(mutexName)))
	if h == 0 {
		return "", fmt.Errorf("create extraction lock: %v", err)
	}
	defer syscall.CloseHandle(syscall.Handle(h))
	status, _, _ := kernel32.NewProc("WaitForSingleObject").Call(h, 60000)
	if status != 0 && status != 0x80 {
		return "", fmt.Errorf("application preparation timed out")
	}
	defer kernel32.NewProc("ReleaseMutex").Call(h)
	if validate(root, entries) {
		return root, nil
	}
	if err := os.MkdirAll(filepath.Dir(root), 0700); err != nil {
		return "", err
	}
	tmp, err := os.MkdirTemp(filepath.Dir(root), version+"-preparing-")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(tmp)
	zr, err := zip.NewReader(bytes.NewReader(bundle), int64(len(bundle)))
	if err != nil {
		return "", err
	}
	for _, zf := range zr.File {
		name := filepath.FromSlash(zf.Name)
		clean := filepath.Clean(name)
		if filepath.IsAbs(name) || strings.Contains(name, ":") || clean == ".." || strings.HasPrefix(clean, ".."+string(os.PathSeparator)) {
			return "", fmt.Errorf("invalid bundle path")
		}
		dest := filepath.Join(tmp, clean)
		if zf.FileInfo().IsDir() {
			if err = os.MkdirAll(dest, 0700); err != nil {
				return "", err
			}
			continue
		}
		if err = os.MkdirAll(filepath.Dir(dest), 0700); err != nil {
			return "", err
		}
		src, err := zf.Open()
		if err != nil {
			return "", err
		}
		out, err := os.OpenFile(dest, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0600)
		if err != nil {
			src.Close()
			return "", err
		}
		_, copyErr := io.Copy(out, src)
		closeErr := out.Close()
		src.Close()
		if copyErr != nil {
			return "", copyErr
		}
		if closeErr != nil {
			return "", closeErr
		}
	}
	if !validate(tmp, entries) {
		return "", fmt.Errorf("bundled files failed integrity check")
	}
	if err = os.RemoveAll(root); err != nil {
		return "", err
	}
	if err = os.Rename(tmp, root); err != nil {
		return "", err
	}
	return root, nil
}
func run() error {
	base := cacheRoot()
	if err := os.MkdirAll(base, 0700); err != nil {
		return err
	}
	logPath := filepath.Join(base, "launcher.log")
	logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer logFile.Close()
	root, err := prepare(base)
	if err != nil {
		return fmt.Errorf("准备程序失败：%w", err)
	}
	exe, _ := os.Executable()
	env := os.Environ()
	env = append(env, "PYTHONUTF8=1", "PYTHONIOENCODING=utf-8", "PDF_COMPARE_PORTABLE=1", "PDF_COMPARE_HOME="+filepath.Join(base, "data"), "PDF_COMPARE_EXE_DIR="+filepath.Dir(exe), "TCL_LIBRARY="+filepath.Join(root, "runtime", "tcl", "tcl8.6"), "TK_LIBRARY="+filepath.Join(root, "runtime", "tcl", "tk8.6"))
	userArgs := append([]string(nil), os.Args[1:]...)
	for i := 0; i+1 < len(userArgs); i++ {
		if userArgs[i] == "--self-test" && !filepath.IsAbs(userArgs[i+1]) {
			if absolute, pathErr := filepath.Abs(userArgs[i+1]); pathErr == nil {
				userArgs[i+1] = absolute
			}
		}
	}
	args := append([]string{filepath.Join(root, "run_desktop.py")}, userArgs...)
	cmd := exec.Command(filepath.Join(root, "runtime", "pythonw.exe"), args...)
	cmd.Dir = root
	cmd.Env = env
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x08000000}
	if err = cmd.Run(); err != nil {
		return fmt.Errorf("程序未能正常启动或已异常退出。\n\n日志：%s\n\n%w", logPath, err)
	}
	return nil
}
func main() {
	if err := run(); err != nil {
		alert(err.Error())
		os.Exit(1)
	}
}
