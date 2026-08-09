#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Next.js App Router 站点规范化工具
=================================
修复从 Vercel 抓取/反编译的 Next.js 静态站点因 Skew Protection 残留导致的问题：

  1. _next/static/{chunks,css}/ 文件名带部署后缀 _<6位hex>（如 webpack-xxx_1032d1.js）
  2. HTML <script src>/<link href> 用【带后缀】文件名；内联 RSC 飞行数据
     self.__next_f.push([1,"..."]) 用【不带后缀】+ ?dpl= 形式 → 二者不一致
  3. ?dpl=dpl_<base64> 查询参数残留在所有 chunk/css 引用里
  4. [locale] 动态路由：引用里 [locale] 与 %5Blocale%5D 混用
  5. 无关 @vite/client 引用（dev HMR 残留）
  6. webpack 运行时 chunk（webpack-*.js）内部映射表也带后缀

所有操作幂等（多次运行结果一致），绝不修改 SSR 可见 HTML 文本，
绝不 un-escape 飞行数据里的转义字符。

独立运行
--------
  python nextjs_normalize.py normalize <站点目录> [--no-backup] [--degrade]
  python nextjs_normalize.py serve    <站点目录> [--port 8000] [--degrade]

作为模块导入（供反编译脚本后处理调用）
------------------------------------
  from nextjs_normalize import normalize_nextjs_site, is_nextjs_site
  if is_nextjs_site(out_dir):
      stats = normalize_nextjs_site(out_dir, log=my_logger, backup=False)
"""

import os
import re
import sys
import shutil
import datetime
import urllib.parse
from http.server import HTTPServer, SimpleHTTPRequestHandler

# --------------------------------------------------------------------------- #
# 正则常量
# --------------------------------------------------------------------------- #
# 部署后缀：一个或多个 _<6位十六进制>。
# 兼容本工具抓取时为带 query 的 URL 附加的 6 位哈希后缀。
_SKEW_GROUPS = r'(?:_[0-9a-fA-F]{6})+'

# 文件名重命名：stem + 一个或多个后缀组 + 扩展名（仅 chunks/css 相关扩展名）
_RENAME_RE = re.compile(r'^(.+?)' + _SKEW_GROUPS + r'\.(js|css|js\.map)$')

# _next/static 资源引用（完整路径片段，含可能的 ?dpl= 查询）。
# 匹配 _next/static/ 起始、到空白/引号/反斜杠/尖括号/花括号为止的连续串。
# 注意：飞行数据是 JS 字符串字面量，内部 "/\" 已转义；反斜杠被排除在匹配外，
# 因此不会越过转义边界，外层转义结构保持不动。
_NEXT_REF_RE = re.compile(r'_next/static/[^\s"\'`\\<>{}]+')

# ?dpl= 查询参数及其值（Vercel 部署 ID，base64url 形式）
_DPL_QUERY_RE = re.compile(r'\?dpl=[A-Za-z0-9_\-]+')

# 引号包裹的纯文件名（webpack chunk 映射表条目）：
#   {0:"webpack-xxx_1032d1.js"}  /  e[N]="static/chunks/xxx_1032d1.js"
# 仅当整个引号内容是一个带后缀的 chunk 文件名时才匹配，避免误伤普通字符串。
_QUOTED_CHUNK_RE = re.compile(
    r'(["\'])([\w./\-]+(?:_[0-9a-fA-F]{6})+\.(?:js|css))(?:\?[^\s"\']*)?\1'
)

# %5Blocale%5D -> [locale]（大小写不敏感）
_LOCALE_ENC_RE = re.compile(r'%5Blocale%5D', re.IGNORECASE)

# @vite/client <script> 标签（dev HMR 残留）
_VITE_SCRIPT_RE = re.compile(
    r'<script\b[^>]*src=["\'][^"\']*?/@vite/client["\'][^>]*>\s*</script\s*>',
    re.IGNORECASE | re.DOTALL,
)

# @vite/client import 语句
_VITE_IMPORT_RE = re.compile(
    r'import\s+["\'][^"\']*?/@vite/client["\'];?', re.IGNORECASE
)

# 静态降级：移除所有 <script> 标签（含自闭合与无内容形式）
_ALL_SCRIPT_RE = re.compile(
    r'(?is)<script\b[^>]*>.*?</script\s*>|<script\b[^>]*?/>'
)

# 识别 Next.js 站点：存在 _next 目录，或 HTML 含 __next_f 飞行数据
_NEXT_DIR_RE = re.compile(r'[\\/]_next[\\/](?:static[\\/])?')


# --------------------------------------------------------------------------- #
# 核心工具函数
# --------------------------------------------------------------------------- #
def strip_skew_suffix(name):
    """去除文件名末尾的部署后缀 _<6hex>（可重复多次）。

    webpack-xxx_1032d1.js            -> webpack-xxx.js
    layout-abc_1032d1_def456.js      -> layout-abc.js
    app/page-abc123.js               -> app/page-abc123.js  （无 _<6hex>，不变）
    """
    m = _RENAME_RE.match(name)
    if m:
        return m.group(1) + '.' + m.group(2)
    return name


def _xform_next_ref(m):
    """改写单个 _next/static/ 引用片段：去查询串 + 去后缀。"""
    run = m.group(0)
    # _next 静态资源不存在合法 query，整段查询/锚点去除
    run = run.split('?', 1)[0].split('#', 1)[0]
    d, base = os.path.split(run)
    base = strip_skew_suffix(base)
    return (d + '/' + base) if d else base


def _xform_quoted_chunk(m):
    """改写引号包裹的纯 chunk 文件名（webpack 映射表条目）。"""
    q, path = m.group(1), m.group(2)
    path = path.split('?', 1)[0].split('#', 1)[0]
    d, base = os.path.split(path)
    base = strip_skew_suffix(base)
    new = (d + '/' + base) if d else base
    return q + new + q


def rewrite_refs_text(text):
    """对一段文本执行全部引用改写（去后缀 + 去 ?dpl=）。

    处理：_next/static/ 完整路径、引号包裹的 chunk 文件名、?dpl= 查询。
    幂等；不触碰转义结构。
    """
    # 1. _next/static/ 完整路径（HTML 属性、飞行数据、sourcemap 路径）
    text = _NEXT_REF_RE.sub(_xform_next_ref, text)
    # 2. 引号包裹的纯 chunk 文件名（webpack 映射表）
    text = _QUOTED_CHUNK_RE.sub(_xform_quoted_chunk, text)
    # 3. 兜底：仍残留的 ?dpl= 查询参数
    text = _DPL_QUERY_RE.sub('', text)
    return text


def unify_locale_path(text):
    """把 %5Blocale%5D 统一为 [locale]。"""
    return _LOCALE_ENC_RE.sub('[locale]', text)


def remove_vite_client(text):
    """移除 @vite/client 的 <script> 与 import 引用。"""
    text = _VITE_SCRIPT_RE.sub('', text)
    text = _VITE_IMPORT_RE.sub('', text)
    return text


def static_degrade_html(html):
    """静态降级：移除所有 <script> 标签，保留 SSR 可见内容 + CSS + 字体。

    用于 RSC 飞行数据在抓取时已被破坏（Connection closed / #423）的兜底：
    导航改为 <a href> 整页跳转仍可工作。
    """
    return _ALL_SCRIPT_RE.sub('', html)


# --------------------------------------------------------------------------- #
# 站点识别
# --------------------------------------------------------------------------- #
def is_nextjs_site(site_dir):
    """判断目录是否为 Next.js 站点。"""
    if not os.path.isdir(site_dir):
        return False
    if os.path.isdir(os.path.join(site_dir, '_next')):
        return True
    # 扫描 HTML 是否含 __next_f 飞行数据（限制扫描数量）
    count = 0
    for root_p, _d, files in os.walk(site_dir):
        for fn in files:
            if fn.endswith(('.html', '.htm')):
                fp = os.path.join(root_p, fn)
                try:
                    with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                        head = f.read(65536)
                    if '__next_f' in head or '_next/static/' in head:
                        return True
                except Exception:
                    pass
                count += 1
                if count > 40:
                    return False
    return False


# --------------------------------------------------------------------------- #
# 步骤 1：规范化文件名（物理重命名 + 去重）
# --------------------------------------------------------------------------- #
def _rename_assets(site_dir, log):
    """遍历 _next/static/ 下文件，去除部署后缀并重命名；处理同名去重。"""
    next_static = os.path.join(site_dir, '_next', 'static')
    if not os.path.isdir(next_static):
        return {'renamed': 0, 'deduped': 0}

    renamed = 0
    deduped = 0

    # 收集所有待重命名文件（自底向上，先文件后目录）
    candidates = []  # (full_path, new_full_path)
    for root_p, _d, files in os.walk(next_static):
        for fn in files:
            if not _RENAME_RE.match(fn):
                continue
            new_fn = strip_skew_suffix(fn)
            if new_fn == fn:
                continue
            candidates.append((os.path.join(root_p, fn),
                               os.path.join(root_p, new_fn)))

    # 按目标路径分组，处理去重
    by_target = {}
    for src, dst in candidates:
        by_target.setdefault(os.path.normpath(dst), []).append(src)

    for dst_norm, srcs in by_target.items():
        # 已存在非候选的同名文件（如飞行数据下载的不带后缀版本）
        existing = [s for s in srcs if os.path.normpath(s) == dst_norm]
        to_move = [s for s in srcs if os.path.normpath(s) != dst_norm]
        # 加上磁盘上已存在、但不在候选列表里的目标文件
        if os.path.isfile(dst_norm) and not existing:
            # 目标已存在且来自其它途径，参与去重比较
            pass

        # 汇总所有将占用 dst 的源（候选 + 已存在目标）
        all_sources = list(to_move)
        if os.path.isfile(dst_norm):
            all_sources.append(dst_norm)

        if not all_sources:
            continue

        # 去重：保留体积最大者（内容最完整）；体积相同保留路径靠前
        all_sources.sort(key=lambda p: (-os.path.getsize(p), p))
        keeper = all_sources[0]

        # 删除非 keeper 的候选源
        for s in all_sources[1:]:
            try:
                if os.path.isfile(s):
                    os.remove(s)
                    deduped += 1
            except OSError:
                pass

        # 把 keeper 移到 dst（若它还不是 dst）
        if os.path.normpath(keeper) != dst_norm:
            os.makedirs(os.path.dirname(dst_norm), exist_ok=True)
            try:
                shutil.move(keeper, dst_norm)
                renamed += 1
            except OSError as e:
                log(f"  [警告] 重命名失败: {os.path.basename(keeper)} -> {os.path.basename(dst_norm)} ({e})")

    return {'renamed': renamed, 'deduped': deduped}


# --------------------------------------------------------------------------- #
# 步骤 2/3/4：改写所有引用
# --------------------------------------------------------------------------- #
def _rewrite_all_refs(site_dir, log):
    """遍历 HTML/JS/CSS/MAP 文件，改写引用（去后缀+去dpl / 统一locale / 移除vite）。"""
    html_n = js_n = css_n = map_n = 0

    for root_p, _d, files in os.walk(site_dir):
        # 跳过备份目录
        if os.sep + '_backup' in root_p or root_p.endswith('_backup'):
            continue
        for fn in files:
            fp = os.path.join(root_p, fn)
            ext = os.path.splitext(fn)[1].lower()
            try:
                if ext in ('.html', '.htm'):
                    with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                        txt = f.read()
                    new = rewrite_refs_text(txt)
                    new = unify_locale_path(new)
                    new = remove_vite_client(new)
                    if new != txt:
                        with open(fp, 'w', encoding='utf-8') as f:
                            f.write(new)
                        html_n += 1
                elif ext in ('.js', '.mjs', '.cjs'):
                    with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                        txt = f.read()
                    new = rewrite_refs_text(txt)
                    new = unify_locale_path(new)
                    new = remove_vite_client(new)
                    if new != txt:
                        with open(fp, 'w', encoding='utf-8') as f:
                            f.write(new)
                        js_n += 1
                elif ext == '.css':
                    with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                        txt = f.read()
                    new = rewrite_refs_text(txt)
                    new = unify_locale_path(new)
                    if new != txt:
                        with open(fp, 'w', encoding='utf-8') as f:
                            f.write(new)
                        css_n += 1
                elif ext == '.map':
                    with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                        txt = f.read()
                    new = rewrite_refs_text(txt)
                    new = unify_locale_path(new)
                    if new != txt:
                        with open(fp, 'w', encoding='utf-8') as f:
                            f.write(new)
                        map_n += 1
            except Exception as e:
                log(f"  [警告] 改写引用失败: {fp} ({e})")

    log(f"  引用改写: HTML {html_n}, JS {js_n}, CSS {css_n}, MAP {map_n}")
    return {'html': html_n, 'js': js_n, 'css': css_n, 'map': map_n}


# --------------------------------------------------------------------------- #
# 主入口：规范化站点
# --------------------------------------------------------------------------- #
def normalize_nextjs_site(site_dir, log=print, backup=True, degrade=False):
    """规范化 Next.js 站点目录（步骤 1-4，可选步骤 6 降级）。

    参数:
        site_dir: 站点根目录（含 _next/ 或含 __next_f 的 HTML）
        log:      日志输出回调
        backup:   是否在改写前整体备份（独立运行建议 True；后处理调用可 False）
        degrade:  是否启用静态降级（移除所有 <script>）

    返回统计 dict。
    """
    if not os.path.isdir(site_dir):
        log(f"[错误] 目录不存在: {site_dir}")
        return {'error': True}

    log("========== Next.js 站点规范化 ==========", "step")
    log(f"目录: {site_dir}")

    # 整体备份
    if backup:
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        bak = site_dir.rstrip('\\/') + f'.bak_{ts}'
        try:
            shutil.copytree(site_dir, bak)
            log(f"  已整体备份至: {bak}", "ok")
        except Exception as e:
            log(f"  [警告] 备份失败（继续执行）: {e}", "warn")

    # 步骤 1：物理重命名（去后缀 + 去重）
    log("  [步骤1] 规范化文件名（去部署后缀）...", "step")
    r1 = _rename_assets(site_dir, log)
    log(f"    重命名 {r1['renamed']} 个，去重 {r1['deduped']} 个", "ok")

    # 步骤 2/3/4：改写所有引用
    log("  [步骤2-4] 统一改写引用（去后缀+去?dpl / 统一[locale] / 移除@vite/client）...", "step")
    r2 = _rewrite_all_refs(site_dir, log)

    # 步骤 6（可选）：静态降级
    degraded = 0
    if degrade:
        log("  [步骤6] 启用静态降级（移除所有 <script>）...", "step")
        for root_p, _d, files in os.walk(site_dir):
            for fn in files:
                if not fn.endswith(('.html', '.htm')):
                    continue
                fp = os.path.join(root_p, fn)
                try:
                    with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                        txt = f.read()
                    new = static_degrade_html(txt)
                    if new != txt:
                        with open(fp, 'w', encoding='utf-8') as f:
                            f.write(new)
                        degraded += 1
                except Exception:
                    pass
        log(f"    静态降级处理 {degraded} 个 HTML", "ok")

    log("  Next.js 规范化完成。", "ok")
    return {
        'renamed': r1['renamed'], 'deduped': r1['deduped'],
        'refs': r2, 'degraded': degraded,
    }


# --------------------------------------------------------------------------- #
# 步骤 5：本地 HTTP 服务器（带 SPA 回退 + 正确字体 MIME + 可选降级）
# --------------------------------------------------------------------------- #
_EXTRA_MIME = {
    '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf',
    '.otf': 'font/otf', '.eot': 'application/vnd.ms-fontobject',
    '.mjs': 'text/javascript', '.js': 'text/javascript',
    '.css': 'text/css', '.map': 'application/json',
    '.webmanifest': 'application/manifest+json',
}


class _NextJSHandler(SimpleHTTPRequestHandler):
    """支持无扩展名路由回退与正确 MIME 的静态服务器。"""

    site_dir = '.'
    degrade = False

    def guess_type(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext in _EXTRA_MIME:
            return _EXTRA_MIME[ext]
        return super().guess_type(path)

    def _resolve_fs_path(self):
        """把 URL 路径解析为文件系统路径，支持目录 index.html 与无扩展名回退。"""
        raw = urllib.parse.unquote(self.path.split('?', 1)[0].split('#', 1)[0])
        rel = raw.lstrip('/')
        if rel == '':
            rel = 'index.html'
        fs = os.path.normpath(os.path.join(self.site_dir, rel))
        # 防越界：确保解析结果仍在站点目录内
        abs_site = os.path.abspath(self.site_dir)
        try:
            if os.path.commonpath([abs_site, os.path.abspath(fs)]) != abs_site:
                fs = os.path.join(self.site_dir, 'index.html')
        except ValueError:
            # 不同驱动器等异常情况，回退到首页
            fs = os.path.join(self.site_dir, 'index.html')

        if os.path.isfile(fs):
            return fs
        # 目录 -> index.html
        if os.path.isdir(fs):
            idx = os.path.join(fs, 'index.html')
            if os.path.isfile(idx):
                return idx
        # 无扩展名路由 -> path/index.html 或 path.html
        if '.' not in os.path.basename(fs):
            idx = os.path.join(fs, 'index.html')
            if os.path.isfile(idx):
                return idx
            alt = fs + '.html'
            if os.path.isfile(alt):
                return alt
        return None

    def do_GET(self):
        fs = self._resolve_fs_path()
        if fs is None:
            self.send_error(404, 'Not Found')
            return
        try:
            with open(fs, 'rb') as f:
                data = f.read()
        except OSError:
            self.send_error(404, 'Not Found')
            return

        if self.degrade and fs.endswith(('.html', '.htm')):
            data = static_degrade_html(
                data.decode('utf-8', errors='replace')).encode('utf-8')

        ctype = self.guess_type(fs)
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.address_string(), fmt % args))


def serve(site_dir, port=8000, degrade=False):
    """启动本地 HTTP 服务器（带 SPA 回退）。按 Ctrl+C 退出。"""
    handler = _NextJSHandler
    handler.site_dir = os.path.abspath(site_dir)
    handler.degrade = degrade
    mode = '（静态降级）' if degrade else ''
    print(f"Next.js 本地服务器{mode}: http://localhost:{port}/  ->  {handler.site_dir}")
    print("提示: 目录请求自动返回 index.html；无扩展名路由回退到 index.html。")
    print("按 Ctrl+C 停止。")
    httpd = HTTPServer(('127.0.0.1', port), handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        httpd.server_close()


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def _cli():
    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help', 'help'):
        print(__doc__)
        print("用法:")
        print("  python nextjs_normalize.py normalize <目录> [--no-backup] [--degrade]")
        print("  python nextjs_normalize.py serve    <目录> [--port 8000] [--degrade]")
        print("  python nextjs_normalize.py detect   <目录>")
        return 0

    cmd = args[0]
    rest = args[1:]

    def _flag(name):
        if name in rest:
            rest.remove(name)
            return True
        return False

    if cmd == 'detect':
        d = rest[0] if rest else '.'
        print("Next.js 站点" if is_nextjs_site(d) else "非 Next.js 站点")
        return 0

    if cmd == 'normalize':
        if not rest:
            print("错误: 缺少目录参数")
            return 2
        site = rest[0]
        no_backup = _flag('--no-backup')
        degrade = _flag('--degrade')
        if not is_nextjs_site(site):
            print("[提示] 未检测到 Next.js 特征，仍将尝试规范化。")
        stats = normalize_nextjs_site(site, log=print,
                                      backup=not no_backup, degrade=degrade)
        print("\n结果:", stats)
        return 0

    if cmd == 'serve':
        if not rest:
            print("错误: 缺少目录参数")
            return 2
        site = rest[0]
        degrade = _flag('--degrade')
        port = 8000
        for i, a in enumerate(rest):
            if a == '--port' and i + 1 < len(rest):
                try:
                    port = int(rest[i + 1])
                except ValueError:
                    pass
        serve(site, port=port, degrade=degrade)
        return 0

    print(f"未知命令: {cmd}")
    return 2


if __name__ == '__main__':
    sys.exit(_cli())
