#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
cursor_mcp_toolctl.py
=====================
Cursor 内置 MCP provider 工具禁用控制器。

用途：修复 "openai: status=400 Invalid schema for function '<server>-<tool>':
      schema must be a JSON Schema object" 类错误。

原理：Cursor 的内置 MCP 服务器（如 cursor-app-control）在模块加载时静态注册工具，
      工具 schema 由 zod 定义。其中 union / discriminatedUnion / record / refine /
      transform 派生的 schema 转 JSON Schema 后不被 OpenAI 接受，会被 API 400 拒绝。
      Cursor 自带的工具级禁用机制（mcpDisabledTools）会在下发前过滤，
      被禁用的工具不会进入 tools 数组 -> 不合法 schema 不再下发 -> 400 消失。

子命令：
  scan     扫描 Cursor 安装包 bundle，判定各内置工具的 schema 风险等级
  status   只读读取当前 mcpDisabledTools（安全，可在 Cursor 运行时执行）
  apply    备份 + 合并写入 + 回读验证（要求 Cursor 已完全退出）
  restore  清空禁用清单，或从指定备份文件恢复

所有输出同时写入 --out 指定的文件（默认 ./.cursor_toolctl_out.txt），
规避部分 Windows 环境下 stdout 被吞的问题。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

# ---------------------------------------------------------------- 常量

DEF_DB = os.path.expandvars(r"%APPDATA%\Cursor\User\globalStorage\state.vscdb")

DEF_KEY = ("src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl"
           ".persistentStorage.applicationUser")

DEF_SERVER_ID = "cursor-app-control"

# cursor-app-control 已观测到的全部内置工具名（用于全禁兜底）
KNOWN_TOOLS = ["move_agent_to_root", "move_agent_to_cloned_root",
               "create_project", "open_resource", "open_automation",
               "get_cursor_user_state", "cursor_dialog", "rename_chat",
               "show_confetti"]

DEF_ANCHORS = ["move_agent_to_root", "cursor-app-control", "appControlMcp"]

# 高风险：实测会触发 400 的 zod 构造
RISK_HIGH = ("union", "discriminatedUnion", "record", "refine",
             "superRefine", "transform", "pipe", "lazy")
# 中风险：OpenAI strict 模式下可能报错，但默认不禁用
RISK_MID = ("optional", "nullable", "default")

CURSOR_OUT_CANDIDATES = [
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Cursor\resources\app\out"),
    os.path.expandvars(r"%LOCALAPPDATA%\cursor\resources\app\out"),
    os.path.expandvars(r"%PROGRAMFILES%\Cursor\resources\app\out"),
    "/Applications/Cursor.app/Contents/Resources/app/out",
    os.path.expanduser("~/.local/share/cursor/resources/app/out"),
]


def log(lines, out_path):
    text = "\n".join(str(x) for x in lines)
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:                                    # pragma: no cover
        sys.stderr.write("write out failed: %s\n" % e)
    # 控制台只回显 ASCII 安全文本，避免 GBK 控制台炸掉
    try:
        sys.stdout.write(text.encode("ascii", "backslashreplace").decode("ascii") + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 基础工具

def cursor_processes():
    """返回疑似 Cursor 进程的描述行列表。"""
    try:
        if os.name == "nt":
            r = subprocess.run(["tasklist", "/FO", "CSV"], capture_output=True,
                               text=True, encoding="gbk", errors="ignore")
            lines = [l for l in r.stdout.splitlines()
                     if re.search(r'"(Cursor|cursor)[^"]*\.exe"', l)]
        else:
            r = subprocess.run(["ps", "-ax"], capture_output=True,
                               text=True, errors="ignore")
            lines = [l for l in r.stdout.splitlines()
                     if re.search(r"Cursor", l) and "grep" not in l]
    except Exception:
        lines = []
    return [l for l in lines if "cursor_mcp_toolctl" not in l]


def open_db(db, rw=False):
    uri = "file:%s?mode=%s" % (db.replace("\\", "/"), "rw" if rw else "ro")
    return sqlite3.connect(uri, uri=True)


def resolve_key(con):
    """精确定位 applicationUser 键；失败则模糊查找。"""
    cur = con.cursor()
    cur.execute("SELECT key FROM ItemTable WHERE key=?", (DEF_KEY,))
    if cur.fetchone():
        return DEF_KEY
    cur.execute("SELECT key FROM ItemTable WHERE key LIKE '%applicationUser%'")
    for (k,) in cur.fetchall():
        return k
    cur.execute("SELECT key, value FROM ItemTable WHERE value LIKE '%mcpDisabledTools%'")
    for k, _v in cur.fetchall():
        return k
    return None


def read_disabled(con, key):
    cur = con.cursor()
    cur.execute("SELECT value FROM ItemTable WHERE key=?", (key,))
    row = cur.fetchone()
    if row is None:
        return None, None
    raw = row[0]
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", "replace")
    data = json.loads(raw)
    return list(data.get("mcpDisabledTools") or []), data


def backup_db(db):
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = db + ".bak_" + ts
    n = 0
    for ext in ("", "-wal", "-shm"):
        src = db + ext
        if os.path.isfile(src):
            shutil.copy2(src, bak + ext)
            n += 1
    return bak, n


# ---------------------------------------------------------------- 代码扫描

def locate_out_dir(cli_dir=None):
    if cli_dir:
        return cli_dir if os.path.isdir(cli_dir) else None
    for c in CURSOR_OUT_CANDIDATES:
        if os.path.isdir(c):
            return c
    return None


def bundle_files(out_dir):
    files = []
    for pat in (os.path.join(out_dir, "vs", "workbench", "*.js"),
                os.path.join(out_dir, "vs", "workbench", "**", "*.js")):
        files.extend(glob.glob(pat, recursive=True))
    return sorted(set(files),
                  key=lambda p: -os.path.getsize(p) if os.path.exists(p) else 0)


def bracket_slice(text, start):
    """从 start（指向左括号）开始做括号配对，返回包含括号的片段。"""
    if start >= len(text) or text[start] != "(":
        return ""
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return ""


def split_top(s):
    """按顶层逗号切分参数列表（忽略字符串与嵌套括号内的逗号）。"""
    parts, cur, depth, instr = [], [], 0, None
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if instr:
            cur.append(c)
            if c == "\\" and i + 1 < n:
                cur.append(s[i + 1])
                i += 2
                continue
            if c == instr:
                instr = None
        else:
            if c in "'\"`":
                instr = c
                cur.append(c)
            elif c in "([{":
                depth += 1
                cur.append(c)
            elif c in ")]}":
                depth -= 1
                cur.append(c)
            elif c == "," and depth == 0:
                parts.append("".join(cur))
                cur = []
            else:
                cur.append(c)
        i += 1
    parts.append("".join(cur))
    return [p.strip() for p in parts]


# 工具名常量声明块：var A="tool_a",B="tool_b",C="tool_c";
PAT_NAME_BLOCK = re.compile(
    r'var\s+([A-Za-z_$][\w$]{0,6})="([a-z][a-z0-9_]{2,60})"'
    r'((?:\s*,\s*[A-Za-z_$][\w$]{0,6}="[a-z][a-z0-9_]{2,60}"){2,})')

# 单个工具名常量（兜底）
PAT_NAME_ONE = re.compile(r'([A-Za-z_$][\w$]{0,6})\s*=\s*"([a-z][a-z0-9_]{2,60})"')

PAT_SCHEMA_DEF = re.compile(
    r'([A-Za-z_$][\w$]{0,6})\s*=\s*([A-Za-z_$][\w$]{0,6})\.(' +
    "|".join(RISK_HIGH + RISK_MID) + r')\s*\(')

PAT_SCHEMA_USE = re.compile(
    r'([A-Za-z_$][\w$]{0,6})\s*=\s*([A-Za-z_$][\w$]{0,6})\.(' +
    "|".join(("object", "string", "number", "boolean", "array")) + r')\s*\(')


def _iter_limited(pattern, text, limit):
    """限量 finditer，避免在大 bundle（40MB+）上全量匹配导致超时。"""
    n = 0
    for m in pattern.finditer(text):
        yield m
        n += 1
        if n >= limit:
            break


def _anchor_window(text, anchors, server_id):
    """围绕第一个命中的锚点取窗口；找不到则返回全文。"""
    for a in list(anchors) + [server_id]:
        i = text.find(a)
        if i >= 0:
            s = max(0, i - 100000)
            e = min(len(text), i + 2000000)
            return text[s:e], a
    return text, None


def collect_tool_consts(region, limit=60):
    """返回 {常量名: 工具名}"""
    consts = {}
    for m in _iter_limited(PAT_NAME_BLOCK, region, limit):
        pairs = [(m.group(1), m.group(2))]
        for c, nm in re.findall(r'([A-Za-z_$][\w$]{0,6})="([a-z][a-z0-9_]{2,60})"',
                                m.group(3)):
            pairs.append((c, nm))
        if len(pairs) >= 3:
            for c, nm in pairs:
                consts[c] = nm
    return consts


def collect_schema_risk(text, limit=400):
    """返回 {schema 变量: {'risk':..., 'evidence':...}}"""
    risk = {}
    for m in _iter_limited(PAT_SCHEMA_DEF, text, limit):
        var, zns, ctor = m.group(1), m.group(2), m.group(3)
        if len(var) < 2:          # 单字母压缩变量噪声过大，跳过
            continue
        frag = bracket_slice(text, m.end() - 1) or text[m.end():m.end() + 4000]
        level = "high" if ctor in RISK_HIGH else "mid"
        ev = "%s.%s(...)" % (zns, ctor)
        for extra in ("refine", "superRefine", "transform", "pipe", "lazy"):
            if ".%s(" % extra in frag:
                level = "high"
                ev += " + .%s()" % extra
        prev = risk.get(var)
        if prev is None or (level == "high" and prev["risk"] != "high"):
            risk[var] = {"risk": level, "evidence": ev}
    return risk


def find_registration(text, const, schema_risk):
    """定位以 const 作为第一个参数的注册调用，返回 (schema_var, risk, evidence)。"""
    pat = re.compile(r'([A-Za-z_$][\w$]{0,6})\s*=\s*([A-Za-z_$][\w$]{0,6})\(\s*'
                     + re.escape(const) + r'\s*,')
    last_safe = None
    for m in _iter_limited(pat, text, 40):
        call = bracket_slice(text, m.end(2))     # m.end(2) 即函数名后的 '('
        if not call:
            continue
        args = split_top(call[1:-1])
        if len(args) < 3:
            continue
        svar = args[2].strip()
        if re.fullmatch(r"[A-Za-z_$][\w$]{0,6}", svar) and svar in schema_risk:
            return svar, schema_risk[svar]["risk"], schema_risk[svar]["evidence"], "high"
        if re.fullmatch(r"[A-Za-z_$][\w$]{0,6}", svar):
            last_safe = svar     # 命中注册但 schema 未列入风险表
    if last_safe:
        return last_safe, "low", "schema var not in risk table (likely plain object)", "high"
    # 降级：该常量出现位置附近 900 字符内是否引用了已知的高风险 schema 变量
    for m in _iter_limited(re.compile(re.escape(const)), text, 40):
        seg = text[m.end():m.end() + 900]
        for var, info in schema_risk.items():
            if info["risk"] != "high":
                continue
            if re.search(r'\b%s\b' % re.escape(var), seg):
                return var, info["risk"], info["evidence"] + " (proximity)", "low"
    return None, None, None, "none"


def scan_file(path, anchors, server_id):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception as e:
        return {"file": path, "error": str(e), "tools": []}

    if not any(a in text for a in list(anchors) + [server_id]):
        return {"file": os.path.basename(path), "skipped": "no anchor", "tools": []}

    region, anchor = _anchor_window(text, anchors, server_id)
    consts = collect_tool_consts(region)
    if not consts and region is not text:
        consts = collect_tool_consts(text, limit=200)
    if not consts:
        known = ("move_agent_to_root", "move_agent_to_cloned_root", "create_project",
                 "open_resource", "open_automation", "get_cursor_user_state",
                 "cursor_dialog", "rename_chat", "show_confetti")
        for c, nm in _iter_limited(PAT_NAME_ONE, region, 4000):
            if nm in known:
                consts[c] = nm

    schema_risk = collect_schema_risk(text)

    tools = []
    for c, name in consts.items():
        svar, risk, ev, conf = find_registration(text, c, schema_risk)
        tools.append({"tool": name, "const": c,
                      "schema_var": svar or "-",
                      "risk": risk or "unknown",
                      "evidence": ev or "-",
                      "confidence": conf})
    return {"file": os.path.basename(path), "anchor": anchor,
            "n_consts": len(consts), "n_risk_schema": len(schema_risk),
            "tools": tools}


def cmd_scan(args, out):
    L = []
    out_dir = locate_out_dir(args.cursor_dir)
    if not out_dir:
        L.append("[X] 未定位到 Cursor 安装目录。请用 --cursor-dir 指定")
        L.append("    （形如 <安装根>/resources/app/out；报错/崩溃日志的 vscode-file:// 路径中含该目录）")
        log(L, args.out)
        return 2
    L.append("[scan] out dir  : %s" % out_dir)
    L.append("[scan] server id: %s" % args.server_id)

    files = bundle_files(out_dir)
    if not files:
        L.append("[X] 未找到 vs/workbench/*.js")
        log(L, args.out)
        return 2

    order_r = {"high": 4, "mid": 3, "unknown": 2, "low": 1}
    order_c = {"high": 2, "low": 1, "none": 0}
    merged = {}
    for p in files:
        r = scan_file(p, DEF_ANCHORS, args.server_id)
        if r.get("skipped") or r.get("error") or not r.get("tools"):
            continue
        L.append("  - %s: 工具常量=%d, 风险schema=%d, 工具=%d"
                 % (r["file"], r["n_consts"], r["n_risk_schema"], len(r["tools"])))
        for t in r["tools"]:
            prev = merged.get(t["tool"])
            if prev is None:
                merged[t["tool"]] = t
            else:
                a = (order_r.get(t["risk"], 0), order_c.get(t["confidence"], 0))
                b = (order_r.get(prev["risk"], 0), order_c.get(prev["confidence"], 0))
                if a > b:
                    merged[t["tool"]] = t

    sure = sorted(t["tool"] for t in merged.values()
                  if t["risk"] == "high" and t["confidence"] == "high")
    maybe = sorted(t["tool"] for t in merged.values()
                   if t["risk"] == "high" and t["confidence"] != "high")
    rest = sorted(t["tool"] for t in merged.values() if t["risk"] != "high")

    L.append("")
    L.append("== [建议禁用] 高置信：精确命中注册调用且 schema 为 union/discriminatedUnion/record/refine ==")
    for t in sure:
        L.append("  [DISABLE] %-32s schema=%-8s %s"
                 % (t, merged[t]["schema_var"], merged[t]["evidence"]))
    if not sure:
        L.append("  （无）")

    if maybe:
        L.append("")
        L.append("== [待定] 低置信：仅邻近匹配，可能是误判；仅在实测仍报 400 时才追加禁用 ==")
        for t in maybe:
            L.append("  [MAYBE ] %-32s schema=%-8s %s"
                     % (t, merged[t]["schema_var"], merged[t]["evidence"]))

    if rest:
        L.append("")
        L.append("== [保留] 未检出高风险 schema ==")
        for t in rest:
            L.append("  [KEEP  ] %-32s risk=%-7s %s"
                     % (t, merged[t]["risk"], merged[t]["evidence"]))

    L.append("")
    L.append("== apply 可直接使用的参数 ==")
    L.append("--server-id %s --tools %s" % (args.server_id, ",".join(sure)))
    if merged:
        L.append("全禁兜底（仅当迭代 2 轮仍报 400 时使用）:")
        L.append("--server-id %s --all-known" % args.server_id)
    L.append("")
    L.append("注意：scan 是加速器不是终局，最终以 apply 后实测是否仍报 400 为准。")
    L.append("      仍报且函数名变化 -> apply --from-error \"<报错原文>\" 继续迭代。")
    log(L, args.out)
    return 0 if sure else 3


# ---------------------------------------------------------------- status / apply / restore

def cmd_status(args, out):
    L = []
    db = args.db or DEF_DB
    if not os.path.isfile(db):
        L.append("[X] 未找到 state.vscdb: %s" % db)
        log(L, args.out)
        return 2
    procs = cursor_processes()
    L.append("[0] Cursor 进程数: %d" % len(procs))
    con = open_db(db)
    key = resolve_key(con)
    if key is None:
        L.append("[X] 未定位到 applicationUser 键")
        con.close()
        log(L, args.out)
        return 2
    cur_list, data = read_disabled(con, key)
    con.close()
    L.append("[1] key: %s" % key)
    L.append("[2] mcpDisabledTools = %s" % json.dumps(cur_list, ensure_ascii=False))
    L.append("[3] applicationUser 顶层字段: %s" % ", ".join(sorted((data or {}).keys())))
    log(L, args.out)
    return 0


def parse_error(text):
    """从 400 报错里解析 (server_id, tool_name)。"""
    m = re.search(r"Invalid schema for function\s*'([^']+)'", text)
    if not m:
        m = re.search(r"function\s*'([^']+)'", text)
    if not m:
        return None
    full = m.group(1)
    if "-" in full and "_" in full:
        head, tail = full.rsplit("-", 1)
        return head, tail
    return None, full


def cmd_apply(args, out):
    L = []
    db = args.db or DEF_DB
    if not os.path.isfile(db):
        L.append("[X] 未找到 state.vscdb: %s" % db)
        log(L, args.out)
        return 2

    targets = []
    if args.from_error:
        parsed = parse_error(args.from_error)
        if not parsed or not parsed[1]:
            L.append("[X] 无法从报错文本解析出函数名。期望形如：")
            L.append("    Invalid schema for function 'cursor-app-control-cursor_dialog'")
            log(L, args.out)
            return 2
        sid, tname = parsed
        if sid:
            args.server_id = sid
        L.append("[i] 从报错解析: server=%s tool=%s" % (args.server_id, tname))
        targets.append(tname)
    if args.all_known:
        targets.extend(KNOWN_TOOLS)
        L.append("[i] --all-known：将禁用 %d 个已知工具" % len(KNOWN_TOOLS))
    if args.tools:
        targets.extend([t.strip() for t in args.tools.split(",") if t.strip()])
    if not targets:
        L.append("[X] 未指定 --tools 或 --from-error")
        log(L, args.out)
        return 2

    entries = ["%s|%s" % (args.server_id, t) for t in dict.fromkeys(targets)]

    procs = cursor_processes()
    L.append("[0] Cursor 进程数: %d" % len(procs))
    for p in procs[:6]:
        L.append("    %s" % p[:150])

    con = open_db(db)
    key = resolve_key(con)
    if key is None:
        L.append("[X] 未定位到 applicationUser 键")
        con.close()
        log(L, args.out)
        return 2
    cur_list, data = read_disabled(con, key)
    con.close()
    L.append("[1] 写入前 mcpDisabledTools = %s" % json.dumps(cur_list, ensure_ascii=False))

    if procs and not args.force:
        L.append("[!] Cursor 仍在运行 —— 按设计不写入。")
        L.append("    请完全退出 Cursor（含托盘图标）后重跑；")
        L.append("    确认已退出但检测误报时可用 --force 跳过（不推荐）。")
        log(L, args.out)
        return 4

    bak, n = backup_db(db)
    L.append("[2] 已备份 %d 个文件 -> %s" % (n, bak))

    con = open_db(db, rw=True)
    cur = con.cursor()
    for e in entries:
        if e not in cur_list:
            cur_list.append(e)
    data["mcpDisabledTools"] = cur_list
    cur.execute("UPDATE ItemTable SET value=? WHERE key=?",
                (json.dumps(data, ensure_ascii=False), key))
    con.commit()
    con.close()
    L.append("[3] 已写入: %s" % json.dumps(cur_list, ensure_ascii=False))

    con = open_db(db)
    _, data2 = read_disabled(con, resolve_key(con))
    con.close()
    ok = all(e in (data2 or {}).get("mcpDisabledTools", []) for e in entries)
    L.append("[4] 回读验证: %s" % ("一致 OK" if ok else "不一致 FAIL"))
    L.append("")
    L.append("下一步：启动 Cursor -> 切到出错的模型 -> 新开一个 chat -> 发送任意消息验证。")
    L.append("回退  ：python cursor_mcp_toolctl.py restore --backup %s" % bak)
    log(L, args.out)
    return 0 if ok else 5


def cmd_restore(args, out):
    L = []
    db = args.db or DEF_DB
    if args.backup:
        if not os.path.isfile(args.backup):
            L.append("[X] 备份文件不存在: %s" % args.backup)
            log(L, args.out)
            return 2
        procs = cursor_processes()
        if procs and not args.force:
            L.append("[!] Cursor 仍在运行，未恢复。请先退出或加 --force。")
            log(L, args.out)
            return 4
        n = 0
        for ext in ("", "-wal", "-shm"):
            s = args.backup + ext
            if os.path.isfile(s):
                shutil.copy2(s, db + ext)
                n += 1
        L.append("[OK] 已从备份恢复 %d 个文件 -> %s" % (n, db))
        log(L, args.out)
        return 0

    procs = cursor_processes()
    if procs and not args.force:
        L.append("[!] Cursor 仍在运行，未修改。请先退出或加 --force。")
        log(L, args.out)
        return 4
    con = open_db(db, rw=True)
    key = resolve_key(con)
    _cur, data = read_disabled(con, key)
    bak, n = backup_db(db)
    data["mcpDisabledTools"] = []
    cur = con.cursor()
    cur.execute("UPDATE ItemTable SET value=? WHERE key=?",
                (json.dumps(data, ensure_ascii=False), key))
    con.commit()
    con.close()
    L.append("[OK] 已清空 mcpDisabledTools（备份 %d 个文件 -> %s）" % (n, bak))
    log(L, args.out)
    return 0


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Cursor 内置 MCP 工具禁用控制器")
    ap.add_argument("cmd", choices=["scan", "status", "apply", "restore"])
    ap.add_argument("--cursor-dir", help="Cursor 的 resources/app/out 目录")
    ap.add_argument("--server-id", default=DEF_SERVER_ID)
    ap.add_argument("--tools", help="逗号分隔的工具原始名（不带 server 前缀）")
    ap.add_argument("--all-known", action="store_true",
                    help="禁用 KNOWN_TOOLS 全部工具（多轮迭代仍失败时的兜底）")
    ap.add_argument("--from-error", help="粘贴 400 报错原文，自动解析 server/tool")
    ap.add_argument("--db", help="state.vscdb 路径")
    ap.add_argument("--out", default=os.path.join(os.getcwd(), ".cursor_toolctl_out.txt"))
    ap.add_argument("--backup", help="restore 时指定备份文件（不含 -wal/-shm 后缀）")
    ap.add_argument("--force", action="store_true", help="跳过 Cursor 运行检查")
    args = ap.parse_args()

    out = []
    if args.cmd == "scan":
        rc = cmd_scan(args, out)
    elif args.cmd == "status":
        rc = cmd_status(args, out)
    elif args.cmd == "apply":
        rc = cmd_apply(args, out)
    else:
        rc = cmd_restore(args, out)
    sys.exit(rc)


if __name__ == "__main__":
    main()
