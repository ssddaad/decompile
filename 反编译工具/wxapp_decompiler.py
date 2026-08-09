#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
反编译工具套件 (GUI)
  - 小程序反编译: wxapkg 解密 + 反编译 (pc_wxapkg_decrypt + wxappUnpacker)
  - 网页反编译:   整站抓取整理 (requests + bs4 + lxml)

运行: python wxapp_decompiler.py    （或双击 启动.bat）
"""

import os
import re
import sys
import json
import time
import queue
import shutil
import tempfile
import threading
import subprocess
import collections
import urllib.parse
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Next.js 站点规范化（修复 Vercel Skew Protection 残留）
try:
    from nextjs_normalize import (normalize_nextjs_site, is_nextjs_site,
                                  static_degrade_html)
except Exception:  # 模块缺失时降级为空操作，不影响其它功能
    normalize_nextjs_site = None
    is_nextjs_site = lambda d: False  # noqa: E731
    static_degrade_html = None

# --------------------------------------------------------------------------- #
# 路径与常量
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DECRYPT_SCRIPT = os.path.join(BASE_DIR, "pc_wxapkg_decrypt", "main.py")
UNPACK_SCRIPT = os.path.join(BASE_DIR, "wxappUnpacker", "wuWxapkg.js")
RESTORE_WXML_SCRIPT = os.path.join(BASE_DIR, "wxappUnpacker", "restoreWxml.js")
DESKTOP_DIR = os.path.join(os.path.expanduser("~"), "Desktop")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
JADX_DIR = os.path.join(BASE_DIR, "jadx")          # 内置 jadx 解压目录
TOOL_TEMP = tempfile.gettempdir()

WXID_PATTERN = re.compile(r'wx[a-f0-9]{16}', re.IGNORECASE)
ENCRYPT_FLAG = b'V1MMWX'
NO_WIN = 0x08000000  # 子进程不弹控制台窗口

# 1x1 透明 PNG 占位图（用于补全缺失的图片资源）
_PNG1x1 = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
           b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01'
           b'\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')

DEVTOOLS_CANDIDATES = [
    r"C:\Program Files (x86)\Tencent\微信web开发者工具\微信开发者工具.exe",
    r"C:\Program Files\Tencent\微信web开发者工具\微信开发者工具.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "微信开发者工具", "微信开发者工具.exe"),
]


# --------------------------------------------------------------------------- #
# 配置持久化
# --------------------------------------------------------------------------- #
def load_config():
    try:
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 通用工具函数
# --------------------------------------------------------------------------- #
def human_size(num):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if num < 1024.0:
            return f"{num:.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}TB"


def _has_files(path):
    """安全检查目录是否有文件（正确关闭 scandir 迭代器，避免 Windows 文件句柄泄漏）。"""
    try:
        with os.scandir(path) as it:
            return any(it)
    except OSError:
        return False


def _under_next(full_path, root_dir):
    """判断文件是否位于 _next/ 目录下（规范化/美化时需跳过，避免破坏 chunk 结构）。"""
    try:
        rel = os.path.relpath(full_path, root_dir).replace('\\', '/')
        return rel == '_next' or rel.startswith('_next/')
    except Exception:
        return False


# ---- 资源模糊匹配：按文件名在输出目录中查找缺失引用 ----
_IMG_SEARCH_DIRS = ("images", "assets", "static", "img", "icons", "res", "resource", "resources")
_IMG_EXTS_SET = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp", ".avif"}


def _build_file_index(out_dir, exts=None):
    """构建输出目录中所有文件的索引: basename_lower -> [full_path, ...]。

    exts 为 None 时索引所有文件，否则仅索引指定扩展名集合。
    """
    index = {}
    for root_p, _d, files in os.walk(out_dir):
        for fn in files:
            if exts is not None:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in exts:
                    continue
            key = fn.lower()
            index.setdefault(key, []).append(os.path.join(root_p, fn))
    return index


def _find_resource_by_name(basename, out_dir, file_index=None, exts=None):
    """按文件名在输出目录中查找资源文件。

    搜索策略:
    1. 精确匹配 basename（大小写不敏感）
    2. 常见图片目录中查找
    3. 去掉路径前缀后按 basename 匹配
    返回找到的第一个完整路径，或 None。
    """
    if not basename:
        return None
    bn = os.path.basename(basename).lower()

    # 使用传入的索引或现场构建
    if file_index is None:
        file_index = _build_file_index(out_dir, exts)

    # 1. 精确匹配 basename
    if bn in file_index:
        candidates = file_index[bn]
        if candidates:
            return candidates[0]

    # 2. 在常见目录中查找
    for subdir in _IMG_SEARCH_DIRS:
        for ext in (exts or _IMG_EXTS_SET):
            test = os.path.join(out_dir, subdir, bn)
            if os.path.isfile(test):
                return test
            # 尝试不带扩展名匹配其他扩展名
            stem = os.path.splitext(bn)[0]
            for alt_ext in (exts or _IMG_EXTS_SET):
                test2 = os.path.join(out_dir, subdir, stem + alt_ext)
                if os.path.isfile(test2):
                    return test2

    # 3. 模糊匹配：basename 去掉路径前缀后的变体
    stem = os.path.splitext(bn)[0]
    for key, paths in file_index.items():
        key_stem = os.path.splitext(key)[0]
        if key_stem == stem or key == bn:
            if paths:
                return paths[0]

    return None


def _extract_base64_images(css_text, out_dir, rel_base_dir, prefix="img"):
    """从 CSS 文本中提取 base64 编码的图片，保存为独立文件并替换为 url() 引用。

    仅提取 > 512 字节的 base64 数据（小图标如 1x1 透明 GIF 跳过）。
    out_dir: 输出根目录
    rel_base_dir: CSS 文件所在目录（用于计算相对路径）
    prefix: 生成文件名前缀
    返回 (修改后的 CSS, 提取数量)
    """
    import base64

    img_dir = os.path.join(out_dir, "images")
    os.makedirs(img_dir, exist_ok=True)

    count = 0

    def repl(m):
        nonlocal count
        mime = m.group(2).lower().strip()
        data_b64 = m.group(3).strip()
        if len(data_b64) < 512:
            return m.group(0)

        # 确定 MIME 类型和扩展名
        ext_map = {
            "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
            "image/gif": ".gif", "image/svg+xml": ".svg", "image/webp": ".webp",
            "image/bmp": ".bmp", "image/x-icon": ".ico", "image/avif": ".avif",
        }
        ext = ext_map.get(mime, ".png")

        try:
            raw = base64.b64decode(data_b64)
            if len(raw) < 100:
                return m.group(0)
            count += 1
            fname = f"{prefix}_{count:04d}{ext}"
            fpath = os.path.join(img_dir, fname)
            # 避免重名
            i = 1
            while os.path.isfile(fpath):
                fname = f"{prefix}_{count:04d}_{i}{ext}"
                fpath = os.path.join(img_dir, fname)
                i += 1
            with open(fpath, "wb") as f:
                f.write(raw)
            rel = os.path.relpath(fpath, rel_base_dir).replace("\\", "/")
            return f"url({rel})"
        except Exception:
            return m.group(0)

    # 匹配 url(data:image/png;base64,xxxx) 和 url("data:...")
    pattern = re.compile(
        r'url\(\s*["\']?(data:([a-z]+/[a-z+.-]+);base64,([A-Za-z0-9+/=\s]+))["\']?\s*\)',
        re.I)
    css_text = pattern.sub(repl, css_text)
    return css_text, count


def detect_encoding(r):
    """正确检测 HTTP 响应编码，修复 requests 默认 ISO-8859-1 导致中文乱码。"""
    # 1. 检查 HTTP 头中的 charset
    enc = r.encoding
    # requests 对 text/* 默认 ISO-8859-1，需用 apparent_encoding 覆盖
    if not enc or enc.lower() in ("iso-8859-1", "latin-1"):
        enc = r.apparent_encoding
    # 2. 从内容前 4KB 检测 meta charset / BOM
    raw = r.content[:4096]
    # UTF-8 BOM
    if raw[:3] == b'\xef\xbb\xbf':
        return "utf-8-sig"
    # UTF-16 LE BOM
    if raw[:2] == b'\xff\xfe':
        return "utf-16"
    # UTF-16 BE BOM
    if raw[:2] == b'\xfe\xff':
        return "utf-16"
    # meta charset 检测
    try:
        head = raw.decode("ascii", errors="ignore").lower()
        m = re.search(r'charset\s*=\s*["\']?\s*([a-z0-9_-]+)', head)
        if m:
            meta_enc = m.group(1)
            # 验证编码是否有效
            try:
                b"".decode(meta_enc)
                return meta_enc
            except (LookupError, TypeError):
                pass
    except Exception:
        pass
    # 3. 如果 apparent_encoding 也不可靠，尝试常见中文编码
    if not enc or enc.lower() in ("iso-8859-1", "latin-1", "ascii"):
        # 检查是否有中文编码特征
        try:
            raw.decode("utf-8")
            return "utf-8"
        except UnicodeDecodeError:
            pass
        try:
            raw.decode("gbk")
            return "gbk"
        except UnicodeDecodeError:
            pass
        try:
            raw.decode("gb2312")
            return "gb2312"
        except UnicodeDecodeError:
            pass
    return enc or "utf-8"


# ---- 用于解密的 Python 解释器探测（tkinter 与 pycryptodome 可能不在同一解释器）----
_DECRYPT_PY_CACHE = {"py": None}


def _candidate_pythons():
    cands = [sys.executable]
    local = os.environ.get("LocalAppData", "")
    home = os.path.expanduser("~")
    if local:
        for v in ("Python313", "Python312", "Python311", "Python310", "Python39"):
            cands.append(os.path.join(local, "Programs", "Python", v, "python.exe"))
    cands += [
        os.path.join(home, "miniconda3", "python.exe"),
        os.path.join(home, "anaconda3", "python.exe"),
        r"C:\ProgramData\miniconda3\python.exe",
        r"C:\ProgramData\anaconda3\python.exe",
    ]
    return cands


def _python_has_crypto(py):
    try:
        r = subprocess.run([py, "-c", "import Crypto"],
                           capture_output=True, creationflags=NO_WIN, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def resolve_decrypt_python():
    if _DECRYPT_PY_CACHE["py"]:
        return _DECRYPT_PY_CACHE["py"]
    if _python_has_crypto(sys.executable):
        _DECRYPT_PY_CACHE["py"] = sys.executable
        return sys.executable
    for py in _candidate_pythons():
        if py and py != sys.executable and os.path.isfile(py) and _python_has_crypto(py):
            _DECRYPT_PY_CACHE["py"] = py
            return py
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "pycryptodome"],
                       capture_output=True, creationflags=NO_WIN, timeout=180)
        if _python_has_crypto(sys.executable):
            _DECRYPT_PY_CACHE["py"] = sys.executable
            return sys.executable
    except Exception:
        pass
    return sys.executable


# ---- Node.js 路径探测 ----
_NODE_CACHE = {"bin": None}


def _find_node():
    """查找 Node.js 可执行文件路径。"""
    if _NODE_CACHE["bin"]:
        return _NODE_CACHE["bin"]
    # 1. 尝试 PATH 中的 node
    try:
        r = subprocess.run(["node", "--version"], capture_output=True, text=True,
                           creationflags=NO_WIN, timeout=8)
        if r.returncode == 0:
            _NODE_CACHE["bin"] = "node"
            return "node"
    except Exception:
        pass
    # 2. 搜索常见安装路径
    local = os.environ.get("LocalAppData", "")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    candidates = [
        os.path.join(program_files, "nodejs", "node.exe"),
        os.path.join(program_files_x86, "nodejs", "node.exe"),
        os.path.join(local, "Programs", "nodejs", "node.exe"),
        os.path.join(local, "Temp", "nodejs", "node-v20.11.1-win-x64", "node.exe"),
        os.path.join(program_files, "Microsoft Visual Studio", "2022", "Community",
                     "MSBuild", "Microsoft", "VisualStudio", "NodeJs", "node.exe"),
    ]
    # 3. 搜索 Temp 下的 nodejs 目录
    if local:
        temp_nodejs = os.path.join(local, "Temp", "nodejs")
        if os.path.isdir(temp_nodejs):
            for d in os.listdir(temp_nodejs):
                np = os.path.join(temp_nodejs, d, "node.exe")
                if os.path.isfile(np):
                    candidates.append(np)
    for c in candidates:
        if os.path.isfile(c):
            try:
                r = subprocess.run([c, "--version"], capture_output=True, text=True,
                                   creationflags=NO_WIN, timeout=8)
                if r.returncode == 0:
                    _NODE_CACHE["bin"] = c
                    return c
            except Exception:
                pass
    _NODE_CACHE["bin"] = "node"  # fallback
    return "node"


NODE_BIN = None  # 延迟初始化


def _get_node():
    global NODE_BIN
    if NODE_BIN is None:
        NODE_BIN = _find_node()
    return NODE_BIN


# ---- 微信缓存扫描根目录（兼容旧版3.x + 新版4.x xwechat）----
def _cache_scan_roots():
    home = os.path.expanduser("~")
    appdata = os.environ.get("AppData", "") or os.path.join(home, "AppData", "Roaming")
    local = os.environ.get("LocalAppData", "") or os.path.join(home, "AppData", "Local")
    roots = [os.path.join(home, "Documents", "WeChat Files", "Applet")]
    xwechat_users = os.path.join(appdata, "Tencent", "xwechat", "radium", "users")
    if os.path.isdir(xwechat_users):
        for d in os.listdir(xwechat_users):
            p = os.path.join(xwechat_users, d, "applet", "packages")
            if os.path.isdir(p):
                roots.append(p)
    roots.append(os.path.join(local, "Tencent", "WeChat", "WeChat Files", "Applet"))
    roots.append(os.path.join(appdata, "Tencent", "WeChat", "WeChat Files", "Applet"))
    seen, valid = set(), []
    for r in roots:
        rp = os.path.normcase(os.path.normpath(r))
        if rp not in seen and os.path.isdir(r):
            seen.add(rp)
            valid.append(r)
    return valid


# ---- jadx / java 探测 ----
def find_jadx(cfg=None):
    """返回 jadx.bat 路径（找不到返回 None）。"""
    cands = []
    if cfg:
        cands.append(cfg.get("jadx_path", ""))
    cands += [
        os.path.join(JADX_DIR, "bin", "jadx.bat"),
        os.path.join(JADX_DIR, "bin", "jadx"),
    ]
    for name in ("jadx.bat", "jadx"):
        w = shutil.which(name)
        if w:
            cands.append(w)
    cands += [
        r"C:\jadx\bin\jadx.bat",
        os.path.join(os.path.expanduser("~"), "jadx", "bin", "jadx.bat"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def find_java():
    """返回 (java_exe, java_home)，找不到返回 (None, None)。jadx 需要 JRE 11+。"""
    # JAVA_HOME
    jh = os.environ.get("JAVA_HOME", "")
    if jh:
        je = os.path.join(jh, "bin", "java.exe")
        if os.path.isfile(je):
            return je, jh
    # PATH
    w = shutil.which("java")
    if w:
        # 推断 java_home
        jh2 = os.path.dirname(os.path.dirname(w))
        return w, jh2
    # 常见安装位置
    home = os.path.expanduser("~")
    bases = [
        r"C:\Program Files\Java", r"C:\Program Files\Eclipse Adoptium",
        r"C:\Program Files\Microsoft\jdk", r"C:\Program Files\Zulu",
        r"C:\Program Files (x86)\Java",
        os.path.join(home, ".jdks"),
        os.path.join(os.environ.get("LocalAppData", ""), "Programs", "Eclipse Adoptium"),
    ]
    # 兼容 TRAE 自带 JRE
    trae_jre = os.path.join(os.environ.get("AppData", ""),
                            "TRAE SOLO CN", "ModularData", "ai-agent", "vm", "tools", "app", "jre")
    if os.path.isdir(trae_jre):
        bases.append(trae_jre)
    for b in bases:
        if not b or not os.path.isdir(b):
            continue
        for d in os.listdir(b):
            je = os.path.join(b, d, "bin", "java.exe")
            if os.path.isfile(je):
                return je, os.path.join(b, d)
            je2 = os.path.join(b, "bin", "java.exe")
            if os.path.isfile(je2):  # b 本身就是 jre
                return je2, b
    return None, None


# ---- Python .pyc 反编译器探测 ----
def find_pyc_decompiler():
    """按优先级探测 Python 字节码反编译器。返回 (名称, 命令列表) 或 (None, None)。"""
    # 1. pycdc (Decompyle++, 独立二进制, 支持最新 Python)
    for name in ("pycdc", "pycdc.exe"):
        w = shutil.which(name)
        if w:
            return ("pycdc", [w])
    local = os.path.join(BASE_DIR, "pycdc", "pycdc.exe")
    if os.path.isfile(local):
        return ("pycdc", [local])
    # 2. 脚本可执行文件: decompyle3 > uncompyle6
    for py in _candidate_pythons():
        if not (py and os.path.isfile(py)):
            continue
        scripts_dir = os.path.join(os.path.dirname(py), "Scripts")
        for mod in ("decompyle3", "uncompyle6"):
            for ext in (".exe", ""):
                exe = os.path.join(scripts_dir, mod + ext)
                if os.path.isfile(exe):
                    return (mod, [exe])
    # 3. Python 模块 API (通过 -c 调用 main)
    for py in _candidate_pythons():
        if not (py and os.path.isfile(py)):
            continue
        for mod in ("decompyle3", "uncompyle6"):
            try:
                r = subprocess.run([py, "-c", f"import {mod}"],
                                   capture_output=True, creationflags=NO_WIN, timeout=10)
                if r.returncode == 0:
                    return (mod, [py, "-c", f"from {mod}.main import main; main()"])
            except Exception:
                pass
    return (None, None)


def ensure_pyc_decompiler():
    """尝试 pip 安装 uncompyle6/decompyle3，返回 (名称, 命令列表)。"""
    name, cmd = find_pyc_decompiler()
    if name:
        return name, cmd
    for pkg in ("decompyle3", "uncompyle6"):
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", pkg],
                           capture_output=True, creationflags=NO_WIN, timeout=120)
        except Exception:
            pass
    return find_pyc_decompiler()


# ---- .NET 反编译器探测 (DLL/EXE → C#) ----
def find_ilspy():
    """探测 ilspycmd。返回 (名称, 命令列表) 或 (None, None)。"""
    w = shutil.which("ilspycmd")
    if w:
        return ("ilspycmd", [w])
    dotnet = shutil.which("dotnet")
    if dotnet:
        try:
            r = subprocess.run([dotnet, "tool", "list", "-g"],
                               capture_output=True, text=True, creationflags=NO_WIN, timeout=10)
            if "ilspycmd" in r.stdout:
                return ("ilspycmd", [dotnet, "ilspycmd"])
        except Exception:
            pass
    home = os.path.expanduser("~")
    for c in [os.path.join(home, ".dotnet", "tools", "ilspycmd.exe"),
              os.path.join(home, ".dotnet", "tools", "ilspycmd")]:
        if os.path.isfile(c):
            return ("ilspycmd", [c])
    return (None, None)


# ---- JS 美化器探测 ----
def find_js_beautifier():
    """探测 JS 美化工具。返回 (类型, 命令列表) 或 (None, None)。"""
    w = shutil.which("js-beautify")
    if w:
        return ("cli", [w])
    for py in _candidate_pythons():
        if not (py and os.path.isfile(py)):
            continue
        try:
            r = subprocess.run([py, "-c", "import jsbeautifier"],
                               capture_output=True, creationflags=NO_WIN, timeout=10)
            if r.returncode == 0:
                return ("python", [py])
        except Exception:
            pass
    return (None, None)


def ensure_js_beautifier():
    """尝试安装 JS 美化器。"""
    typ, cmd = find_js_beautifier()
    if typ:
        return typ, cmd
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "jsbeautifier"],
                       capture_output=True, creationflags=NO_WIN, timeout=120)
    except Exception:
        pass
    return find_js_beautifier()


# ---- PE .NET 程序集检测 ----
def is_dotnet_assembly(filepath):
    """检测 PE 文件是否为 .NET 程序集（含 CLR 头）。"""
    try:
        with open(filepath, "rb") as f:
            data = f.read(1024)
        if len(data) < 0x80 or data[:2] != b'MZ':
            return False
        pe_off = int.from_bytes(data[0x3C:0x40], "little")
        if pe_off + 6 > len(data) or data[pe_off:pe_off + 4] != b'PE\x00\x00':
            return False
        magic = int.from_bytes(data[pe_off + 24:pe_off + 26], "little")
        if magic == 0x10B:      # PE32
            dd_off = pe_off + 24 + 96
        elif magic == 0x20B:    # PE32+
            dd_off = pe_off + 24 + 112
        else:
            return False
        # CLR Runtime Header = 数据目录第 15 项 (索引 14), 每项 8 字节
        clr_off = dd_off + 14 * 8
        if clr_off + 8 > len(data):
            return False
        rva = int.from_bytes(data[clr_off:clr_off + 4], "little")
        return rva != 0
    except Exception:
        return False


# ---- 反编译质量评估 ----
def calculate_quality(out_dir, ftype="wxapp"):
    """扫描输出目录，计算五维质量指标（0-100）。"""
    metrics = {"代码完整度": 0, "资源提取率": 0, "结构保真度": 0, "可读性": 0, "元数据保留": 0}
    if not os.path.isdir(out_dir):
        return metrics

    src_exts = {
        "wxapp":   (".js", ".wxml", ".wxss", ".json", ".wxs"),
        "jadx":    (".java", ".kt"),
        "python":  (".py",),
        "dotnet":  (".cs",),
        "js":      (".js",),
        "web":     (".html", ".htm", ".js", ".css", ".json"),
    }
    res_exts = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".bmp", ".webp", ".avif",
                ".css", ".mp3", ".mp4", ".webm", ".wav", ".ogg", ".woff", ".woff2", ".ttf", ".eot", ".otf")
    meta_files = {
        "wxapp":  ("app.json", "project.config.json", "app-config.json", "sitemap.json"),
        "jadx":   ("AndroidManifest.xml", "resources.arsc", "apktool.yml"),
        "python": (),
        "dotnet": (),
        "js":     (),
        "web":    ("index.html", "favicon.ico", "robots.txt", "manifest.json", "sitemap.xml"),
    }
    exts = src_exts.get(ftype, (".js",))
    metas = meta_files.get(ftype, ())

    src_cnt = res_cnt = dir_cnt = meta_cnt = 0
    well_fmt = 0
    src_samples = []        # 采样文件列表（限制读取数量以提升性能）
    MAX_SAMPLES = 80        # 最多读取 80 个源文件进行格式质量评估

    for root, dirs, files in os.walk(out_dir):
        dir_cnt += len(dirs)
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            full = os.path.join(root, fn)
            if ext in exts:
                src_cnt += 1
                if len(src_samples) < MAX_SAMPLES:
                    src_samples.append(full)
            elif ext in res_exts:
                res_cnt += 1
            if fn in metas:
                meta_cnt += 1

    # 仅对采样的源文件检查格式质量（避免大项目读取全部文件）
    for full in src_samples:
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if lines:
                avg_len = sum(len(l.rstrip()) for l in lines) / len(lines)
                has_indent = any(l.startswith(("    ", "\t")) for l in lines if l.strip())
                if avg_len < 150 and has_indent:
                    well_fmt += 1
        except Exception:
            pass
    # 按采样比例推算总格式化率
    sampled = len(src_samples)

    if ftype == "web":
        metrics["代码完整度"] = min(100, src_cnt * 3)
        metrics["资源提取率"] = min(100, res_cnt * 3)
        metrics["结构保真度"] = min(100, dir_cnt * 5 + (20 if os.path.isfile(os.path.join(out_dir, "index.html")) else 0))
        metrics["可读性"] = min(100, int(well_fmt / sampled * 100)) if sampled > 0 else 0
        metrics["元数据保留"] = min(100, meta_cnt * 25)
    elif ftype == "js":
        metrics["代码完整度"] = 100 if src_cnt > 0 else 0
        metrics["资源提取率"] = min(100, res_cnt * 4)
        metrics["结构保真度"] = min(100, dir_cnt * 7)
        metrics["可读性"] = min(100, int(well_fmt / sampled * 100)) if sampled > 0 else 0
        metrics["元数据保留"] = min(100, meta_cnt * 30)
    else:
        metrics["代码完整度"] = min(100, src_cnt * 4)
        metrics["资源提取率"] = min(100, res_cnt * 4)
        metrics["结构保真度"] = min(100, dir_cnt * 7)
        metrics["可读性"] = min(100, int(well_fmt / sampled * 100)) if sampled > 0 else 0
        metrics["元数据保留"] = min(100, meta_cnt * 30)
    return metrics


def show_radar_chart(parent, title, metrics):
    """在弹窗中显示科技风五角能力图（雷达图），含动画与侧栏详情。"""
    try:
        import matplotlib
        matplotlib.use("TkAgg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        import numpy as np
        import matplotlib.patheffects as pe
    except ImportError:
        messagebox.showwarning("缺少依赖", "质量评估需要 matplotlib:\n  pip install matplotlib")
        return

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS"]
    plt.rcParams["axes.unicode_minus"] = False

    labels = list(metrics.keys())
    values = list(metrics.values())
    n = len(labels)
    angles = np.linspace(0, 2 * np.pi, n, endpoint=False).tolist()
    angles_c = angles + [angles[0]]

    # 色彩体系 — 浅灰底 + 白色线条
    BG     = "#e0e0e0"
    PANEL  = "#d4d4d4"
    ACCENT = "#4a9eff"
    GRID   = "#ffffff"
    GRID_H = "#ffffff"
    TEXT   = "#333333"
    DIM    = "#888888"

    def sc(v):
        if v >= 80: return "#4caf50"
        if v >= 60: return "#4a9eff"
        if v >= 40: return "#ff9800"
        return "#e53935"

    avg = sum(values) / len(values)
    ac  = sc(avg)

    # ---- 窗口 ----
    win = tk.Toplevel(parent)
    win.title(f"质量评估 — {title}")
    win.geometry("680x500")
    win.configure(bg=BG)
    win.transient(parent)

    header = tk.Frame(win, bg=BG)
    header.pack(fill="x", padx=16, pady=(12, 0))
    tk.Label(header, text="◆ 反编译质量评估", bg=BG, fg=ACCENT,
             font=("Microsoft YaHei", 14, "bold")).pack(side="left")
    tk.Label(header, text=title, bg=BG, fg=DIM,
             font=("Microsoft YaHei", 10)).pack(side="left", padx=(8, 0), pady=(4, 0))

    body = tk.Frame(win, bg=BG)
    body.pack(fill="both", expand=True, padx=16, pady=12)

    left = tk.Frame(body, bg=BG)
    left.pack(side="left", fill="both", expand=True)
    right = tk.Frame(body, bg=PANEL, width=210)
    right.pack(side="right", fill="y", padx=(12, 0))
    right.pack_propagate(False)

    # ---- matplotlib 雷达图 ----
    fig, ax = plt.subplots(figsize=(4.8, 4.6), subplot_kw=dict(polar=True))
    fig.patch.set_facecolor(BG)
    canvas = FigureCanvasTkAgg(fig, master=left)
    canvas.get_tk_widget().pack(fill="both", expand=True)

    # 标签偏移（5个顶点各自的角度位置）
    lbl_off = [(0, -16), (18, 8), (10, 22), (-10, 22), (-18, 8)]

    def draw(progress):
        ax.clear()
        ax.set_facecolor(BG)
        ax.set_ylim(0, 100)
        ax.set_yticks([20, 40, 60, 80, 100])
        ax.set_yticklabels([])
        ax.set_xticks(angles)
        ax.set_xticklabels([])
        ax.grid(color=GRID, linewidth=0.8, alpha=0.7)
        ax.spines['polar'].set_color(GRID_H)
        ax.spines['polar'].set_linewidth(1.5)
        for a in angles:
            ax.plot([a, a], [0, 100], color=GRID, lw=0.8, alpha=0.5, zorder=1)

        eased = 1 - (1 - progress) ** 3
        cv = [v * eased for v in values]
        cv_c = cv + [cv[0]]

        # 多层光晕（浅底上降低透明度）
        for alpha, ex in [(0.03, 10), (0.05, 6), (0.08, 3), (0.12, 0)]:
            exp = [min(100, v + ex) for v in cv_c]
            ax.fill(angles_c, exp, alpha=alpha, color=ACCENT, zorder=2)
        ax.fill(angles_c, cv_c, alpha=0.15, color=ACCENT, zorder=3)

        # 描边
        ax.plot(angles_c, cv_c, color=ACCENT, lw=6, alpha=0.08, zorder=4)
        ax.plot(angles_c, cv_c, color=ACCENT, lw=2.5, zorder=6)

        # 顶点
        for i, (a, v) in enumerate(zip(angles, cv)):
            if v < 1:
                continue
            c = sc(values[i])
            ax.scatter(a, v, color=c, s=240, alpha=0.08, zorder=7)
            ax.scatter(a, v, color=c, s=120, alpha=0.15, zorder=8)
            ax.scatter(a, v, color=c, s=50, zorder=9, edgecolors=BG, linewidth=1.5)
            ax.scatter(a, v, color="#ffffff", s=6, zorder=10, alpha=0.9)

        # 标签 + 分数
        for i, (a, v) in enumerate(zip(angles, cv)):
            if v < 5:
                continue
            c = sc(values[i])
            ox, oy = lbl_off[i]
            ax.annotate(labels[i], xy=(a, v), xytext=(ox, oy),
                        textcoords="offset points", ha="center", va="center",
                        color=TEXT, fontsize=9, fontweight="bold",
                        path_effects=[pe.withStroke(linewidth=2.5, foreground=BG)], zorder=12)
            ax.annotate(f"{int(values[i])}", xy=(a, v), xytext=(ox, oy + 14),
                        textcoords="offset points", ha="center", va="center",
                        color=c, fontsize=13, fontweight="bold",
                        path_effects=[pe.withStroke(linewidth=3, foreground=BG)], zorder=12)

        # 中心分数
        if progress >= 0.92:
            ax.text(0, 0, f"{avg:.0f}", ha="center", va="center",
                    color=ac, fontsize=22, fontweight="bold",
                    path_effects=[pe.withStroke(linewidth=5, foreground=BG)], zorder=13)

        canvas.draw_idle()

    # ---- 右侧面板 ----
    tk.Label(right, text="维度详情", bg=PANEL, fg=ACCENT,
             font=("Microsoft YaHei", 11, "bold")).pack(pady=(14, 10))
    for label, value in zip(labels, values):
        row = tk.Frame(right, bg=PANEL)
        row.pack(fill="x", padx=14, pady=5)
        c = sc(value)
        tk.Label(row, text=label, bg=PANEL, fg=TEXT,
                 font=("Microsoft YaHei", 9)).pack(anchor="w")
        bar_bg = tk.Frame(row, bg="#c0c0c0", height=6)
        bar_bg.pack(fill="x", pady=(3, 2))
        bar_fill = tk.Frame(bar_bg, bg=c, height=6)
        bar_fill.place(x=0, y=0, height=6, width=int(182 * value / 100))
        tk.Label(row, text=f"{int(value)}/100", bg=PANEL, fg=c,
                 font=("Consolas", 9, "bold")).pack(anchor="e")

    tk.Frame(right, bg="#bbbbbb", height=1).pack(fill="x", padx=14, pady=10)
    if avg >= 80:   stars, rtxt = "★★★★★", "优秀"
    elif avg >= 60: stars, rtxt = "★★★★☆", "良好"
    elif avg >= 40: stars, rtxt = "★★★☆☆", "一般"
    else:           stars, rtxt = "★★☆☆☆", "较差"
    tk.Label(right, text=stars, bg=PANEL, fg=ac,
             font=("Microsoft YaHei", 16)).pack()
    tk.Label(right, text=rtxt, bg=PANEL, fg=ac,
             font=("Microsoft YaHei", 12, "bold")).pack(pady=(2, 0))

    # ---- 动画 ----
    win.attributes("-alpha", 0.0)
    step = [0]
    STEPS = 18

    def anim():
        if step[0] <= STEPS:
            draw(step[0] / STEPS)
            win.attributes("-alpha", min(1.0, step[0] / STEPS * 1.2))
            step[0] += 1
            win.after(22, anim)
        else:
            draw(1.0)
            win.attributes("-alpha", 1.0)

    win.after(60, anim)

    def on_close():
        plt.close(fig)
        win.destroy()
    win.protocol("WM_DELETE_WINDOW", on_close)


# --------------------------------------------------------------------------- #
# 基础标签页：日志/线程/子进程等共享能力
# --------------------------------------------------------------------------- #
class BaseTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.cfg = app.cfg
        self.log_queue = queue.Queue()
        self.worker = None
        self.after(80, self._drain_log)

    # ---- 日志 ----
    def log(self, msg, tag="info"):
        self.log_queue.put((str(msg) if msg is not None else "", tag))

    def _drain_log(self):
        if not hasattr(self, "log_text"):
            self.after(80, self._drain_log)
            return
        try:
            while True:
                msg, tag = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n", tag)
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        except Exception:
            pass  # 控件可能在关闭时被销毁
        try:
            self.after(80, self._drain_log)
        except Exception:
            pass  # 应用可能在关闭中

    def make_log_widget(self, parent):
        f = ttk.Frame(parent)
        f.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            f, wrap="word", bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="#d4d4d4", font=("Consolas", 9), relief="flat", padx=6, pady=4,
        )
        for t, c in (("info", "#d4d4d4"), ("ok", "#6a9955"), ("warn", "#d7ba7d"),
                     ("err", "#f48771"), ("step", "#569cd6")):
            self.log_text.tag_configure(t, foreground=c)
        sb = ttk.Scrollbar(f, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set, state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return f

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ---- 子进程 ----
    def run_cmd(self, cmd, cwd=None, env=None):
        self.log("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "info")
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1,
                text=True, encoding="utf-8", errors="replace", cwd=cwd, env=env,
                creationflags=NO_WIN,
            )
        except Exception as e:
            self.log(f"  [错误] 启动子进程失败: {e}", "err")
            return -1
        try:
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if line:
                    self.log("  " + line)
            proc.wait()
            return proc.returncode
        finally:
            proc.stdout.close()

    # ---- 线程 ----
    def start_worker(self, target, *args):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "当前有任务正在运行，请等待完成。")
            return False
        self.set_busy(True)
        self.worker = threading.Thread(target=self._worker_wrap, args=(target, args), daemon=True)
        self.worker.start()
        return True

    def _worker_wrap(self, target, args):
        try:
            target(*args)
        except Exception as e:
            self.log(f"[异常] 任务中断: {e}", "err")
            import traceback
            self.log(traceback.format_exc(), "err")
        finally:
            self.after(0, lambda: self.set_busy(False))

    def set_busy(self, busy):
        """子类重写：控制各按钮的启用/禁用。"""
        pass


# --------------------------------------------------------------------------- #
# 标签页 1：小程序反编译
# --------------------------------------------------------------------------- #
class WxappTab(BaseTab):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.scanned_files = []
        self.last_output_dir = None
        self.last_metrics = None
        self.last_quality_title = ""
        self._build()
        self.after(300, self._env_check)

    def _build(self):
        top = ttk.LabelFrame(self, text="选择 wxapkg 文件")
        top.pack(fill="x", padx=8, pady=6)
        r = ttk.Frame(top); r.pack(fill="x", padx=8, pady=6)
        ttk.Label(r, text="文件路径:").pack(side="left")
        self.file_var = tk.StringVar()
        ttk.Entry(r, textvariable=self.file_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r, text="浏览…", width=8, command=self.browse_file).pack(side="left")
        ttk.Button(r, text="扫描缓存", width=10, command=self.scan_cache).pack(side="left", padx=(4, 0))

        scan = ttk.LabelFrame(self, text="扫描结果（双击一行载入到文件路径）")
        scan.pack(fill="x", padx=8, pady=4)
        self.tree = ttk.Treeview(scan, columns=("appid", "name", "ptype", "enc", "size", "time"),
                                 show="headings", height=4)
        for col, txt, w, an in (("appid", "小程序 AppID", 150, "w"),
                                ("name", "文件名", 170, "w"),
                                ("ptype", "类型", 60, "center"),
                                ("enc", "加密", 50, "center"),
                                ("size", "大小", 70, "e"),
                                ("time", "修改时间", 130, "w")):
            self.tree.heading(col, text=txt)
            self.tree.column(col, width=w, anchor=an)
        self.tree.tag_configure("recommended", background="#c8e6c9")
        sb = ttk.Scrollbar(scan, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
        sb.pack(side="right", fill="y", pady=6, padx=(0, 8))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_tree_select())
        self.tree.bind("<Double-1>", lambda e: self._on_tree_select())

        bottom = ttk.Frame(self); bottom.pack(side="bottom", fill="x", padx=8, pady=(4, 8))
        self.start_btn = ttk.Button(bottom, text="▶  开始反编译", command=self.start_decompile)
        self.start_btn.pack(side="left", fill="x", expand=True, ipady=8)
        self.quality_btn = ttk.Button(bottom, text="质量评估", state="disabled", command=self.show_quality)
        self.quality_btn.pack(side="left", padx=(8, 0), ipady=8)
        self.open_dir_btn = ttk.Button(bottom, text="打开输出目录", state="disabled", command=self.open_output_dir)
        self.open_dir_btn.pack(side="left", padx=(8, 0), ipady=8)
        self.devtools_btn = ttk.Button(bottom, text="用开发者工具打开", state="disabled", command=self.open_in_devtools)
        self.devtools_btn.pack(side="left", padx=(8, 0), ipady=8)

        ttk.Label(self, text="运行日志").pack(anchor="w", padx=8)
        self.make_log_widget(self)

    def set_busy(self, busy):
        self.start_btn.configure(state="disabled" if busy else "normal")
        if busy:
            self.open_dir_btn.configure(state="disabled")
            self.devtools_btn.configure(state="disabled")
            self.quality_btn.configure(state="disabled")

    def _env_check(self):
        self.log("===== 环境自检 =====", "step")
        self.log(f"  GUI 解释器: {sys.executable}", "info")
        dpy = resolve_decrypt_python()
        self.log(f"  解密解释器: {dpy}", "info")
        problems = []
        if dpy == sys.executable and not _python_has_crypto(sys.executable):
            problems.append("未检测到 pycryptodome，解密将失败: pip install pycryptodome")
        try:
            node_bin = _get_node()
            r = subprocess.run([node_bin, "--version"], capture_output=True, text=True,
                               creationflags=NO_WIN, timeout=8)
            if r.returncode != 0:
                raise RuntimeError()
            self.log(f"  Node.js: {r.stdout.strip()} ({node_bin})", "info")
        except Exception:
            problems.append("未检测到 Node.js，反编译将失败，请安装 Node.js 并加入 PATH。")
        if not os.path.isfile(DECRYPT_SCRIPT):
            problems.append(f"未找到解密脚本: {DECRYPT_SCRIPT}")
        if not os.path.isfile(UNPACK_SCRIPT):
            problems.append(f"未找到反编译脚本: {UNPACK_SCRIPT}")
        if not os.path.isfile(RESTORE_WXML_SCRIPT):
            problems.append(f"未找到 WXML 恢复脚本: {RESTORE_WXML_SCRIPT}")
        if problems:
            for p in problems:
                self.log("  [警告] " + p, "warn")
        else:
            self.log("  小程序反编译环境就绪。", "ok")

    # ---- 文件选择 / 扫描 ----
    def browse_file(self):
        roots = _cache_scan_roots()
        path = filedialog.askopenfilename(
            title="选择 wxapkg 文件",
            filetypes=[("微信小程序包", "*.wxapkg"), ("所有文件", "*.*")],
            initialdir=roots[0] if roots else os.path.expanduser("~"))
        if path:
            self.file_var.set(path)

    def scan_cache(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.scanned_files = []
        roots = _cache_scan_roots()
        if not roots:
            messagebox.showwarning("未找到缓存", "未在常见位置找到微信缓存目录，可点“浏览…”手动定位。")
            return
        self.log("===== 扫描微信缓存 =====", "step")
        for r in roots:
            self.log(f"  扫描: {r}", "info")
        rows = []
        for root_dir in roots:
            for dp, _d, files in os.walk(root_dir):
                has_app = any(f.lower() == "__app__.wxapkg" for f in files)
                for fn in files:
                    if not fn.lower().endswith(".wxapkg"):
                        continue
                    full = os.path.join(dp, fn)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    m = WXID_PATTERN.search(full.replace("\\", "/"))
                    appid = m.group(0) if m else ""
                    # 检测加密状态
                    try:
                        with open(full, "rb") as f:
                            head = f.read(6)
                        encrypted = "是" if head[:6] == ENCRYPT_FLAG else "否"
                    except Exception:
                        encrypted = "?"
                    # 检测包类型
                    if fn.lower() == "__app__.wxapkg":
                        ptype = "主包"
                    elif not appid:
                        ptype = "公共库"
                    elif has_app:
                        ptype = "分包"
                    else:
                        ptype = "主包*"
                    rows.append((appid or "（公共库/未知）", fn, ptype, encrypted,
                                 st.st_size, st.st_mtime, full))
        rows.sort(key=lambda x: x[5], reverse=True)

        # 推荐: 每个 AppID 最近的主包
        recommended_set, seen_appids = set(), set()
        for appid, _fn, ptype, _enc, _sz, _mt, full in rows:
            if ptype == "主包" and appid not in seen_appids:
                recommended_set.add(full)
                seen_appids.add(appid)

        rec_cnt = 0
        for appid, fn, ptype, enc, sz, mt, full in rows:
            tags = ("recommended",) if full in recommended_set else ()
            self.tree.insert("", "end",
                             values=(appid, fn, ptype, enc, human_size(sz),
                                     time.strftime("%Y-%m-%d %H:%M", time.localtime(mt))),
                             tags=tags)
            self.scanned_files.append(full)
            if full in recommended_set:
                rec_cnt += 1

        self.log(f"  共找到 {len(rows)} 个 wxapkg 文件（按时间倒序）。", "ok" if rows else "warn")
        if rec_cnt:
            self.log(f"  ★ 已标记 {rec_cnt} 个主包为推荐目标（浅绿色高亮行）。", "ok")
            self.log("  提示: 主包(__APP__.wxapkg)含完整源码，建议优先反编译。", "info")
            self.log("  分包会自动合并到主包反编译，公共库无需单独反编译。", "info")

    def _on_tree_select(self):
        sel = self.tree.selection()
        if not sel or not self.scanned_files:
            return
        idx = self.tree.index(sel[0])
        if 0 <= idx < len(self.scanned_files):
            self.file_var.set(self.scanned_files[idx])

    # ---- 主流程 ----
    def start_decompile(self):
        fp = self.file_var.get().strip().strip('"')
        if not fp:
            messagebox.showwarning("提示", "请先选择或扫描一个 wxapkg 文件。")
            return
        if not os.path.isfile(fp):
            messagebox.showerror("文件不存在", f"文件不存在:\n{fp}")
            return
        self.last_output_dir = None
        self.open_dir_btn.configure(state="disabled")
        self.devtools_btn.configure(state="disabled")
        self.quality_btn.configure(state="disabled")
        self.clear_log()
        self.start_worker(self._pipeline, fp)

    @staticmethod
    def _extract_appid(path):
        m = WXID_PATTERN.search(path.replace("\\", "/"))
        return m.group(0) if m else None

    @staticmethod
    def _detect_encryption(path):
        with open(path, "rb") as f:
            head = f.read(6)
        if head[:6] == ENCRYPT_FLAG:
            return "encrypted", "检测到 V1MMWX 头，文件已加密，需要解密。"
        if head[:1] == b'\xBE':
            return "plain", "检测到 0xBE 头，文件未加密，可直接反编译。"
        return "unknown", f"未知的文件头: {head!r}，按未加密处理。"

    @staticmethod
    def _sanitize_name(name):
        name = re.sub(r'[\\/:*?"<>|]', "_", name).strip().strip(" .")
        return name if name else "未命名小程序"

    @staticmethod
    def _unique_dir(parent, name):
        cand = os.path.join(parent, name)
        if not os.path.exists(cand):
            return cand
        i = 2
        while i < 10000:
            c = os.path.join(parent, f"{name}_{i}")
            if not os.path.exists(c):
                return c
            i += 1
        # 极端情况：用时间戳保证唯一
        import time as _t
        return os.path.join(parent, f"{name}_{int(_t.time())}")

    def _try_json(self, fp, *keys):
        try:
            if not os.path.isfile(fp):
                return None
            with open(fp, "r", encoding="utf-8-sig") as f:
                cur = json.load(f)
            for k in keys:
                cur = cur.get(k) if isinstance(cur, dict) else None
            if isinstance(cur, str):
                cur = cur.strip()
            return cur or None
        except Exception:
            return None

    def _extract_name(self, outdir, appid):
        for fp, keys in [
            (os.path.join(outdir, "project.config.json"), ("projectname",)),
            (os.path.join(outdir, "app-config.json"), ("global", "window", "navigationBarTitleText")),
            (os.path.join(outdir, "app.json"), ("window", "navigationBarTitleText")),
        ]:
            n = self._try_json(fp, *keys)
            if n:
                self.log(f"  名称来源 {os.path.basename(fp)}: {n}", "info")
                return n
        return appid or "未命名小程序"

    def _copy_tree(self, src, dst):
        os.makedirs(dst, exist_ok=True)
        ok, bad = 0, 0
        for root, _d, files in os.walk(src):
            rel = os.path.relpath(root, src)
            tgt = dst if rel == "." else os.path.join(dst, rel)
            os.makedirs(tgt, exist_ok=True)
            for fn in files:
                try:
                    shutil.copy2(os.path.join(root, fn), os.path.join(tgt, fn)); ok += 1
                except Exception as e:
                    bad += 1
                    self.log(f"  跳过: {fn} — {e}", "warn")
        self.log(f"  复制完成: 成功 {ok}，跳过 {bad}。", "info")

    def _sanitize_app_json(self, out_dir):
        """清理 app.json：修复分包页面归属、移除非法字段、转换 iconData→文件。"""
        app_json = os.path.join(out_dir, "app.json")
        if not os.path.isfile(app_json):
            return
        try:
            with open(app_json, "r", encoding="utf-8-sig") as f:
                app = json.load(f)
        except Exception:
            return

        changed = False

        # ---- 统一工具函数 ----
        def _page_exists(p):
            """检查页面路径在磁盘上是否存在四件套文件。"""
            p = p.strip("/")
            for ext in (".js", ".wxml", ".wxss", ".json"):
                if os.path.isfile(os.path.join(out_dir, p + ext)):
                    return True
            return False

        def _fix_dotted_path(page, root_prefix=""):
            """修复页面路径中任意段落的非法点号。
            例: pages/index.index/index → pages/index/index/index
            """
            page = page.strip("/")
            if "." not in page:
                return page
            full = (root_prefix + page) if root_prefix else page
            if _page_exists(full):
                return page  # 原路径存在，无需修复

            parts = page.split("/")
            # 策略1: 所有含点号的段都拆分
            new_parts = []
            for part in parts:
                if "." in part:
                    new_parts.extend(part.split("."))
                else:
                    new_parts.append(part)
            candidate = "/".join(new_parts)
            full_c = (root_prefix + candidate) if root_prefix else candidate
            if _page_exists(full_c):
                return candidate

            # 策略2: 仅最后一段拆分前两段
            last = parts[-1]
            if "." in last:
                segs = last.split(".")
                if len(segs) >= 2:
                    c2 = "/".join(parts[:-1] + [segs[0], segs[1]])
                    full_c2 = (root_prefix + c2) if root_prefix else c2
                    if _page_exists(full_c2):
                        return c2
                    # 策略3: 最后一段全部拆分
                    if len(segs) > 2:
                        c3 = "/".join(parts[:-1] + segs)
                        full_c3 = (root_prefix + c3) if root_prefix else c3
                        if _page_exists(full_c3):
                            return c3
                    # 回退到策略1结果
                    return candidate
            return candidate

        # ---- 1. 修复分包结构 ----
        sub_pkgs = app.get("subPackages", app.get("subpackages", []))
        if sub_pkgs:
            main_pages = app.get("pages", [])
            moved = 0
            for sp in sub_pkgs:
                root = sp.get("root", "").strip("/")
                if not root:
                    continue
                if "pages" not in sp or not isinstance(sp["pages"], list):
                    sp["pages"] = []
                root_prefix = root + "/"
                new_main = []
                for page in main_pages:
                    page = page.strip("/")
                    if page.startswith(root_prefix):
                        sub_page = page[len(root_prefix):]
                        if sub_page not in sp["pages"]:
                            sp["pages"].append(sub_page)
                        moved += 1
                    else:
                        new_main.append(page)
                main_pages = new_main
                # 移除 plugins 中的非法 subpackage 字段
                plugins = sp.get("plugins", {})
                if isinstance(plugins, dict):
                    for pname, pconf in plugins.items():
                        if isinstance(pconf, dict) and "subpackage" in pconf:
                            del pconf["subpackage"]
                            changed = True
                            self.log(f"  移除分包 {root} plugins.{pname} 非法字段 subpackage", "info")
            if moved > 0:
                app["pages"] = main_pages
                if "subpackages" in app:
                    app["subPackages"] = app.pop("subpackages")
                changed = True
                self.log(f"  分包修复: {moved} 个页面从主包移入对应分包", "ok")
                for sp in app.get("subPackages", []):
                    self.log(f"    分包 {sp.get('root', '')}: {len(sp.get('pages', []))} 页面", "info")

        # ---- 2. 清理 tabBar 非法字段 ----
        tabbar = app.get("tabBar")
        if isinstance(tabbar, dict):
            # 移除 tabBar 顶层非法字段
            invalid_tab_keys = {"fontSize", "iconWidth", "spacing", "height"}
            for key in list(tabbar.keys()):
                if key in invalid_tab_keys:
                    del tabbar[key]
                    changed = True
                    self.log(f"  移除 tabBar 非法字段: {key}", "info")

            # 转换 iconData/selectedIconData → iconPath/selectedIconPath
            import base64 as _b64
            import hashlib as _hl
            for item in tabbar.get("list", []):
                for data_key, path_key in [("iconData", "iconPath"),
                                           ("selectedIconData", "selectedIconPath")]:
                    if data_key in item:
                        raw = item.pop(data_key)
                        changed = True
                        if raw and path_key not in item:
                            try:
                                icon_bytes = _b64.b64decode(raw)
                                # 检测图片格式
                                if icon_bytes[:3] == b'\xff\xd8\xff':
                                    ext = ".jpg"
                                elif icon_bytes[:4] == b'GIF8':
                                    ext = ".gif"
                                elif icon_bytes[:4] == b'\x89PNG':
                                    ext = ".png"
                                else:
                                    ext = ".png"
                                hname = _hl.md5(icon_bytes).hexdigest()[:10]
                                icon_dir = os.path.join(out_dir, "assets", "tabbar")
                                os.makedirs(icon_dir, exist_ok=True)
                                icon_file = os.path.join(icon_dir, f"{hname}{ext}")
                                if not os.path.isfile(icon_file):
                                    with open(icon_file, "wb") as f:
                                        f.write(icon_bytes)
                                rel = os.path.relpath(icon_file, out_dir).replace("\\", "/")
                                item[path_key] = rel
                                self.log(f"  tabBar {data_key} → {rel}", "ok")
                            except Exception as e:
                                # 转换失败，用占位图标
                                icon_dir = os.path.join(out_dir, "assets", "tabbar")
                                os.makedirs(icon_dir, exist_ok=True)
                                ph = os.path.join(icon_dir, "placeholder.png")
                                if not os.path.isfile(ph):
                                    with open(ph, "wb") as f:
                                        f.write(_PNG1x1)
                                item[path_key] = "assets/tabbar/placeholder.png"
                                self.log(f"  [警告] {data_key} 转换失败，使用占位图标", "warn")

            # 修复 tabBar.list[].pagePath 中的非法点号
            for idx, item in enumerate(tabbar.get("list", [])):
                pp = item.get("pagePath", "")
                if not pp:
                    continue
                original_pp = pp
                # 去掉文件扩展名 (.wxml/.js/.json/.wxss 等)
                for ext in (".wxml", ".js", ".json", ".wxss", ".html"):
                    if pp.endswith(ext):
                        pp = pp[:-len(ext)]
                        break
                # 修复任意段中的非法点号
                pp = _fix_dotted_path(pp)
                if pp != original_pp:
                    item["pagePath"] = pp
                    changed = True
                    self.log(f"  tabBar pagePath 修复: {original_pp} → {pp}", "ok")

        # ---- 3. 清理 pages 数组中的非法点号 ----
        pages = app.get("pages", [])
        if isinstance(pages, list):
            new_pages = []
            page_fixed = False
            for page in pages:
                original = page
                page = page.strip("/")
                # 去掉文件扩展名
                for ext in (".wxml", ".js", ".json", ".wxss", ".html"):
                    if page.endswith(ext):
                        page = page[:-len(ext)]
                        break
                # 修复任意段中的非法点号
                page = _fix_dotted_path(page)
                if page != original:
                    page_fixed = True
                if page and page not in new_pages:
                    new_pages.append(page)
            if page_fixed:
                app["pages"] = new_pages
                changed = True
                self.log(f"  pages 数组点号修复: {len(new_pages)} 个页面", "ok")

        # ---- 4. 清理 subPackages pages 中的非法点号 ----
        for sp in app.get("subPackages", app.get("subpackages", [])):
            sp_root = sp.get("root", "").strip("/")
            sp_pages = sp.get("pages", [])
            if not isinstance(sp_pages, list):
                continue
            new_sp_pages = []
            sp_fixed = False
            for page in sp_pages:
                original = page
                page = page.strip("/")
                for ext in (".wxml", ".js", ".json", ".wxss", ".html"):
                    if page.endswith(ext):
                        page = page[:-len(ext)]
                        break
                # 修复任意段中的非法点号（含 root 前缀检查）
                page = _fix_dotted_path(page, root_prefix=(sp_root + "/") if sp_root else "")
                if page != original:
                    sp_fixed = True
                if page and page not in new_sp_pages:
                    new_sp_pages.append(page)
            if sp_fixed:
                sp["pages"] = new_sp_pages
                changed = True
                self.log(f"  分包 {sp.get('root', '')} pages 点号修复", "ok")

        # ---- 4b. 校验 tabBar pagePath（在 pages/subPackages 修复后执行）----
        tabbar = app.get("tabBar")
        if isinstance(tabbar, dict):
            valid_pages = set(app.get("pages", []))
            for sp in app.get("subPackages", app.get("subpackages", [])):
                root = sp.get("root", "").strip("/")
                for p in sp.get("pages", []):
                    valid_pages.add((root + "/" + p) if root else p)
            for item in tabbar.get("list", []):
                pp = item.get("pagePath", "")
                if not pp or pp in valid_pages:
                    continue
                # 模糊匹配：在 valid_pages 中找末段相同的路径
                pp_last = pp.split("/")[-1]
                matched = None
                for vp in valid_pages:
                    if vp.split("/")[-1] == pp_last:
                        matched = vp
                        break
                if matched and matched != pp:
                    item["pagePath"] = matched
                    changed = True
                    self.log(f"  tabBar pagePath 匹配修复: {pp} → {matched}", "ok")

        # ---- 5. 校验 entryPagePath ----
        entry = app.get("entryPagePath", "")
        if entry:
            entry = entry.strip("/")
            # 去除扩展名
            for ext in (".wxml", ".js", ".json", ".wxss", ".html"):
                if entry.endswith(ext):
                    entry = entry[:-len(ext)]
                    break
            # 修复任意段中的非法点号
            entry = _fix_dotted_path(entry)
            # 校验是否存在
            all_valid = set(app.get("pages", []))
            for sp in app.get("subPackages", app.get("subpackages", [])):
                root = sp.get("root", "").strip("/")
                for p in sp.get("pages", []):
                    all_valid.add((root + "/" + p) if root else p)
            if entry not in all_valid:
                # 尝试模糊匹配
                entry_last = entry.split("/")[-1]
                matched = None
                for vp in all_valid:
                    if vp.split("/")[-1] == entry_last:
                        matched = vp
                        break
                if matched:
                    app["entryPagePath"] = matched
                    changed = True
                    self.log(f"  entryPagePath 匹配修复: {entry} → {matched}", "ok")
                else:
                    # 使用第一个页面作为 entryPagePath
                    first_page = app.get("pages", [""])[0] if app.get("pages") else ""
                    if first_page:
                        app["entryPagePath"] = first_page
                        changed = True
                        self.log(f"  entryPagePath 不存在，设为首页: {first_page}", "ok")
            elif entry != app.get("entryPagePath", ""):
                app["entryPagePath"] = entry
                changed = True
                self.log(f"  entryPagePath 规范化: {entry}", "ok")

        # ---- 6. 修复全局 usingComponents 中的非法点号路径 ----
        uc = app.get("usingComponents", {})
        if isinstance(uc, dict):
            uc_fixed = 0
            for cname, cpath in list(uc.items()):
                if "." in cpath and cpath.startswith(("/", ".")):
                    is_abs = cpath.startswith("/")
                    fixed = _fix_dotted_path(cpath)
                    if is_abs:
                        fixed = "/" + fixed
                    if fixed != cpath:
                        uc[cname] = fixed
                        uc_fixed += 1
            if uc_fixed:
                changed = True
                self.log(f"  全局 usingComponents 路径修复 {uc_fixed} 个", "ok")

        if changed:
            with open(app_json, "w", encoding="utf-8") as f:
                json.dump(app, f, ensure_ascii=False, indent=4)
            self.log("  app.json 清理完成", "ok")

    def _post_process(self, out_dir, appid):
        """反编译后处理：修复缺失的关键文件，确保可用微信开发者工具预览。"""
        fixes = []

        # 1. app.json — 从 app-config.json 生成或创建最小版本
        app_json = os.path.join(out_dir, "app.json")
        if not os.path.isfile(app_json):
            app_cfg = os.path.join(out_dir, "app-config.json")
            if os.path.isfile(app_cfg):
                try:
                    with open(app_cfg, "r", encoding="utf-8-sig") as f:
                        cfg = json.load(f)
                    app = {
                        "pages": cfg.get("pages", []),
                        "window": (cfg.get("global") or {}).get("window", {}),
                    }
                    for key in ("tabBar", "subPackages", "networkTimeout",
                                "navigateToMiniProgramAppIdList"):
                        if key in cfg:
                            app[key] = cfg[key]
                    with open(app_json, "w", encoding="utf-8") as f:
                        json.dump(app, f, ensure_ascii=False, indent=4)
                    fixes.append("从 app-config.json 生成 app.json")
                except Exception as e:
                    self.log(f"  [警告] 生成 app.json 失败: {e}", "warn")
            else:
                # 从目录结构推断页面列表
                pages = []
                for root, _d, files in os.walk(out_dir):
                    for fn in files:
                        if fn.endswith(".wxml"):
                            rel = os.path.relpath(os.path.join(root, fn), out_dir)
                            pages.append(rel.replace("\\", "/").replace(".wxml", ""))
                if not pages:
                    pages = ["pages/index/index"]
                app = {
                    "pages": pages,
                    "window": {
                        "navigationBarTitleText": appid or "小程序",
                        "navigationBarBackgroundColor": "#000000",
                        "navigationBarTextStyle": "white",
                    },
                }
                with open(app_json, "w", encoding="utf-8") as f:
                    json.dump(app, f, ensure_ascii=False, indent=4)
                fixes.append(f"创建最小 app.json（推断 {len(pages)} 个页面）")

        # 1b. 清理 app.json（修复分包、移除非法字段、转换 iconData）
        self._sanitize_app_json(out_dir)

        # 2. project.config.json
        proj_cfg = os.path.join(out_dir, "project.config.json")
        if not os.path.isfile(proj_cfg):
            proj = {
                "description": "反编译生成",
                "packOptions": {"ignore": [], "include": []},
                "setting": {"urlCheck": False, "es6": True, "postcss": True,
                            "minified": True, "newFeature": False},
                "compileType": "miniProgram",
                "libVersion": "3.3.4",
                "appid": appid or "wx0000000000000000",
                "projectname": "",
                "condition": {},
            }
            with open(proj_cfg, "w", encoding="utf-8") as f:
                json.dump(proj, f, ensure_ascii=False, indent=4)
            fixes.append("创建 project.config.json")

        # 3. app.js
        app_js = os.path.join(out_dir, "app.js")
        if not os.path.isfile(app_js):
            with open(app_js, "w", encoding="utf-8") as f:
                f.write("// app.js\nApp({\n  onLaunch() {},\n  globalData: {}\n})\n")
            fixes.append("创建 app.js")

        # 4. app.wxss
        app_wxss = os.path.join(out_dir, "app.wxss")
        if not os.path.isfile(app_wxss):
            with open(app_wxss, "w", encoding="utf-8") as f:
                f.write("/* app.wxss */\n")
            fixes.append("创建 app.wxss")

        # 5. sitemap.json
        sitemap = os.path.join(out_dir, "sitemap.json")
        if not os.path.isfile(sitemap):
            with open(sitemap, "w", encoding="utf-8") as f:
                json.dump({"desc": "https://developers.weixin.qq.com/miniprogram/dev/framework/sitemap.html",
                           "rules": [{"action": "allow", "page": "*"}]},
                          f, ensure_ascii=False, indent=4)
            fixes.append("创建 sitemap.json")

        if fixes:
            self.log("  后处理修复:", "step")
            for fix in fixes:
                self.log(f"    + {fix}", "ok")

        # 统计输出文件类型
        file_types = {}
        for root, _d, files in os.walk(out_dir):
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                file_types[ext] = file_types.get(ext, 0) + 1
        type_str = "  ".join(f"{ext}={cnt}" for ext, cnt in
                             sorted(file_types.items(), key=lambda x: -x[1])[:8])
        total = sum(file_types.values())
        self.log(f"  输出统计: {total} 文件  {type_str}", "info")

        # 关键文件检查
        key_files = {"app.json": app_json, "app.js": app_js,
                     "app.wxss": app_wxss, "project.config.json": proj_cfg}
        missing = [n for n, p in key_files.items() if not os.path.isfile(p)]
        if missing:
            self.log(f"  [警告] 仍缺失: {', '.join(missing)}", "warn")
        else:
            self.log("  关键文件齐全，可用微信开发者工具打开预览。", "ok")

    def _deep_verify(self, out_dir, appid):
        """深度校验：检查所有页面/组件/资源文件完整性，自动补全缺失文件。"""
        fixes = []
        changed = False  # app.json 是否被修改

        # 读取 app.json
        app_json_path = os.path.join(out_dir, "app.json")
        if not os.path.isfile(app_json_path):
            return 0, 0
        try:
            with open(app_json_path, "r", encoding="utf-8-sig") as f:
                app_cfg = json.load(f)
        except Exception:
            return 0, 0

        all_pages = list(app_cfg.get("pages", []))

        # 收集分包页面
        for sp in app_cfg.get("subPackages", app_cfg.get("subpackages", [])):
            root = sp.get("root", "").strip("/")
            for pg in sp.get("pages", []):
                all_pages.append(f"{root}/{pg}" if root else pg)

        # 1. 检查每个页面的四件套（.js/.wxml/.wxss/.json）
        page_fixes = 0
        for page in all_pages:
            page = page.strip("/")
            for ext, tmpl in [
                (".js",   '// {n}\nPage({{\n  data: {{}},\n  onLoad() {{}},\n}})\n'),
                (".wxml", '<!--{n}-->\n<view class="container">\n  <text>{n}</text>\n</view>\n'),
                (".wxss", '/* {n} */\n.container {{\n  padding: 20rpx;\n}}\n'),
                (".json", '{{\n  "usingComponents": {{}}\n}}\n'),
            ]:
                fp = os.path.join(out_dir, page + ext)
                if not os.path.isfile(fp):
                    os.makedirs(os.path.dirname(fp), exist_ok=True)
                    with open(fp, "w", encoding="utf-8") as f:
                        f.write(tmpl.format(n=page))
                    page_fixes += 1
        if page_fixes:
            fixes.append(f"补全页面文件 {page_fixes} 个")

        # 2. 检查 usingComponents 引用的组件文件（递归检查嵌套组件）
        comp_fixes = 0
        checked = set()

        def _check_component_files(cpath, base_json_dir):
            """递归检查组件文件完整性，包括嵌套引用的子组件。"""
            if cpath in checked:
                return 0
            checked.add(cpath)
            if cpath.startswith("/"):
                comp_base = os.path.join(out_dir, cpath.lstrip("/"))
            else:
                comp_base = os.path.join(base_json_dir, cpath)
            fixes = 0
            for ext, tmpl in [
                (".js",   '// Component: {n}\nComponent({{\n  properties: {{}},\n  data: {{}},\n  methods: {{}},\n}})\n'),
                (".wxml", '<!--{n}-->\n<view class="component">\n  <slot></slot>\n</view>\n'),
                (".wxss", '/* {n} */\n'),
                (".json", '{{\n  "component": true,\n  "usingComponents": {{}}\n}}\n'),
            ]:
                fp = comp_base + ext
                if not os.path.isfile(fp):
                    os.makedirs(os.path.dirname(fp), exist_ok=True)
                    with open(fp, "w", encoding="utf-8") as f:
                        f.write(tmpl.format(n=os.path.basename(comp_base)))
                    fixes += 1
            # 递归检查该组件自身引用的子组件
            cjson = comp_base + ".json"
            if os.path.isfile(cjson):
                try:
                    with open(cjson, "r", encoding="utf-8-sig") as f:
                        ccfg = json.load(f)
                    for _n, nested_cpath in ccfg.get("usingComponents", {}).items():
                        fixes += _check_component_files(nested_cpath, os.path.dirname(cjson))
                except Exception:
                    pass
            return fixes

        for page in all_pages:
            page = page.strip("/")
            pjson = os.path.join(out_dir, page + ".json")
            if not os.path.isfile(pjson):
                continue
            try:
                with open(pjson, "r", encoding="utf-8-sig") as f:
                    pcfg = json.load(f)
            except Exception:
                continue
            for cname, cpath in pcfg.get("usingComponents", {}).items():
                comp_fixes += _check_component_files(cpath, os.path.dirname(pjson))
        # 检查 app.json 中的全局 usingComponents
        for cname, cpath in app_cfg.get("usingComponents", {}).items():
            comp_fixes += _check_component_files(cpath, out_dir)
        if comp_fixes:
            fixes.append(f"补全组件文件 {comp_fixes} 个")

        # 3-5. 合并为单次 os.walk：检查 wxss @import、wxml 图片、wxss url() 引用
        # 构建文件索引用于模糊匹配
        _file_idx = _build_file_index(out_dir)
        _img_idx = _build_file_index(out_dir, _IMG_EXTS_SET)

        # 3a. 先检查 tabBar 图标
        tabbar = app_cfg.get("tabBar", {})
        icon_fixes = 0
        icon_fuzzy = 0
        for item in tabbar.get("list", []):
            for key in ("iconPath", "selectedIconPath"):
                ip = item.get(key)
                if ip and not os.path.isfile(os.path.join(out_dir, ip)):
                    # 尝试模糊匹配
                    found = _find_resource_by_name(ip, out_dir, _img_idx, _IMG_EXTS_SET)
                    if found:
                        new_rel = os.path.relpath(found, out_dir).replace("\\", "/")
                        item[key] = new_rel
                        icon_fuzzy += 1
                        continue
                    ph = os.path.join(out_dir, ip)
                    os.makedirs(os.path.dirname(ph), exist_ok=True)
                    with open(ph, "wb") as f:
                        f.write(_PNG1x1)
                    icon_fixes += 1
        if icon_fuzzy:
            fixes.append(f"图标路径模糊匹配修正 {icon_fuzzy} 个")
            changed = True
        if icon_fixes:
            fixes.append(f"补全 tabBar 图标 {icon_fixes} 个")

        # 3b. 单次遍历检查 wxss/wxml 引用
        imp_fixes = 0
        img_missing = 0
        img_fuzzy = 0
        imp_fuzzy = 0
        wxml_imp_fuzzy = 0
        b64_extracted = 0
        _IMG_EXTS = r'\.(?:png|jpg|jpeg|gif|svg|webp|ico|avif)'
        _re_import = re.compile(r'@import\s+["\']([^"\']+)["\']')
        _re_wxml_img = re.compile(
            r'(?:src|src-mode|data-src|data-original)\s*=\s*["\']([^"\']+' + _IMG_EXTS + r')["\']', re.I)
        _re_srcset = re.compile(r'(?:srcset|data-srcset)\s*=\s*["\']([^"\']+)["\']', re.I)
        _re_img_ext = re.compile(_IMG_EXTS + r'$', re.I)
        _re_url_img = re.compile(
            r'url\(\s*[\'"]?([^\'")+]+' + _IMG_EXTS + r')\s*[\'"]?\)', re.I)
        _re_wxml_imp = re.compile(r'<(?:import|include)\s+[^>]*src\s*=\s*["\']([^"\']+)["\']', re.I)
        _re_js_req = re.compile(r'(?:require|import)\s*\(\s*["\']([^"\']+)["\']\s*\)')
        _re_js_import_from = re.compile(r'import\s+[^;]+\s+from\s+["\']([^"\']+)["\']')
        _re_js_static_import = re.compile(r'import\s+["\']([^"\']+)["\']')

        wxml_ref_fixes = 0
        js_ref_fixes = 0

        for root_p, _d, files in os.walk(out_dir):
            for fn in files:
                fp = os.path.join(root_p, fn)
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except Exception:
                    continue

                content_modified = False

                if fn.endswith(".wxss"):
                    # 提取 base64 图片为独立文件
                    new_content, b64_cnt = _extract_base64_images(
                        content, out_dir, os.path.dirname(fp),
                        prefix=os.path.splitext(fn)[0])
                    if b64_cnt > 0:
                        content = new_content
                        content_modified = True
                        b64_extracted += b64_cnt

                    # 检查 @import
                    for m in _re_import.finditer(content):
                        imp = m.group(1)
                        if imp.startswith(("http://", "https://", "//")):
                            continue
                        imp_full = os.path.join(out_dir, imp.lstrip("/")) if imp.startswith("/") \
                            else os.path.join(os.path.dirname(fp), imp)
                        if not os.path.isfile(imp_full):
                            # 尝试模糊匹配
                            found = _find_resource_by_name(imp, out_dir, _file_idx)
                            if found:
                                new_rel = os.path.relpath(
                                    found, os.path.dirname(fp)).replace("\\", "/")
                                content = content.replace(m.group(0),
                                    f'@import "{new_rel}"')
                                content_modified = True
                                imp_fuzzy += 1
                                continue
                            os.makedirs(os.path.dirname(imp_full), exist_ok=True)
                            with open(imp_full, "w", encoding="utf-8") as f:
                                f.write(f"/* {os.path.basename(imp_full)} */\n")
                            imp_fixes += 1
                    # 检查 url() 图片
                    for m in _re_url_img.finditer(content):
                        img_path = m.group(1)
                        if img_path.startswith(("http://", "https://", "//", "data:")):
                            continue
                        img_full = os.path.join(out_dir, img_path.lstrip("/")) if img_path.startswith("/") \
                            else os.path.join(os.path.dirname(fp), img_path)
                        if not os.path.isfile(img_full):
                            # 尝试模糊匹配
                            found = _find_resource_by_name(img_path, out_dir, _img_idx, _IMG_EXTS_SET)
                            if found:
                                new_rel = os.path.relpath(
                                    found, os.path.dirname(fp)).replace("\\", "/")
                                content = content.replace(img_path, new_rel)
                                content_modified = True
                                img_fuzzy += 1
                                continue
                            os.makedirs(os.path.dirname(img_full), exist_ok=True)
                            with open(img_full, "wb") as f:
                                f.write(_PNG1x1)
                            img_missing += 1

                elif fn.endswith(".wxml"):
                    # 检查 src/data-src 图片
                    for m in _re_wxml_img.finditer(content):
                        img_path = m.group(1)
                        if img_path.startswith(("http://", "https://", "//", "data:", "{{")):
                            continue
                        img_full = os.path.join(out_dir, img_path.lstrip("/")) if img_path.startswith("/") \
                            else os.path.join(os.path.dirname(fp), img_path)
                        if not os.path.isfile(img_full):
                            # 尝试模糊匹配
                            found = _find_resource_by_name(img_path, out_dir, _img_idx, _IMG_EXTS_SET)
                            if found:
                                new_rel = os.path.relpath(
                                    found, os.path.dirname(fp)).replace("\\", "/")
                                content = content.replace(img_path, new_rel)
                                content_modified = True
                                img_fuzzy += 1
                                continue
                            os.makedirs(os.path.dirname(img_full), exist_ok=True)
                            with open(img_full, "wb") as f:
                                f.write(_PNG1x1)
                            img_missing += 1
                    # 检查 srcset/data-srcset
                    for m in _re_srcset.finditer(content):
                        for part in m.group(1).split(","):
                            tokens = part.strip().split()
                            part_url = tokens[0] if tokens else ""
                            if not part_url or part_url.startswith(("http://", "https://", "//", "data:", "{{")):
                                continue
                            if _re_img_ext.search(part_url):
                                img_full = os.path.join(out_dir, part_url.lstrip("/")) if part_url.startswith("/") \
                                    else os.path.join(os.path.dirname(fp), part_url)
                                if not os.path.isfile(img_full):
                                    # 尝试模糊匹配
                                    found = _find_resource_by_name(part_url, out_dir, _img_idx, _IMG_EXTS_SET)
                                    if found:
                                        new_rel = os.path.relpath(
                                            found, os.path.dirname(fp)).replace("\\", "/")
                                        content = content.replace(part_url, new_rel)
                                        content_modified = True
                                        img_fuzzy += 1
                                        continue
                                    os.makedirs(os.path.dirname(img_full), exist_ok=True)
                                    with open(img_full, "wb") as f:
                                        f.write(_PNG1x1)
                                    img_missing += 1
                    # 检查 <import src="..."/> 和 <include src="..."/>
                    for m in _re_wxml_imp.finditer(content):
                        ref = m.group(1)
                        if ref.startswith(("http://", "https://", "//")):
                            continue
                        ref_full = os.path.join(out_dir, ref.lstrip("/")) if ref.startswith("/") \
                            else os.path.join(os.path.dirname(fp), ref)
                        if not os.path.isfile(ref_full):
                            # 尝试模糊匹配
                            found = _find_resource_by_name(ref, out_dir, _file_idx)
                            if found:
                                new_rel = os.path.relpath(
                                    found, os.path.dirname(fp)).replace("\\", "/")
                                content = content.replace(ref, new_rel)
                                content_modified = True
                                wxml_imp_fuzzy += 1
                                continue
                            os.makedirs(os.path.dirname(ref_full), exist_ok=True)
                            with open(ref_full, "w", encoding="utf-8") as f:
                                f.write(f"<!-- {os.path.basename(ref_full)} -->\n")
                            wxml_ref_fixes += 1

                elif fn.endswith(".js"):
                    # 检查 require()/import 引用的本地模块
                    for pat in (_re_js_req, _re_js_import_from, _re_js_static_import):
                        for m in pat.finditer(content):
                            ref = m.group(1)
                            if ref.startswith(("http://", "https://", "//", "node:", "npm:")):
                                continue
                            # 跳过 npm 包（无路径前缀的标识符）
                            if not ref.startswith((".", "/", "~")):
                                continue
                            ref_full = os.path.normpath(
                                os.path.join(out_dir, ref.lstrip("/")) if ref.startswith("/") \
                                else os.path.join(os.path.dirname(fp), ref))
                            # 尝试多种扩展名
                            found = False
                            for try_ext in ("", ".js", ".json", "/index.js", "/index.json"):
                                if os.path.isfile(ref_full + try_ext):
                                    found = True
                                    break
                            if not found:
                                # 创建占位 JS 文件（路径已有 .js/.json 扩展名时不追加）
                                if ref_full.endswith((".js", ".json")):
                                    target = ref_full
                                else:
                                    target = ref_full + ".js"
                                os.makedirs(os.path.dirname(target), exist_ok=True)
                                with open(target, "w", encoding="utf-8") as f:
                                    f.write(f"// {os.path.basename(target)} (auto-generated)\n")
                                js_ref_fixes += 1

                # 写回修改后的内容（模糊匹配/base64提取后路径已更新）
                if content_modified:
                    try:
                        with open(fp, "w", encoding="utf-8") as f:
                            f.write(content)
                    except Exception:
                        pass

        # 写回 app.json（tabBar 图标路径可能已被模糊匹配修正）
        if changed:
            try:
                with open(app_json_path, "w", encoding="utf-8") as f:
                    json.dump(app_cfg, f, ensure_ascii=False, indent=4)
            except Exception:
                pass

        if imp_fixes:
            fixes.append(f"补全 WXSS @import {imp_fixes} 个")
        if imp_fuzzy:
            fixes.append(f"WXSS @import 模糊匹配修正 {imp_fuzzy} 个")
        if img_missing:
            fixes.append(f"补全缺失图片 {img_missing} 个")
        if img_fuzzy:
            fixes.append(f"图片路径模糊匹配修正 {img_fuzzy} 个")
        if wxml_ref_fixes:
            fixes.append(f"补全 WXML import/include 引用 {wxml_ref_fixes} 个")
        if wxml_imp_fuzzy:
            fixes.append(f"WXML import/include 模糊匹配修正 {wxml_imp_fuzzy} 个")
        if js_ref_fixes:
            fixes.append(f"补全 JS require/import 引用 {js_ref_fixes} 个")
        if b64_extracted:
            fixes.append(f"提取 base64 图片为独立文件 {b64_extracted} 个")

        if fixes:
            self.log("  深度校验修复:", "step")
            for fix in fixes:
                self.log(f"    + {fix}", "ok")

        # 最终完整性统计
        total_pages = len(all_pages)
        complete_pages = 0
        for page in all_pages:
            page = page.strip("/")
            if all(os.path.isfile(os.path.join(out_dir, page + ext))
                   for ext in (".js", ".wxml", ".wxss", ".json")):
                complete_pages += 1

        critical = ["app.json", "app.js", "app.wxss", "project.config.json", "sitemap.json"]
        missing_c = [f for f in critical if not os.path.isfile(os.path.join(out_dir, f))]
        if missing_c:
            self.log(f"  [警告] 仍缺失关键文件: {', '.join(missing_c)}", "warn")
        else:
            self.log("  所有关键文件就绪。", "ok")
        self.log(f"  页面完整性: {complete_pages}/{total_pages} 页面四件套齐全", "info")

        return total_pages, complete_pages

    # ------------------------------------------------------------------
    # WXML 恢复：从编译后的 $gwx 函数中还原页面模板
    # ------------------------------------------------------------------
    def _is_wxml_placeholder(self, fp):
        """检测 .wxml 文件是否为占位符（非真实模板内容）。"""
        if not os.path.isfile(fp):
            return True
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                content = f.read().strip()
        except Exception:
            return True
        if not content:
            return True
        if len(content) < 20:
            return True
        # 不含任何标签或数据绑定
        if "<" not in content and "{{" not in content:
            return True
        # 内容就是文件名本身或文件路径
        page_name = os.path.basename(fp).replace(".wxml", "")
        if content == page_name or content.endswith(page_name):
            return True
        # 检测 _deep_verify / _standardize_structure 生成的占位符模板
        if (f'class="container"' in content and f'<text>{page_name}</text>' in content
                and "wx:for" not in content and "bindtap" not in content
                and "catchtap" not in content):
            return True
        # 检测 restore_wxml 失败后残留的错误信息
        if content.startswith("<!--") and "restore" in content.lower() and "error" in content.lower():
            return True
        # 检测仅含注释和单一空标签的占位符
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        real_tags = [l for l in lines if l.startswith("<") and not l.startswith("<!--")
                     and not l.startswith("</") and l not in ("<view>", "</view>", "<text>", "</text>",
                                                              "<view/>", "<text/>",
                                                              '<view class="container">', '<view class="component">')]
        if len(real_tags) == 0 and len(lines) <= 6:
            return True
        return False

    def _restore_wxml(self, out_dir):
        """检测占位符 WXML 并尝试从 $gwx 恢复真实模板。"""
        # 1. 收集所有页面路径
        app_json = os.path.join(out_dir, "app.json")
        if not os.path.isfile(app_json):
            return
        try:
            with open(app_json, "r", encoding="utf-8-sig") as f:
                app = json.load(f)
        except Exception:
            return

        all_pages = list(app.get("pages", []))
        for sp in app.get("subPackages", app.get("subpackages", [])):
            root = sp.get("root", "").strip("/")
            for pg in sp.get("pages", []):
                all_pages.append((root + "/" + pg) if root else pg)

        # 2. 检测占位符
        placeholders = []
        for page in all_pages:
            page = page.strip("/")
            wxml_fp = os.path.join(out_dir, page + ".wxml")
            if self._is_wxml_placeholder(wxml_fp):
                placeholders.append(page)

        if not placeholders:
            self.log("  所有 WXML 文件均有真实内容，无需恢复。", "ok")
            return

        self.log(f"  检测到 {len(placeholders)} 个占位符 WXML，尝试从 $gwx 恢复...", "warn")

        # 3. 检查 page-frame.html / app-wxss.js 中是否含 $gwx
        gwx_file = None
        for fname in ("page-frame.html", "app-wxss.js", "page-frame.js"):
            fp = os.path.join(out_dir, fname)
            if os.path.isfile(fp):
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        head = f.read(2000000)
                    if "$gwx" in head or "gz$gwx" in head:
                        gwx_file = fname
                        break
                except Exception:
                    pass

        if not gwx_file:
            self.log("  未找到 $gwx 函数（page-frame.html 可能已被删除或格式不兼容），WXML 恢复跳过。", "warn")
            return

        # 3b. 确认恢复脚本存在，否则不删除占位符
        if not os.path.isfile(RESTORE_WXML_SCRIPT):
            self.log("  [错误] restoreWxml.js 不存在，跳过 WXML 恢复（保留占位符）。", "err")
            return

        self.log(f"  在 {gwx_file} 中找到 $gwx 函数", "info")

        # 4. 删除占位符 WXML 文件，让 doFrame 重新生成
        deleted = 0
        for page in placeholders:
            wxml_fp = os.path.join(out_dir, page + ".wxml")
            try:
                os.remove(wxml_fp)
                deleted += 1
            except Exception:
                pass
        if deleted:
            self.log(f"  删除 {deleted} 个占位符 WXML 文件", "info")

        # 5. 调用 Node.js 恢复脚本
        self.log("  调用 restoreWxml.js 恢复 WXML...", "step")
        rc = self.run_cmd([_get_node(), RESTORE_WXML_SCRIPT, out_dir],
                          cwd=os.path.dirname(RESTORE_WXML_SCRIPT))

        # 6. 检查恢复结果
        restored = 0
        still_placeholder = 0
        failed_pages = []
        for page in placeholders:
            wxml_fp = os.path.join(out_dir, page + ".wxml")
            if not os.path.isfile(wxml_fp):
                # 文件被删除但未重新生成（doFrame 完全失败）
                still_placeholder += 1
                failed_pages.append(page)
            elif not self._is_wxml_placeholder(wxml_fp):
                restored += 1
            else:
                still_placeholder += 1
                failed_pages.append(page)

        if restored > 0:
            self.log(f"  WXML 恢复成功: {restored} 个页面" +
                     (f"，仍有 {still_placeholder} 个占位符" if still_placeholder else ""), "ok")
        else:
            self.log("  WXML 恢复未成功（$gwx 格式可能不兼容或 doFrame 解析失败）。", "warn")
            self.log("  请查看上方 [diagnose] 和 [wuWxml] 日志了解具体原因。", "info")
            self.log("  常见原因: 1) 编译格式变更导致标记不匹配 2) VM 沙箱执行异常 3) 文件已被清理", "info")

        # 记录未能恢复的页面（前10个）
        if failed_pages:
            sample = failed_pages[:10]
            self.log(f"  未恢复页面: {', '.join(sample)}" +
                     (f" 等 {len(failed_pages)} 个" if len(failed_pages) > 10 else ""), "info")

    # ------------------------------------------------------------------
    # 项目结构规范化：确保每个页面有独立文件夹和四件套
    # ------------------------------------------------------------------
    def _standardize_structure(self, out_dir):
        """规范化项目结构：确保页面文件在正确目录、清理冗余文件。"""
        app_json = os.path.join(out_dir, "app.json")
        if not os.path.isfile(app_json):
            return
        try:
            with open(app_json, "r", encoding="utf-8-sig") as f:
                app = json.load(f)
        except Exception:
            return

        all_pages = list(app.get("pages", []))
        for sp in app.get("subPackages", app.get("subpackages", [])):
            root = sp.get("root", "").strip("/")
            for pg in sp.get("pages", []):
                all_pages.append((root + "/" + pg) if root else pg)

        moved = 0
        created = 0

        for page in all_pages:
            page = page.strip("/")
            if not page:
                continue

            # 目标文件路径
            target_dir = os.path.join(out_dir, os.path.dirname(page))
            page_name = os.path.basename(page)
            os.makedirs(target_dir, exist_ok=True)

            # 确保四件套存在
            for ext, default in [
                (".wxml", f'<!-- {page_name} -->\n<view class="container">\n  <text>{page_name}</text>\n</view>\n'),
                (".js",   f'// {page_name}\nPage({{\n  data: {{}},\n  onLoad() {{}},\n}})\n'),
                (".wxss", f'/* {page_name} */\n.container {{\n  padding: 20rpx;\n}}\n'),
                (".json", '{{\n  "usingComponents": {{}}\n}}\n'),
            ]:
                target_fp = os.path.join(out_dir, page + ext)
                if not os.path.isfile(target_fp):
                    # 尝试从根目录找同名文件（扁平结构 → 目录结构）
                    flat_fp = os.path.join(out_dir, page_name + ext)
                    if os.path.isfile(flat_fp) and flat_fp != target_fp:
                        try:
                            shutil.move(flat_fp, target_fp)
                            moved += 1
                            continue
                        except Exception:
                            pass
                    # 创建默认文件
                    with open(target_fp, "w", encoding="utf-8") as f:
                        f.write(default)
                    created += 1

        # 清理根目录下的孤立页面文件（已移入目录的）
        root_files = set()
        for page in all_pages:
            page = page.strip("/")
            page_name = os.path.basename(page)
            for ext in (".js", ".wxml", ".wxss", ".json"):
                # 如果页面在子目录但根目录有同名文件且内容相同，删除根目录的
                root_fp = os.path.join(out_dir, page_name + ext)
                target_fp = os.path.join(out_dir, page + ext)
                if (os.path.isfile(root_fp) and os.path.isfile(target_fp)
                        and root_fp != target_fp):
                    try:
                        with open(root_fp, "r", errors="replace") as f:
                            r = f.read()
                        with open(target_fp, "r", errors="replace") as f:
                            t = f.read()
                        if r == t:
                            os.remove(root_fp)
                            root_files.add(root_fp)
                    except Exception:
                        pass

        # 删除空目录（递归，自底向上）
        for root_p, dirs, _files in os.walk(out_dir, topdown=False):
            for d in dirs:
                dp = os.path.join(root_p, d)
                try:
                    if not _has_files(dp):
                        os.rmdir(dp)
                except OSError:
                    pass

        if moved:
            self.log(f"  移动扁平文件到目录结构: {moved} 个", "ok")
        if created:
            self.log(f"  补全页面四件套: {created} 个", "ok")
        if not moved and not created:
            self.log("  项目结构已规范。", "ok")

    def _cleanup_intermediates(self, out_dir):
        """清理 -d 保留的中间文件（page-frame.html, app-service.js 等）。"""
        intermediates = [
            "page-frame.html", "page-frame.js", "app-wxss.js",
            "app-service.js", "app-config.json", "workers.js",
            "subContext.js", "game.js",
        ]
        removed = 0
        for fname in intermediates:
            fp = os.path.join(out_dir, fname)
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                    removed += 1
                except Exception:
                    pass
        # 清理 .ori.js 文件（doFrame 失败时保存的原始代码）
        for root_p, _d, files in os.walk(out_dir):
            for fn in files:
                if fn.endswith(".ori.js"):
                    try:
                        os.remove(os.path.join(root_p, fn))
                        removed += 1
                    except Exception:
                        pass
        if removed:
            self.log(f"  清理中间文件: {removed} 个", "info")

    # ------------------------------------------------------------------
    # 小程序代码工程化美化
    # ------------------------------------------------------------------
    def _beautify_wxapp_code(self, out_dir):
        """对反编译后的小程序代码进行全面工程化美化。"""
        self.log("  开始代码工程化美化...", "step")
        wxml_count = 0
        wxss_count = 0
        js_count = 0
        json_count = 0
        errors = 0

        # jsbeautifier 配置
        try:
            import jsbeautifier
            js_opts = jsbeautifier.default_options()
            js_opts.indent_size = 2
            js_opts.preserve_newlines = False
            js_opts.max_preserve_newlines = 2
            js_opts.unescape_strings = True
            js_opts.end_with_newline = True
            js_opts.brace_style = "collapse"
            has_jsb = True
        except Exception:
            has_jsb = False
            js_opts = None

        # 跳过中间/系统文件
        skip_files = {
            "page-frame.html", "page-frame.js", "app-wxss.js",
            "app-service.js", "app-config.json", "workers.js",
            "subContext.js", "game.js",
        }

        for root_p, _d, files in os.walk(out_dir):
            # 跳过第三方目录
            rel_dir = os.path.relpath(root_p, out_dir)
            if "miniprogram_npm" in rel_dir or "node_modules" in rel_dir:
                continue

            for fn in files:
                if fn in skip_files:
                    continue
                fp = os.path.join(root_p, fn)
                ext = os.path.splitext(fn)[1].lower()

                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    if not content.strip():
                        continue

                    new_content = content

                    if ext == ".wxml":
                        new_content = self._beautify_wxml(content)
                        if new_content != content:
                            wxml_count += 1

                    elif ext == ".wxss":
                        new_content = self._beautify_wxss(content)
                        if new_content != content:
                            wxss_count += 1

                    elif ext == ".js":
                        if len(content) > 500000:
                            continue
                        new_content = self._beautify_wxapp_js(content, has_jsb, js_opts)
                        if new_content != content:
                            js_count += 1

                    elif ext == ".json":
                        new_content = self._beautify_wxapp_json(content, fn)
                        if new_content != content:
                            json_count += 1

                    if new_content != content:
                        with open(fp, "w", encoding="utf-8") as f:
                            f.write(new_content)

                except Exception:
                    errors += 1

        parts = []
        if wxml_count:
            parts.append(f"WXML {wxml_count}")
        if wxss_count:
            parts.append(f"WXSS {wxss_count}")
        if js_count:
            parts.append(f"JS {js_count}")
        if json_count:
            parts.append(f"JSON {json_count}")
        if errors:
            parts.append(f"跳过 {errors}")

        if parts:
            self.log(f"  代码美化: {', '.join(parts)}", "ok")
        else:
            self.log("  代码已规范，无需美化。", "ok")

    # ---- WXML 格式化 ----

    @staticmethod
    def _beautify_wxml(code):
        """WXML 格式化：标签缩进、属性对齐、结构规范化。"""
        code = code.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not code:
            return code

        # 小程序自闭合/void 标签
        void_tags = {"image", "input", "icon", "progress", "switch", "slider",
                      "audio", "video", "camera", "live-player", "live-pusher",
                      "open-data", "web-view", "cover-image", "navigator",
                      "import", "include", "wxs"}

        # ---- 1. 分词 ----
        tokens = []
        i = 0
        n = len(code)
        while i < n:
            # 注释
            if code[i:i + 4] == "<!--":
                end = code.find("-->", i)
                if end == -1:
                    tokens.append(("comment", code[i:].strip()))
                    break
                tokens.append(("comment", code[i:end + 3].strip()))
                i = end + 3
            # CDATA
            elif code[i:i + 9] == "<![CDATA[":
                end = code.find("]]>", i)
                if end == -1:
                    tokens.append(("text", code[i:].strip()))
                    break
                tokens.append(("text", code[i:end + 3].strip()))
                i = end + 3
            # 标签
            elif code[i] == '<':
                in_quote = None
                j = i + 1
                while j < n:
                    c = code[j]
                    if in_quote:
                        if c == in_quote:
                            in_quote = None
                    elif c in ('"', "'"):
                        in_quote = c
                    elif c == '>':
                        j += 1
                        break
                    j += 1
                tag = code[i:j].strip()
                if tag.startswith("</"):
                    tokens.append(("close", tag))
                elif tag.endswith("/>"):
                    tokens.append(("selfclose", tag))
                else:
                    m = re.match(r'<([\w-]+)', tag)
                    tag_name = m.group(1) if m else ""
                    if tag_name in void_tags:
                        tokens.append(("void", tag))
                    else:
                        tokens.append(("open", tag))
                i = j
            # 文本
            else:
                j = code.find("<", i)
                if j == -1:
                    j = n
                text = code[i:j].strip()
                if text:
                    tokens.append(("text", text))
                i = j

        # ---- 2. 格式化输出 ----
        lines = []
        depth = 0
        indent = "  "
        idx = 0
        total = len(tokens)

        while idx < total:
            ttype, content = tokens[idx]

            if ttype == "comment":
                lines.append(indent * depth + content)
                idx += 1

            elif ttype == "close":
                # void 标签的闭合标签不减少缩进（因为开标签未增加）
                close_m = re.match(r'</([\w-]+)', content)
                close_name = close_m.group(1) if close_m else ""
                if close_name not in void_tags:
                    depth = max(0, depth - 1)
                lines.append(indent * depth + content)
                idx += 1

            elif ttype in ("selfclose", "void"):
                if len(content) > 100:
                    lines.extend(WxappTab._wxml_multiline_tag(content, depth, indent, True))
                else:
                    lines.append(indent * depth + content)
                idx += 1

            elif ttype == "open":
                # 检测内联模式: <tag>text</tag>
                if (idx + 2 < total and
                        tokens[idx + 1][0] == "text" and
                        tokens[idx + 2][0] == "close"):
                    open_m = re.match(r'<([\w-]+)', content)
                    close_m = re.match(r'</([\w-]+)', tokens[idx + 2][1])
                    if (open_m and close_m and
                            open_m.group(1) == close_m.group(1)):
                        text = tokens[idx + 1][1]
                        close_tag = tokens[idx + 2][1]
                        combined = content + text + close_tag
                        if len(combined) <= 100:
                            lines.append(indent * depth + combined)
                        else:
                            lines.append(indent * depth + content)
                            lines.append(indent * (depth + 1) + text)
                            lines.append(indent * depth + close_tag)
                        idx += 3
                        continue

                # 普通开标签
                if len(content) > 100:
                    lines.extend(WxappTab._wxml_multiline_tag(content, depth, indent, False))
                else:
                    lines.append(indent * depth + content)
                depth += 1
                idx += 1

            elif ttype == "text":
                lines.append(indent * depth + content)
                idx += 1

            else:
                idx += 1

        # ---- 3. 清理空行 ----
        result = []
        blank = False
        for line in lines:
            if not line.strip():
                if not blank and result:
                    result.append("")
                blank = True
            else:
                blank = False
                result.append(line)

        return "\n".join(result).rstrip() + "\n"

    @staticmethod
    def _wxml_multiline_tag(tag, depth, indent, self_closing):
        """将长标签的属性拆分到多行，提升可读性。"""
        name_match = re.match(r'<([\w-]+)', tag)
        if not name_match:
            return [indent * depth + tag]

        tag_name = name_match.group(1)
        rest = tag[len(name_match.group(0)):]

        if self_closing and rest.endswith("/>"):
            rest = rest[:-2].strip()
            closing = "/>"
        elif rest.endswith(">"):
            rest = rest[:-1].strip()
            closing = ">"
        else:
            closing = ">"

        # 解析属性: key="value" | key='value' | key | key="{{expr}}"
        attrs = re.findall(
            r'([\w:.\-@]+)(?:\s*=\s*("[^"]*"|\'[^\']*\'|\{\{[^}]*\}\}))?',
            rest
        )

        result = [indent * depth + f"<{tag_name}"]
        for attr_name, attr_value in attrs:
            attr_name = attr_name.strip()
            if not attr_name:
                continue
            if attr_value:
                result.append(indent * (depth + 1) + f"{attr_name}={attr_value}")
            else:
                result.append(indent * (depth + 1) + attr_name)
        result.append(indent * depth + closing)
        return result

    # ---- WXSS 格式化 ----

    @staticmethod
    def _beautify_wxss(code):
        """保守 WXSS 美化：保护字符串/注释内容，仅规范化结构空白。

        修复旧版的破坏性问题：
        - 不再压缩字符串内空白（content: "hello world" 不会被破坏）
        - 不再移除冒号两侧空格（后代选择器 a :hover 不被破坏）
        - 正确处理 @media/@supports/@keyframes 嵌套缩进
        - 保护注释 /* ... */ 内容不变
        - 保护 url() 内容不被组合符规则破坏
        """
        code = code.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not code:
            return code

        # 1. 提取注释和字符串，用占位符替换，防止被修改
        placeholders = []

        def _stash(m):
            placeholders.append(m.group(0))
            return f"\x00PH{len(placeholders) - 1}\x00"

        # 保护块注释
        code = re.sub(r'/\*[\s\S]*?\*/', _stash, code)
        # 保护双引号字符串
        code = re.sub(r'"[^"]*"', _stash, code)
        # 保护单引号字符串
        code = re.sub(r"'[^']*'", _stash, code)

        # 2. 规范化结构空白（此时字符串/注释已被保护）
        code = re.sub(r'[ \t]+', ' ', code)
        code = re.sub(r'\s*\n\s*', '\n', code)
        code = re.sub(r'\n{2,}', '\n', code)
        # 花括号前后加换行
        code = re.sub(r'\s*\{\s*', ' {\n  ', code)
        code = re.sub(r'\s*\}\s*', '\n}\n', code)
        # 分号后加换行
        code = re.sub(r';\s*', ';\n  ', code)
        # 逗号后加空格
        code = re.sub(r',\s*', ', ', code)

        # 3. 逐行处理缩进
        lines = code.split('\n')
        result = []
        indent = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # 处理闭括号
            if line == '}':
                indent = max(0, indent - 1)
                result.append('  ' * indent + '}')
                continue
            # 处理开括号行（选择器或@规则）
            if line.endswith('{'):
                # 规范化 @import / @charset
                if line.startswith('@') and 'import' in line:
                    line = re.sub(r'@(import|charset)\s*', r'@\1 ', line, count=1)
                result.append('  ' * indent + line)
                indent += 1
                continue
            # 处理同一行有 } 的情况（如 } selector {）
            if line.startswith('}'):
                indent = max(0, indent - 1)
                rest = line[1:].strip()
                if rest:
                    if rest.endswith('{'):
                        result.append('  ' * indent + rest)
                        indent += 1
                    else:
                        result.append('  ' * indent + rest)
                else:
                    result.append('  ' * indent + '}')
                continue
            # 普通属性行：冒号后加空格（如果是属性声明）
            if ':' in line and not line.startswith('@') and '{' not in line:
                idx = line.index(':')
                prop = line[:idx].strip()
                val = line[idx + 1:].strip()
                # 仅在声明块内作为属性处理
                if indent > 0 and not any(c in prop for c in ('>', '~', '+', '.', '#', '[', '*')):
                    line = f"{prop}: {val}"
            result.append('  ' * indent + line)

        code_out = '\n'.join(result)

        # 4. 还原占位符
        for i, ph in enumerate(placeholders):
            code_out = code_out.replace(f"\x00PH{i}\x00", ph)

        # 5. 选择器组合符规范化（保护 url() 内容）
        urls = []
        def _stash_url(m):
            urls.append(m.group(0))
            return f"\x00URL{len(urls) - 1}\x00"
        code_out = re.sub(r'url\([^)]*\)', _stash_url, code_out)
        # 组合符两侧加空格（但不动 >= 等属性选择器）
        code_out = re.sub(r'([>~+])(?!=)', r' \1 ', code_out)
        # 合并多余空格（仅非行首位置，保护缩进）
        code_out = re.sub(r'(?<=\S)  +', ' ', code_out)
        for i, u in enumerate(urls):
            code_out = code_out.replace(f"\x00URL{i}\x00", u)

        return code_out.strip() + '\n'

    # ---- JS 美化 + 轻量反混淆 ----

    @staticmethod
    def _beautify_wxapp_js(code, has_jsb, js_opts):
        """JS 代码美化：jsbeautifier 格式化 + 轻量反混淆清理。"""
        if has_jsb and js_opts:
            try:
                import jsbeautifier
                beautified = jsbeautifier.beautify(code, js_opts)
            except Exception:
                beautified = code
        else:
            beautified = code

        # ---- 轻量反混淆 / 清理 ----
        lines = beautified.split('\n')
        cleaned = []
        for line in lines:
            stripped = line.strip()
            # 移除行尾空白
            line = line.rstrip()
            # 合并连续空行（最多保留2行）
            if not stripped:
                if cleaned and not cleaned[-1].strip():
                    if len(cleaned) >= 2 and not cleaned[-2].strip():
                        continue
                cleaned.append(line)
                continue
            cleaned.append(line)

        result = '\n'.join(cleaned)

        # 移除常见反编译噪音
        # 1. 移除重复 "use strict"
        result = re.sub(r'(?m)^["\']use strict["\'];\s*\n', '', result, count=1)
        # 2. 规范化连续分号后的空行
        result = re.sub(r';\s*\n\s*\n\s*\n', ';\n\n', result)
        # 3. 移除行尾多余分号后的空白行
        result = re.sub(r'\{\s*\n\s*\}', '{}', result)

        # ---- 为小程序入口函数添加注释标记 ----
        # 在 Page(, App(, Component(, Behavior( 前添加注释
        def add_section_comment(m):
            fn_name = m.group(1)
            comments = {
                'Page': '// ===== 页面逻辑 =====',
                'App': '// ===== 应用入口 =====',
                'Component': '// ===== 组件定义 =====',
                'Behavior': '// ===== 行为定义 =====',
            }
            comment = comments.get(fn_name, '')
            if comment and not result[:m.start()].rstrip().endswith(comment):
                return f"{comment}\n{m.group(0)}"
            return m.group(0)

        result = re.sub(r'(?m)^(Page|App|Component|Behavior)\s*\(', add_section_comment, result)

        if not result.endswith('\n'):
            result += '\n'

        return result

    # ---- JSON 格式化 ----

    @staticmethod
    def _beautify_wxapp_json(code, filename=""):
        """JSON 格式化：统一缩进、确保合法。"""
        try:
            data = json.loads(code)
        except (json.JSONDecodeError, ValueError):
            return code
        return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n"

    def _find_subpackages(self, filepath, appid):
        """查找与主包同一目录、同一 AppID 的分包 wxapkg 文件。

        微信缓存目录结构:
          {wxid}/__APP__.wxapkg   ← 主包
          {wxid}/0.wxapkg         ← 分包
          {wxid}/1.wxapkg         ← 分包
          ...
        返回分包文件路径列表（按文件名排序，不含主包本身）。
        """
        pkg_dir = os.path.dirname(filepath)
        subs = []
        for fn in os.listdir(pkg_dir):
            if not fn.lower().endswith(".wxapkg"):
                continue
            full = os.path.join(pkg_dir, fn)
            if os.path.normcase(full) == os.path.normcase(filepath):
                continue
            if fn.lower() == "__app__.wxapkg":
                continue  # 主包本身
            # 如果有 appid，校验分包路径也含同一 appid
            if appid:
                sub_appid = self._extract_appid(full)
                if sub_appid and sub_appid.lower() != appid.lower():
                    continue  # 不同 AppID，跳过
            subs.append(full)
        subs.sort(key=lambda p: os.path.basename(p).lower())
        return subs

    def _pipeline(self, filepath):
        appid = self._extract_appid(filepath)
        self.log("========== 开始反编译 ==========", "step")
        self.log(f"输入文件: {filepath}", "info")
        self.log(f"AppID: {appid or '（未识别）'}", "info")

        # 智能选主包
        sib = os.path.join(os.path.dirname(filepath), "__APP__.wxapkg")
        if os.path.basename(filepath).lower() != "__app__.wxapkg" and os.path.isfile(sib):
            self.log("  所选为分包，自动切换到主包 __APP__.wxapkg", "warn")
            filepath = sib
            appid = self._extract_appid(filepath) or appid

        # 查找同目录下的分包
        subpackages = self._find_subpackages(filepath, appid)
        if subpackages:
            self.log(f"  ★ 发现 {len(subpackages)} 个分包，将合并反编译:", "ok")
            for sp in subpackages:
                self.log(f"    - {os.path.basename(sp)}", "info")

        # 步骤1 加密检测
        self.log("\n[1/10] 检测加密状态", "step")
        try:
            status, desc = self._detect_encryption(filepath)
        except Exception as e:
            self.log(f"  [错误] 读取文件头失败: {e}", "err"); return
        self.log("  " + desc, "info")

        work = tempfile.mkdtemp(prefix="wxapp_")
        base = os.path.splitext(os.path.basename(filepath))[0]
        wp = os.path.join(work, base + ".wxapkg")
        self.log(f"  临时目录: {work}", "info")

        try:
            self._pipeline_core(filepath, status, work, base, wp, appid,
                                subpackages=subpackages)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # 分包中间文件（不应合并到主包）
    _SUB_INTERMEDIATE = frozenset({
        "app-config.json", "app-service.js", "page-frame.html",
        "page-frame.js", "app-wxss.js", "workers.js",
        "subContext.js", "game.js", "game.json",
        "app.json", "project.config.json", "sitemap.json",
        "ext.json", "ext-app.json",
    })

    def _merge_subpackage_files(self, work_dir, main_out_dir):
        """合并分包目录中的所有有用文件到主包输出。

        wuWxapkg.js 处理分包时，JS/WXML/WXSS 输出到主包目录（via -s=），
        但 .json 页面配置、.wxs 脚本、图片资源等仍留在分包解包目录中。
        本方法将所有非中间文件合并到主包输出（不覆盖已有文件）。
        """
        merged = 0
        skipped = 0
        main_norm = os.path.normcase(os.path.normpath(main_out_dir))

        for item in os.listdir(work_dir):
            sub_dir = os.path.join(work_dir, item)
            if not os.path.isdir(sub_dir):
                continue
            if os.path.normcase(os.path.normpath(sub_dir)) == main_norm:
                continue
            # 查找含 app-config.json 的 workDir（分包内部结构）
            for root, _dirs, files in os.walk(sub_dir):
                if 'app-config.json' not in files:
                    continue
                # 找到 workDir，复制所有非中间文件到主包输出
                for root2, _d2, files2 in os.walk(root):
                    for fn in files2:
                        # 跳过中间文件
                        if fn in self._SUB_INTERMEDIATE:
                            skipped += 1
                            continue
                        # 跳过 .ori.js（doFrame 失败时的备份）
                        if fn.endswith('.ori.js'):
                            skipped += 1
                            continue
                        src = os.path.join(root2, fn)
                        rel = os.path.relpath(src, root).replace("\\", "/")
                        dst = os.path.join(main_out_dir, rel)
                        # 不覆盖主包已有文件
                        if os.path.isfile(dst):
                            continue
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        try:
                            shutil.copy2(src, dst)
                            merged += 1
                        except Exception:
                            pass
                break  # 找到 workDir 后不再搜索该子目录
        if merged:
            self.log(f"  合并分包文件 {merged} 个到主包（跳过中间文件 {skipped} 个）", "ok")

    # 孤立资源扩展名（图片/字体/wxs/wasm）
    _ORPHAN_RES_EXTS = frozenset({
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp", ".avif",
        ".woff", ".woff2", ".ttf", ".eot", ".otf",
        ".wxs", ".wasm", ".json",
    })

    def _scan_orphaned_resources(self, work_dir, main_out_dir):
        """扫描工作目录中未被合并的孤立资源文件。

        _merge_subpackage_files 基于 workDir（含 app-config.json 的目录）合并文件，
        但部分资源可能在 workDir 之外的层级（如解密产生的嵌套目录、
        wuWxapkg.js 的 findDir 未覆盖的路径）。
        本方法扫描整个工作目录，将遗漏的资源文件合并到主包输出。
        """
        main_norm = os.path.normcase(os.path.normpath(main_out_dir))
        # 构建主包已有文件索引（basename_lower -> [full_path]）
        main_index = _build_file_index(main_out_dir)

        merged = 0
        skipped_dup = 0

        for root_p, _dirs, files in os.walk(work_dir):
            # 跳过主包输出目录及其子目录
            norm_root = os.path.normcase(os.path.normpath(root_p))
            try:
                rel_to_main = os.path.relpath(root_p, main_out_dir)
                if not rel_to_main.startswith(".."):
                    continue
            except ValueError:
                continue

            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in self._ORPHAN_RES_EXTS:
                    continue
                # 跳过中间文件
                if fn in self._SUB_INTERMEDIATE:
                    continue
                # 跳过 .ori.js
                if fn.endswith(".ori.js"):
                    continue
                # 主包已有同名文件 → 跳过
                if fn.lower() in main_index:
                    skipped_dup += 1
                    continue

                src = os.path.join(root_p, fn)
                # 计算目标相对路径：尝试从路径中提取有意义的相对路径
                try:
                    rel = os.path.relpath(src, work_dir).replace("\\", "/")
                    # 去掉可能的解密中间目录前缀（如 "0/0/" 或 "sub_pkg/0/0/"）
                    parts = rel.split("/")
                    # 查找包含分包标识的路径段（如 packageA, pages 等）
                    meaningful_start = -1
                    known_segs = {"pages", "components", "images", "assets", "static",
                                  "img", "icons", "res", "resource", "resources",
                                  "utils", "libs", "lib", "miniprogram_npm"}
                    for i, part in enumerate(parts):
                        if part in known_segs or (part.startswith("package") and part != "package.json"):
                            meaningful_start = i
                            break
                    if meaningful_start >= 0:
                        rel = "/".join(parts[meaningful_start:])
                    else:
                        # 如果没有已知路径段，只取文件名
                        rel = fn
                except Exception:
                    rel = fn

                dst = os.path.join(main_out_dir, rel)
                if os.path.isfile(dst):
                    skipped_dup += 1
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                    merged += 1
                    main_index.setdefault(fn.lower(), []).append(dst)
                except Exception:
                    pass

        if merged:
            self.log(f"  合并孤立资源文件 {merged} 个（跳过重复 {skipped_dup} 个）", "ok")

    def _reorganize_after_merge(self, out_dir):
        """合并后结构重组：修正错位文件、去重、补全目录。

        wuWxapkg.js 处理分包时，部分文件可能落在错误位置：
        - 页面配置 .json 可能缺少分包 root 前缀
        - 资源文件可能散落在根目录
        - 同名文件可能存在多个副本
        本方法读取 app.json 的 subPackages 配置，将文件移到正确位置。
        """
        app_json = os.path.join(out_dir, "app.json")
        if not os.path.isfile(app_json):
            # 没有 app.json 也需要处理 .ori.js 和空目录
            self._reorganize_cleanup(out_dir, 0, 0)
            return
        try:
            with open(app_json, "r", encoding="utf-8-sig") as f:
                app = json.load(f)
        except Exception:
            self._reorganize_cleanup(out_dir, 0, 0)
            return

        sub_pkgs = app.get("subPackages", app.get("subpackages", []))

        moved = 0
        deduped = 0
        content_deduped = 0

        # 收集所有分包 root
        sub_roots = set()
        for sp in sub_pkgs:
            root = sp.get("root", "").strip("/")
            if root:
                sub_roots.add(root)

        # 收集所有合法页面路径（用于判断哪个副本应保留）
        valid_pages = set()
        for pg in app.get("pages", []):
            valid_pages.add(pg.strip("/"))
        for sp in sub_pkgs:
            root = sp.get("root", "").strip("/")
            for pg in sp.get("pages", []):
                full = (root + "/" + pg) if root else pg
                valid_pages.add(full.strip("/"))

        # 1. 修正分包页面文件位置（仅当有分包时）
        for sp in sub_pkgs:
            root = sp.get("root", "").strip("/")
            if not root:
                continue
            sp_pages = sp.get("pages", [])
            if not isinstance(sp_pages, list):
                continue

            for page in sp_pages:
                page = page.strip("/")
                if not page:
                    continue
                # 期望路径: root/page.ext
                for ext in (".js", ".wxml", ".wxss", ".json", ".wxs"):
                    expected = os.path.join(out_dir, root, page + ext)
                    if os.path.isfile(expected):
                        continue  # 已在正确位置
                    # 搜索错位文件：可能在根目录下 page+ext（缺少 root 前缀）
                    misplaced = os.path.join(out_dir, page + ext)
                    if os.path.isfile(misplaced):
                        os.makedirs(os.path.dirname(expected), exist_ok=True)
                        try:
                            shutil.move(misplaced, expected)
                            moved += 1
                        except Exception:
                            pass

        # 2. 内容去重：相同内容的文件只保留一个
        #    优先保留在合法页面路径下的副本，删除其他位置的重复文件
        import hashlib
        size_map = {}  # file_size -> [(full_path, is_valid_location), ...]
        for root_p, _d, files in os.walk(out_dir):
            # 跳过 miniprogram_npm 第三方目录
            if "miniprogram_npm" in root_p:
                continue
            for fn in files:
                if fn.endswith(".ori.js"):
                    continue
                fp = os.path.join(root_p, fn)
                try:
                    fsize = os.path.getsize(fp)
                except OSError:
                    continue
                # 判断是否在合法位置
                rel = os.path.relpath(fp, out_dir).replace("\\", "/")
                stem = os.path.splitext(rel)[0]
                is_valid = stem in valid_pages
                size_map.setdefault(fsize, []).append((fp, is_valid))

        for fsize, entries in size_map.items():
            if len(entries) < 2:
                continue
            # 计算每个文件的 MD5
            hash_groups = {}  # md5 -> [(fp, is_valid), ...]
            for fp, is_valid in entries:
                try:
                    with open(fp, "rb") as f:
                        h = hashlib.md5(f.read()).hexdigest()
                except Exception:
                    continue
                hash_groups.setdefault(h, []).append((fp, is_valid))

            for h, group in hash_groups.items():
                if len(group) < 2:
                    continue
                # 优先保留 is_valid=True 的文件
                valid_ones = [g for g in group if g[1]]
                if valid_ones:
                    # 保留所有合法位置的文件，只删除非法位置的重复
                    valid_paths = {fp for fp, _ in valid_ones}
                    for fp, _ in group:
                        if fp not in valid_paths:
                            try:
                                os.remove(fp)
                                content_deduped += 1
                            except Exception:
                                pass
                else:
                    # 全部非法位置：保留第一个，删除其余
                    keep = group[0][0]
                    for fp, _ in group:
                        if fp != keep:
                            try:
                                os.remove(fp)
                                content_deduped += 1
                            except Exception:
                                pass

        # 3. 去重：同一页面路径下，如果 .js 是占位符但 .js.ori 有真实内容
        for root_p, _d, files in os.walk(out_dir):
            for fn in files:
                if fn.endswith(".ori.js"):
                    ori_fp = os.path.join(root_p, fn)
                    js_fn = fn[:-7] + ".js"  # test.ori.js → test.js
                    js_fp = os.path.join(root_p, js_fn)
                    if os.path.isfile(js_fp):
                        try:
                            ori_size = os.path.getsize(ori_fp)
                            js_size = os.path.getsize(js_fp)
                            # 如果 .js 是占位符（很小）而 .ori.js 有内容，替换
                            if js_size < 100 < ori_size:
                                with open(js_fp, "r", errors="replace") as f:
                                    js_content = f.read()
                                if "Page({" in js_content and len(js_content) < 200:
                                    shutil.copy2(ori_fp, js_fp)
                                    deduped += 1
                        except Exception:
                            pass

        # 4. 清理空目录
        for root_p, dirs, _files in os.walk(out_dir, topdown=False):
            for d in dirs:
                dp = os.path.join(root_p, d)
                try:
                    if not _has_files(dp):
                        os.rmdir(dp)
                except OSError:
                    pass

        if moved:
            self.log(f"  修正错位文件 {moved} 个", "ok")
        if content_deduped:
            self.log(f"  内容去重删除 {content_deduped} 个重复文件", "ok")
        if deduped:
            self.log(f"  恢复占位符文件 {deduped} 个", "ok")

    def _reorganize_cleanup(self, out_dir, moved, deduped):
        """无 app.json 时的兜底清理：处理 .ori.js 和空目录。"""
        for root_p, _d, files in os.walk(out_dir):
            for fn in files:
                if fn.endswith(".ori.js"):
                    ori_fp = os.path.join(root_p, fn)
                    js_fn = fn[:-7] + ".js"
                    js_fp = os.path.join(root_p, js_fn)
                    if os.path.isfile(js_fp):
                        try:
                            ori_size = os.path.getsize(ori_fp)
                            js_size = os.path.getsize(js_fp)
                            if js_size < 100 < ori_size:
                                with open(js_fp, "r", errors="replace") as f:
                                    js_content = f.read()
                                if "Page({" in js_content and len(js_content) < 200:
                                    shutil.copy2(ori_fp, js_fp)
                                    deduped += 1
                        except Exception:
                            pass
        for root_p, dirs, _files in os.walk(out_dir, topdown=False):
            for d in dirs:
                dp = os.path.join(root_p, d)
                try:
                    if not _has_files(dp):
                        os.rmdir(dp)
                except OSError:
                    pass
        if moved:
            self.log(f"  修正错位文件 {moved} 个", "ok")
        if deduped:
            self.log(f"  恢复占位符文件 {deduped} 个", "ok")

    def _pipeline_core(self, filepath, status, work, base, wp, appid,
                       subpackages=None):
        """反编译主流程（由 _pipeline 调用，确保临时目录被清理）。"""
        # 步骤2 解密
        if status == "encrypted":
            self.log("\n[2/10] 解密", "step")
            if not appid:
                appid = self._ask_appid()
                if not appid:
                    self.log("  [错误] 未提供 AppID，终止。", "err"); return
            dpy = resolve_decrypt_python()
            rc = self.run_cmd([dpy, DECRYPT_SCRIPT, "--wxid", appid, "-f", filepath, "-o", wp])
            if rc != 0 or not os.path.isfile(wp):
                self.log("  [错误] 解密失败，请检查 AppID / pycryptodome。", "err"); return
            try:
                with open(wp, "rb") as f:
                    h = f.read(1)
                self.log("  解密成功，校验 0xBE " + ("通过" if h == b'\xBE' else f"异常({h!r})"),
                         "ok" if h == b'\xBE' else "warn")
            except Exception as e:
                self.log(f"  [警告] 校验失败: {e}", "warn")
        else:
            self.log("\n[2/10] 跳过解密（未加密），复制到临时目录", "step")
            try:
                shutil.copy2(filepath, wp)
            except Exception as e:
                self.log(f"  [错误] 复制失败: {e}", "err"); return

        # 步骤3 反编译（-d 保留 page-frame.html 等中间文件供 WXML 恢复使用）
        self.log("\n[3/10] 反编译", "step")
        rc = self.run_cmd([_get_node(), UNPACK_SCRIPT, "-d", wp], cwd=os.path.dirname(UNPACK_SCRIPT))
        out = os.path.join(work, base)
        if not os.path.isdir(out) or not os.listdir(out):
            self.log("  [错误] 未生成输出，请查看日志。", "err"); return
        if rc != 0:
            self.log("  反编译有报错但有输出，按成功处理（plugin-private 等错误不影响主体）。", "warn")
        else:
            self.log("  反编译完成。", "ok")

        # 步骤3b 反编译分包（合并到主包输出目录）
        if subpackages:
            self.log(f"\n[3b/10] 反编译分包（{len(subpackages)} 个，合并到主包）", "step")
            dpy = resolve_decrypt_python()
            ok_cnt = 0
            for i, sub_fp in enumerate(subpackages):
                sub_fn = os.path.basename(sub_fp)
                self.log(f"  ({i+1}/{len(subpackages)}) 处理分包: {sub_fn}", "info")
                # 检测加密
                try:
                    sub_status, sub_desc = self._detect_encryption(sub_fp)
                except Exception as e:
                    self.log(f"    [跳过] 读取文件头失败: {e}", "warn")
                    continue
                # 解密（如需）
                sub_base = os.path.splitext(sub_fn)[0]
                sub_wp = os.path.join(work, sub_base + ".wxapkg")
                if sub_status == "encrypted":
                    if not appid:
                        self.log("    [跳过] 分包已加密但无 AppID", "warn")
                        continue
                    self.log(f"    解密中...", "info")
                    rc2 = self.run_cmd([dpy, DECRYPT_SCRIPT, "--wxid", appid,
                                        "-f", sub_fp, "-o", sub_wp])
                    if rc2 != 0 or not os.path.isfile(sub_wp):
                        self.log("    [跳过] 分包解密失败", "warn")
                        continue
                else:
                    try:
                        shutil.copy2(sub_fp, sub_wp)
                    except Exception as e:
                        self.log(f"    [跳过] 复制失败: {e}", "warn")
                        continue
                # 反编译分包（-s= 指向主包输出目录，-d 保留中间文件）
                self.log(f"    反编译中（合并到主包）...", "info")
                rc2 = self.run_cmd([_get_node(), UNPACK_SCRIPT, "-d",
                                    "-s=" + out, sub_wp],
                                   cwd=os.path.dirname(UNPACK_SCRIPT))
                if rc2 != 0:
                    self.log(f"    [警告] 分包反编译有报错（可能部分成功）", "warn")
                else:
                    self.log(f"    分包反编译完成", "ok")
                    ok_cnt += 1
            self.log(f"  分包合并完成: {ok_cnt}/{len(subpackages)} 成功", "ok" if ok_cnt else "warn")

            # 合并分包产生的孤立文件（.json/.wxs/图片等）到主包输出
            self._merge_subpackage_files(work, out)

            # 步骤3b+ 扫描孤立资源（workDir 之外遗漏的资源文件）
            self._scan_orphaned_resources(work, out)

            # 步骤3c 合并后结构重组（修正错位文件、内容去重）
            self.log("\n  [3c] 结构重组（修正错位文件、内容去重）", "step")
            self._reorganize_after_merge(out)

            # 统计合并后文件数
            _post_merge_count = sum(len(files) for _, _, files in os.walk(out))
            self.log(f"  合并后输出: {_post_merge_count} 个文件", "info")

        # 步骤4 后处理修复
        self.log("\n[4/10] 后处理（修复关键文件）", "step")
        self._post_process(out, appid)

        # 步骤5 WXML 恢复（先于深度校验，避免占位符覆盖真实模板）
        self.log("\n[5/10] WXML 恢复（从 $gwx 还原页面模板）", "step")
        self._restore_wxml(out)

        # 步骤6 深度校验（补全仍缺失的页面/组件/资源）
        self.log("\n[6/10] 深度校验（补全页面/组件/资源）", "step")
        total_pg, complete_pg = self._deep_verify(out, appid)

        # 步骤7 结构规范化
        self.log("\n[7/10] 项目结构规范化", "step")
        self._standardize_structure(out)

        # 步骤7.5 清理中间文件（-d 保留的 page-frame.html 等）
        self._cleanup_intermediates(out)

        # 步骤8 代码工程化美化
        self.log("\n[8/10] 代码工程化美化", "step")
        self._beautify_wxapp_code(out)

        # 步骤9 名称
        self.log("\n[9/10] 提取小程序名称", "step")
        name = self._sanitize_name(self._extract_name(out, appid))
        self.log(f"  名称: {name}", "ok")

        # 步骤10 输出
        self.log("\n[10/10] 整理输出", "step")
        os.makedirs(DESKTOP_DIR, exist_ok=True)
        final = self._unique_dir(DESKTOP_DIR, name)
        self.log(f"  目标: {final}", "info")
        self._copy_tree(out, final)
        self.log("  临时文件将在流程结束后自动清理。", "info")

        self.last_output_dir = final
        self.log("\n========== 反编译完成 ==========", "ok")
        self.log(f"输出目录: {final}", "ok")

        # 质量评估
        metrics = calculate_quality(final, "wxapp")
        self.last_metrics = metrics
        self.last_quality_title = name
        avg = sum(metrics.values()) / len(metrics)
        self.log(f"  质量评分: 综合 {avg:.0f}/100  "
                 + "  ".join(f"{k}{v}" for k, v in metrics.items()), "info")

        self.after(0, lambda: self.open_dir_btn.configure(state="normal"))
        self.after(0, lambda: self.devtools_btn.configure(state="normal"))
        self.after(0, lambda: self.quality_btn.configure(state="normal"))
        self.after(0, lambda: show_radar_chart(self, name, metrics))
        self.after(0, lambda: messagebox.showinfo("完成",
                     f"反编译完成！\n输出目录:\n{final}\n综合评分: {avg:.0f}/100"))

    def show_quality(self):
        if self.last_metrics:
            show_radar_chart(self, self.last_quality_title or "小程序", self.last_metrics)
        else:
            messagebox.showinfo("提示", "请先完成一次反编译。")

    def _ask_appid(self):
        res = {"v": None, "done": False}

        def ask():
            dlg = tk.Toplevel(self)
            dlg.title("输入 AppID"); dlg.geometry("360x150")
            dlg.transient(self.app); dlg.grab_set()
            ttk.Label(dlg, text="未识别到 AppID，请手动输入：").pack(padx=12, pady=(12, 4), anchor="w")
            ttk.Label(dlg, text="（wx 开头18位，如 wx5cd1a5c01fb2282f）").pack(padx=12, anchor="w")
            var = tk.StringVar(); e = ttk.Entry(dlg, textvariable=var)
            e.pack(fill="x", padx=12, pady=8); e.focus_set()
            ttk.Button(dlg, text="确定", command=lambda: (res.__setitem__("v", var.get().strip()), dlg.destroy())).pack(side="left", padx=80, pady=4)
            ttk.Button(dlg, text="取消", command=dlg.destroy).pack(side="left")
            dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
            self.app.wait_window(dlg)
            res["done"] = True

        self.after(0, ask)
        while not res["done"]:
            time.sleep(0.05)
        return res["v"]
    def open_output_dir(self):
        if self.last_output_dir and os.path.isdir(self.last_output_dir):
            try:
                os.startfile(self.last_output_dir)
            except Exception as e:
                messagebox.showerror("打开失败", str(e))
        else:
            messagebox.showinfo("提示", "还没有可打开的输出目录。")

    def open_in_devtools(self):
        if not self.last_output_dir:
            return
        exe = next((c for c in DEVTOOLS_CANDIDATES if c and os.path.isfile(c)), None)
        if not exe:
            messagebox.showwarning("未找到开发者工具",
                                   "未找到微信开发者工具，请手动打开后导入项目：\n" + self.last_output_dir)
            return
        try:
            subprocess.Popen([exe, "cli", "open", "--project", self.last_output_dir], creationflags=NO_WIN)
            self.log("已调用开发者工具打开项目。", "ok")
        except Exception:
            try:
                os.startfile(exe)
            except Exception as e:
                messagebox.showerror("启动失败", str(e))


# --------------------------------------------------------------------------- #
# 标签页 2：网页反编译（整站抓取整理）
# --------------------------------------------------------------------------- #
class WebTab(BaseTab):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.last_output_dir = None
        self.last_metrics = None
        self.last_quality_title = ""
        self._build()

    def _build(self):
        f1 = ttk.LabelFrame(self, text="抓取设置"); f1.pack(fill="x", padx=8, pady=6)
        r = ttk.Frame(f1); r.pack(fill="x", padx=8, pady=6)
        ttk.Label(r, text="起始网址:").pack(side="left")
        self.url_var = tk.StringVar()
        ttk.Entry(r, textvariable=self.url_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Label(r, text="深度:").pack(side="left")
        self.depth_var = tk.IntVar(value=2)
        ttk.Spinbox(r, from_=0, to=10, width=4, textvariable=self.depth_var).pack(side="left", padx=4)
        self.same_domain_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="仅同域名", variable=self.same_domain_var).pack(side="left", padx=4)

        r2 = ttk.Frame(f1); r2.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(r2, text="输出目录:").pack(side="left")
        self.out_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.out_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r2, text="浏览…", width=8, command=self.browse_out).pack(side="left")

        bottom = ttk.Frame(self); bottom.pack(side="bottom", fill="x", padx=8, pady=(4, 8))
        self.start_btn = ttk.Button(bottom, text="▶  开始抓取", command=self.start)
        self.start_btn.pack(side="left", fill="x", expand=True, ipady=8)
        self.quality_btn = ttk.Button(bottom, text="质量评估", state="disabled", command=self.show_quality)
        self.quality_btn.pack(side="left", padx=(8, 0), ipady=8)
        self.open_dir_btn = ttk.Button(bottom, text="打开输出目录", state="disabled", command=self.open_output_dir)
        self.open_dir_btn.pack(side="left", padx=(8, 0), ipady=8)

        ttk.Label(self, text="运行日志").pack(anchor="w", padx=8)
        self.make_log_widget(self)

    def set_busy(self, busy):
        self.start_btn.configure(state="disabled" if busy else "normal")
        if busy:
            self.open_dir_btn.configure(state="disabled")
            self.quality_btn.configure(state="disabled")

    def browse_out(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.out_var.set(d)

    def start(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请输入起始网址。")
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
            self.url_var.set(url)
        out = self.out_var.get().strip()
        if not out:
            try:
                host = urllib.parse.urlparse(url).netloc
            except Exception:
                host = "website"
            out = os.path.join(DESKTOP_DIR, "网站_" + re.sub(r'[\\/:*?"<>|]', "_", host))
            self.out_var.set(out)
        if os.path.exists(out) and os.listdir(out):
            if not messagebox.askyesno("目录非空", f"输出目录已存在且非空:\n{out}\n是否继续（已有文件可能被覆盖）？"):
                return
        self.last_output_dir = None
        self.open_dir_btn.configure(state="disabled")
        self.quality_btn.configure(state="disabled")
        self.clear_log()
        self.start_worker(self._crawl, url, out, int(self.depth_var.get()), bool(self.same_domain_var.get()))

    def _crawl(self, start_url, out_dir, max_depth, same_domain):
        try:
            import requests
            from bs4 import BeautifulSoup
        except Exception as e:
            self.log(f"[错误] 缺少依赖 requests/bs4/lxml：{e}\n  请执行: pip install requests beautifulsoup4 lxml", "err")
            return
        self.log("========== 开始抓取网站 ==========", "step")
        self.log(f"起始: {start_url}", "info")
        self.log(f"输出: {out_dir}  深度: {max_depth}  仅同域: {same_domain}", "info")

        sess = requests.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
        # 尝试连接，若 SSL 失败则禁用验证重试
        ssl_verify = True
        try:
            sess.head(start_url, timeout=10, allow_redirects=True)
        except requests.exceptions.SSLError:
            ssl_verify = False
            self.log("  [警告] SSL 证书验证失败，已禁用 SSL 验证", "warn")
        except Exception:
            pass  # 其他错误在后续 fetch 中处理
        base = urllib.parse.urlparse(start_url)
        domain = base.netloc
        os.makedirs(out_dir, exist_ok=True)

        visited = set()
        asset_cache = {}   # url -> local path
        page_cache = {}    # url -> local path
        MAX_PAGES = 800

        def url_to_local(u):
            p = urllib.parse.urlparse(u)
            path = urllib.parse.unquote(p.path or "/")
            if path == "" or path.endswith("/"):
                path += "index.html"
            path = path.lstrip("/")
            parts = [re.sub(r'[<>:"|?*]', "_", seg) for seg in path.split("/") if seg not in ("", ".", "..")]
            if parts and "." not in os.path.splitext(parts[-1])[1]:
                parts[-1] = parts[-1] + ".html"
            if not parts:
                parts = ["index.html"]

            # Windows 保留名处理 (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
            win_reserved = {"CON", "PRN", "AUX", "NUL",
                            "COM1", "COM2", "COM3", "COM4", "COM5",
                            "COM6", "COM7", "COM8", "COM9",
                            "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
                            "LPT6", "LPT7", "LPT8", "LPT9"}
            for i, seg in enumerate(parts):
                name_no_ext = os.path.splitext(seg)[0].upper()
                if name_no_ext in win_reserved:
                    parts[i] = "_" + seg

            # 查询字符串消歧：不同 query 的同路径 URL 加短哈希后缀
            # 例外：_next/static/ 资源的 ?dpl= 是 Vercel 部署残留，非内容区分，跳过哈希
            # （否则同一 chunk 会被存为 xxx_<hash>.js，由后处理统一去后缀）
            if p.query and '/_next/static/' not in ('/' + path):
                import hashlib
                qhash = hashlib.md5(p.query.encode()).hexdigest()[:6]
                name, ext = os.path.splitext(parts[-1])
                parts[-1] = f"{name}_{qhash}{ext}"

            # 路径长度限制（Windows 260 字符限制）
            full_path = os.path.join(out_dir, *parts)
            if len(full_path) > 240:
                # 截断最末段文件名
                name, ext = os.path.splitext(parts[-1])
                max_name = 240 - len(out_dir) - sum(len(s) + 1 for s in parts[:-1]) - len(ext)
                if max_name < 10:
                    max_name = 20
                parts[-1] = name[:max_name] + ext

            return os.path.join(out_dir, *parts)

        def rel_link(from_file, to_file):
            return os.path.relpath(to_file, os.path.dirname(from_file)).replace("\\", "/")

        def fetch(u, binary=False, retries=2):
            # 跳过非 HTTP(S) URL（防止本地文件路径被误当作 URL 请求）
            if not u or not u.startswith(("http://", "https://")):
                return None
            for attempt in range(retries + 1):
                try:
                    r = sess.get(u, timeout=25, allow_redirects=True, verify=ssl_verify)
                    if r.status_code == 200:
                        return r
                    elif r.status_code in (429, 503) and attempt < retries:
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    elif r.status_code == 403 and attempt == 0:
                        # 尝试添加 Referer 头
                        sess.headers["Referer"] = u.rsplit("/", 1)[0] + "/"
                        continue
                    elif r.status_code in (301, 302, 303, 307, 308):
                        # 重定向已由 allow_redirects=True 处理，此处不应到达
                        return r
                    return None
                except requests.exceptions.SSLError:
                    if ssl_verify and attempt == 0:
                        # 首次 SSL 失败，禁用验证重试
                        try:
                            r = sess.get(u, timeout=25, allow_redirects=True, verify=False)
                            if r.status_code == 200:
                                return r
                        except Exception:
                            pass
                    return None
                except requests.exceptions.Timeout:
                    if attempt < retries:
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    self.log(f"  [跳过] 请求超时: {u}", "warn")
                    return None
                except Exception as e:
                    if attempt < retries:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    self.log(f"  [跳过] 请求失败: {u} — {e}", "warn")
                    return None
            return None

        def download_asset(abs_url, from_file=None):
            if abs_url in asset_cache:
                cached = asset_cache[abs_url]
                if from_file:
                    return rel_link(from_file, cached)
                return cached
            r = fetch(abs_url)
            if r is None:
                return None
            local = url_to_local(abs_url)
            os.makedirs(os.path.dirname(local), exist_ok=True)
            ctype = r.headers.get("Content-Type", "").lower()

            # 根据内容类型修正扩展名
            url_path = abs_url.lower().split("?")[0]
            ext_map = {
                "text/css": ".css", "javascript": ".js",
                "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                "image/svg+xml": ".svg", "image/webp": ".webp", "image/x-icon": ".ico",
                "image/bmp": ".bmp", "image/avif": ".avif",
                "font/woff": ".woff", "font/woff2": ".woff2",
                "application/font-woff": ".woff", "application/font-woff2": ".woff2",
                "application/x-font-ttf": ".ttf", "application/vnd.ms-fontobject": ".eot",
                "font/ttf": ".ttf", "font/otf": ".otf",
                "video/mp4": ".mp4", "video/webm": ".webm", "audio/mpeg": ".mp3", "audio/ogg": ".ogg",
                "application/json": ".json", "application/xml": ".xml",
                "application/manifest+json": ".webmanifest",
                "text/csv": ".csv", "text/plain": ".txt",
            }
            current_ext = os.path.splitext(local)[1].lower()
            if not current_ext or current_ext == ".html":
                for ct_key, ext_val in ext_map.items():
                    if ct_key in ctype:
                        local = local.rsplit(".", 1)[0] + ext_val if "." in os.path.basename(local) else local + ext_val
                        break

            if url_path.endswith(".css") or "css" in ctype:
                txt = r.content.decode(detect_encoding(r), errors="replace")
                txt = rewrite_css(txt, abs_url, local)
                # 提取 base64 图片为独立文件
                txt, _ = _extract_base64_images(
                    txt, out_dir, os.path.dirname(local),
                    prefix=os.path.splitext(os.path.basename(local))[0])
                with open(local, "w", encoding="utf-8") as f:
                    f.write(txt)
            else:
                with open(local, "wb") as f:
                    f.write(r.content)
            asset_cache[abs_url] = local
            if from_file:
                return rel_link(from_file, local)
            return local

        def download_asset_safe(abs_url, from_file=None):
            """下载资源，失败时创建占位文件并返回相对路径。"""
            result = download_asset(abs_url, from_file)
            if result is not None:
                return result
            # 下载失败：计算预期本地路径并创建占位
            local = url_to_local(abs_url)
            if not os.path.isfile(local):
                os.makedirs(os.path.dirname(local), exist_ok=True)
                ext = os.path.splitext(local)[1].lower()
                if ext in (".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".svg", ".avif"):
                    with open(local, "wb") as f:
                        f.write(_PNG1x1)
                elif ext == ".css":
                    with open(local, "w", encoding="utf-8") as f:
                        f.write(f"/* placeholder for {abs_url} */\n")
                elif ext in (".js", ".mjs"):
                    with open(local, "w", encoding="utf-8") as f:
                        f.write(f"// placeholder for {abs_url}\n")
                else:
                    with open(local, "wb") as f:
                        f.write(b"")
            asset_cache[abs_url] = local
            if from_file:
                return rel_link(from_file, local)
            return local

        def rewrite_css(txt, css_url, css_local):
            def repl(m):
                raw = m.group(2).strip().strip("'\"")
                if raw.startswith(("data:", "#", "http://", "https://", "//")):
                    return m.group(0)
                # 跳过 format() 提示（@font-face src: url(...) format("...")）
                if raw.startswith("format(") or raw == "":
                    return m.group(0)
                absu = urllib.parse.urljoin(css_url, raw)
                lp = download_asset_safe(absu, css_local)
                if lp:
                    return f"url({m.group(1)}{lp}{m.group(1)})"
                return m.group(0)
            txt = re.sub(r'url\((\s*[\'"]?)([^\'")]+)([\'"]?\s*)\)', repl, txt)

            # 处理 @import "xxx.css" 或 @import url("xxx.css")
            def imp_repl(m):
                raw = m.group(2).strip().strip("'\"")
                if raw.startswith(("data:", "http://", "https://", "//")):
                    return m.group(0)
                absu = urllib.parse.urljoin(css_url, raw)
                lp = download_asset_safe(absu, css_local)
                if lp:
                    return f"@import {m.group(1)}{lp}{m.group(1)}"
                return m.group(0)
            txt = re.sub(r'@import\s+(["\'])([^"\']+)\1', imp_repl, txt)

            # 处理 content: url(...) 属性中的图片引用
            def content_repl(m):
                raw = m.group(1).strip().strip("'\"")
                if raw.startswith(("data:", "#", "http://", "https://", "//")):
                    return m.group(0)
                absu = urllib.parse.urljoin(css_url, raw)
                lp = download_asset_safe(absu, css_local)
                if lp:
                    return f"url({lp})"
                return m.group(0)
            txt = re.sub(r'content:\s*url\(\s*[\'"]?([^\'")]+)[\'"]?\s*\)', content_repl, txt)

            return txt

        # BFS
        url_aliases = {}  # original_url -> final_url (redirect tracking)
        q = collections.deque([(start_url, 0)])
        visited.add(start_url)
        pages_done = 0
        while q:
            cur_url, depth = q.popleft()
            r = fetch(cur_url)
            if r is None:
                continue
            # 追踪重定向：如果最终 URL 与请求 URL 不同，记录别名
            final_url = str(r.url) if hasattr(r, 'url') and r.url else cur_url
            if final_url != cur_url:
                url_aliases[cur_url] = final_url
                # 如果最终 URL 尚未被访问，也用最终 URL 的路径
                if final_url not in visited:
                    visited.add(final_url)
            # 使用最终 URL 计算本地路径（确保链接一致性）
            effective_url = final_url if final_url != cur_url else cur_url

            ctype = r.headers.get("Content-Type", "").lower()
            if ("html" not in ctype and "xml" not in ctype
                    and not effective_url.lower().endswith((".html", ".htm", "/", ".xhtml"))):
                # 当作资源下载
                download_asset_safe(effective_url)
                continue
            enc = detect_encoding(r)
            html = r.content.decode(enc, errors="replace")
            # 空页面保护：如果内容为空或过短，创建最小 HTML
            if not html or len(html.strip()) < 10:
                html = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>页面</title></head><body><h1>页面内容为空</h1></body></html>"""
            soup = BeautifulSoup(html, "lxml")
            page_local = url_to_local(effective_url)
            os.makedirs(os.path.dirname(page_local), exist_ok=True)
            # 如果发生了重定向，也为原始 URL 创建本地路径映射
            if effective_url != cur_url:
                orig_local = url_to_local(cur_url)
                if os.path.normpath(orig_local) != os.path.normpath(page_local):
                    page_cache[cur_url] = page_local  # 别名指向同一文件

            # 资源标签（含扩展懒加载属性）
            asset_specs = [
                ("img", "src"), ("script", "src"), ("link", "href"),
                ("source", "src"), ("video", "src"), ("video", "poster"),
                ("audio", "src"), ("iframe", "src"), ("embed", "src"), ("object", "data"),
                ("img", "data-src"), ("img", "data-original"),
                ("img", "data-lazy"), ("img", "data-lazy-src"),
                ("img", "data-original-src"), ("img", "data-image"),
                ("div", "data-bg"), ("div", "data-background"),
                ("section", "data-bg"), ("section", "data-background"),
                ("video", "data-poster"), ("video", "data-thumb"),
                ("input", "src"), ("track", "src"), ("area", "href"),
                ("image", "href"), ("image", "xlink:href"),  # SVG image
                ("use", "href"), ("use", "xlink:href"),  # SVG use
                ("tref", "xlink:href"),  # SVG tref
            ]
            for tag, attr in asset_specs:
                for node in soup.find_all(tag):
                    val = node.get(attr)
                    if not val:
                        continue
                    new = self._process_refs(val, effective_url, page_local, download_asset_safe, None)
                    if new is not None:
                        node[attr] = new

            # 处理 <noscript> 内部资源（noscript 包含完整 HTML，可能有 img 等）
            for ns in soup.find_all("noscript"):
                ns_html = str(ns)
                ns_soup = BeautifulSoup(ns_html, "lxml")
                for tag, attr in asset_specs:
                    for node in ns_soup.find_all(tag):
                        val = node.get(attr)
                        if not val:
                            continue
                        new = self._process_refs(val, effective_url, page_local, download_asset_safe, None)
                        if new is not None:
                            node[attr] = new
                # 替换 noscript 内容
                ns.clear()
                for child in ns_soup.body.children if ns_soup.body else []:
                    ns.append(child)

            # 处理 <template> 内部资源
            for tpl in soup.find_all("template"):
                tpl_html = str(tpl)
                tpl_soup = BeautifulSoup(tpl_html, "lxml")
                for tag, attr in asset_specs:
                    for node in tpl_soup.find_all(tag):
                        val = node.get(attr)
                        if not val:
                            continue
                        new = self._process_refs(val, effective_url, page_local, download_asset_safe, None)
                        if new is not None:
                            node[attr] = new
                # 更新 template 内容
                tpl.clear()
                for child in tpl_soup.body.children if tpl_soup.body else []:
                    tpl.append(child)

            # link[rel] 资源: preload/prefetch/manifest/icon/apple-touch-icon
            for node in soup.find_all("link", href=True):
                rel = (node.get("rel") or [""])[0].lower() if isinstance(node.get("rel"), list) else (node.get("rel") or "").lower()
                if rel in ("preload", "prefetch", "manifest", "icon", "shortcut icon",
                           "apple-touch-icon", "apple-touch-icon-precomposed",
                           "modulepreload", "dns-prefetch"):
                    new = self._process_refs(node["href"], effective_url, page_local, download_asset_safe, None)
                    if new is not None:
                        node["href"] = new
            # meta property=og:image / name=twitter:image
            for node in soup.find_all("meta"):
                prop = (node.get("property") or node.get("name") or "").lower()
                if prop in ("og:image", "og:image:url", "og:image:secure_url",
                            "twitter:image", "twitter:image:src"):
                    val = node.get("content")
                    if val:
                        new = self._process_refs(val, effective_url, page_local, download_asset_safe, None)
                        if new is not None:
                            node["content"] = new
            # srcset (img, source)
            for node in soup.find_all(attrs={"srcset": True}):
                new = self._process_refs(node["srcset"], effective_url, page_local, download_asset_safe, srcset=True)
                if new is not None:
                    node["srcset"] = new
            # data-srcset (lazy load)
            for node in soup.find_all(attrs={"data-srcset": True}):
                new = self._process_refs(node["data-srcset"], effective_url, page_local, download_asset_safe, srcset=True)
                if new is not None:
                    node["data-srcset"] = new
            # 内联 style url()
            for st in soup.find_all("style"):
                if st.string:
                    css_txt = rewrite_css(st.string, effective_url, page_local)
                    css_txt, _ = _extract_base64_images(
                        css_txt, out_dir, os.path.dirname(page_local),
                        prefix="inline")
                    st.string = css_txt
            # style 属性
            for node in soup.find_all(style=True):
                css_txt = rewrite_css(node["style"], effective_url, page_local)
                css_txt, _ = _extract_base64_images(
                    css_txt, out_dir, os.path.dirname(page_local),
                    prefix="inline_style")
                node["style"] = css_txt

            # 处理 <script> 内联代码中的 URL 引用（AJAX 端点、动态资源路径）
            for script in soup.find_all("script"):
                if not script.string or not script.get("type", ""):
                    js_code = script.string
                elif script.get("type", "").lower() in ("", "text/javascript", "application/javascript", "module"):
                    js_code = script.string
                else:
                    continue
                if not js_code:
                    continue
                # 捕获 JS 中的字符串 URL（/path/to/resource.ext 格式）
                rewritten = self._rewrite_js_urls(js_code, effective_url, page_local, download_asset_safe)
                if rewritten != js_code:
                    script.string = rewritten

            # 处理 source map 引用（//# sourceMappingURL=xxx.map）
            for script in soup.find_all("script", src=True):
                src_val = script.get("src", "")
                # 尝试下载对应的 .map 文件
                if src_val and not src_val.startswith(("data:", "http://", "https://")):
                    # 已经被改写为本地路径
                    local_js = os.path.normpath(os.path.join(os.path.dirname(page_local), src_val.split("?")[0]))
                    if os.path.isfile(local_js):
                        try:
                            with open(local_js, "r", encoding="utf-8", errors="replace") as jf:
                                js_content = jf.read()
                            # 查找 sourceMappingURL 注释
                            sm_match = re.search(r'//#\s*sourceMappingURL=(.+?)(?:\s|$)', js_content)
                            if sm_match:
                                sm_url = sm_match.group(1).strip().strip("'\"")
                                if not sm_url.startswith(("data:", "http://", "https://")):
                                    sm_abs = urllib.parse.urljoin(effective_url, sm_url)
                                    sm_local = download_asset_safe(sm_abs, local_js)
                                    # 改写 sourceMappingURL 为本地路径
                                    if sm_local:
                                        js_content = re.sub(
                                            r'//#\s*sourceMappingURL=.+?(?:\s|$)',
                                            f'//# sourceMappingURL={sm_local}\n', js_content)
                                        with open(local_js, "w", encoding="utf-8") as jf:
                                            jf.write(js_content)
                        except Exception:
                            pass

            # 处理 CSS 文件中的 source map
            for link in soup.find_all("link", rel=True):
                rel_val = (link.get("rel") or [""])[0].lower() if isinstance(link.get("rel"), list) else (link.get("rel") or "").lower()
                if rel_val == "stylesheet" and link.get("href"):
                    href_val = link["href"]
                    local_css = os.path.normpath(os.path.join(os.path.dirname(page_local), href_val.split("?")[0]))
                    if os.path.isfile(local_css):
                        try:
                            with open(local_css, "r", encoding="utf-8", errors="replace") as cf:
                                css_content = cf.read()
                            sm_match = re.search(r'/\*#\s*sourceMappingURL=(.+?)\s*\*/', css_content)
                            if sm_match:
                                sm_url = sm_match.group(1).strip().strip("'\"")
                                if not sm_url.startswith(("data:", "http://", "https://")):
                                    sm_abs = urllib.parse.urljoin(effective_url, sm_url)
                                    sm_local = download_asset_safe(sm_abs, local_css)
                                    # 改写 sourceMappingURL 为本地路径
                                    if sm_local:
                                        css_content = re.sub(
                                            r'/\*#\s*sourceMappingURL=.+?\s*\*/',
                                            f'/*# sourceMappingURL={sm_local} */', css_content)
                                        with open(local_css, "w", encoding="utf-8") as cf:
                                            cf.write(css_content)
                        except Exception:
                            pass

            # form action 改写
            for form in soup.find_all("form", action=True):
                action = form["action"].strip()
                if action and not action.startswith(("#", "javascript:", "data:")):
                    if action.startswith("//"):
                        action = "https:" + action
                    if not action.startswith(("http://", "https://")):
                        action = urllib.parse.urljoin(effective_url, action)
                    pu = urllib.parse.urlparse(action)
                    if pu.netloc == domain or not same_domain:
                        tgt_local = url_to_local(action)
                        form["action"] = rel_link(page_local, tgt_local)

            # 处理 <meta http-equiv="refresh"> 跳转
            for meta in soup.find_all("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)}):
                content_val = meta.get("content", "")
                # 格式: "0;url=http://example.com/page" 或 "5; url=/path"
                m = re.match(r'\d+\s*;\s*url\s*=\s*(.+)', content_val, re.I)
                if m:
                    refresh_url = m.group(1).strip().strip("'\"")
                    if refresh_url and not refresh_url.startswith(("#", "javascript:", "data:")):
                        if refresh_url.startswith("//"):
                            refresh_url = (urllib.parse.urlparse(effective_url).scheme or "https") + ":" + refresh_url
                        if not refresh_url.startswith(("http://", "https://")):
                            refresh_url = urllib.parse.urljoin(effective_url, refresh_url)
                        pu = urllib.parse.urlparse(refresh_url)
                        if pu.netloc == domain or not same_domain:
                            tgt_local = url_to_local(refresh_url)
                            meta["content"] = f"0; url={rel_link(page_local, tgt_local)}"
                            # 将目标页面加入队列
                            if depth + 1 <= max_depth and refresh_url not in visited and len(visited) < MAX_PAGES:
                                visited.add(refresh_url)
                                q.append((refresh_url, depth + 1))

            # 注入 viewport meta（如果缺失）
            if not soup.find("meta", attrs={"name": "viewport"}):
                viewport_meta = soup.new_tag("meta", attrs={"name": "viewport",
                    "content": "width=device-width, initial-scale=1.0"})
                if soup.head:
                    soup.head.append(viewport_meta)

            # 移除 <base href> 标签（已通过 urljoin 处理相对路径，保留 base 会导致双重解析）
            for base_tag in soup.find_all("base"):
                base_tag.decompose()

            # 页面链接 <a href>：抓取 + 改写为本地
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
                    continue
                absu = urllib.parse.urljoin(effective_url, href).split("#")[0]
                if not absu.startswith(("http://", "https://")):
                    continue
                pu = urllib.parse.urlparse(absu)
                is_same = (pu.netloc == domain)
                # 检查是否通过重定向别名已知
                if absu in url_aliases:
                    absu = url_aliases[absu]
                # 改写为本页面本地相对路径（若将被抓取则有效，否则保留外链）
                if is_same or not same_domain:
                    tgt_local = url_to_local(absu)
                    a["href"] = rel_link(page_local, tgt_local)
                    if (is_same or not same_domain) and depth + 1 <= max_depth and absu not in visited and len(visited) < MAX_PAGES:
                        visited.add(absu)
                        q.append((absu, depth + 1))
                else:
                    a["href"] = absu  # 外部链接保持原样

            # 确保 charset 声明为 utf-8（文件实际以 utf-8 写入）
            # 1. 修正 <meta charset="xxx"> 标签
            for m in soup.find_all("meta", attrs={"charset": True}):
                m["charset"] = "utf-8"
            # 2. 修正 <meta http-equiv="Content-Type" content="text/html; charset=xxx"> 标签
            for m in soup.find_all("meta", attrs={"http-equiv": re.compile("content-type", re.I)}):
                c = m.get("content", "")
                m["content"] = re.sub(r"charset=[a-z0-9_-]+", "charset=utf-8", c, flags=re.I)
            # 3. 如果没有任何 charset 声明，添加一个
            if not soup.find("meta", attrs={"charset": True}) and not soup.find("meta", attrs={"content": re.compile("charset=utf-8", re.I)}):
                meta = soup.new_tag("meta", charset="utf-8")
                if soup.head:
                    soup.head.insert(0, meta)
            with open(page_local, "w", encoding="utf-8") as f:
                # 保留/补全 doctype 声明
                page_html = str(soup)
                # 修正 XML 声明中的 encoding（XHTML 页面可能有 <?xml encoding="gbk"?>）
                page_html = re.sub(
                    r'(<\?xml[^>]*encoding=["\'])[^"\']+(["\'])',
                    r'\1utf-8\2', page_html, flags=re.I)
                if "<!DOCTYPE" not in page_html[:200].upper() and "<!doctype" not in page_html[:200]:
                    page_html = "<!DOCTYPE html>\n" + page_html
                f.write(page_html)
            page_cache[cur_url] = page_local
            if effective_url != cur_url:
                page_cache[effective_url] = page_local
            pages_done += 1
            self.log(f"  [{pages_done}] 保存页面: {effective_url}  (队列 {len(q)})", "info")
            time.sleep(0.15)

        self.log(f"\n抓取完成: 页面 {pages_done}，资源 {len(asset_cache)}。", "ok")

        # 下载 favicon.ico
        favicon_url = f"{base.scheme}://{domain}/favicon.ico"
        favicon_local = os.path.join(out_dir, "favicon.ico")
        if not os.path.isfile(favicon_local):
            r = fetch(favicon_url)
            if r is not None:
                with open(favicon_local, "wb") as f:
                    f.write(r.content)
                self.log("  已下载 favicon.ico", "ok")

        # 确保根目录 index.html
        root_index = os.path.join(out_dir, "index.html")
        start_local = url_to_local(start_url)
        if not os.path.isfile(root_index) and os.path.isfile(start_local):
            if os.path.normpath(root_index) != os.path.normpath(start_local):
                shutil.copy2(start_local, root_index)
            self.log("  已确保根目录 index.html", "ok")
        elif os.path.isfile(start_local) and os.path.normpath(root_index) == os.path.normpath(start_local):
            # 起始页就是 index.html，无需额外处理
            pass
        if not os.path.isfile(root_index) and os.path.isfile(start_local):
            shutil.copy2(start_local, root_index)

        # 完整性校验
        self.log("\n===== 网站完整性校验 =====", "step")
        integrity = self._verify_integrity(out_dir, domain, base.scheme, sess, asset_cache)
        self.log(f"  本地资源引用: {integrity['total_refs']}，缺失: {integrity['missing']}，已修复: {integrity['fixed']}", "info")
        if integrity['missing'] > 0 and integrity['fixed'] < integrity['missing']:
            self.log(f"  [警告] 仍有 {integrity['missing'] - integrity['fixed']} 个资源无法获取", "warn")
        else:
            self.log("  所有资源引用已解决。", "ok")

        # Next.js 站点规范化（修复 Vercel Skew Protection 残留）
        if normalize_nextjs_site is not None and is_nextjs_site(out_dir):
            self.log("\n===== Next.js 站点规范化 =====", "step")
            try:
                nj = normalize_nextjs_site(out_dir, log=self.log, backup=False)
                self.log(f"  文件重命名 {nj.get('renamed', 0)}，去重 {nj.get('deduped', 0)}，"
                         f"引用改写 {sum(nj.get('refs', {}).values())}", "ok")
            except Exception as e:
                self.log(f"  [警告] Next.js 规范化失败: {e}", "warn")

        # 项目结构规范化
        self.log("\n===== 项目结构规范化 =====", "step")
        self._standardize_web_structure(out_dir)

        # 代码美化
        self.log("\n===== 代码美化 =====", "step")
        self._beautify_web_code(out_dir)

        self.log(f"\n输出目录: {out_dir}", "ok")
        self.last_output_dir = out_dir

        # 质量评估
        metrics = calculate_quality(out_dir, "web")
        self.last_metrics = metrics
        title = f"网站_{domain}"
        self.last_quality_title = title
        avg = sum(metrics.values()) / len(metrics)
        self.log(f"  质量评分: 综合 {avg:.0f}/100  "
                 + "  ".join(f"{k}{v}" for k, v in metrics.items()), "info")

        self.after(0, lambda: self.open_dir_btn.configure(state="normal"))
        self.after(0, lambda: self.quality_btn.configure(state="normal"))
        self.after(0, lambda: show_radar_chart(self, title, metrics))
        self.after(0, lambda: messagebox.showinfo("完成",
                     f"抓取完成！\n页面 {pages_done} 个，资源 {len(asset_cache)} 个。\n"
                     f"完整性: {integrity['total_refs'] - integrity['missing'] + integrity['fixed']}/{integrity['total_refs']} 引用已解决\n"
                     f"综合评分: {avg:.0f}/100\n输出: {out_dir}"))

    @staticmethod
    def _process_refs(val, page_url, page_local, download_asset, srcset=False):
        """处理单个属性值中的 URL（支持 srcset 多 URL），返回改写后的值。"""
        def one(raw):
            raw = raw.strip()
            if not raw or raw.startswith(("data:", "#", "mailto:", "tel:", "javascript:")):
                return raw, False
            # 跳过 Windows 本地文件路径（如 C:/... 或 C:\...），防止误当作 URL
            if re.match(r'^[A-Za-z]:[\\/]', raw):
                return raw, False
            if raw.startswith("//"):
                # 使用页面 URL 的协议（http 或 https）
                page_scheme = urllib.parse.urlparse(page_url).scheme or "https"
                raw = page_scheme + ":" + raw
            if not raw.startswith(("http://", "https://")):
                raw = urllib.parse.urljoin(page_url, raw)
            lp = download_asset(raw, page_local)
            return (lp if lp else raw), True

        if srcset:
            out = []
            for item in val.split(","):
                item = item.strip()
                if not item:
                    continue
                parts = item.split()
                url = parts[0]
                new, ok = one(url)
                parts[0] = new
                out.append(" ".join(parts))
            return ", ".join(out)
        else:
            new, ok = one(val)
            return new

    @staticmethod
    def _rewrite_js_urls(js_code, page_url, page_local, download_asset):
        """保守地改写内联 JS 中的资源 URL 字符串。

        处理引号内、以 / ./ ../ 开头且以资源扩展名结尾的路径。
        避免：破坏模板字符串、正则表达式、非 URL 字符串。
        """
        resource_exts = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp',
                         '.ico', '.css', '.js', '.mjs', '.woff', '.woff2', '.ttf',
                         '.otf', '.eot', '.mp4', '.webm', '.ogg', '.mp3', '.wav',
                         '.json', '.xml', '.html', '.htm', '.pdf')

        def repl(m):
            quote = m.group(1)
            path = m.group(2)
            # 跳过以 // 开头的协议相对 URL
            if path.startswith('//'):
                return m.group(0)
            # 检查是否以资源扩展名结尾（忽略查询参数和锚点）
            clean_path = path.split('?')[0].split('#')[0].lower()
            if not clean_path.endswith(resource_exts):
                return m.group(0)
            # 下载并改写
            try:
                absu = urllib.parse.urljoin(page_url, path)
                lp = download_asset(absu, page_local)
                if lp:
                    return f"{quote}{lp}{quote}"
            except Exception:
                pass
            return m.group(0)

        # 匹配 "..." 或 '...' 中包含 / ./ ../ 开头的资源路径
        pattern = r'(["\'])((?:\./|\.\./|/)[^\]\s"\'`<>{}]+\.(?:png|jpg|jpeg|gif|svg|webp|bmp|ico|css|js|mjs|woff|woff2|ttf|otf|eot|mp4|webm|ogg|mp3|wav|json|xml|html|htm|pdf)(?:\?[^"\'`]*)?)(["\'])'
        return re.sub(pattern, repl, js_code, flags=re.I)

    def _verify_integrity(self, out_dir, domain, scheme, sess, asset_cache):
        """校验网站完整性：扫描所有本地引用，补全缺失资源。"""
        from bs4 import BeautifulSoup

        total_refs = 0
        missing = 0
        fixed = 0
        fuzzy_fixed = 0
        b64_extracted = 0
        url_map = {}  # local_path -> original_url (逆映射)
        for abs_url, local in asset_cache.items():
            url_map[os.path.normpath(local)] = abs_url

        # 构建文件索引用于模糊匹配
        _file_idx = _build_file_index(out_dir)
        _img_idx = _build_file_index(out_dir, _IMG_EXTS_SET)

        # 资源属性列表（与抓取阶段保持一致）
        res_attrs = [
            ("img", "src"), ("script", "src"), ("link", "href"),
            ("source", "src"), ("video", "src"), ("video", "poster"),
            ("audio", "src"), ("iframe", "src"), ("embed", "src"), ("object", "data"),
            ("img", "data-src"), ("img", "data-original"),
            ("img", "data-lazy"), ("img", "data-lazy-src"),
            ("img", "data-original-src"), ("img", "data-image"),
            ("div", "data-bg"), ("div", "data-background"),
            ("section", "data-bg"), ("section", "data-background"),
            ("video", "data-poster"), ("video", "data-thumb"),
            ("input", "src"), ("track", "src"), ("area", "href"),
            ("image", "href"), ("image", "xlink:href"),
            ("use", "href"), ("use", "xlink:href"),
        ]

        # 1. 扫描所有 HTML 和 CSS 文件（单次 os.walk）
        html_files = []
        css_files = []
        for root_p, _d, files in os.walk(out_dir):
            for fn in files:
                if fn.endswith((".html", ".htm")):
                    html_files.append(os.path.join(root_p, fn))
                elif fn.endswith(".css"):
                    css_files.append(os.path.join(root_p, fn))

        self.log(f"  扫描 {len(html_files)} 个 HTML 文件，{len(css_files)} 个 CSS 文件...", "info")

        for html_file in html_files:
            try:
                with open(html_file, "r", encoding="utf-8", errors="replace") as f:
                    html = f.read()
                soup = BeautifulSoup(html, "lxml")
            except Exception:
                continue

            file_dir = os.path.dirname(html_file)

            # 检查资源引用
            refs_in_page = []
            for tag, attr in res_attrs:
                for node in soup.find_all(tag):
                    val = node.get(attr)
                    if val:
                        refs_in_page.append(val)
            for node in soup.find_all(attrs={"srcset": True}):
                refs_in_page.append(node["srcset"])
            for node in soup.find_all(attrs={"data-srcset": True}):
                refs_in_page.append(node["data-srcset"])
            for node in soup.find_all("link", href=True):
                refs_in_page.append(node["href"])
            for st in soup.find_all("style"):
                if st.string:
                    # 从 <style> 标签内容中只提取 url(...) 引用
                    for url_m in re.finditer(r'url\(\s*[\'"]?([^\'")]+)[\'"]?\s*\)', st.string):
                        refs_in_page.append(url_m.group(1).strip())
            for node in soup.find_all(style=True):
                # 从 style 属性中只提取 url(...) 引用
                style_val = node["style"]
                for url_m in re.finditer(r'url\(\s*[\'"]?([^\'")]+)[\'"]?\s*\)', style_val):
                    refs_in_page.append(url_m.group(1).strip())

            for ref in refs_in_page:
                # 将 srcset "url1 1x, url2 2x" 拆分为单个 URL
                individual_urls = []
                if "," in ref:
                    # 多 URL srcset: "url1 1x, url2 2x"
                    for item in ref.split(","):
                        item = item.strip()
                        if item:
                            parts = item.split()
                            if parts:
                                individual_urls.append(parts[0])
                            else:
                                individual_urls.append(item)
                elif " " in ref:
                    # 单 URL srcset: "url 1x" 或 CSS 多值
                    parts = ref.strip().split()
                    if parts:
                        individual_urls.append(parts[0])
                else:
                    individual_urls.append(ref.strip())

                for ref_url in individual_urls:
                    if not ref_url or ref_url.startswith(("data:", "#", "mailto:", "tel:",
                                                           "javascript:", "http://", "https://", "//")):
                        continue
                    # 跳过 Windows 本地文件路径（如 C:/... 或 C:\...）
                    if re.match(r'^[A-Za-z]:[\\/]', ref_url):
                        continue
                    # 跳过包含 Windows 非法文件名字符的引用（如 CSS 属性值）
                    # 允许的字符：字母、数字、/_-.~+%?=&#:- 以及常见 URL 字符
                    # 但排除包含 ; 或连续 CSS 属性模式（如 min-height:10rem）的值
                    if ';' in ref_url or '%' in ref_url.split('?')[0]:
                        continue
                    # 跳过看起来像 CSS 属性值（xxx:yyy 格式但不是路径）
                    if re.match(r'^[a-z-]+:[a-z0-9]', ref_url, re.I) and '/' not in ref_url:
                        continue
                    # 跳过包含 CSS 函数调用（如 background:url(...)）
                    if 'url(' in ref_url.lower() or 'var(' in ref_url.lower():
                        continue

                    total_refs += 1
                    # 解析为本地路径
                    local_path = os.path.normpath(os.path.join(file_dir, ref_url))
                    if os.path.isfile(local_path):
                        continue

                    # 缺失文件
                    missing += 1

                    # 先尝试模糊匹配本地文件
                    bn = os.path.basename(ref_url.split("?")[0].split("#")[0])
                    found = _find_resource_by_name(bn, out_dir, _img_idx, _IMG_EXTS_SET)
                    if not found:
                        found = _find_resource_by_name(bn, out_dir, _file_idx)
                    if found:
                        # 修正引用路径
                        new_rel = os.path.relpath(found, file_dir).replace("\\", "/")
                        try:
                            with open(html_file, "r", encoding="utf-8", errors="replace") as f:
                                html_content = f.read()
                            html_content = html_content.replace(ref_url, new_rel)
                            with open(html_file, "w", encoding="utf-8") as f:
                                f.write(html_content)
                            fuzzy_fixed += 1
                            fixed += 1
                            continue
                        except Exception:
                            pass

                    # 尝试从 asset_cache 逆映射找到原始 URL 并重新下载
                    orig_url = url_map.get(os.path.normpath(local_path))
                    if not orig_url:
                        # 尝试从相对路径推断 URL
                        rel = os.path.relpath(local_path, out_dir).replace("\\", "/")
                        orig_url = f"{scheme}://{domain}/{rel}"

                    try:
                        r = sess.get(orig_url, timeout=15)
                        if r.status_code == 200:
                            os.makedirs(os.path.dirname(local_path), exist_ok=True)
                            ctype = r.headers.get("Content-Type", "").lower()
                            if orig_url.lower().split("?")[0].endswith(".css") or "css" in ctype:
                                with open(local_path, "w", encoding="utf-8") as f:
                                    f.write(r.content.decode(detect_encoding(r), errors="replace"))
                            else:
                                with open(local_path, "wb") as f:
                                    f.write(r.content)
                            fixed += 1
                            continue
                    except Exception:
                        pass

                    # 无法下载，创建占位文件
                    ext = os.path.splitext(ref_url)[1].lower()
                    try:
                        os.makedirs(os.path.dirname(local_path), exist_ok=True)
                        if ext in (".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".svg", ".avif"):
                            with open(local_path, "wb") as f:
                                f.write(_PNG1x1)
                        elif ext in (".css",):
                            with open(local_path, "w", encoding="utf-8") as f:
                                f.write(f"/* placeholder for {ref_url} */\n")
                        elif ext in (".js", ".mjs"):
                            with open(local_path, "w", encoding="utf-8") as f:
                                f.write(f"// placeholder for {ref_url}\n")
                        else:
                            with open(local_path, "w", encoding="utf-8") as f:
                                f.write("")
                        fixed += 1
                    except (OSError, ValueError):
                        # 路径非法（如 CSS 属性值被误识别为文件名），跳过
                        pass

        # 2. 扫描所有 CSS 文件（已在步骤1中收集）
        for css_file in css_files:
            try:
                with open(css_file, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:
                continue
            file_dir = os.path.dirname(css_file)
            css_modified = False

            # 提取 base64 图片为独立文件
            new_content, b64_cnt = _extract_base64_images(
                content, out_dir, file_dir,
                prefix=os.path.splitext(os.path.basename(css_file))[0])
            if b64_cnt > 0:
                content = new_content
                css_modified = True
                b64_extracted += b64_cnt

            for m in re.finditer(r'url\(\s*[\'"]?([^\'")]+)[\'"]?\s*\)', content):
                ref_url = m.group(1).strip()
                if not ref_url or ref_url.startswith(("data:", "#", "http://", "https://", "//")):
                    continue
                total_refs += 1
                local_path = os.path.normpath(os.path.join(file_dir, ref_url))
                if os.path.isfile(local_path):
                    continue
                missing += 1
                # 先尝试模糊匹配
                bn = os.path.basename(ref_url.split("?")[0].split("#")[0])
                found = _find_resource_by_name(bn, out_dir, _img_idx, _IMG_EXTS_SET)
                if not found:
                    found = _find_resource_by_name(bn, out_dir, _file_idx)
                if found:
                    new_rel = os.path.relpath(found, file_dir).replace("\\", "/")
                    content = content.replace(ref_url, new_rel)
                    css_modified = True
                    fuzzy_fixed += 1
                    fixed += 1
                    continue
                # 创建占位
                try:
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    ext = os.path.splitext(ref_url)[1].lower()
                    if ext in (".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".svg", ".bmp", ".avif"):
                        with open(local_path, "wb") as f:
                            f.write(_PNG1x1)
                    else:
                        with open(local_path, "wb") as f:
                            f.write(b"")
                    fixed += 1
                except (OSError, ValueError):
                    pass

            # 检查 @import
            for m in re.finditer(r'@import\s+["\']([^"\']+)["\']', content):
                ref_url = m.group(1).strip()
                if not ref_url or ref_url.startswith(("http://", "https://", "//")):
                    continue
                total_refs += 1
                local_path = os.path.normpath(os.path.join(file_dir, ref_url))
                if os.path.isfile(local_path):
                    continue
                missing += 1
                # 先尝试模糊匹配
                found = _find_resource_by_name(ref_url, out_dir, _file_idx)
                if found:
                    new_rel = os.path.relpath(found, file_dir).replace("\\", "/")
                    content = content.replace(ref_url, new_rel)
                    css_modified = True
                    fuzzy_fixed += 1
                    fixed += 1
                    continue
                try:
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    with open(local_path, "w", encoding="utf-8") as f:
                        f.write(f"/* placeholder for {ref_url} */\n")
                    fixed += 1
                except (OSError, ValueError):
                    pass

            # 写回修改后的 CSS
            if css_modified:
                try:
                    with open(css_file, "w", encoding="utf-8") as f:
                        f.write(content)
                except Exception:
                    pass

        # 3. 检查内部链接是否指向存在的本地文件，创建占位页面
        broken_links = 0
        created_placeholders = 0
        for html_file in html_files:
            try:
                with open(html_file, "r", encoding="utf-8", errors="replace") as f:
                    html = f.read()
                soup = BeautifulSoup(html, "lxml")
            except Exception:
                continue
            file_dir = os.path.dirname(html_file)
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not href or href.startswith(("#", "mailto:", "tel:", "javascript:",
                                                "data:", "http://", "https://", "//")):
                    continue
                total_refs += 1
                local_path = os.path.normpath(os.path.join(file_dir, href.split("#")[0].split("?")[0]))
                if os.path.isfile(local_path):
                    continue
                # 尝试补全 index.html
                if os.path.isdir(local_path):
                    idx = os.path.join(local_path, "index.html")
                    if os.path.isfile(idx):
                        continue
                missing += 1
                broken_links += 1
                # 创建占位页面
                page_name = os.path.basename(local_path).replace(".html", "")
                link_text = a.get_text(strip=True) or page_name
                placeholder = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{link_text}</title>
</head>
<body>
<h1>{link_text}</h1>
<p>此页面尚未抓取或为动态生成。</p>
<p><a href="{os.path.relpath(os.path.join(out_dir, "index.html"), file_dir).replace(chr(92), "/")}">返回首页</a></p>
</body>
</html>
"""
                try:
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    if not os.path.isfile(local_path):
                        with open(local_path, "w", encoding="utf-8") as f:
                            f.write(placeholder)
                        created_placeholders += 1
                        fixed += 1
                except Exception:
                    pass

        if broken_links:
            self.log(f"  断链修复: {created_placeholders}/{broken_links} 个占位页面已创建", "info" if created_placeholders else "warn")
        if fuzzy_fixed:
            self.log(f"  资源路径模糊匹配修正: {fuzzy_fixed} 个", "ok")
        if b64_extracted:
            self.log(f"  base64 图片提取为独立文件: {b64_extracted} 个", "ok")

        return {"total_refs": total_refs, "missing": missing, "fixed": fixed,
                "fuzzy_fixed": fuzzy_fixed, "b64_extracted": b64_extracted}

    # ------------------------------------------------------------------
    # 网站项目结构规范化
    # ------------------------------------------------------------------
    def _standardize_web_structure(self, out_dir):
        """规范化网站目录结构：资源归类、路由修复、引用更新。"""
        from bs4 import BeautifulSoup

        self.log("  开始规范化项目结构...", "step")

        # 1. 收集所有文件并分类
        html_files = []
        css_files = []
        js_files = []
        img_files = []
        font_files = []
        media_files = []
        other_files = []

        for root_p, _d, files in os.walk(out_dir):
            for fn in files:
                fp = os.path.join(root_p, fn)
                rel = os.path.relpath(fp, out_dir)
                # 跳过 _next/ 目录：Next.js chunk 结构由规范化阶段维护，
                # 移动会破坏 chunk 间引用与 webpack 映射表
                if _under_next(fp, out_dir):
                    continue
                ext = os.path.splitext(fn)[1].lower()
                if ext in (".html", ".htm"):
                    html_files.append(fp)
                elif ext == ".css":
                    css_files.append(fp)
                elif ext in (".js", ".mjs", ".cjs"):
                    js_files.append(fp)
                elif ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".avif"):
                    img_files.append(fp)
                elif ext in (".woff", ".woff2", ".ttf", ".otf", ".eot"):
                    font_files.append(fp)
                elif ext in (".mp4", ".webm", ".ogg", ".mp3", ".wav", ".avi", ".mov"):
                    media_files.append(fp)
                else:
                    other_files.append(fp)

        # 2. 将资源文件移动到标准目录
        moves = {}  # old_path -> new_path
        std_dirs = {"css": "css", "js": "js", "images": "images", "fonts": "fonts", "media": "media"}

        def move_to_std(fp, target_dir, existing_list):
            """将文件移动到标准目录，处理重名冲突。"""
            fn = os.path.basename(fp)
            target = os.path.join(out_dir, target_dir, fn)
            # 处理重名：加序号（限制最多 9999 次尝试）
            if os.path.isfile(target) and os.path.normpath(target) != os.path.normpath(fp):
                name, ext = os.path.splitext(fn)
                i = 2
                while os.path.isfile(target) and i < 10000:
                    target = os.path.join(out_dir, target_dir, f"{name}_{i}{ext}")
                    i += 1
                if os.path.isfile(target):
                    return fp  # 极端情况：放弃移动
            if os.path.normpath(target) == os.path.normpath(fp):
                return fp
            os.makedirs(os.path.dirname(target), exist_ok=True)
            try:
                shutil.move(fp, target)
                moves[os.path.normpath(fp)] = os.path.normpath(target)
                return target
            except Exception:
                return fp

        moved_count = 0
        for fp in css_files[:]:
            new = move_to_std(fp, "css", css_files)
            if new != fp:
                moved_count += 1
        for fp in js_files[:]:
            new = move_to_std(fp, "js", js_files)
            if new != fp:
                moved_count += 1
        for fp in img_files[:]:
            # favicon.ico 保持在根目录
            if os.path.basename(fp) == "favicon.ico" and os.path.dirname(fp) == out_dir:
                continue
            new = move_to_std(fp, "images", img_files)
            if new != fp:
                moved_count += 1
        for fp in font_files[:]:
            new = move_to_std(fp, "fonts", font_files)
            if new != fp:
                moved_count += 1
        for fp in media_files[:]:
            new = move_to_std(fp, "media", media_files)
            if new != fp:
                moved_count += 1

        if moved_count:
            self.log(f"  资源归类: 移动 {moved_count} 个文件到标准目录", "ok")

        # 3. 更新所有 HTML 文件中的引用（与抓取阶段保持一致）
        ref_attrs = [
            ("img", "src"), ("script", "src"), ("link", "href"),
            ("source", "src"), ("video", "src"), ("video", "poster"),
            ("audio", "src"), ("iframe", "src"), ("embed", "src"), ("object", "data"),
            ("img", "data-src"), ("img", "data-original"),
            ("img", "data-lazy"), ("img", "data-lazy-src"),
            ("img", "data-original-src"), ("img", "data-image"),
            ("div", "data-bg"), ("div", "data-background"),
            ("section", "data-bg"), ("section", "data-background"),
            ("video", "data-poster"), ("video", "data-thumb"),
            ("input", "src"), ("track", "src"), ("area", "href"),
            ("image", "href"), ("image", "xlink:href"),
            ("use", "href"), ("use", "xlink:href"),
        ]

        updated_refs = 0
        for html_file in html_files:
            try:
                with open(html_file, "r", encoding="utf-8", errors="replace") as f:
                    html = f.read()
                soup = BeautifulSoup(html, "lxml")
            except Exception:
                continue

            file_dir = os.path.dirname(html_file)
            changed = False

            def fix_ref(val):
                """修复单个引用路径，返回新路径或 None。"""
                nonlocal changed
                if not val or val.startswith(("data:", "#", "mailto:", "tel:",
                                               "javascript:", "http://", "https://", "//")):
                    return None
                local_path = os.path.normpath(os.path.join(file_dir, val.split("?")[0].split("#")[0]))
                if local_path in moves:
                    new_path = moves[local_path]
                    new_rel = os.path.relpath(new_path, file_dir).replace("\\", "/")
                    changed = True
                    return new_rel
                return None

            for tag, attr in ref_attrs:
                for node in soup.find_all(tag):
                    val = node.get(attr)
                    if val:
                        new_val = fix_ref(val)
                        if new_val is not None:
                            node[attr] = new_val
                            updated_refs += 1

            # srcset
            for node in soup.find_all(attrs={"srcset": True}):
                parts = []
                for item in node["srcset"].split(","):
                    item = item.strip()
                    if not item:
                        continue
                    tokens = item.split()
                    new_val = fix_ref(tokens[0])
                    if new_val:
                        tokens[0] = new_val
                    parts.append(" ".join(tokens))
                if parts:
                    node["srcset"] = ", ".join(parts)

            for node in soup.find_all(attrs={"data-srcset": True}):
                parts = []
                for item in node["data-srcset"].split(","):
                    item = item.strip()
                    if not item:
                        continue
                    tokens = item.split()
                    new_val = fix_ref(tokens[0])
                    if new_val:
                        tokens[0] = new_val
                    parts.append(" ".join(tokens))
                if parts:
                    node["data-srcset"] = ", ".join(parts)

            # 内联 style 中的 url()
            for st in soup.find_all("style"):
                if st.string:
                    new_css = self._fix_css_refs(st.string, file_dir, moves)
                    if new_css != st.string:
                        st.string = new_css
                        changed = True

            # style 属性中的 url()
            for node in soup.find_all(style=True):
                new_style = self._fix_css_refs(node["style"], file_dir, moves)
                if new_style != node["style"]:
                    node["style"] = new_style
                    changed = True

            if changed:
                with open(html_file, "w", encoding="utf-8") as f:
                    f.write(str(soup))

        # 4. 更新 CSS 文件中的引用
        # 重新收集 CSS 文件（可能已移动）
        for root_p, _d, files in os.walk(out_dir):
            for fn in files:
                if not fn.endswith(".css"):
                    continue
                fp = os.path.join(root_p, fn)
                if _under_next(fp, out_dir):
                    continue
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except Exception:
                    continue
                file_dir = os.path.dirname(fp)
                new_content = self._fix_css_refs(content, file_dir, moves)
                if new_content != content:
                    with open(fp, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    updated_refs += 1

        # 4b. 更新 JS 文件中的引用路径（import/require 中的相对路径）
        js_updated = 0
        for root_p, _d, files in os.walk(out_dir):
            for fn in files:
                if not fn.endswith((".js", ".mjs", ".cjs")):
                    continue
                fp = os.path.join(root_p, fn)
                if _under_next(fp, out_dir):
                    continue
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except Exception:
                    continue
                # 跳过过大的文件
                if len(content) > 500000:
                    continue
                file_dir = os.path.dirname(fp)
                new_content = self._fix_js_refs(content, file_dir, moves)
                if new_content != content:
                    with open(fp, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    js_updated += 1

        if js_updated:
            self.log(f"  JS 引用路径更新: {js_updated} 处", "ok")

        if updated_refs:
            self.log(f"  引用路径更新: {updated_refs} 处", "ok")

        # 5. 修复内部链接路由
        route_fixed = 0
        for html_file in html_files:
            try:
                with open(html_file, "r", encoding="utf-8", errors="replace") as f:
                    html = f.read()
                soup = BeautifulSoup(html, "lxml")
            except Exception:
                continue
            file_dir = os.path.dirname(html_file)
            changed = False
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not href or href.startswith(("#", "mailto:", "tel:", "javascript:",
                                                 "data:", "http://", "https://", "//")):
                    continue
                local_path = os.path.normpath(os.path.join(file_dir, href.split("#")[0].split("?")[0]))
                if os.path.isfile(local_path):
                    continue
                # 尝试补全 index.html
                if os.path.isdir(local_path):
                    idx = os.path.join(local_path, "index.html")
                    if os.path.isfile(idx):
                        base_href = href.rstrip("/").split("#")[0].split("?")[0]
                        a["href"] = base_href + "/index.html" + ("#" + href.split("#")[1] if "#" in href else "")
                        changed = True
                        route_fixed += 1
                        continue
                # 创建占位页面
                if local_path.endswith(".html") or "." not in os.path.basename(local_path):
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    if not os.path.isfile(local_path):
                        page_name = os.path.basename(local_path).replace(".html", "")
                        # 计算到根 index.html 的正确相对路径
                        root_idx_rel = os.path.relpath(os.path.join(out_dir, "index.html"), file_dir).replace("\\", "/")
                        placeholder = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{page_name}</title>
</head>
<body>
<h1>{page_name}</h1>
<p>此页面尚未抓取或为动态生成。</p>
<p><a href="{root_idx_rel}">返回首页</a></p>
</body>
</html>
"""
                        with open(local_path, "w", encoding="utf-8") as f:
                            f.write(placeholder)
                        route_fixed += 1
                        changed = True

            if changed:
                with open(html_file, "w", encoding="utf-8") as f:
                    f.write(str(soup))

        if route_fixed:
            self.log(f"  路由修复: {route_fixed} 个链接", "ok")

        # 6. 确保根目录 index.html
        root_index = os.path.join(out_dir, "index.html")
        if not os.path.isfile(root_index):
            # 找一个 HTML 文件作为首页
            for fp in html_files:
                if os.path.dirname(fp) == out_dir:
                    shutil.copy2(fp, root_index)
                    break
            else:
                # 创建最小首页
                with open(root_index, "w", encoding="utf-8") as f:
                    f.write("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>首页</title>
</head>
<body>
<h1>首页</h1>
</body>
</html>
""")
            self.log("  已确保根目录 index.html", "ok")

        # 7. 清理空目录
        cleaned = 0
        for root_p, dirs, _files in os.walk(out_dir, topdown=False):
            for d in dirs:
                dp = os.path.join(root_p, d)
                try:
                    if not _has_files(dp):
                        os.rmdir(dp)
                        cleaned += 1
                except OSError:
                    pass
        if cleaned:
            self.log(f"  清理空目录: {cleaned} 个", "info")

        self.log("  项目结构规范化完成。", "ok")

    @staticmethod
    def _fix_css_refs(css_text, file_dir, moves):
        """修复 CSS 文本中的 url() 和 @import 引用。"""
        def fix_url(m, quoted=False):
            if quoted:
                quote = m.group(1)
                raw = m.group(2).strip()
            else:
                quote = ""
                raw = m.group(1).strip().strip("'\"")
            if raw.startswith(("data:", "#", "http://", "https://", "//")):
                return m.group(0)
            local_path = os.path.normpath(os.path.join(file_dir, raw.split("?")[0].split("#")[0]))
            if local_path in moves:
                new_path = moves[local_path]
                new_rel = os.path.relpath(new_path, file_dir).replace("\\", "/")
                return f"url({quote}{new_rel}{quote})"
            return m.group(0)

        css_text = re.sub(r'url\(\s*(["\'])([^"\']+)\1\s*\)', lambda m: fix_url(m, quoted=True), css_text)
        css_text = re.sub(r'url\(\s*([^\'")\s]+)\s*\)', lambda m: fix_url(m, quoted=False), css_text)

        def fix_import(m):
            quote = m.group(1)
            raw = m.group(2).strip()
            if raw.startswith(("data:", "http://", "https://", "//")):
                return m.group(0)
            local_path = os.path.normpath(os.path.join(file_dir, raw.split("?")[0].split("#")[0]))
            if local_path in moves:
                new_path = moves[local_path]
                new_rel = os.path.relpath(new_path, file_dir).replace("\\", "/")
                return f"@import {quote}{new_rel}{quote}"
            return m.group(0)

        css_text = re.sub(r'@import\s+(["\'])([^"\']+)\1', fix_import, css_text)
        return css_text

    @staticmethod
    def _fix_js_refs(js_text, file_dir, moves):
        """修复 JS 文本中的 import/require 相对路径引用。

        处理模式：
        - import './module.js'
        - import("./module.js")
        - require('./module.js')
        - import x from './module.js'
        """
        def fix_path(quote, raw):
            """尝试修复单个路径，返回新路径或原路径。"""
            if raw.startswith(("data:", "http://", "https://", "//", "#")):
                return raw
            if not raw.startswith(("./", "../", "/")):
                return raw  # 不是文件路径（可能是包名）
            local_path = os.path.normpath(os.path.join(file_dir, raw.split("?")[0].split("#")[0]))
            if local_path in moves:
                new_path = moves[local_path]
                new_rel = os.path.relpath(new_path, file_dir).replace("\\", "/")
                if not new_rel.startswith("."):
                    new_rel = "./" + new_rel
                return new_rel
            return raw

        # 修复 import '...' / import "..."
        def fix_import(m):
            quote = m.group(1)
            raw = m.group(2)
            new_raw = fix_path(quote, raw)
            return f"import {quote}{new_raw}{quote}"
        js_text = re.sub(r'import\s+(["\'])([^"\']+)\1', fix_import, js_text)

        # 修复 import('...') / import("...")
        def fix_import_dyn(m):
            quote = m.group(1)
            raw = m.group(2)
            new_raw = fix_path(quote, raw)
            return f"import({quote}{new_raw}{quote})"
        js_text = re.sub(r'import\(\s*(["\'])([^"\']+)\1\s*\)', fix_import_dyn, js_text)

        # 修复 require('...') / require("...")
        def fix_require(m):
            quote = m.group(1)
            raw = m.group(2)
            new_raw = fix_path(quote, raw)
            return f"require({quote}{new_raw}{quote})"
        js_text = re.sub(r'require\(\s*(["\'])([^"\']+)\1\s*\)', fix_require, js_text)

        return js_text

    # ------------------------------------------------------------------
    # 网页代码美化
    # ------------------------------------------------------------------
    def _beautify_web_code(self, out_dir):
        """美化 HTML/CSS/JS 代码（保守策略，优先保真）。"""
        self.log("  开始美化代码...", "step")
        html_count = 0
        css_count = 0
        js_count = 0

        # 检查 jsbeautifier 是否可用
        try:
            import jsbeautifier
            js_opts = jsbeautifier.default_options()
            js_opts.indent_size = 4
            js_opts.preserve_newlines = False
            js_opts.unescape_strings = True
            has_jsb = True
        except Exception:
            has_jsb = False

        for root_p, _d, files in os.walk(out_dir):
            for fn in files:
                fp = os.path.join(root_p, fn)
                # 跳过 _next/ 目录：美化会破坏压缩 chunk 的 sourcemap 行映射与
                # webpack 运行时结构，Next.js 资源保持原样
                if _under_next(fp, out_dir):
                    continue
                ext = os.path.splitext(fn)[1].lower()

                if ext in (".html", ".htm"):
                    try:
                        with open(fp, "r", encoding="utf-8", errors="replace") as f:
                            html = f.read()
                        # 保守 HTML 格式化：不使用 prettify（会破坏内联元素）
                        # 仅规范化空行和行尾空白，保留原始结构
                        beautified = self._beautify_html_conservative(html)
                        if beautified != html:
                            with open(fp, "w", encoding="utf-8") as f:
                                f.write(beautified)
                            html_count += 1
                    except Exception:
                        pass

                elif ext == ".css":
                    try:
                        with open(fp, "r", encoding="utf-8", errors="replace") as f:
                            css = f.read()
                        beautified = self._beautify_css(css)
                        if beautified != css:
                            with open(fp, "w", encoding="utf-8") as f:
                                f.write(beautified)
                            css_count += 1
                    except Exception:
                        pass

                elif ext in (".js", ".mjs", ".cjs"):
                    if not has_jsb:
                        continue
                    try:
                        with open(fp, "r", encoding="utf-8", errors="replace") as f:
                            js = f.read()
                        # 跳过过大的文件
                        if len(js) > 500000:
                            continue
                        beautified = jsbeautifier.beautify(js, js_opts)
                        if beautified and beautified != js:
                            with open(fp, "w", encoding="utf-8") as f:
                                f.write(beautified)
                            js_count += 1
                    except Exception:
                        pass

        self.log(f"  代码美化: HTML {html_count}, CSS {css_count}, JS {js_count}", "ok")

    @staticmethod
    def _beautify_html_conservative(html):
        """保守 HTML 格式化：仅清理空行和行尾空白，注入 viewport，保留原始结构。

        保护 <script>/<style>/<pre>/<code>/<textarea> 内容不被修改。
        """
        # 保留 doctype
        doctype = ""
        dt_match = re.match(r'(<!DOCTYPE[^>]*>\s*)', html, re.I)
        if dt_match:
            doctype = dt_match.group(1)
            html = html[len(doctype):]

        # 提取需要保护的标签内容（script/style/pre/code/textarea）
        protected = []

        def _protect(m):
            protected.append(m.group(0))
            return f"\x00PROTECT{len(protected) - 1}\x00"

        # 保护 <script>...</script>
        html = re.sub(r'<script[\s\S]*?</script>', _protect, html, flags=re.I)
        # 保护 <style>...</style>
        html = re.sub(r'<style[\s\S]*?</style>', _protect, html, flags=re.I)
        # 保护 <pre>...</pre>
        html = re.sub(r'<pre[\s\S]*?</pre>', _protect, html, flags=re.I)
        # 保护 <textarea>...</textarea>
        html = re.sub(r'<textarea[\s\S]*?</textarea>', _protect, html, flags=re.I)
        # 保护 <code>...</code>（可能包含格式敏感内容）
        html = re.sub(r'<code[\s\S]*?</code>', _protect, html, flags=re.I)

        # 移除行尾空白
        lines = html.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        cleaned = []
        blank_count = 0
        for line in lines:
            stripped = line.rstrip()
            if not stripped:
                blank_count += 1
                # 最多保留2个连续空行
                if blank_count <= 2:
                    cleaned.append("")
            else:
                blank_count = 0
                cleaned.append(stripped)

        result = "\n".join(cleaned).strip()

        # 注入 viewport meta（如果缺失）— 在还原保护内容前检查，避免误判
        has_viewport = "viewport" in result.lower()
        has_head = "<head" in result.lower()
        has_html = "<html" in result.lower()
        if not has_viewport:
            if has_head:
                # 在 <head> 后插入
                result = re.sub(
                    r'(<head[^>]*>)',
                    r'\1\n    <meta name="viewport" content="width=device-width, initial-scale=1.0">',
                    result,
                    count=1,
                    flags=re.I
                )
            elif has_html:
                # 没有 head 标签的情况，在 html 后插入
                result = re.sub(
                    r'(<html[^>]*>)',
                    r'\1\n<head>\n    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n</head>',
                    result,
                    count=1,
                    flags=re.I
                )

        # 还原被保护的内容
        for i, content in enumerate(protected):
            result = result.replace(f"\x00PROTECT{i}\x00", content)

        # 恢复 doctype
        if doctype:
            result = doctype + "\n" + result

        return result + "\n"

    @staticmethod
    def _beautify_css(css):
        """保守 CSS 美化：保护字符串/注释内容，仅规范化结构空白。

        修复旧版的破坏性问题：
        - 不再压缩字符串内空白（content: "hello world" 不会被破坏）
        - 不再移除冒号两侧空格（a :hover 后代选择器 vs a:hover 伪类）
        - 正确处理 @media/@supports/@keyframes 嵌套缩进
        - 保护注释 /* ... */ 内容不变
        """
        # 1. 提取注释和字符串，用占位符替换，防止被修改
        placeholders = []

        def _stash(m):
            placeholders.append(m.group(0))
            return f"\x00PH{len(placeholders) - 1}\x00"

        # 保护块注释
        css = re.sub(r'/\*[\s\S]*?\*/', _stash, css)
        # 保护双引号字符串
        css = re.sub(r'"[^"]*"', _stash, css)
        # 保护单引号字符串
        css = re.sub(r"'[^']*'", _stash, css)

        # 2. 规范化结构空白（此时字符串/注释已被保护）
        # 移除多余空白为单个空格
        css = re.sub(r'[ \t]+', ' ', css)
        # 移除换行符周围的空白
        css = re.sub(r'\s*\n\s*', '\n', css)
        # 合并多个换行为单个
        css = re.sub(r'\n{2,}', '\n', css)
        # 花括号前后加换行
        css = re.sub(r'\s*\{\s*', ' {\n  ', css)
        css = re.sub(r'\s*\}\s*', '\n}\n', css)
        # 分号后加换行
        css = re.sub(r';\s*', ';\n  ', css)
        # 逗号后加空格（但不在 url() 内部）
        css = re.sub(r',\s*', ', ', css)
        # 冒号后加空格（属性值），但不影响伪类/伪元素选择器
        # 仅在声明块内部处理（以缩进开头的行）
        # 伪类选择器如 a:hover 不应被修改

        # 3. 逐行处理缩进
        lines = css.split('\n')
        result = []
        indent = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # 处理闭括号
            if line == '}':
                indent = max(0, indent - 1)
                result.append('  ' * indent + '}')
                continue
            # 处理开括号行（选择器或@规则）
            if line.endswith('{'):
                result.append('  ' * indent + line)
                indent += 1
                continue
            # 处理同一行有 } 的情况（如 } selector {）
            if line.startswith('}'):
                indent = max(0, indent - 1)
                # 可能后面还有内容
                rest = line[1:].strip()
                if rest:
                    if rest.endswith('{'):
                        result.append('  ' * indent + rest)
                        indent += 1
                    else:
                        result.append('  ' * indent + rest)
                else:
                    result.append('  ' * indent + '}')
                continue
            # 普通属性行：冒号后加空格（如果是属性声明）
            # 判断是否为属性行：包含冒号且不以 @ 开头，且不在选择器位置
            if ':' in line and not line.startswith('@') and '{' not in line:
                # 分割第一个冒号，加空格
                idx = line.index(':')
                prop = line[:idx].strip()
                val = line[idx + 1:].strip()
                # 但要避免修改伪类选择器（如 a:hover）
                # 伪类选择器通常在选择器位置（不缩进或紧跟选择器）
                if indent > 0 and not any(c in prop for c in ('>', '~', '+', '.', '#', '[', '*')):
                    # 在声明块内，作为属性处理
                    line = f"{prop}: {val}"
                # 否则保持原样（可能是嵌套选择器）
            result.append('  ' * indent + line)

        css_out = '\n'.join(result)

        # 4. 还原占位符
        for i, ph in enumerate(placeholders):
            css_out = css_out.replace(f"\x00PH{i}\x00", ph)

        # 5. 选择器组合符规范化（仅在实际选择器中，不在属性值中）
        # 保护 url() 内容
        urls = []
        def _stash_url(m):
            urls.append(m.group(0))
            return f"\x00URL{len(urls) - 1}\x00"
        css_out = re.sub(r'url\([^)]*\)', _stash_url, css_out)
        # 组合符两侧加空格
        css_out = re.sub(r'([>~+])(?!=)', r' \1 ', css_out)
        # 合并多余空格（仅非行首位置，保护缩进）
        css_out = re.sub(r'(?<=\S)  +', ' ', css_out)
        # 还原 url()
        for i, u in enumerate(urls):
            css_out = css_out.replace(f"\x00URL{i}\x00", u)

        return css_out.strip() + '\n'

    def show_quality(self):
        if self.last_metrics:
            show_radar_chart(self, self.last_quality_title or "网站", self.last_metrics)
        else:
            messagebox.showinfo("提示", "请先完成一次网站抓取。")

    def open_output_dir(self):
        if self.last_output_dir and os.path.isdir(self.last_output_dir):
            try:
                os.startfile(self.last_output_dir)
            except Exception as e:
                messagebox.showerror("打开失败", str(e))


# --------------------------------------------------------------------------- #
# 主应用
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.title("反编译工具套件  —  小程序 / 网页")
        self.geometry("960x720")
        self.minsize(880, 620)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.wxapp_tab = WxappTab(nb, self)
        self.web_tab = WebTab(nb, self)
        nb.add(self.wxapp_tab, text="  小程序反编译  ")
        nb.add(self.web_tab, text="  网页反编译  ")


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()


if __name__ == "__main__":
    main()

