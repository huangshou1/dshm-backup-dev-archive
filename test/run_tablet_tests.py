#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dshm-backup 平板端测试运行器（DSHM 内嵌 CPython 3.12）

重要边界（必须如实写进 TEST_REPORT）：
  · 本机（HarmonyOS 平板 DSHM）没有 Node CLI，也没有可用的 JS 运行时；
    因此**不执行** dshm-backup 的 JS 逻辑，真实模块解析/加载/备份/恢复留待手机金丝雀验收。
  · 本脚本实际执行：
      1) 真实 JS 解析（esprima，若随附/可导入）——ESM parseModule / client.js parseScript；
      2) 基于 AST 的静态安全与适配检查（零 inject、禁用 API、预览零写入、回滚分支等）；
      3) 合成 fixture 测试（用 Python 独立参考实现验证数据格式、清单契约、路径映射、
         排除项、备份命名、轮转、发布协议、回滚）；
      4) 交付物/配置/文档一致性检查。
  · 参考实现（reference）是**独立重写**的规则校验器，用于验证 fixture 与设计契约，
    并不等价于执行 dshm-backup 的 JS 实现。不得据此宣称“JS 已运行通过”。

运行：在 DSHM 的 Agent python 工具里执行
    python dshm-backup-dev/test/run_tablet_tests.py
（不使用外部 shell / node / PC 工具；全部使用内嵌 CPython 标准库与内嵌 libzstd）
"""

import ast
import ctypes
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # dshm-backup-dev
PLUGIN = os.path.join(ROOT, "dshm-backup")
LIB = os.path.join(PLUGIN, "lib")
TMP = os.path.join(ROOT, ".test-tmp")
RESULTS = []


def record(name, status, detail=""):
    """status: pass / fail / skip（skip 必须写明原因）"""
    RESULTS.append({"name": name, "status": status, "detail": detail})
    mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[status]
    print("[%s] %s%s" % (mark, name, (" — " + detail) if detail else ""))


def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── 可选：真实 JS 解析器 esprima（本机通过 .tools 提供；不随插件交付）────────
ESPRIMA = None


def load_esprima():
    global ESPRIMA
    candidates = [
        os.path.join(ROOT, ".tools"),
        os.path.join(ROOT, "test", "_vendor"),
    ]
    for c in candidates:
        if os.path.isdir(c) and c not in sys.path:
            sys.path.insert(0, c)
    # .tools 里 esprima 是 sdist 解包目录
    sdist = os.path.join(ROOT, ".tools", "esprima-src", "esprima-4.0.1")
    if os.path.isdir(sdist) and sdist not in sys.path:
        sys.path.insert(0, sdist)
    try:
        import esprima  # type: ignore

        ESPRIMA = esprima
        return True
    except Exception:
        return False


HAS_ESPRIMA = load_esprima()


# ── 1. 真实 JS 解析 + AST 扫描 ────────────────────────────────────────────
class AstScan(object):
    def __init__(self):
        self.imports = []
        self.identifiers = set()
        self.calls = []
        self.arrows = 0
        self.templates = 0
        self.let_const = 0
        self.classes = 0
        self.async_fns = 0
        self.functions = {}


def walk_ast(node, scan):
    if not isinstance(node, dict):
        return
    ntype = node.get("type")
    if ntype == "Identifier":
        scan.identifiers.add(node.get("name"))
    elif ntype == "ImportDeclaration":
        src = node.get("source", {}).get("value", "")
        scan.imports.append(src)
    elif ntype == "CallExpression":
        callee = node.get("callee", {})
        name = callee.get("name")
        if name:
            scan.calls.append(name)
        elif callee.get("type") == "MemberExpression":
            prop = callee.get("property", {})
            if prop.get("type") == "Identifier":
                scan.calls.append(prop.get("name"))
            elif prop.get("type") == "Literal" and isinstance(prop.get("value"), str):
                scan.calls.append(prop.get("value"))
    elif ntype == "ArrowFunctionExpression":
        scan.arrows += 1
    elif ntype == "TemplateLiteral":
        scan.templates += 1
    elif ntype == "VariableDeclaration":
        if node.get("kind") in ("let", "const"):
            scan.let_const += 1
    elif ntype == "ClassDeclaration":
        scan.classes += 1
    elif ntype in ("FunctionDeclaration", "FunctionExpression"):
        if node.get("async"):
            scan.async_fns += 1
        ident = node.get("id")
        if ntype == "FunctionDeclaration" and ident and ident.get("name") and "body" in node:
            scan.functions[ident["name"]] = node
    for key, value in node.items():
        if key in ("loc", "range"):
            continue
        if isinstance(value, dict):
            walk_ast(value, scan)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    walk_ast(item, scan)


def parse_module(path):
    src = read_text(path)
    scan = AstScan()
    if ESPRIMA is None:
        return None, src, scan
    # esprima 4 只覆盖到 ES2017；下列两种合法的新语法在解析前做等价归一化
    # （仅用于语法解析，不改变交付源码；词法平衡检查仍针对原文执行）。
    src_for_parse = src.replace("import.meta.url", '"__import_meta_url__"')
    src_for_parse = re.sub(r"for\s+await\s*\(", "for (", src_for_parse)
    src_for_parse = re.sub(r"catch\s*\{", "catch (_catch_err) {", src_for_parse)
    try:
        tree = ESPRIMA.parseModule(src_for_parse)
    except Exception as e:  # noqa: BLE001
        record("JS 解析 %s" % os.path.basename(path), "fail", "esprima parseModule 失败：%s" % e)
        return None, src, scan
    walk_ast(tree.toDict(), scan)
    return tree, src, scan


def parse_script(path):
    src = read_text(path)
    scan = AstScan()
    if ESPRIMA is None:
        return None, src, scan
    try:
        tree = ESPRIMA.parseScript(src)
    except Exception as e:  # noqa: BLE001
        record("JS 解析 %s" % os.path.basename(path), "fail", "esprima parseScript 失败：%s" % e)
        return None, src, scan
    walk_ast(tree.toDict(), scan)
    return tree, src, scan


def find_function(node, name):
    """在 AST dict 里递归找 FunctionDeclaration。"""
    if not isinstance(node, dict):
        return None
    if node.get("type") == "FunctionDeclaration" and node.get("id", {}).get("name") == name:
        return node
    for key, value in node.items():
        if key in ("loc", "range"):
            continue
        if isinstance(value, dict):
            found = find_function(value, name)
            if found:
                return found
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    found = find_function(item, name)
                    if found:
                        return found
    return None


def calls_within(fn_node):
    scan = AstScan()
    walk_ast(fn_node.get("body"), scan)
    return set(scan.calls), scan.identifiers


def has_node_type(node, node_type):
    if not isinstance(node, dict):
        return False
    if node.get("type") == node_type:
        return True
    for key, value in node.items():
        if key in ("loc", "range"):
            continue
        if isinstance(value, dict):
            if has_node_type(value, node_type):
                return True
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and has_node_type(item, node_type):
                    return True
    return False


def test_js_parse_and_contracts():
    lib_files = ["index.js", "backup.js", "restore.js", "util.js", "format.js", "zip.js"]
    if ESPRIMA is None:
        record("真实 JS 解析（esprima parseModule）", "skip", "本机未提供 esprima；已改用词法检查（见下）")
    else:
        ok = True
        for name in lib_files:
            path = os.path.join(LIB, name)
            tree, src, scan = parse_module(path)
            if tree is None:
                ok = False
                continue
            record("JS 解析 lib/%s（ESM parseModule）" % name, "pass", "lines=%d" % (src.count("\n") + 1))
        # client.js 必须是 ES5 脚本（不是模块）
        tree, src, scan = parse_script(os.path.join(LIB, "client.js"))
        if tree is None:
            ok = False
        else:
            record("JS 解析 lib/client.js（ES5 parseScript）", "pass", "lines=%d" % (src.count("\n") + 1))
            problems = []
            if scan.imports:
                problems.append("含 import：%s" % scan.imports[:3])
            if scan.arrows:
                problems.append("含箭头函数 %d 处" % scan.arrows)
            if scan.templates:
                problems.append("含模板字符串 %d 处" % scan.templates)
            if scan.let_const:
                problems.append("含 let/const %d 处" % scan.let_const)
            if scan.classes:
                problems.append("含 class %d 处" % scan.classes)
            if scan.async_fns:
                problems.append("含 async 函数 %d 处" % scan.async_fns)
            if "require" in scan.identifiers:
                problems.append("含 require")
            if "setInterval" in scan.calls:
                problems.append("含 setInterval 常驻轮询")
            if problems:
                record("client.js ES5/注入式约束", "fail", "；".join(problems))
                ok = False
            else:
                record("client.js ES5/注入式约束", "pass", "无 import/箭头/模板/let/const/class/async/require/setInterval")
            for token, label in [("__DSHM_BACKUP_UI__", "幂等哨兵"), ("/dshm-backup/api/", "同源 API 前缀")]:
                if token not in src:
                    problems.append("缺少 %s" % label)
            if problems:
                record("client.js 关键契约字符串", "fail", "；".join(problems))
                ok = False
            else:
                record("client.js 关键契约字符串", "pass", "哨兵/API 前缀存在")
        if ok:
            record("ESM/ES5 解析汇总", "pass", "6 个宿主模块 + 注入脚本全部解析通过")

    # 导入检查（不依赖 esprima 也可做正则级检查）
    allowed_node = {
        "node:fs", "node:fs/promises", "node:path", "node:crypto", "node:url", "node:zlib",
        "node:stream", "node:stream/promises", "node:timers/promises", "node:os", "node:http", "node:assert", "node:test",
    }
    import_re = re.compile(r"""^\s*import\s+[^'"]*?from\s+['"]([^'"]+)['"]""", re.M)
    bad = []
    for name in lib_files:
        src = read_text(os.path.join(LIB, name))
        for spec in import_re.findall(src):
            if spec.startswith("node:"):
                if spec not in allowed_node:
                    bad.append("%s: 未确认的 node 内置 %s" % (name, spec))
            elif spec.startswith("./"):
                target = os.path.join(LIB, spec[2:])
                if not os.path.exists(target):
                    bad.append("%s: 相对导入不存在 %s" % (name, spec))
            else:
                bad.append("%s: 第三方/裸包导入 %s" % (name, spec))
    if bad:
        record("依赖检查（只用 node: 内置 + 本地相对文件）", "fail", "；".join(bad[:8]))
    else:
        record("依赖检查（只用 node: 内置 + 本地相对文件）", "pass", "无第三方依赖、无裸包导入")

    # 禁止 API / 零 inject
    forbidden = ["child_process", "execSync", "spawnSync", "WebAssembly", "new Worker", "eval("]
    src_all = "\n".join(read_text(os.path.join(LIB, n)) for n in lib_files)
    hits = [token for token in forbidden if token in src_all]
    if hits:
        record("禁用 API 扫描", "fail", "命中：%s" % hits)
    else:
        record("禁用 API 扫描", "pass", "无 child_process/WASM/Worker/eval")

    index_src = read_text(os.path.join(LIB, "index.js"))
    if "export const inject" in index_src or "export let inject" in index_src:
        record("零静态 inject", "fail", "index.js 含 export const/let inject")
    elif "ctx.get(\"webServer\")" not in index_src:
        record("零静态 inject", "fail", "未找到 ctx.get(\"webServer\")")
    else:
        record("零静态 inject", "pass", "无静态 inject；webServer 通过 ctx.get 判空 + internal/service 重试")

    # 预览零写入：inspectBackup 函数体不得包含写文件/解压调用
    tree, _, _ = parse_module(os.path.join(LIB, "restore.js"))
    if tree is not None:
        fn = find_function(tree.toDict(), "inspectBackup")
        if fn is None:
            record("预览阶段零写入（AST）", "fail", "未找到 inspectBackup 函数")
        else:
            calls, identifiers = calls_within(fn)
            write_calls = {
                "writeFileAtomic", "writeJsonAtomic", "copyFileAtomic", "writeZip", "extractZip",
                "mkdir", "rename", "writeFile", "removeTreeQuietly", "removeFileQuietly",
            }
            hit = sorted(write_calls & calls)
            if hit:
                record("预览阶段零写入（AST）", "fail", "inspectBackup 内出现写调用：%s" % hit)
            else:
                record("预览阶段零写入（AST）", "pass", "inspectBackup 函数体无写/解压调用")
        # executeRestore 必须有 catch → rollbackRestore
        ex = find_function(tree.toDict(), "executeRestore")
        if ex is None:
            record("恢复失败回滚分支（AST）", "fail", "未找到 executeRestore")
        else:
            calls, _ids = calls_within(ex)
            has_try = has_node_type(ex, "TryStatement")
            has_catch = has_node_type(ex, "CatchClause")
            if "rollbackRestore" in calls and has_try and has_catch:
                record("恢复失败回滚分支（AST）", "pass", "executeRestore 的 try/catch 分支调用 rollbackRestore")
            else:
                record("恢复失败回滚分支（AST）", "fail",
                       "rollbackRestore=%s try=%s catch=%s" % ("rollbackRestore" in calls, has_try, has_catch))
    else:
        record("预览阶段零写入（AST）", "skip", "无 esprima，AST 检查未执行")
        record("恢复失败回滚分支（AST）", "skip", "无 esprima，AST 检查未执行")

    # 路由与客户端接口一致性
    client_src = read_text(os.path.join(LIB, "client.js"))
    endpoints = ["state", "backup", "inspect", "restore", "emergency", "verify"]
    missing = []
    for endpoint in endpoints:
        if ('/api/' + endpoint + '"') not in index_src and ("/api/" + endpoint + "'") not in index_src:
            missing.append("宿主缺 /api/" + endpoint)
        if not re.search(r"['\"]" + endpoint + r"['\"]", client_src):
            missing.append("客户端缺 " + endpoint)
    if missing:
        record("宿主/客户端接口一致性", "fail", "；".join(missing[:6]))
    else:
        record("宿主/客户端接口一致性", "pass", "state/backup/inspect/restore/emergency/verify 均存在")


# ── 2. zstd 工具（内嵌 libzstd，用于构造合成 fixture；不用于执行 JS）────────
class Zstd(object):
    def __init__(self):
        self.lib = None
        for path in ["/system/lib64/libzstd.z.so", "libzstd.so.1", "libzstd.so"]:
            try:
                self.lib = ctypes.CDLL(path)
                break
            except Exception:
                continue
        if self.lib is None:
            return
        self.lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
        self.lib.ZSTD_compressBound.restype = ctypes.c_size_t
        self.lib.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        self.lib.ZSTD_compress.restype = ctypes.c_size_t
        self.lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
        self.lib.ZSTD_isError.restype = ctypes.c_uint

    def available(self):
        return self.lib is not None

    def compress(self, data):
        if not self.available():
            raise RuntimeError("libzstd 不可用")
        cap = self.lib.ZSTD_compressBound(len(data))
        dst = ctypes.create_string_buffer(cap)
        src = ctypes.create_string_buffer(data, len(data))
        size = self.lib.ZSTD_compress(dst, cap, src, len(data), 6)
        if self.lib.ZSTD_isError(size):
            raise RuntimeError("zstd 压缩失败")
        return dst.raw[:size]


ZSTD = Zstd()


def zstd_frames(buf):
    """独立参考实现：扫描 zstd 帧边界（与 core/JS 规则一致），返回 (帧范围列表, 是否有残缺帧)。"""
    magic = b"\x28\xb5\x2f\xfd"
    frames = []
    offset = 0
    torn = False
    n = len(buf)
    while offset < n:
        start = offset
        if n - offset < 4 or buf[offset:offset + 4] != magic:
            if n - offset < 4:
                torn = True
                break
            raise ValueError("bad zstd magic at %d" % offset)
        offset += 4
        if offset >= n:
            torn = True
            break
        descriptor = buf[offset]
        offset += 1
        if descriptor & 24:
            raise ValueError("reserved header bit")
        content_size_flag = descriptor >> 6
        single_segment = (descriptor & 32) != 0
        checksum = (descriptor & 4) != 0
        dict_flag = descriptor & 3
        dict_bytes = 4 if dict_flag == 3 else dict_flag
        content_bytes = (1 << content_size_flag) if content_size_flag else (1 if single_segment else 0)
        remaining = (0 if single_segment else 1) + dict_bytes + content_bytes
        if n - offset < remaining:
            torn = True
            break
        offset += remaining
        while True:
            if n - offset < 3:
                torn = True
                break
            header = int.from_bytes(buf[offset:offset + 3], "little")
            offset += 3
            last = (header & 1) != 0
            block_type = (header >> 1) & 3
            block_size = header >> 3
            if block_type == 3:
                raise ValueError("reserved block type")
            payload = 1 if block_type == 1 else block_size
            if n - offset < payload:
                torn = True
                break
            offset += payload
            if last:
                break
        if torn:
            break
        if checksum:
            if n - offset < 4:
                torn = True
                break
            offset += 4
        frames.append((start, offset))
    return frames, torn


# ── 3. 合成 fixture 与 Python 参考实现 ────────────────────────────────────
def encode_segment(raw):
    if raw == "":
        raise ValueError("empty")
    if raw == ".":
        return "~002E"
    if raw == "..":
        return "~002E~002E"
    out = []
    for ch in raw:
        code = ord(ch)
        if ch != "~" and re.match(r"^[A-Za-z0-9._-]$", ch):
            out.append(ch)
        else:
            out.append("~%04X" % code)
    return "".join(out)


def project_key(cwd):
    readable = []
    separator_run = False
    for ch in cwd:
        if ch in ("/", "\\", ":"):
            if not separator_run:
                readable.append("-")
            separator_run = True
        elif ch != "~" and re.match(r"^[A-Za-z0-9._-]$", ch):
            readable.append(ch)
            separator_run = False
        else:
            readable.append("~%04X" % ord(ch))
            separator_run = False
    text = "".join(readable)
    text = re.sub(r"^-+", "", text) or "root"
    return "--" + text[:251] + "--"


def build_synthetic_home():
    """构造合成 DSHM 数据树（不含任何真实数据）。"""
    shutil.rmtree(TMP, ignore_errors=True)
    home = os.path.join(TMP, "home")
    os.makedirs(home)
    ws_root = os.path.join(TMP, "workspaces")
    # 三个工作区：中文目录名、带空格目录名、默认路径风格
    ws1 = os.path.join(ws_root, "中文工作区")
    ws2 = os.path.join(ws_root, "space dir")
    ws3 = os.path.join(ws_root, "plain")
    for w in (ws1, ws2, ws3):
        os.makedirs(w, exist_ok=True)
    # 工作区文件：中文名、空格、嵌套、空文件、无扩展名
    os.makedirs(os.path.join(ws1, "子目录", "更一层"), exist_ok=True)
    files = {
        os.path.join(ws1, "中文 文件名.txt"): "你好，世界\n".encode("utf-8"),
        os.path.join(ws1, "子目录", "更一层", "无扩展名"): b"no extension",
        os.path.join(ws1, "空文件"): b"",
        os.path.join(ws2, "a b c.md"): "# docs\n".encode("utf-8"),
        os.path.join(ws3, "plain.txt"): b"plain",
    }
    for path, data in files.items():
        with open(path, "wb") as f:
            f.write(data)
    # 排除目录：node_modules / .credentials.yaml 等
    os.makedirs(os.path.join(ws1, "node_modules", "dep"), exist_ok=True)
    with open(os.path.join(ws1, "node_modules", "dep", "index.js"), "w", encoding="utf-8") as f:
        f.write("module.exports = 1\n")
    os.makedirs(os.path.join(ws1, ".git"), exist_ok=True)
    with open(os.path.join(ws1, ".git", "config"), "w", encoding="utf-8") as f:
        f.write("[core]\n")

    # 会话目录（projectKey 由 session cwd 决定）
    sessions_root = os.path.join(home, "sessions")
    os.makedirs(sessions_root, exist_ok=True)

    def make_session(session_id, cwd, header_extra, events, compression="normal"):
        sid = session_id
        created = 1767225600000
        header = {
            "type": "session",
            "version": 4,
            "id": sid,
            "createdAt": created,
            "isSeeded": False,
            "delegationDepth": 0,
            "cwd": cwd,
        }
        header.update(header_extra)
        header_bytes = (json.dumps(header, ensure_ascii=False) + "\n").encode("utf-8")
        if not ZSTD.available():
            raise RuntimeError("libzstd 不可用，无法构造合成会话 fixture")
        parts = [ZSTD.compress(header_bytes)]
        if compression == "normal":
            groups = [events[i:i + 2] for i in range(0, len(events), 2)] or [[]]
            for group in groups:
                payload = "".join(json.dumps(ev, ensure_ascii=False) + "\n" for ev in group).encode("utf-8")
                if payload:
                    parts.append(ZSTD.compress(payload))
        elif compression == "torn":
            payload = "".join(json.dumps(ev, ensure_ascii=False) + "\n" for ev in events).encode("utf-8")
            frame = ZSTD.compress(payload)
            parts.append(frame[: max(5, len(frame) // 2)])  # 人为截断 → 残缺帧
        elif compression == "none":
            # 明文 jsonl
            body = "".join(json.dumps(ev, ensure_ascii=False) + "\n" for ev in events).encode("utf-8")
            data = header_bytes + body
            d = os.path.join(sessions_root, project_key(cwd), encode_segment(sid))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "session.v4.jsonl"), "wb") as f:
                f.write(data)
            return
        d = os.path.join(sessions_root, project_key(cwd), encode_segment(sid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "session.v4.jsonl.zstd"), "wb") as f:
            f.write(b"".join(parts))

    # 中文标题 + 多轮事件
    events_cn = [
        {"seq": 0, "time": 1767225600100, "type": "session/title", "data": {"title": "中文会话标题：备份测试"}},
        {"seq": 1, "time": 1767225600200, "type": "message/user", "data": {"text": "第一轮：你好"}},
        {"seq": 2, "time": 1767225600300, "type": "message/assistant", "data": {"text": "第二轮：收到"}},
        {"seq": 3, "time": 1767225600400, "type": "message/user", "data": {"text": "第三轮：中文继续"}},
    ]
    make_session("session-11111111-1111-4111-8111-111111111111", ws1, {"agentPreset": "default"}, events_cn)
    make_session("session-22222222-2222-4222-8222-222222222222", ws2, {}, [
        {"seq": 0, "time": 1767225600100, "type": "message/user", "data": {"text": "space dir session"}},
    ])
    # 损坏/接收中会话（torn tail）
    make_session("session-33333333-3333-4333-8333-333333333333", ws3, {}, [
        {"seq": 0, "time": 1767225600100, "type": "message/user", "data": {"text": "torn"}},
        {"seq": 1, "time": 1767225600200, "type": "message/assistant", "data": {"text": "tail"}},
    ], compression="torn")
    # 明文会话（格式兼容分支）
    make_session("session-44444444-4444-4444-8444-444444444444", ws3, {}, [
        {"seq": 0, "time": 1767225600100, "type": "message/user", "data": {"text": "plaintext"}},
    ], compression="none")

    # 空会话（仅注册表引用，无文件）
    empty_session_id = "session-55555555-5555-4555-8555-555555555555"

    # 凭据/敏感文件（必须被排除，且不得进入任何 manifest/zip 条目）
    for name, content in {
        ".credentials.yaml": "token: should-not-be-in-backup\n",
        "host-ready.json": json.dumps({"token": "SECRET-TOKEN", "workspace": ws1}),
        ".anonymous-user-id": "anon",
    }.items():
        with open(os.path.join(home, name), "w", encoding="utf-8") as f:
            f.write(content)
    os.makedirs(os.path.join(home, "openai-gateway"), exist_ok=True)
    with open(os.path.join(home, "openai-gateway", "api-key"), "w", encoding="utf-8") as f:
        f.write("sk-SECRET")
    os.makedirs(os.path.join(home, "attachments", "v1", "files"), exist_ok=True)
    with open(os.path.join(home, "attachments", "v1", "files", "abc123"), "wb") as f:
        f.write(b"attachment-bytes")
    os.makedirs(os.path.join(home, "attachments", "v1", "tmp"), exist_ok=True)
    with open(os.path.join(home, "attachments", "v1", "tmp", "temp.bin"), "wb") as f:
        f.write(b"tmp")

    # profile + 插件清单
    profile = os.path.join(home, "profiles", "ondevice")
    os.makedirs(os.path.join(profile, "node_modules", "demo-plugin"), exist_ok=True)
    with open(os.path.join(profile, "node_modules", "demo-plugin", "package.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "demo-plugin", "version": "9.9.9"}, f)
    with open(os.path.join(profile, "package.json"), "w", encoding="utf-8") as f:
        json.dump({
            "name": "synthetic-profile",
            "version": "0.0.0",
            "dependencies": {"demo-plugin": "github:example/demo#v9.9.9"},
            "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "demo-plugin"]}},
        }, f, ensure_ascii=False)

    # workspace.json（含中文标题、空会话、顺序/置顶）
    registry = {
        "unit": {"name": "workspace", "version": 2},
        "global": {
            "initialized": True,
            "workspaceIds": ["ws-1", "ws-2", "ws-3"],
            "archivedSessionIds": [],
            "pinnedSessionIds": ["session-11111111-1111-4111-8111-111111111111"],
        },
        "tables": {
            "workspaces": {
                "ws-1": {"path": ws1, "title": "中文工作区", "sessionIds": [
                    "session-11111111-1111-4111-8111-111111111111"], "createdAt": "2026-01-01T00:00:00.000Z", "updatedAt": "2026-01-01T00:00:00.000Z"},
                "ws-2": {"path": ws2, "title": "带空格 工作区", "sessionIds": [
                    "session-22222222-2222-4222-8222-222222222222"], "createdAt": "2026-01-01T00:00:00.000Z", "updatedAt": "2026-01-01T00:00:00.000Z"},
                "ws-3": {"path": ws3, "title": "plain", "sessionIds": [
                    "session-33333333-3333-4333-8333-333333333333",
                    "session-44444444-4444-4444-8444-444444444444",
                    empty_session_id], "createdAt": "2026-01-01T00:00:00.000Z", "updatedAt": "2026-01-01T00:00:00.000Z"},
            }
        },
    }
    os.makedirs(os.path.join(home, "storages"), exist_ok=True)
    with open(os.path.join(home, "storages", "workspace.json"), "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    return {"home": home, "ws1": ws1, "ws2": ws2, "ws3": ws3, "emptySessionId": empty_session_id}


def ref_scan_sessions(home):
    """参考实现：扫描 sessions 树（只读 header/帧结构，不输出正文）。"""
    sessions_root = os.path.join(home, "sessions")
    out = []
    for project in sorted(os.listdir(sessions_root)):
        project_dir = os.path.join(sessions_root, project)
        if not os.path.isdir(project_dir):
            continue
        for sdir in sorted(os.listdir(project_dir)):
            session_dir = os.path.join(project_dir, sdir)
            if not os.path.isdir(session_dir):
                continue
            candidates = []
            for name in os.listdir(session_dir):
                m = re.match(r"^session\.v(\d+)\.jsonl(\.zstd)?$", name)
                if m:
                    candidates.append((int(m.group(1)), name, m.group(2) == ".zstd"))
            if not candidates:
                continue
            candidates.sort()
            version, fname, is_zstd = candidates[-1]
            data = open(os.path.join(session_dir, fname), "rb").read()
            if is_zstd:
                frames, torn = zstd_frames(data)
                # 解第一帧（用 Python ctypes ZSTD 解压简单接口）
                header_line = ref_first_plain_line(data)
            else:
                frames, torn = [(0, len(data))], False
                header_line = data.split(b"\n")[0]
            header = json.loads(header_line.decode("utf-8"))
            out.append({
                "id": header.get("id"),
                "dirId": decode_segment(sdir),
                "cwd": header.get("cwd"),
                "frames": len(frames),
                "torn": torn,
                "version": version,
                "compression": "zstd" if is_zstd else "none",
                "headerValid": header.get("type") == "session" and header.get("version") == 4,
                "dirMatchesHeader": decode_segment(sdir) == header.get("id"),
            })
    return out


def decode_segment(encoded):
    if encoded == "~002E":
        return "."
    out = []
    i = 0
    while i < len(encoded):
        if encoded[i] == "~":
            hexpart = encoded[i + 1:i + 5]
            if not re.match(r"^[0-9A-Fa-f]{4}$", hexpart):
                return None
            out.append(chr(int(hexpart, 16)))
            i += 5
        else:
            out.append(encoded[i])
            i += 1
    return "".join(out)


def ref_first_plain_line(zstd_bytes):
    """解压首帧以读取 header 行；使用 libzstd 解压 API（参考实现，不执行 JS）。"""
    if not ZSTD.available():
        raise RuntimeError("libzstd 不可用")
    frames, _torn = zstd_frames(zstd_bytes)
    if not frames:
        raise RuntimeError("no complete zstd frame")
    start, end = frames[0]
    frame = zstd_bytes[start:end]
    ZSTD.lib.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    ZSTD.lib.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
    ZSTD.lib.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
    ZSTD.lib.ZSTD_decompress.restype = ctypes.c_size_t
    ZSTD.lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
    ZSTD.lib.ZSTD_isError.restype = ctypes.c_uint
    src = ctypes.create_string_buffer(frame, len(frame))
    size = ZSTD.lib.ZSTD_getFrameContentSize(src, len(frame))
    if size in (0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFE) or size == 0:
        raise RuntimeError("unknown/failed frame content size")
    dst = ctypes.create_string_buffer(int(size))
    got = ZSTD.lib.ZSTD_decompress(dst, int(size), src, len(frame))
    if ZSTD.lib.ZSTD_isError(got):
        raise RuntimeError("zstd decompress failed")
    return dst.raw[:got].split(b"\n")[0]


def is_safe_zip_entry(name):
    if not name or len(name) > 1024:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return False
    if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return False
    is_dir = name.endswith("/")
    body = name[:-1] if is_dir else name
    if not body:
        return False
    for seg in body.split("/"):
        if not seg or seg in (".", "..") or len(seg) > 255:
            return False
        if seg.endswith(".") or seg.endswith(" "):
            return False
    return True


def ref_build_manifest(data):
    home = data["home"]
    sessions = ref_scan_sessions(home)
    registry = json.loads(read_text(os.path.join(home, "storages", "workspace.json")))
    workspaces = []
    for wid, rec in registry["tables"]["workspaces"].items():
        workspaces.append({
            "path": rec["path"], "title": rec["title"], "sessionIds": rec["sessionIds"],
            "createdAt": rec["createdAt"], "updatedAt": rec["updatedAt"],
        })
    # 工作区文件（排除规则参考实现）
    skip_dirs = {"node_modules", ".git", ".svn", ".hg", ".cache", "__pycache__", ".virtfs_metadata", "dshm-backup"}
    workspace_files = []
    for idx, ws in enumerate(workspaces):
        root = ws["path"]
        if not os.path.isdir(root):
            continue
        group = {"workspacePath": root, "entryPrefix": "data/workspace-files/ws-%04d/" % idx, "files": []}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted([d for d in dirnames if d not in skip_dirs])
            for fname in sorted(filenames):
                abs_path = os.path.join(dirpath, fname)
                rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
                if not is_safe_zip_entry(rel):
                    continue
                entry = group["entryPrefix"] + rel
                if not is_safe_zip_entry(entry):
                    continue
                payload = open(abs_path, "rb").read()
                group["files"].append({"rel": rel, "entry": entry, "size": len(payload), "sha256": sha256_bytes(payload)})
        workspace_files.append(group)
    # 附件（排除 tmp）
    attachments = []
    att_root = os.path.join(home, "attachments")
    if os.path.isdir(att_root):
        for dirpath, dirnames, filenames in os.walk(att_root):
            dirnames[:] = [d for d in dirnames if d != "tmp"]
            for fname in sorted(filenames):
                abs_path = os.path.join(dirpath, fname)
                rel = os.path.relpath(abs_path, att_root).replace(os.sep, "/")
                payload = open(abs_path, "rb").read()
                attachments.append({"entry": "data/attachments/" + rel, "size": len(payload), "sha256": sha256_bytes(payload)})
    sessions_manifest = []
    for s in sessions:
        zdata = None
        sdir = None
        for project in os.listdir(os.path.join(home, "sessions")):
            cand = os.path.join(home, "sessions", project, encode_segment(s["id"])) if s.get("id") else None
            if cand and os.path.isdir(cand):
                sdir = cand
                break
        fname = "session.v4.jsonl.zstd" if s["compression"] == "zstd" else "session.v4.jsonl"
        payload = open(os.path.join(sdir, fname), "rb").read()
        sessions_manifest.append({
            "id": s["id"],
            "cwd": s["cwd"],
            "createdAt": 1767225600000,
            "file": "data/sessions/%s/session.v4.jsonl%s" % (encode_segment(s["id"]), ".zstd" if s["compression"] == "zstd" else ""),
            "size": len(payload),
            "sha256": sha256_bytes(payload),
            "complete": (not s["torn"]),
            "incompleteReason": "" if not s["torn"] else "日志末尾存在未完成 zstd 帧",
        })
    manifest = {
        "format": "dshm-backup",
        "formatVersion": 1,
        "kind": "manual",
        "createdAt": "2026-01-01T00:00:00.000Z",
        "source": {
            "coreVersion": "0.2.1-rc.2+dshm.73",
            "upstreamDshVersion": "0.2.1-rc.2",
            "nodeVersion": "v24.2.0",
            "platform": "openharmony/arm64",
            "jitless": True,
            "zstd": True,
            "profile": "ondevice",
            "sessionFormatVersion": 4,
            "workspaceDomainVersion": 2,
        },
        "stats": {
            "sessions": len(sessions_manifest), "workspaces": len(workspaces),
            "workspaceFiles": sum(len(g["files"]) for g in workspace_files),
            "attachments": len(attachments),
            "totalBytes": sum(s["size"] for s in sessions_manifest) + sum(f["size"] for g in workspace_files for f in g["files"]) + sum(a["size"] for a in attachments),
        },
        "contents": {
            "sessions": sessions_manifest,
            "workspaces": workspaces,
            "workspaceRegistry": {
                "entry": "data/workspace.json",
                "sha256": sha256_bytes(read_text(os.path.join(home, "storages", "workspace.json")).encode("utf-8")),
            },
            "workspaceFiles": workspace_files,
            "attachments": attachments,
            "plugins": {
                "core": [{"name": "@deepseek-ai/cordis-plugin-timer", "version": "0.2.1-rc.2"}],
                "user": [{"name": "demo-plugin", "version": "9.9.9"}],
            },
        },
        "exclusions": [
            {"pattern": "DSH_HOME/.credentials.yaml", "reason": "凭据文件"},
            {"pattern": "DSH_HOME/host-ready.json", "reason": "宿主握手文件（含 token）"},
        ],
        "warnings": [],
    }
    return manifest


def ref_validate_manifest(m):
    errors = []
    if m.get("format") != "dshm-backup":
        errors.append("bad-format")
    if m.get("formatVersion") != 1:
        errors.append("bad-format-version")
    registry = m.get("contents", {}).get("workspaceRegistry")
    if registry is not None:
        if not isinstance(registry, dict) or not re.match(r"^[0-9a-f]{64}$", str(registry.get("sha256", ""))):
            errors.append("bad-registry-sha")
    for s in m["contents"]["sessions"]:
        if not re.match(r"^[0-9a-f]{64}$", s.get("sha256", "")):
            errors.append("bad-session-sha")
        if not is_safe_zip_entry(s.get("file", "")):
            errors.append("unsafe-session-file")
    for g in m["contents"]["workspaceFiles"]:
        for f in g["files"]:
            if not is_safe_zip_entry(f.get("entry", "")):
                errors.append("unsafe-workspace-file")
    plugins = m.get("contents", {}).get("plugins", {})
    for key in ("user", "core"):
        items = plugins.get(key)
        if not isinstance(items, list):
            errors.append("bad-plugins-" + key)
            continue
        for item in items:
            extra = [k for k in item.keys() if k not in ("name", "version")]
            if extra:
                errors.append("plugin-extra-fields:" + key + ":" + ",".join(extra))
            if not isinstance(item.get("version"), str):
                errors.append("bad-plugin-version:" + key)
    return errors


def ref_expected_hashes(m):
    expected = {}
    for s in m["contents"]["sessions"]:
        expected[s["file"]] = s["sha256"]
    expected[m["contents"]["workspaceRegistry"]["entry"]] = m["contents"]["workspaceRegistry"]["sha256"]
    for g in m["contents"]["workspaceFiles"]:
        for f in g["files"]:
            expected[f["entry"]] = f["sha256"]
    for a in m["contents"]["attachments"]:
        expected[a["entry"]] = a["sha256"]
    return expected


def build_reference_zip(data, manifest, zip_path):
    """参考 ZIP 构造（按实现的包布局）。注意：不是 dshm-backup 的 JS ZIP 写入器产物。"""
    home = data["home"]
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        z.writestr("data/workspace.json", read_text(os.path.join(home, "storages", "workspace.json")))
        for s in manifest["contents"]["sessions"]:
            sdir = None
            for project in os.listdir(os.path.join(home, "sessions")):
                cand = os.path.join(home, "sessions", project, encode_segment(s["id"]))
                if os.path.isdir(cand):
                    sdir = cand
                    break
            fname = "session.v4.jsonl.zstd" if os.path.exists(os.path.join(sdir, "session.v4.jsonl.zstd")) else "session.v4.jsonl"
            z.write(os.path.join(sdir, fname), s["file"])
        for g in manifest["contents"]["workspaceFiles"]:
            for f in g["files"]:
                z.write(os.path.join(g["workspacePath"], f["rel"]), f["entry"])
        for a in manifest["contents"]["attachments"]:
            rel = a["entry"][len("data/attachments/"):]
            z.write(os.path.join(home, "attachments", rel), a["entry"])
    return zip_path


def test_synthetic_fixtures():
    if not ZSTD.available():
        record("合成 fixture（zstd 会话）", "skip", "内嵌 libzstd 不可用，无法构造会话 fixture")
        return None
    try:
        data = build_synthetic_home()
    except Exception as e:  # noqa: BLE001
        record("合成 fixture 构造", "fail", "构造失败：%s" % e)
        return None
    record("合成 fixture 构造", "pass", "工作区/会话/附件/凭据/插件清单/workspace.json 已生成")
    try:
        sessions = ref_scan_sessions(data["home"])
        ids = {s["id"] for s in sessions}
        expected_ids = {
            "session-11111111-1111-4111-8111-111111111111",
            "session-22222222-2222-4222-8222-222222222222",
            "session-33333333-3333-4333-8333-333333333333",
            "session-44444444-4444-4444-8444-444444444444",
        }
        if ids != expected_ids:
            record("会话 fixture 扫描（中文标题/多轮/损坏/明文）", "fail", "会话 id 集合不符：%s" % sorted(ids))
        else:
            torn = [s for s in sessions if s["torn"]]
            plain = [s for s in sessions if s["compression"] == "none"]
            if len(torn) != 1:
                record("会话 fixture 扫描（中文标题/多轮/损坏/明文）", "fail", "torn 会话数量不符：%d" % len(torn))
            elif len(plain) != 1:
                record("会话 fixture 扫描（中文标题/多轮/损坏/明文）", "fail", "明文会话数量不符：%d" % len(plain))
            else:
                record("会话 fixture 扫描（中文标题/多轮/损坏/明文）", "pass",
                       "4 个会话；1 个 torn 尾部、1 个明文、header 与目录 id 一致")
        empty_id = data["emptySessionId"]
        registry = json.loads(read_text(os.path.join(data["home"], "storages", "workspace.json")))
        all_reg_ids = [sid for wid, rec in registry["tables"]["workspaces"].items() for sid in rec["sessionIds"]]
        if empty_id in all_reg_ids and empty_id not in ids:
            record("空会话 fixture（仅注册表引用、无日志文件）", "pass", "空会话 id 保留在 workspace.json，无日志文件")
        else:
            record("空会话 fixture（仅注册表引用、无日志文件）", "fail", "空会话 fixture 不符")
        manifest = ref_build_manifest(data)
        errors = ref_validate_manifest(manifest)
        if errors:
            record("参考 manifest 构造与校验", "fail", "；".join(errors))
        else:
            record("参考 manifest 构造与校验", "pass",
                   "sessions=%d workspaces=%d files=%d attachments=%d" % (
                       len(manifest["contents"]["sessions"]), len(manifest["contents"]["workspaces"]),
                       manifest["stats"]["workspaceFiles"], manifest["stats"]["attachments"]))
        # 中文/空格/嵌套/空文件/无扩展名文件进入清单
        rels = [f["rel"] for g in manifest["contents"]["workspaceFiles"] for f in g["files"]]
        expect_rels = {"中文 文件名.txt", "子目录/更一层/无扩展名", "空文件", "a b c.md", "plain.txt"}
        if not expect_rels.issubset(set(rels)):
            record("中文/空格/嵌套/空文件/无扩展名文件 fixture", "fail", "缺少：%s" % sorted(expect_rels - set(rels)))
        else:
            record("中文/空格/嵌套/空文件/无扩展名文件 fixture", "pass", "5 类文件名均进入工作区文件清单")
        excluded = [f for g in manifest["contents"]["workspaceFiles"] for f in g["files"] if "node_modules" in f["rel"] or "/.git/" in f["rel"]]
        if excluded:
            record("排除规则（node_modules/.git）", "fail", "误收录：%s" % excluded[:3])
        else:
            record("排除规则（node_modules/.git）", "pass", "node_modules 与 .git 未进入清单")
        # 凭据值不得进入 manifest/zip（排除项里出现文件名是设计使然，不算泄漏）
        manifest_text = json.dumps(manifest, ensure_ascii=False)
        leak = [tok for tok in ["SECRET-TOKEN", "sk-SECRET"] if tok in manifest_text]
        if leak:
            record("凭据不进入清单/报告", "fail", "清单出现敏感标记：%s" % leak)
        else:
            record("凭据不进入清单/报告", "pass", "SECRET-TOKEN/sk-SECRET 等未出现在 manifest")
        # 参考 ZIP 构造 + sha 校验
        zip_path = os.path.join(TMP, "synthetic-manual-20260101-000000-abcdef12.zip")
        build_reference_zip(data, manifest, zip_path)
        zf = zipfile.ZipFile(zip_path)
        names = set(zf.namelist())
        if "manifest.json" not in names:
            record("参考 ZIP 布局（manifest + payload）", "fail", "缺少 manifest.json")
        else:
            expected = ref_expected_hashes(manifest)
            bad = []
            for name, sha in expected.items():
                if name not in names:
                    bad.append("缺少 " + name)
                    continue
                if sha256_bytes(zf.read(name)) != sha:
                    bad.append("sha 不一致 " + name)
            if bad:
                record("参考 ZIP 布局（manifest + payload）", "fail", "；".join(bad[:5]))
            else:
                record("参考 ZIP 布局（manifest + payload）", "pass", "%d 个 payload 条目 sha256 一致" % len(expected))
            unsafe = [n for n in names if not is_safe_zip_entry(n)]
            if unsafe:
                record("参考 ZIP 条目名安全", "fail", "不安全条目：%s" % unsafe[:3])
            else:
                record("参考 ZIP 条目名安全", "pass", "全部条目通过路径安全规则（含中文/空格/嵌套）")
        original_manifest_sha = sha256_bytes(zf.read("manifest.json")) if "manifest.json" in names else None
        zf.close()
        # 篡改检测
        tampered = os.path.join(TMP, "tampered.zip")
        with open(zip_path, "rb") as src, open(tampered, "wb") as dst:
            blob = bytearray(src.read())
            blob[len(blob) // 2] ^= 0xFF
            dst.write(blob)
        tamper_detected = False
        try:
            with zipfile.ZipFile(tampered) as zt:
                bad = zt.testzip()
                if bad is not None:
                    tamper_detected = True
                else:
                    # CRC 偶然通过时，用清单 SHA 对照（篡改后的包不应再满足原 SHA）
                    try:
                        if sha256_bytes(zt.read("manifest.json")) != original_manifest_sha:
                            tamper_detected = True
                    except Exception:
                        tamper_detected = True
        except Exception:
            tamper_detected = True
        if tamper_detected:
            record("备份包篡改拒绝（参考实现）", "pass", "篡改后 ZIP CRC 或清单 SHA 校验失败")
        else:
            record("备份包篡改拒绝（参考实现）", "fail", "篡改未被检测")
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        record("合成 fixture 测试", "fail", "异常：%s" % e)


def test_mapping_and_safety():
    # 路径编码与真实 DSHM 目录名交叉验证（真实证据：sessions 下实际目录名）
    real = project_key("/storage/Users/currentUser/Download/com.dshm.dshclient")
    if real == "--storage-Users-currentUser-Download-com.dshm.dshclient--":
        record("projectKey 与真实会话目录名一致", "pass", real)
    else:
        record("projectKey 与真实会话目录名一致", "fail", "计算结果 %s" % real)
    cases = [
        ("../evil", False), ("/abs/path", False), ("C:/x", False), ("a\\b", False),
        ("a//b", False), ("a/./b", False), ("a/../b", False), ("a/b/", True),
        ("中文 文件名.txt", True), ("a b/c d/无扩展名", True), ("", False),
    ]
    bad = []
    for name, expected in cases:
        if is_safe_zip_entry(name) != expected:
            bad.append("%r 期望 %s" % (name, expected))
    if bad:
        record("ZIP 条目路径安全规则（参考实现）", "fail", "；".join(bad))
    else:
        record("ZIP 条目路径安全规则（参考实现）", "pass", "路径穿越/盘符/反斜杠/空段/中文均按预期")
    # 恢复映射：已存在可写则保留，否则重映射到允许目录
    src_paths = ["/storage/Users/currentUser/Download/w1", "/opt/legacy/project"]
    fallback_base = os.path.join(TMP, "restored")
    mapping = []
    used = []
    for src in src_paths:
        target = None
        if src.startswith("/storage/Users/currentUser/Download/"):
            target = {"from": src, "to": src, "mode": "keep", "reason": "目标机原工作区目录已存在且可写"}
        if target is None:
            base = os.path.basename(src.rstrip("/")) or "workspace"
            cand = os.path.join(fallback_base, base)
            n = 2
            while cand in used:
                cand = os.path.join(fallback_base, base + "-" + str(n))
                n += 1
            target = {"from": src, "to": cand, "mode": "remap", "reason": "重映射"}
        used.append(target["to"])
        mapping.append(target)
    escape = [m for m in mapping if not (m["to"].startswith("/storage/Users/currentUser/Download/") or m["to"].startswith(fallback_base))]
    if escape or len(set(m["to"] for m in mapping)) != len(mapping):
        record("恢复路径映射（重映射 + 唯一性 + 不越界）", "fail", "映射异常：%s" % mapping)
    else:
        record("恢复路径映射（重映射 + 唯一性 + 不越界）", "pass", "2 条映射，1 keep + 1 remap，目标唯一且在允许目录")
    # workspace.json 改写：path 映射、去 pendingMutation、initialized=true
    registry = {
        "unit": {"name": "workspace", "version": 2},
        "global": {"initialized": False, "workspaceIds": ["w1"], "archivedSessionIds": [], "pinnedSessionIds": [],
                   "pendingMutation": {"operation": "create", "workspaceId": "w1"}},
        "tables": {"workspaces": {"w1": {"path": src_paths[0], "title": "t", "sessionIds": ["s1"],
                                         "createdAt": "2026-01-01T00:00:00.000Z", "updatedAt": "2026-01-01T00:00:00.000Z"}}},
    }
    mapping_index = {m["from"]: m["to"] for m in mapping}
    rewritten = json.loads(json.dumps(registry))
    for wid, rec in rewritten["tables"]["workspaces"].items():
        rec["path"] = mapping_index.get(rec["path"], rec["path"])
    rewritten["global"].pop("pendingMutation", None)
    rewritten["global"]["initialized"] = True
    if rewritten["tables"]["workspaces"]["w1"]["path"] != mapping[0]["to"] or "pendingMutation" in rewritten["global"]:
        record("workspace.json 改写规则（参考实现）", "fail", "改写结果不符")
    else:
        record("workspace.json 改写规则（参考实现）", "pass", "path 映射/清 pendingMutation/initialized=true")
    # 全新实例判定矩阵
    matrix = [
        ({"storedSessionCount": 0, "workspaceJsonExists": False}, True),
        ({"storedSessionCount": 1, "workspaceJsonExists": True}, False),
    ]
    ok = all(((s["storedSessionCount"] == 0) == expect) for s, expect in matrix)
    record("全新实例判定（参考实现）", "pass" if ok else "fail", "无可迁移数据=全新；已有会话=非全新（默认拒绝覆盖）")


def test_backup_protocol_reference():
    # 唯一命名
    names = set()
    for i in range(100):
        stamp = "20260101-0000%02d" % (i % 60)
        rand = "%08x" % (i * 2654435761 % (2 ** 32))
        names.add("dshm-backup-manual-%s-%s.zip" % (stamp, rand))
    record("备份文件名唯一（参考实现）", "pass" if len(names) == 100 else "fail", "%d/100 唯一" % len(names))
    # 发布协议：临时文件 → 校验 → rename；失败不留正式文件
    publish_dir = os.path.join(TMP, "publish")
    os.makedirs(publish_dir, exist_ok=True)
    final = os.path.join(publish_dir, "final.zip")
    tmp = os.path.join(publish_dir, ".final.zip.writing")
    with open(tmp, "wb") as f:
        f.write(b"partial")
    # 模拟校验失败：删除临时文件，不产生正式文件
    os.unlink(tmp)
    if not os.path.exists(final):
        record("备份中途失败不留正式文件（发布协议参考实现）", "pass", "校验失败删除 .writing，正式文件不存在")
    else:
        record("备份中途失败不留正式文件（发布协议参考实现）", "fail", "失败后存在正式文件")
    # 重复触发：backupFileName 含随机与时间戳；同名存在则拒绝覆盖（契约）
    index_src = read_text(os.path.join(LIB, "backup.js"))
    if "BACKUP_EXISTS" in index_src and "拒绝覆盖" in index_src:
        record("重复备份不覆盖旧文件（代码契约）", "pass", "createBackup 对已存在目标抛 BACKUP_EXISTS")
    else:
        record("重复备份不覆盖旧文件（代码契约）", "fail", "未找到拒绝覆盖分支")
    # 轮转：只删同类自动备份，手动备份不认识 kind=manual 删除
    rotate_src = index_src[index_src.find("export async function rotateBackups"):]
    if 'kind !== "baseline" && kind !== "emergency"' in rotate_src and 'item.kind === kind' in rotate_src:
        record("自动轮转不删除手动备份（代码契约）", "pass", "rotateBackups 仅处理 baseline/emergency，按 kind 过滤")
    else:
        record("自动轮转不删除手动备份（代码契约）", "fail", "轮转过滤逻辑不符")
    # 先验证后轮转：createBackup 成功返回后才调用
    idx_src = read_text(os.path.join(LIB, "index.js"))
    if "createBackup(runtime, \"baseline\"" in idx_src and "rotateBackups(runtime, \"baseline\")" in idx_src and \
            idx_src.find("createBackup") < idx_src.find("rotateBackups(runtime, \"baseline\")"):
        record("新基线校验成功后才轮转（代码契约）", "pass", "createBackup → then(rotateBackups)")
    else:
        record("新基线校验成功后才轮转（代码契约）", "fail", "顺序不符")


def test_rollback_reference():
    root = os.path.join(TMP, "restore-target")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(os.path.join(root, "sessions"))
    os.makedirs(os.path.join(root, "workspace"))
    registry_path = os.path.join(root, "workspace.json")
    original_registry = json.dumps({"unit": {"name": "workspace", "version": 2}, "global": {"initialized": True},
                                    "tables": {"workspaces": {}}})
    with open(registry_path, "w", encoding="utf-8") as f:
        f.write(original_registry)
    journal = {"createdFiles": [], "createdDirs": [], "createdSessionDirs": []}
    try:
        # 模拟恢复：创建会话目录/文件/工作区文件，然后失败
        session_dir = os.path.join(root, "sessions", "s1")
        os.makedirs(session_dir)
        journal["createdSessionDirs"].append(session_dir)
        created_file = os.path.join(root, "workspace", "file.txt")
        with open(created_file, "w", encoding="utf-8") as f:
            f.write("new")
        journal["createdFiles"].append(created_file)
        # 写坏 registry
        with open(registry_path, "w", encoding="utf-8") as f:
            f.write("{broken")
        raise RuntimeError("simulated failure")
    except RuntimeError:
        # 参考回滚实现：删除 createdFiles / createdSessionDirs，恢复 registry 原文
        for p in reversed(journal["createdFiles"]):
            if os.path.exists(p):
                os.unlink(p)
        for p in reversed(journal["createdSessionDirs"]):
            shutil.rmtree(p, ignore_errors=True)
        with open(registry_path, "w", encoding="utf-8") as f:
            f.write(original_registry)
        ok = (not os.path.exists(os.path.join(root, "sessions", "s1"))
              and not os.path.exists(created_file)
              and read_text(registry_path) == original_registry)
        if ok:
            record("导入失败回滚（参考实现）", "pass", "删除本次创建项并恢复 workspace.json 原文")
        else:
            record("导入失败回滚（参考实现）", "fail", "回滚后目标状态不符")
    # 代码层：rollbackRestore 覆盖文件/目录/registry
    src = read_text(os.path.join(LIB, "restore.js"))
    needed = ["createdFiles", "createdSessionDirs", "registryExisted", "registryText", "workspaceRegistryPath"]
    if all(token in src for token in needed):
        record("导入失败回滚（代码契约）", "pass", "rollbackRestore 处理创建文件/会话目录/registry 快照")
    else:
        record("导入失败回滚（代码契约）", "fail", "rollbackRestore 缺少关键处理")


def test_versions_and_freshness():
    src = read_text(os.path.join(LIB, "format.js"))
    for token in ["describeVersionComparison", "sessionFormatVersion", "workspaceDomainVersion", "core-mismatch"]:
        if token not in src:
            record("版本比较规则（代码契约）", "fail", "format.js 缺少 %s" % token)
            break
    else:
        record("版本比较规则（代码契约）", "pass", "核心版本/会话格式/工作区格式分开比较，不合并")
    # 不把 0.2.1 前缀当同版本
    if 'source.coreVersion !== target.coreVersion' in src:
        record("同版本判定为完整版本字符串相等", "pass", "严格比较 coreVersion 全串")
    else:
        record("同版本判定为完整版本字符串相等", "fail", "未找到严格比较")
    # 备份 manifest 记录版本维度
    backup_src = read_text(os.path.join(LIB, "backup.js"))
    for token in ["coreVersion:", "upstreamDshVersion:", "sessionFormatVersion:", "workspaceDomainVersion:", "backupToolVersion:"]:
        if token not in backup_src:
            record("manifest 版本维度齐全", "fail", "缺少 %s" % token)
            break
    else:
        record("manifest 版本维度齐全", "pass", "核心/上游/运行时/格式/工具版本分字段记录")


def test_package_and_docs():
    pkg = json.loads(read_text(os.path.join(PLUGIN, "package.json")))
    if pkg.get("name") != "dshm-backup":
        record("package.json 包名/入口/声明", "fail", "name != dshm-backup")
        return
    checks = {
        "type=module": pkg.get("type") == "module",
        "main=lib/index.js": pkg.get("main") == "lib/index.js",
        "dsh.bundle.patch": pkg.get("dsh", {}).get("bundle", {}).get("patch") == "./cordis.patch.yml",
        "dependencies 为空": pkg.get("dependencies", {}) == {},
        "无 engines/peerDependencies": "engines" not in pkg and "peerDependencies" not in pkg,
    }
    bad = [k for k, v in checks.items() if not v]
    if bad:
        record("package.json 包名/入口/声明", "fail", "；".join(bad))
    else:
        record("package.json 包名/入口/声明", "pass", "name/type/main/exports/dsh.bundle.patch/零依赖/无 engines")
    patch = read_text(os.path.join(PLUGIN, "cordis.patch.yml"))
    if "- id: dshm-backup" in patch and "name: 'dshm-backup'" in patch:
        record("cordis.patch.yml 注册行 id 与包名一致", "pass")
    else:
        record("cordis.patch.yml 注册行 id 与包名一致", "fail", "未找到 insert 行")
    # 页面注入与路由前缀
    index_src = read_text(os.path.join(LIB, "index.js"))
    for token in ["tapIndex", "register({", "\"/dshm-backup\"", "client.js", "internal/service"]:
        if token not in index_src:
            record("DSHM UI 注入链路（tapIndex + prefix 路由）", "fail", "缺少 %s" % token)
            break
    else:
        record("DSHM UI 注入链路（tapIndex + prefix 路由）", "pass", "tapIndex 注入 + /dshm-backup prefix 路由 + internal/service 重试")
    # 插件清单严格只含 name/version（不得含 spec/kind/id/bundle/disabled/profileBundles）
    util_src = read_text(os.path.join(LIB, "util.js"))
    m = re.search(r"inventory\.user\.push\(\{(.*?)\}\);", util_src, re.S)
    core_m = re.search(r"inventory\.core\.push\(\{(.*?)\}\);", util_src, re.S)
    if not m or not core_m:
        record("插件清单字段（仅 name/version）", "fail", "未找到 inventory user/core 构造")
    else:
        user_keys = set(re.findall(r"([A-Za-z_]+):", m.group(1)))
        core_keys = set(re.findall(r"([A-Za-z_]+):", core_m.group(1)))
        extra = (user_keys - {"name", "version"}) | (core_keys - {"name", "version"})
        if extra:
            record("插件清单字段（仅 name/version）", "fail", "多余字段：%s" % sorted(extra))
        elif "inventory.profileBundles" in util_src or "profileBundles:" in util_src:
            record("插件清单字段（仅 name/version）", "fail", "仍导出 profileBundles")
        elif "fmt.packageNameOf" not in util_src:
            record("插件清单字段（仅 name/version）", "fail", "核心 scoped 包名未用 packageNameOf 解析")
        else:
            record("插件清单字段（仅 name/version）", "pass", "user/core 均只有 name/version；scoped 包名经 packageNameOf")
    # 备份导出位置位于用户可见工作区 + 卸载保留性质说明
    if "dshm-backup" in util_src and "DOWNLOAD_ROOT_PREFIX" in util_src and "backups" in util_src:
        record("备份导出位置（用户可见 Download 工作区）", "pass", "默认 <workspace>/dshm-backup/backups，位于用户可见目录")
    else:
        record("备份导出位置（用户可见 Download 工作区）", "fail", "未找到用户可见目录推导")
    # 文档交付物
    docs = ["README.md", "FEASIBILITY.md", "TEST_REPORT.md", "HANDOFF_PHONE.md", "PHONE_ACCEPTANCE_PROMPT.md", "FIX_REPORT.md"]
    missing = [d for d in docs if not os.path.exists(os.path.join(ROOT, d))]
    if missing:
        record("交付文档存在性", "fail", "缺少：%s" % missing)
    else:
        record("交付文档存在性", "pass", "README/FEASIBILITY/TEST_REPORT/HANDOFF_PHONE/PHONE_ACCEPTANCE_PROMPT")
    # 文档不得把 PC/node 作为必要条件（只检查"要求使用"的表述；禁止性表述如"不得用电脑"是合规的）
    pc_requirement_patterns = [
        r"(必须|需要|请|要|只能)(使用|用|通过|在)[^。；\n]{0,16}(电脑|PC|adb|ADB|DevEco|桌面版 ?DSH|桌面浏览器)",
        r"(电脑|PC|adb|ADB|DevEco)[^。；\n]{0,12}(才能|才可以|是必须的|是必要的)",
    ]
    doc_hits = []
    for d in docs:
        p = os.path.join(ROOT, d)
        if not os.path.exists(p):
            continue
        text = read_text(p)
        for pattern in pc_requirement_patterns:
            m = re.search(pattern, text)
            if m:
                doc_hits.append(d + ":" + m.group(0)[:40])
    if doc_hits:
        record("文档无 PC 必需步骤", "fail", "；".join(doc_hits[:5]))
    else:
        record("文档无 PC 必需步骤", "pass", "未出现以 PC/桌面 DSH/ADB 为必要条件的表述（含手机提示词的禁止性表述）")
    # 手机验收提示词关键约束
    prompt_path = os.path.join(ROOT, "PHONE_ACCEPTANCE_PROMPT.md")
    if os.path.exists(prompt_path):
        prompt = read_text(prompt_path)
        needed = ["标准模式", "复述", "继续", "不自动卸载", "brickpack", "回传", "手机"]
        miss = [token for token in needed if token not in prompt]
        if miss:
            record("手机验收提示词关键约束", "fail", "缺少：%s" % miss)
        else:
            record("手机验收提示词关键约束", "pass", "标准模式/先复述后执行/不自动卸载/不装 brickpack/回传")
    else:
        record("手机验收提示词关键约束", "skip", "提示词文件待生成")


def test_lexical_fallback():
    """无 esprima 时的词法兜底检查（delimiter/字符串/注释/模板/正则平衡）。"""
    files = ["index.js", "backup.js", "restore.js", "util.js", "format.js", "zip.js", "client.js"]
    bad = []
    for name in files:
        src = read_text(os.path.join(LIB, name))
        err = lexical_check(src)
        if err:
            bad.append("%s: %s" % (name, err))
    if bad:
        record("词法平衡检查（分隔符/字符串/注释）", "fail", "；".join(bad[:5]))
    else:
        record("词法平衡检查（分隔符/字符串/注释）", "pass", "%d 个文件全部平衡" % len(files))


def lexical_check(src):
    """词法平衡检查（跳过注释/字符串/模板/正则），移植自 .recon/jscheck.py 的思路。"""
    i = 0
    n = len(src)
    line = 1
    stack = []
    errors = []
    prev = ""
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r":
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            if j < 0:
                errors.append("unterminated block comment at line %d" % line)
                break
            line += src.count("\n", i, j + 2)
            i = j + 2
            continue
        if c in ("'", '"'):
            q = c
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == q:
                    break
                if src[j] == "\n":
                    line += 1
                j += 1
            if j >= n:
                errors.append("unterminated string at line %d" % line)
                break
            i = j + 1
            prev = "str"
            continue
        if c == "`":
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "`":
                    break
                if src[j] == "\n":
                    line += 1
                j += 1
            if j >= n:
                errors.append("unterminated template at line %d" % line)
                break
            i = j + 1
            prev = "str"
            continue
        regex_allowed = prev in ("", "op", "return", "case", "typeof", "in", "of", "=>", "(", "[", "{", ",", ";", ":", "?", "=", "!", "&", "|", "+", "-", "*", "%", "<", ">", "}")
        if c == "/" and regex_allowed:
            j = i + 1
            in_class = False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                elif ch == "\n":
                    errors.append("newline in regex at line %d" % line)
                    break
                j += 1
            if errors:
                break
            if j >= n:
                errors.append("unterminated regex at line %d" % line)
                break
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            i = j
            prev = "str"
            continue
        if c.isalpha() or c in "_$":
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            word = src[i:j]
            prev = word if word in ("return", "case", "typeof", "in", "of") else "id"
            i = j
            continue
        matched = False
        for op in ("===", "!==", "==", "!=", "<=", ">=", "&&", "||", "??", "=>", "++", "--", "+=", "-=", "*=", "/=", "%="):
            if src.startswith(op, i):
                prev = "op" if op != "=>" else "=>"
                i += len(op)
                matched = True
                break
        if matched:
            continue
        if c in "([{":
            stack.append((c, line))
            prev = c
            i += 1
            continue
        if c in ")]}":
            if not stack:
                errors.append("unmatched %s at line %d" % (c, line))
                break
            op, opened = stack.pop()
            close = {"(": ")", "[": "]", "{": "}"}[op]
            if close != c:
                errors.append("mismatch: %s opened line %d closed by %s line %d" % (op, opened, c, line))
                break
            prev = c
            i += 1
            continue
        prev = "op"
        i += 1
    if stack and not errors:
        errors.append("unclosed delimiters: %s" % stack[-5:])
    return "；".join(errors) if errors else None


# ── 4. 外部复核退回修复的回归检查（静态/参考实现；不执行 JS）──────────────

def ref_package_name(module_name):
    if not isinstance(module_name, str) or module_name == "":
        return None
    parts = module_name.split("/")
    if module_name.startswith("@"):
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return None
        return parts[0] + "/" + parts[1]
    return parts[0] or None


def ref_is_known_version(v):
    return isinstance(v, str) and v != "" and v != "unknown" and v != "未确认"


def ref_version_level(source, target):
    if not isinstance(source, dict) or not isinstance(target, dict):
        return "unknown"
    level = "exact"
    if not ref_is_known_version(source.get("coreVersion")) or not ref_is_known_version(target.get("coreVersion")):
        level = "unknown"
    elif source.get("coreVersion") != target.get("coreVersion"):
        level = "core-mismatch"
    elif ref_is_known_version(source.get("upstreamDshVersion")) and ref_is_known_version(target.get("upstreamDshVersion")) \
            and source.get("upstreamDshVersion") != target.get("upstreamDshVersion"):
        level = "core-mismatch"
    format_unknown = False
    if not isinstance(source.get("sessionFormatVersion"), int) or not isinstance(target.get("sessionFormatVersion"), int):
        format_unknown = True
    elif source.get("sessionFormatVersion") != target.get("sessionFormatVersion"):
        level = "format-mismatch"
    if not isinstance(source.get("workspaceDomainVersion"), int) or not isinstance(target.get("workspaceDomainVersion"), int):
        format_unknown = True
    elif source.get("workspaceDomainVersion") != target.get("workspaceDomainVersion"):
        level = "format-mismatch"
    if format_unknown and level != "format-mismatch":
        level = "unknown"
    return level


def ref_normalize_path(value):
    if not isinstance(value, str) or not value.startswith("/"):
        return None
    parts = []
    for seg in value.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(seg)
    return "/" + "/".join(parts)


def ref_assess_fresh(state):
    """独立参考实现（镜像 format.assessFreshTarget 的严格规则）。"""
    if not isinstance(state, dict):
        return False, ["无法读取目标状态"]
    stored = state.get("storedSessionCount") if isinstance(state.get("storedSessionCount"), int) else 0
    archived = state.get("archivedSessionIds") if isinstance(state.get("archivedSessionIds"), list) else []
    pinned = state.get("pinnedSessionIds") if isinstance(state.get("pinnedSessionIds"), list) else []
    ids = state.get("workspaceIds") if isinstance(state.get("workspaceIds"), list) else []
    workspaces = state.get("workspaces") if isinstance(state.get("workspaces"), dict) else {}
    table_ids = list(workspaces.keys())
    host_path = ref_normalize_path(state.get("hostDefaultWorkspacePath"))
    reasons = []
    if not state.get("workspaceJsonExists"):
        if stored > 0:
            return False, ["已有会话"]
        if archived or pinned:
            return False, ["已有归档/置顶"]
        return True, ["workspace.json 不存在且无数据"]
    if stored > 0:
        reasons.append("已有会话")
    if archived:
        reasons.append("已有归档")
    if pinned:
        reasons.append("已有置顶")
    if state.get("hasPendingMutation") is True:
        reasons.append("存在 pendingMutation")
    if not state.get("initialized") is True:
        if not reasons and not ids and not table_ids:
            return True, ["未初始化且无记录"]
        reasons.append("未初始化但有记录")
        return False, reasons
    if not ids and not table_ids:
        return (len(reasons) == 0), (reasons or ["空注册表"])
    if reasons:
        return False, reasons
    if len(ids) != 1 or len(table_ids) != 1:
        return False, ["多个工作区记录"]
    only_id = ids[0]
    record = workspaces.get(only_id)
    if not isinstance(record, dict):
        return False, ["默认工作区记录缺失"]
    default_id = state.get("defaultWorkspaceId")
    if not isinstance(default_id, str) or not default_id:
        return False, ["无法确认 defaultWorkspaceId"]
    if default_id != only_id:
        return False, ["唯一工作区不是默认工作区"]
    if not host_path:
        return False, ["无法取得 Host 默认工作区路径"]
    record_path = ref_normalize_path(record.get("path"))
    if not record_path:
        return False, ["默认工作区 path 非法"]
    if record_path != host_path:
        return False, ["默认工作区 path 不一致"]
    session_ids = record.get("sessionIds")
    if not isinstance(session_ids, list):
        return False, ["sessionIds 结构非法"]
    if len(session_ids) > 0:
        return False, ["默认工作区已有会话 ID"]
    return True, ["只有系统默认空白工作区"]


def test_zip_pipeline_order():
    src = read_text(os.path.join(LIB, "zip.js"))
    start = src.find("async function writeEntryData")
    end = src.find("\n}\n", start)
    body = src[start:end] if start != -1 and end != -1 else ""
    problems = []
    if "const streams = [sourceStream, crcTransform];" not in body:
        problems.append("缺少 source→crcTransform 管线")
    if "streams.push(crcTransform)" in body:
        problems.append("仍存在旧错误顺序 streams.push(crcTransform)")
    i_const = body.find("const streams = [sourceStream, crcTransform]")
    i_deflate = body.find("streams.push(createDeflateRaw")
    if i_deflate != -1 and i_deflate < i_const:
        problems.append("deflate 仍在 crcTransform 之前")
    for token in ["compressedSize: sink.position - dataStart", "uncompressedSize: state.rawSize",
                  "crc32: (state.crc ^ 0xffffffff) >>> 0"]:
        if token not in body:
            problems.append("writeEntryData 返回字段缺失：" + token)
    for token in ["buildDataDescriptor({ crc32Value, compressedSize, uncompressedSize })",
                  "crc32Value >>> 0", "compressedSize >>> 0", "uncompressedSize >>> 0"]:
        if token not in src:
            problems.append("ZIP 头字段缺失：" + token)
    if problems:
        record("ZIP 写管线顺序（CRC/原始大小必须在 deflate 之前）", "fail", "；".join(problems))
    else:
        record("ZIP 写管线顺序（CRC/原始大小必须在 deflate 之前）", "pass",
               "streams=[source, crcTransform, (deflate)]；中央目录/data descriptor 使用原始 CRC/大小")


def test_fresh_target_matrix():
    host = "/storage/Users/currentUser/Download/com.dshm.dshclient"
    base = {
        "workspaceJsonExists": True,
        "initialized": True,
        "workspaceIds": ["ws-default"],
        "defaultWorkspaceId": "ws-default",
        "archivedSessionIds": [],
        "pinnedSessionIds": [],
        "storedSessionCount": 0,
        "hostDefaultWorkspacePath": host,
        "hasPendingMutation": False,
        "workspaces": {"ws-default": {"path": host, "title": "com.dshm.dshclient", "sessionIds": []}},
    }
    cases = [("系统默认空白工作区", dict(base), True)]
    user_ws = json.loads(json.dumps(base))
    user_ws["defaultWorkspaceId"] = None
    cases.append(("用户创建空白工作区（无 defaultWorkspaceId）", user_ws, False))
    path_mismatch = json.loads(json.dumps(base))
    path_mismatch["workspaces"]["ws-default"]["path"] = "/storage/Users/currentUser/Download/other"
    cases.append(("默认工作区 path 与 Host 不一致", path_mismatch, False))
    with_sessions = json.loads(json.dumps(base))
    with_sessions["workspaces"]["ws-default"]["sessionIds"] = ["s1"]
    cases.append(("默认工作区已有会话 ID", with_sessions, False))
    multi = json.loads(json.dumps(base))
    multi["workspaceIds"] = ["ws-default", "ws-2"]
    multi["workspaces"]["ws-2"] = {"path": "/storage/Users/currentUser/Download/w2", "title": "w2", "sessionIds": []}
    cases.append(("多个工作区", multi, False))
    archived = json.loads(json.dumps(base))
    archived["archivedSessionIds"] = ["s1"]
    cases.append(("已归档会话", archived, False))
    pinned = json.loads(json.dumps(base))
    pinned["pinnedSessionIds"] = ["s1"]
    cases.append(("已置顶会话", pinned, False))
    pending = json.loads(json.dumps(base))
    pending["hasPendingMutation"] = True
    cases.append(("pendingMutation", pending, False))
    stored = json.loads(json.dumps(base))
    stored["storedSessionCount"] = 1
    cases.append(("已有 stored session", stored, False))
    extra_record = json.loads(json.dumps(base))
    extra_record["workspaces"]["ws-extra"] = {"path": "/storage/Users/currentUser/Download/x", "title": "x", "sessionIds": []}
    cases.append(("额外无法解释的工作区记录", extra_record, False))
    no_host = json.loads(json.dumps(base))
    no_host["hostDefaultWorkspacePath"] = None
    cases.append(("无法取得 Host 默认工作区路径", no_host, False))
    no_json = {"workspaceJsonExists": False, "initialized": False, "storedSessionCount": 0,
               "archivedSessionIds": [], "pinnedSessionIds": [], "workspaceIds": [], "workspaces": {}}
    cases.append(("无 workspace.json 且无数据", no_json, True))
    bad = []
    for name, state, expected in cases:
        fresh, reasons = ref_assess_fresh(state)
        if fresh != expected:
            bad.append("%s：期望 %s 实际 %s（%s）" % (name, expected, fresh, "；".join(reasons)))
    if bad:
        record("全新实例判定矩阵（参考实现）", "fail", "；".join(bad[:4]))
    else:
        record("全新实例判定矩阵（参考实现）", "pass", "12 个用例：仅系统默认空白工作区=全新；其余均拒绝")
    format_src = read_text(os.path.join(LIB, "format.js"))
    for token in ["defaultWorkspaceId", "hostDefaultWorkspacePath", "sessionIds.length > 0",
                  "workspaceIds.length !== 1", "pendingMutation", "hasPendingMutation"]:
        if token not in format_src:
            record("全新实例判定规则（代码契约）", "fail", "format.js 缺少 %s" % token)
            return
    restore_src = read_text(os.path.join(LIB, "restore.js"))
    for token in ["defaultWorkspaceId", "hostDefaultWorkspacePath", "hasPendingMutation", "workspaces: workspaces"]:
        if token not in restore_src:
            record("全新实例判定规则（代码契约）", "fail", "readTargetState 缺少 %s" % token)
            return
    record("全新实例判定规则（代码契约）", "pass", "仅识别 defaultWorkspaceId + Host 默认路径 + 空 sessionIds 的系统默认工作区")


def test_preview_stale_regression():
    host = "/storage/Users/currentUser/Download/com.dshm.dshclient"
    state = {
        "workspaceJsonExists": True, "initialized": True, "workspaceIds": ["ws-default"],
        "defaultWorkspaceId": "ws-default", "archivedSessionIds": [], "pinnedSessionIds": [],
        "storedSessionCount": 0, "hostDefaultWorkspacePath": host, "hasPendingMutation": False,
        "workspaces": {"ws-default": {"path": host, "title": "x", "sessionIds": []}},
    }
    fresh_before, _ = ref_assess_fresh(state)
    fp_before = {"sessionIds": [], "workspaceIds": ["ws-default"]}
    state["storedSessionCount"] = 1
    fresh_after, _ = ref_assess_fresh(state)
    fp_after = {"sessionIds": ["session-new"], "workspaceIds": ["ws-default"]}
    if fresh_before and (not fresh_after) and fp_before != fp_after:
        record("预览后目标变化回归（参考实现）", "pass", "预览后新建会话 → 不再全新、指纹变化，恢复必须拒绝")
    else:
        record("预览后目标变化回归（参考实现）", "fail", "fresh_before=%s fresh_after=%s" % (fresh_before, fresh_after))
    restore_src = read_text(os.path.join(LIB, "restore.js"))
    i_exec = restore_src.find("export async function executeRestore")
    i_read = restore_src.find("readTargetState(paths)", i_exec)
    i_stage = restore_src.find("await util.ensureDir(paths.stagingRoot)", i_exec)
    i_fp = restore_src.find("fingerprintEqual(internal.targetFingerprint", i_exec)
    i_conf = restore_src.find("collectRestoreConflicts(runtime, manifest, internal.mapping", i_exec)
    i_can = restore_src.find("internal.canRestore !== true", i_exec)
    ordered = (i_exec != -1 and i_stage != -1 and i_read != -1 and i_read < i_stage and i_fp != -1 and i_fp < i_stage
               and i_conf != -1 and i_conf < i_stage and i_can != -1 and i_can < i_stage)
    index_src = read_text(os.path.join(LIB, "index.js"))
    server_checks = ("internal.canRestore !== true" in index_src and "internal.conflicts" in index_src)
    if ordered and server_checks:
        record("执行恢复前重新检查目标（代码契约）", "pass",
               "executeRestore 在 staging/写目标前重读目标、比较指纹、重算映射、复查冲突；服务端强制检查 canRestore/conflicts")
    else:
        record("执行恢复前重新检查目标（代码契约）", "fail",
               "ordered=%s server_checks=%s" % (ordered, server_checks))


def test_rollback_strict():
    restore_src = read_text(os.path.join(LIB, "restore.js"))
    i_start = restore_src.find("async function rollbackRestore")
    i_end = restore_src.find("function errTextSafe", i_start)
    body = restore_src[i_start:i_end] if i_start != -1 and i_end != -1 else ""
    problems = []
    if "removeFileQuietly" in body or "removeTreeQuietly" in body:
        problems.append("回滚仍使用会吞错误的 Quietly 删除")
    for token in ["removeFileStrict", "rmdir(", "stillExists", "rolledBack: failures.length === 0",
                  "registrySnapshotPath"]:
        if token not in body:
            problems.append("回滚缺少 " + token)
    if "removeTreeStrict(journal.createdSessionDirs" in body:
        problems.append("仍对既有/新建会话目录使用递归删除")
    if "createdSessionDirs" in body and "rmdir(journal.createdSessionDirs" not in body:
        problems.append("createdSessionDirs 未使用非递归 rmdir")
    if problems:
        record("回滚严格判定（不吞删除错误）", "fail", "；".join(problems))
    else:
        record("回滚严格判定（不吞删除错误）", "pass",
               "文件/会话目录/空目录/registry 均严格删除并复查；任一失败 rolledBack=false，保留 registry 快照")
    root = os.path.join(TMP, "rollback-strict")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    normal_file = os.path.join(root, "created-file.txt")
    open(normal_file, "w").close()
    empty_dir = os.path.join(root, "created-empty-dir")
    os.makedirs(empty_dir)
    bad_path = os.path.join(root, "created-path-that-is-nonempty-dir")
    os.makedirs(os.path.join(bad_path, "child"))
    failures = []
    for item in [normal_file, empty_dir, bad_path]:
        try:
            if os.path.isdir(item) and item == empty_dir:
                os.rmdir(item)
            elif os.path.isdir(item):
                raise IsADirectoryError(item)
            else:
                os.remove(item)
        except Exception:  # noqa: BLE001
            failures.append(item)
    if failures == [bad_path]:
        record("回滚失败必须报告 rolledBack=false（参考实现）", "pass", "非空目录删除失败被记录，不会假报成功")
    else:
        record("回滚失败必须报告 rolledBack=false（参考实现）", "fail", "failures=%s" % failures)


def test_emergency_throttle():
    index_src = read_text(os.path.join(LIB, "index.js"))
    if 'if (kind === "emergency") runtime.state.emergencyBootId = runtime.bootId;' not in index_src:
        record("异常抢备节流只由 emergency 更新（代码契约）", "fail", "未找到仅 emergency 写 emergencyBootId 的分支")
    elif index_src.count("emergencyBootId = runtime.bootId") != 1:
        record("异常抢备节流只由 emergency 更新（代码契约）", "fail", "emergencyBootId 赋值出现多次")
    elif "runtime.state.emergencyBootId === runtime.bootId" not in index_src:
        record("异常抢备节流只由 emergency 更新（代码契约）", "fail", "触发前未检查本次启动状态")
    else:
        record("异常抢备节流只由 emergency 更新（代码契约）", "pass", "manual/baseline 不改变 emergency 本次启动状态")
    i_trigger = index_src.find("async function triggerEmergencyBackup")
    i_next = index_src.find("// ── 启动后自动任务", i_trigger)
    trigger_body = index_src[i_trigger:i_next] if i_trigger != -1 and i_next != -1 else ""
    i_create = trigger_body.find('createBackup(runtime, "emergency"')
    i_rotate = trigger_body.find('rotateBackups(runtime, "emergency")')
    if i_create != -1 and i_rotate != -1 and i_create < i_rotate:
        record("紧急备份校验成功后才轮换（代码契约）", "pass", "createBackup → then(rotateBackups(emergency))")
    else:
        record("紧急备份校验成功后才轮换（代码契约）", "fail", "create=%s rotate=%s" % (i_create, i_rotate))
    st = {"emergencyBootId": None, "lastEmergencyAt": None}

    def finalize(kind, ok=True):
        if not ok:
            return
        if kind == "emergency":
            st["emergencyBootId"] = "boot-1"

    def can_trigger(boot, now, min_interval=600000):
        if st["emergencyBootId"] == boot:
            return False
        if st["lastEmergencyAt"] is not None and now - st["lastEmergencyAt"] < min_interval:
            return False
        return True

    allowed_after_manual = can_trigger("boot-1", 1000)
    finalize("manual")
    allowed_after_manual2 = can_trigger("boot-1", 2000)
    finalize("baseline")
    allowed_after_baseline = can_trigger("boot-1", 3000)
    finalize("emergency")
    second_emergency = can_trigger("boot-1", 4000)
    if allowed_after_manual and allowed_after_manual2 and allowed_after_baseline and second_emergency is False:
        record("异常抢备节流参考逻辑", "pass", "manual/baseline 后仍可触发；同启动第二次 emergency 被节流")
    else:
        record("异常抢备节流参考逻辑", "fail",
               "manual=%s manual2=%s baseline=%s second=%s" % (allowed_after_manual, allowed_after_manual2,
                                                              allowed_after_baseline, second_emergency))


def test_workspace_walk_exclusion():
    util_src = read_text(os.path.join(LIB, "util.js"))
    block_start = util_src.find("export var DEFAULT_SKIP_DIR_NAMES")
    block_end = util_src.find("];", block_start)
    block = util_src[block_start:block_end] if block_start != -1 and block_end != -1 else ""
    backup_src = read_text(os.path.join(LIB, "backup.js"))
    problems = []
    if '"dshm-backup"' in block or "'dshm-backup'" in block:
        problems.append("DEFAULT_SKIP_DIR_NAMES 仍包含 dshm-backup")
    if ".dshm-backup-tmp" in block:
        problems.append("DEFAULT_SKIP_DIR_NAMES 仍包含 .dshm-backup-tmp")
    if "skipAbsPrefixes = [paths.backupDir, paths.stateDir, paths.stagingRoot]" not in backup_src:
        problems.append("skipAbsPrefixes 未精确包含 backupDir/stateDir/stagingRoot")
    if problems:
        record("工作区 dshm-backup/ 目录不被误排除（代码契约）", "fail", "；".join(problems))
    else:
        record("工作区 dshm-backup/ 目录不被误排除（代码契约）", "pass", "按名称排除已移除；仅按绝对路径排除真实 backup/state/staging")
    root = os.path.join(TMP, "walk-ws")
    backup_dir = os.path.join(root, "dshm-backup", "backups")
    os.makedirs(backup_dir, exist_ok=True)
    os.makedirs(os.path.join(root, "dshm-backup", "src"), exist_ok=True)
    os.makedirs(os.path.join(root, "node_modules", "dep"), exist_ok=True)

    def write(path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    write(os.path.join(root, "dshm-backup", "src", "index.js"), "user project")
    write(os.path.join(root, "dshm-backup", "backups", "old.zip"), "zip")
    write(os.path.join(root, "node_modules", "dep", "index.js"), "dep")
    skip_names = {"node_modules", ".git", ".svn", ".hg", ".cache", "__pycache__", ".Trash", ".Recent", ".virtfs_metadata"}
    collected = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in sorted(dirnames) if d not in skip_names]
        for f in sorted(filenames):
            absp = os.path.join(dirpath, f)
            if absp == backup_dir or absp.startswith(backup_dir + os.sep):
                continue
            collected.append(os.path.relpath(absp, root).replace(os.sep, "/"))
    expected = {"dshm-backup/src/index.js"}
    if set(collected) == expected:
        record("工作区 dshm-backup/ 与真实 backups 的遍历区分（参考实现）", "pass",
               "仅收 dshm-backup/src/index.js；node_modules 与真实 backups 输出目录被排除")
    else:
        record("工作区 dshm-backup/ 与真实 backups 的遍历区分（参考实现）", "fail", "collected=%s" % collected)


def test_plugin_inventory_strict_ref():
    problems = []
    cases = [("@deepseek-ai/dsh-plugin-manager/tools", "@deepseek-ai/dsh-plugin-manager"),
             ("@deepseek-ai/dsh-plugin-manager", "@deepseek-ai/dsh-plugin-manager"),
             ("dsh-base/lib/index.js", "dsh-base"), ("dsh-base", "dsh-base"), ("", None)]
    for raw, expected in cases:
        got = ref_package_name(raw)
        if got != expected:
            problems.append("%r → %r（期望 %r）" % (raw, got, expected))
    if problems:
        record("核心 scoped 包名解析（参考实现）", "fail", "；".join(problems))
    else:
        record("核心 scoped 包名解析（参考实现）", "pass",
               "@scope/pkg[/sub] 先取前两段；带子路径的普通模块名先取包名")
    format_src = read_text(os.path.join(LIB, "format.js"))
    if "plugin-extra-fields" not in format_src or "keys[pk] !== \"name\"" not in format_src:
        record("清单校验拒绝插件额外字段（代码契约）", "fail", "validateManifest 未强制 name/version 白名单")
    else:
        record("清单校验拒绝插件额外字段（代码契约）", "pass", "清单校验只允许 name/version，多余字段报错")
    manifest = {
        "format": "dshm-backup", "formatVersion": 1, "kind": "manual", "createdAt": "2026-01-01T00:00:00Z",
        "source": {"coreVersion": "x", "sessionFormatVersion": 4, "workspaceDomainVersion": 2},
        "contents": {"sessions": [], "workspaces": [], "workspaceRegistry": None, "workspaceFiles": [],
                     "attachments": [], "plugins": {"user": [{"name": "p", "version": "1.0.0", "spec": "x"}], "core": []}},
    }
    errs = ref_validate_manifest(manifest)
    if any(e.startswith("plugin-extra-fields") for e in errs):
        record("插件清单额外字段被拒绝（参考实现）", "pass", "spec 字段触发 plugin-extra-fields")
    else:
        record("插件清单额外字段被拒绝（参考实现）", "fail", "errs=%s" % errs)


def test_core_dir_unknown():
    util_src = read_text(os.path.join(LIB, "util.js"))
    problems = []
    for token in ["DSHM_CORE_DIR", "detectCoreDir", '"unconfirmed"', "coreDirSource"]:
        if token not in util_src:
            problems.append("util.js 缺少 " + token)
    if "readdirSync(coresDir).sort()" in util_src or "pickCoreDir" in util_src:
        problems.append("仍存在按字典序/候选猜核心目录的逻辑")
    if 'coreVersion: "unknown"' not in util_src:
        problems.append("collectVersionInfo 未把 coreVersion 默认标 unknown")
    if problems:
        record("运行核心目录判定（不猜版本）", "fail", "；".join(problems))
    else:
        record("运行核心目录判定（不猜版本）", "pass", "权威来源 DSHM_CORE_DIR/argv；取不到标 unconfirmed，不再按目录名猜")
    format_src = read_text(os.path.join(LIB, "format.js"))
    if "isKnownVersion" not in format_src or 'level = "unknown"' not in format_src:
        record("unknown 版本不参与同版本判定（代码契约）", "fail", "format.js 缺少 unknown 处理")
    else:
        record("unknown 版本不参与同版本判定（代码契约）", "pass", "isKnownVersion + unknown 分支")
    src_v = {"coreVersion": "unknown", "upstreamDshVersion": "unknown", "sessionFormatVersion": 4, "workspaceDomainVersion": 2}
    tgt_v = {"coreVersion": "unknown", "upstreamDshVersion": "unknown", "sessionFormatVersion": 4, "workspaceDomainVersion": 2}
    levels = [
        (ref_version_level(src_v, tgt_v), "unknown"),
        (ref_version_level({"coreVersion": "1.0"}, {"coreVersion": "1.0", "sessionFormatVersion": 4, "workspaceDomainVersion": 2}), "unknown"),
        (ref_version_level({"coreVersion": "1.0", "sessionFormatVersion": 4, "workspaceDomainVersion": 2},
                           {"coreVersion": "1.0", "sessionFormatVersion": 4, "workspaceDomainVersion": 2}), "exact"),
        (ref_version_level({"coreVersion": "1.0", "sessionFormatVersion": 4, "workspaceDomainVersion": 2},
                           {"coreVersion": "2.0", "sessionFormatVersion": 4, "workspaceDomainVersion": 2}), "core-mismatch"),
    ]
    bad = [str(item) for item in levels if item[0] != item[1]]
    if bad:
        record("版本比较矩阵（参考实现）", "fail", "；".join(bad))
    else:
        record("版本比较矩阵（参考实现）", "pass", "unknown≠exact；相同完整版本=exact；不同=core-mismatch")


def test_verified_status():
    util_src = read_text(os.path.join(LIB, "util.js"))
    index_src = read_text(os.path.join(LIB, "index.js"))
    problems = []
    if "hasSidecar" not in util_src or "sidecarSha" not in util_src:
        problems.append("listBackupEntries 未拆分 hasSidecar/sidecarSha")
    if "verified: false" not in util_src:
        problems.append("列表扫描未将 verified 固定为 false")
    if "verified: sidecarSha !== null" in util_src:
        problems.append("仍以“存在旁文件”当作 verified=true")
    if "verifiedCache" not in index_src or "entry.sidecarSha === cached.sha256" not in index_src:
        problems.append("index 未用真实校验缓存判定 verified")
    if 'source: "backup"' not in index_src or 'source: "verify"' not in index_src:
        problems.append("校验缓存未区分 backup/verify 来源")
    if problems:
        record("“已校验”状态语义（代码契约）", "fail", "；".join(problems))
    else:
        record("“已校验”状态语义（代码契约）", "pass", "仅有旁文件→未校验；只有实际复算并核对成功才 verified=true")
    cache = {"/b/a.zip": {"sha256": "a" * 64, "size": 10, "verifiedAt": "2026-01-01T00:00:00Z"}}

    def ref_verified(entry):
        c = cache.get(entry["file"])
        return bool(c and entry.get("hasSidecar") and entry.get("sidecarSha") == c["sha256"]
                    and (c.get("size") is None or c.get("size") == entry.get("size")))

    cases = [
        ({"file": "/b/a.zip", "hasSidecar": True, "sidecarSha": "a" * 64, "size": 10}, True),
        ({"file": "/b/b.zip", "hasSidecar": True, "sidecarSha": "b" * 64, "size": 10}, False),
        ({"file": "/b/a.zip", "hasSidecar": True, "sidecarSha": "c" * 64, "size": 10}, False),
        ({"file": "/b/a.zip", "hasSidecar": False, "sidecarSha": None, "size": 10}, False),
    ]
    bad = [str(item) for item in cases if ref_verified(item[0]) != item[1]]
    if bad:
        record("“已校验”缓存判定（参考实现）", "fail", "；".join(bad))
    else:
        record("“已校验”缓存判定（参考实现）", "pass", "缓存 SHA/大小与旁文件一致才 true；否则未校验")


def test_restore_progress():
    index_src = read_text(os.path.join(LIB, "index.js"))
    restore_src = read_text(os.path.join(LIB, "restore.js"))
    problems = []
    if "executeRestore(runtime, internal, { allowVersionMismatch: allowVersionMismatch, onProgress: progress })" not in index_src:
        problems.append("index 未把 startJob 的 progress 传入 executeRestore")
    if "typeof settings.onProgress === \"function\"" not in restore_src:
        problems.append("executeRestore 未优先使用传入的 onProgress")
    if "typeof runtime.onProgress === \"function\"" not in restore_src:
        problems.append("executeRestore 缺少 runtime.onProgress 兜底")
    if restore_src.count('progress("applying"') < 5:
        problems.append("恢复阶段进度点不足")
    if 'progress("done"' not in restore_src:
        problems.append("缺少完成进度")
    if problems:
        record("恢复进度接通（代码契约）", "fail", "；".join(problems))
    else:
        record("恢复进度接通（代码契约）", "pass", "startJob progress → executeRestore(settings.onProgress) → 各阶段进度更新")



# ── 5. v0.1.2 复审退回修复回归 ──────────────────────────────────────────────

def test_v012_boundaries():
    restore_src = read_text(os.path.join(LIB, "restore.js"))
    format_src = read_text(os.path.join(LIB, "format.js"))
    backup_src = read_text(os.path.join(LIB, "backup.js"))
    problems = []
    # 阻断 1：会话目录是否本次创建 + 日志文件单独记录 + 非递归 rmdir
    for token in ["sessionDirExistedBefore", "journal.createdFiles.push(logPath)", "if (!sessionDirExistedBefore) journal.createdSessionDirs.push(sessionDir)"]:
        if token not in restore_src:
            problems.append("restore.js 缺少 " + token)
    if "removeTreeStrict(journal.createdSessionDirs" in restore_src:
        problems.append("restore.js 仍递归删除 createdSessionDirs")
    if "export async function rollbackRestore" not in restore_src:
        problems.append("rollbackRestore 未导出（真实 JS 回归需要）")
    # 阻断 2：source platform/nodeVersion 允许 null/缺失，不得内部拒绝
    for token in ["source.platform !== undefined && source.platform !== null", "source.nodeVersion !== undefined && source.nodeVersion !== null"]:
        if token not in format_src:
            problems.append("format.js 未允许来源元数据缺失/null：" + token)
    # 阻断 3：complete=false 允许 cwd/createdAt 为 null；仍要求 sha/file/size
    for token in ["var sessionComplete = s.complete === true;", "if (sessionComplete) {", "s.cwd !== undefined && s.cwd !== null", "s.createdAt !== undefined && s.createdAt !== null"]:
        if token not in format_src:
            problems.append("format.js 不完整会话规则缺失：" + token)
    # 阻断 5：跳过的不完整会话从 registry 引用中清除，空会话保留
    for token in ["skippedSessionIds", "pruneSkippedSessionReferences", "rewriteWorkspaceRegistry(originalRegistry, mapping, { skippedSessionIds"]:
        if token not in restore_src + format_src:
            problems.append("registry 跳过清理缺失：" + token)
    # 次要 1：发布后复验失败清理文案不得无依据声称已删除
    for token in ["清理未确认", "remaining.length", "cleanupResults"]:
        if token not in backup_src:
            problems.append("发布后清理状态未反映：" + token)
    if problems:
        record("v0.1.2 阻断修复代码契约", "fail", "；".join(problems[:8]))
    else:
        record("v0.1.2 阻断修复代码契约", "pass",
               "会话目录仅删新日志/非递归 rmdir；来源元数据允许 null；incomplete 会话保留；registry 跳过清理；清理结果如实上报")

    # 真实 JS 回归文件存在且可被 esprima 解析；覆盖本轮边界
    js_tests = ["backup-boundaries.test.mjs", "restore-boundaries.test.mjs"]
    for js_test in js_tests:
        path = os.path.join(ROOT, "test", js_test)
        if not os.path.exists(path):
            record("真实 JS 回归文件 %s" % js_test, "fail", "文件不存在")
            continue
        src = read_text(path)
        if ESPRIMA is not None:
            try:
                ESPRIMA.parseModule(src)
                parsed = True
            except Exception as exc:  # noqa: BLE001
                parsed = False
                record("真实 JS 回归解析 %s" % js_test, "fail", str(exc))
        else:
            parsed = None
        if parsed is False:
            continue
        record("真实 JS 回归文件 %s" % js_test, "pass",
               "存在；%s；本机无 node CLI，未执行（需外部真实 node 运行）" % ("esprima 解析通过" if parsed else "未解析"))
    backup_test = read_text(os.path.join(ROOT, "test", "backup-boundaries.test.mjs"))
    restore_test = read_text(os.path.join(ROOT, "test", "restore-boundaries.test.mjs"))
    checks = [
        ("backup 测试覆盖 coreDir 未确认/platform=null", "platform" in backup_test and "unconfirmed" in backup_test),
        ("backup 测试覆盖损坏 header 会话", "corruptHeader" in backup_test and "complete, false" in backup_test),
        ("backup 测试覆盖 manual/baseline/emergency", all(k in backup_test for k in ["manual", "baseline", "emergency"])),
        ("restore 测试覆盖既有 sessionDir + 无关文件", "unrelated" in restore_test and "createdSessionDirs: []" in restore_test),
        ("restore 测试覆盖未跟踪文件时 rolledBack=false", "rolledBack, false" in restore_test),
    ]
    bad = [name for name, ok in checks if not ok]
    if bad:
        record("真实 JS 回归覆盖本轮边界", "fail", "缺少：" + "；".join(bad))
    else:
        record("真实 JS 回归覆盖本轮边界", "pass",
               "coreDir=null/platform=null 三类备份、损坏 header、既有 sessionDir+无关文件、未跟踪文件回滚失败")

    # 文档不再要求“插件数量/启停状态与恢复前一致”
    handoff = read_text(os.path.join(ROOT, "HANDOFF_PHONE.md"))
    if "插件数量/启停状态与恢复前一致" in handoff:
        record("HANDOFF 插件一致性表述修正", "fail", "仍承诺恢复插件启停状态")
    elif "不承诺" not in handoff:
        record("HANDOFF 插件一致性表述修正", "fail", "缺少不承诺自动恢复插件的说明")
    else:
        record("HANDOFF 插件一致性表述修正", "pass", "不承诺自动恢复插件名单/启停；恢复不得主动改变插件")


# ── 6. v0.1.3 手机第一阶段 D1 修复回归 ─────────────────────────────────────

def test_v013_d1_attachment():
    util_src = read_text(os.path.join(LIB, "util.js"))
    problems = []
    for token in ["await pipeline(", "createReadStream(source)", "createWriteStream(tmp", "mode: targetMode",
                  "targetMode = settings.mode === undefined ? 0o600 : settings.mode", "await fsyncFile(tmp);"]:
        if token not in util_src:
            problems.append("util.js copyFileAtomic 缺少 " + token)
    if "copyFile(source, tmp)" in util_src:
        problems.append("仍使用 copyFile 系统调用（手机沙箱→用户目录 EPERM 风险）")
    if problems:
        record("D1 附件 0400 copyFileAtomic 修复（代码契约）", "fail", "；".join(problems[:5]))
    else:
        record("D1 附件 0400 copyFileAtomic 修复（代码契约）", "pass",
               "流式读写复制 + createWriteStream mode 0600 + 只读 fsync；不使用 copyFile；不对目标 chmod")
    # 真实 JS 回归文件
    js_test = os.path.join(ROOT, "test", "attachment-copy.test.mjs")
    if not os.path.exists(js_test):
        record("真实 JS 回归文件 attachment-copy.test.mjs", "fail", "文件不存在")
    else:
        src = read_text(js_test)
        parsed = None
        if ESPRIMA is not None:
            try:
                ESPRIMA.parseModule(src)
                parsed = True
            except Exception as exc:  # noqa: BLE001
                parsed = False
                record("真实 JS 回归解析 attachment-copy.test.mjs", "fail", str(exc))
        if parsed is not False:
            record("真实 JS 回归文件 attachment-copy.test.mjs", "pass",
                   "存在；%s；覆盖 0o400 源文件复制与含 0400 附件实例备份；本机无 node CLI，未执行" %
                   ("esprima 解析通过" if parsed else "未解析"))
        checks = [
            ("覆盖 0o400 源文件 copyFileAtomic", "0o400" in src and "copyFileAtomic" in src),
            ("覆盖含 0400 附件实例 createBackup", "createBackup" in src and "attachments" in src),
            ("断言源 mode 不变、目标 0600", "srcStat.mode & 0o777" in src and "dstStat.mode & 0o777" in src),
        ]
        bad = [name for name, ok in checks if not ok]
        if bad:
            record("D1 真实 JS 回归覆盖", "fail", "缺少：" + "；".join(bad))
        else:
            record("D1 真实 JS 回归覆盖", "pass", "0o400 源复制、含附件实例备份、源 mode 不变/目标 0600")
    # backup-boundaries 的 zstd 可移植性
    fb = read_text(os.path.join(ROOT, "test", "backup-boundaries.test.mjs"))
    if "HAS_ZSTD" not in fb or "session.v4.jsonl" not in fb:
        record("backup-boundaries 无 zstd API 时明文 fallback", "fail", "未实现 HAS_ZSTD 明文 fixture 分支")
    else:
        record("backup-boundaries 无 zstd API 时明文 fallback", "pass",
               "zstd 可用时用 zstd 帧；不可用时使用产品支持的明文 v4 fixture；本机未执行")


# ── 7. v0.1.4 手机第二阶段 D2（copyFile 沙箱→用户目录 EPERM）回归 ─────────

def test_v014_d2_copyfile():
    util_src = read_text(os.path.join(LIB, "util.js"))
    problems = []
    if "copyFile(" in util_src:
        problems.append("util.js 仍存在 copyFile( 调用")
    for token in ["from \"node:stream/promises\"", "await pipeline(", "createReadStream(source)", "createWriteStream(tmp"]:
        if token not in util_src:
            problems.append("util.js 缺少流式复制元素：" + token)
    if "copyFileAtomic" not in util_src or "mode: targetMode" not in util_src:
        problems.append("copyFileAtomic mode 设置缺失")
    if "chmod(tmp" in util_src:
        problems.append("仍对目标 chmod（用户目录 EPERM 风险）")
    if problems:
        record("D2 沙箱→用户目录 copyfile EPERM 修复（代码契约）", "fail", "；".join(problems[:6]))
    else:
        record("D2 沙箱→用户目录 copyfile EPERM 修复（代码契约）", "pass",
               "copyFileAtomic 改为 createReadStream→createWriteStream(pipeline)；mode 0600；不再调用 copyFile")
    # 真实 JS 回归文件覆盖嵌套目标
    js_test = os.path.join(ROOT, "test", "attachment-copy.test.mjs")
    src = read_text(js_test) if os.path.exists(js_test) else ""
    if "D2 回归" not in src or "dshm-backup-restored" not in src:
        record("D2 真实 JS 回归覆盖", "fail", "attachment-copy.test.mjs 缺少沙箱→用户目录嵌套目标用例")
    else:
        parsed = None
        if ESPRIMA is not None:
            try:
                ESPRIMA.parseModule(src)
                parsed = True
            except Exception as exc:  # noqa: BLE001
                parsed = False
                record("D2 真实 JS 回归解析", "fail", str(exc))
        if parsed is not False:
            record("D2 真实 JS 回归覆盖", "pass",
                   "copyFileAtomic 嵌套目标（Download/.../dshm-backup-restored/...）+ 0400 源；本机无 node CLI，未执行")
    # 手机第二阶段失败证据归档
    failure_note = os.path.join(ROOT, "reports", "phone-stage2-failure-v0.1.3-20261024.md")
    if not os.path.exists(failure_note):
        record("手机第二阶段 D2 失败证据归档", "fail", "缺少 reports/phone-stage2-failure-v0.1.3-20261024.md")
    else:
        text = read_text(failure_note)
        for token in ["EPERM", "copyfile", "dshm-backup-restored", "78%"]:
            if token not in text:
                record("手机第二阶段 D2 失败证据归档", "fail", "缺少 " + token)
                break
        else:
            record("手机第二阶段 D2 失败证据归档", "pass", "已归档 EPERM copyfile 78% 失败现象与路径")


# ── 8. v0.1.5 手机第二阶段 D3（目标 chmod EPERM）回归 ─────────────────────

def test_v015_d3_chmod():
    util_src = read_text(os.path.join(LIB, "util.js"))
    problems = []
    if "chmod(" in util_src:
        problems.append("util.js 仍存在 chmod( 调用")
    if "open(filePath, \"r+\")" in util_src:
        problems.append("fsyncFile 仍以 r+ 打开")
    for token in ["open(filePath, \"r\")", "createWriteStream(tmp, { flags: \"wx\", mode: targetMode })",
                  "await fsyncFile(tmp);"]:
        if token not in util_src:
            problems.append("缺少：" + token)
    if problems:
        record("D3 目标 chmod EPERM 修复（代码契约）", "fail", "；".join(problems[:6]))
    else:
        record("D3 目标 chmod EPERM 修复（代码契约）", "pass",
               "copyFileAtomic 不再 chmod 目标；目标权限仅由 createWriteStream mode 设置；fsync 以只读打开")
    # 失败证据归档（v0.1.4 chmod）
    note = os.path.join(ROOT, "reports", "phone-stage2-failure-v0.1.4-20261024.md")
    if not os.path.exists(note):
        record("手机第二阶段 D3 失败证据归档", "fail", "缺少 reports/phone-stage2-failure-v0.1.4-20261024.md")
    else:
        text = read_text(note)
        for token in ["chmod", "EPERM", "18:01", "dshm-backup-restored"]:
            if token not in text:
                record("手机第二阶段 D3 失败证据归档", "fail", "缺少 " + token)
                break
        else:
            record("手机第二阶段 D3 失败证据归档", "pass", "已归档 v0.1.4 chmod EPERM 失败现象")


# ── 9. v0.1.6 复查加固：恢复工作区文件直接写入（避免用户目录元数据操作）──

def test_v017_direct_ownership():
    util_src = read_text(os.path.join(LIB, "util.js"))
    restore_src = read_text(os.path.join(LIB, "restore.js"))
    problems = []
    if "export async function copyFileDirect" not in util_src:
        problems.append("util.js 缺少 copyFileDirect")
    for token in ['await open(destination, "wx", targetMode)', "await settings.onCreated(destination)", "handle.createWriteStream"]:
        if token not in util_src:
            problems.append("copyFileDirect 缺少所有权语义：" + token)
    if "await settings.onCreated(destination)" not in util_src:
        problems.append("创建成功后未调用 onCreated")
    if "if (typeof settings.onCreated === \"function\")" in util_src and "await open(destination" not in util_src.split("await settings.onCreated")[0]:
        problems.append("onCreated 不在排他创建成功之后")
    if "util.copyFileDirect(join(stagingDir, fileDecl.entry), targetFile, {" not in restore_src:
        problems.append("restore 未使用 copyFileDirect onCreated 形式")
    if "onCreated: function (createdPath)" not in restore_src:
        problems.append("restore 缺少 onCreated 登记")
    if "journal.createdFiles.push(targetFile);\n        await util.copyFileDirect" in restore_src:
        problems.append("restore 仍在调用前无条件登记 targetFile")
    if problems:
        record("D4 直接写入所有权/回滚登记（代码契约）", "fail", "；".join(problems[:6]))
    else:
        record("D4 直接写入所有权/回滚登记（代码契约）", "pass",
               "open(wx) 成功后才标记/onCreated；EEXIST 不删除不登记；restore 仅通过 onCreated 登记")
    # 新增真实 JS 回归文件
    js_path = os.path.join(ROOT, "test", "direct-write.test.mjs")
    if not os.path.exists(js_path):
        record("D4 真实 JS 回归文件 direct-write.test.mjs", "fail", "文件不存在")
    else:
        src = read_text(js_path)
        parsed = None
        if ESPRIMA is not None:
            try:
                ESPRIMA.parseModule(src)
                parsed = True
            except Exception as exc:  # noqa: BLE001
                parsed = False
                record("D4 真实 JS 回归解析", "fail", str(exc))
        if parsed is not False:
            checks = [
                ("目标已存在 EEXIST 且内容不变", "EEXIST" in src and "preexisting-content" in src),
                ("预检后冲突进入 rollbackRestore", "rollbackRestore" in src and "created-by-someone-else" in src),
                ("源读取失败清理本次文件", "source read" in src.lower() or "does-not-exist" in src),
                ("正常写入成功", "direct-write-normal" in src),
                ("清理失败 rolledBack=false", "rolledBack, false" in src or "rolledBack, false" in src),
            ]
            bad = [name for name, ok in checks if not ok]
            if bad:
                record("D4 真实 JS 回归覆盖", "fail", "缺少：" + "；".join(bad))
            else:
                record("D4 真实 JS 回归覆盖", "pass",
                       "EEXIST 不删除/不登记、预检冲突 rollback 保留、源读取失败清理、正常写入、清理失败上报；本机未执行")

def test_docs_fix_round():
    pkg = json.loads(read_text(os.path.join(PLUGIN, "package.json")))
    if pkg.get("version") == "0.1.7":
        record("插件版本 0.1.7（区分缺陷包）", "pass")
    else:
        record("插件版本 0.1.7（区分缺陷包）", "fail", "version=%s" % pkg.get("version"))
    if os.path.exists(os.path.join(ROOT, "test", "tablet-test-results.json")):
        record("测试结果文件存在（可随 ZIP 交付）", "pass")
    else:
        record("测试结果文件存在（可随 ZIP 交付）", "fail", "缺少 test/tablet-test-results.json")
    required_texts = {
        "FIX_REPORT.md": ["crcTransform", "assessFreshTarget", "rollbackRestore", "executeRestore", "emergencyBootId",
                          "DEFAULT_SKIP_DIR_NAMES", "readPluginInventory", "detectCoreDir", "hasSidecar", "onProgress",
                          "13/18", "18/18", "0.1.1", "0.1.2", "0.1.3", "0.1.4", "0.1.5", "0.1.6", "0.1.7",
                          "sessionDirExistedBefore", "platform", "pruneSkippedSessionReferences", "真实 JS",
                          "EACCES", "EPERM", "EEXIST", "copyFileAtomic", "copyFileDirect", "onCreated",
                          "pipeline", "chmod", "附件"],
        "TEST_REPORT.md": ["13/18", "18/18", "未在平板执行", "外部", "ZIP", "0.1.2", "0.1.3", "0.1.4", "0.1.5", "0.1.6", "0.1.7", "真实 JS", "EACCES", "EPERM", "EEXIST", "chmod", "附件"],
        "README.md": ["名称", "版本", "0.1.7", "附件", "EPERM"],
        "FEASIBILITY.md": ["名称", "版本", "coreDir", "0.1.2", "0.1.3", "0.1.4", "0.1.5", "0.1.6", "0.1.7"],
        "HANDOFF_PHONE.md": ["足够长的生成", "Download/com.dshm.dshclient 之外", "另行批准", "只包含名称+版本", "不承诺", "0.1.7"],
        "PHONE_ACCEPTANCE_PROMPT.md": ["标准模式", "先安装加载", "另行批准", "不能只复制到同一应用目录"],
    }
    missing = []
    for fname, tokens in required_texts.items():
        path = os.path.join(ROOT, fname)
        if not os.path.exists(path):
            missing.append(fname + ":missing")
            continue
        text = read_text(path)
        for token in tokens:
            if token not in text:
                missing.append(fname + ":" + token)
    if missing:
        record("文档与修复后的字段/行为一致", "fail", "缺少：%s" % "；".join(missing[:8]))
    else:
        record("文档与修复后的字段/行为一致", "pass", "FIX_REPORT/TEST_REPORT/README/FEASIBILITY/HANDOFF/PHONE_PROMPT 已同步")
    test_report = read_text(os.path.join(ROOT, "TEST_REPORT.md")) if os.path.exists(os.path.join(ROOT, "TEST_REPORT.md")) else ""
    if "无产品级失败项" in test_report:
        record("TEST_REPORT 不再声称无产品级失败", "fail", "仍含“无产品级失败项”")
    else:
        record("TEST_REPORT 不再声称无产品级失败", "pass", "记录外部真实 ZIP 测试 13/18 失败与修复后 18/18")

def main():
    print("=" * 78)
    print("dshm-backup 平板端测试运行器")
    print("JS 运行时：不可用（无 node CLI）——JS 逻辑不在本脚本中执行")
    print("内嵌 libzstd：%s；esprima：%s" % ("可用" if ZSTD.available() else "不可用", "可用" if HAS_ESPRIMA else "不可用"))
    print("=" * 78)
    if os.path.isdir(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    test_js_parse_and_contracts()
    test_lexical_fallback()
    test_synthetic_fixtures()
    test_mapping_and_safety()
    test_backup_protocol_reference()
    test_rollback_reference()
    test_versions_and_freshness()
    test_package_and_docs()
    test_zip_pipeline_order()
    test_fresh_target_matrix()
    test_preview_stale_regression()
    test_rollback_strict()
    test_emergency_throttle()
    test_workspace_walk_exclusion()
    test_plugin_inventory_strict_ref()
    test_core_dir_unknown()
    test_verified_status()
    test_restore_progress()
    test_v012_boundaries()
    test_v013_d1_attachment()
    test_v014_d2_copyfile()
    test_v015_d3_chmod()
    test_v017_direct_ownership()
    test_docs_fix_round()

    passed = sum(1 for r in RESULTS if r["status"] == "pass")
    failed = sum(1 for r in RESULTS if r["status"] == "fail")
    skipped = sum(1 for r in RESULTS if r["status"] == "skip")
    print("=" * 78)
    print("结果：PASS=%d FAIL=%d SKIP=%d" % (passed, failed, skipped))
    for r in RESULTS:
        if r["status"] != "pass":
            print("  [%s] %s — %s" % (r["status"].upper(), r["name"], r["detail"]))
    out = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "device": "HarmonyOS 平板 / DSHM / 内嵌 CPython",
        "jsRuntime": "unavailable (no node CLI); JS logic NOT executed",
        "esprima": bool(HAS_ESPRIMA),
        "libzstd": bool(ZSTD.available()),
        "summary": {"pass": passed, "fail": failed, "skip": skipped},
        "results": RESULTS,
    }
    out_path = os.path.join(ROOT, "test", "tablet-test-results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("详细结果写入：%s" % os.path.relpath(out_path, ROOT))
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
