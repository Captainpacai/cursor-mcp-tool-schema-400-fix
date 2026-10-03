---
name: cursor-mcp-tool-schema-400-fix
description: 修复 Cursor Agent 模式下 "openai: status=400 Invalid schema for function '<server>-<tool>': schema must be a JSON Schema object" 报错。当 Cursor 内置 MCP 服务器（cursor-app-control 等）用 zod union/discriminatedUnion/record/refine 定义的工具 schema 不被 OpenAI 接受时，通过 Cursor 官方工具级禁用机制（state.vscdb 的 mcpDisabledTools）在下发前过滤掉坏 schema 工具，使 GPT 系模型恢复可用。触发词：Cursor 400、Invalid schema for function、move_agent_to_root、cursor-app-control、Agent 模式报错、换 GPT 就报错、schema must be a JSON Schema。
agent_created: true
---

# Cursor 内置 MCP 工具 schema 400 修复

## 0. 一句话结论

Cursor 的内置 MCP Provider（如 `cursor-app-control`）在**模块加载时静态注册**工具，其中部分工具的 zod schema（`union` / `discriminatedUnion` / `record` / `refine` / `transform`）转成 JSON Schema 后**不被 OpenAI 接受**，导致走 OpenAI 协议的模型（GPT 系）**每次请求必然被 400 拒绝**。
解法：用 Cursor 自带的**工具级禁用机制**把坏 schema 工具写进 `mcpDisabledTools`，它们在请求构造阶段就被过滤掉，不再进入 `tools` 数组 → 400 消失 → GPT 恢复可用。

> 本 Skill 已在 Cursor 3.11.25 / Windows 11 上完整实测闭环（含三次迭代）。脚本为本机验证过的可运行版本。

---

## 1. 适用场景与触发条件

### 1.1 应当调用（满足全部条件）

| 条件 | 判定方法 |
|---|---|
| 报错文本含 `Invalid schema for function` | 用户粘贴的报错原文中可 grep 到 |
| 报错文本含 `schema must be a JSON Schema` | 同上（常被 UI 截断成 `JS`，仍算命中） |
| 函数名为 `<server>-<tool>` 形态 | 如 `cursor-app-control-move_agent_to_root` |
| server 是 **Cursor 内置**的 | 在已知集合内（`cursor-app-control`、`cursor-dev-control`、`fsd` 等），或经 §4.2 scan 确认 |
| 在 **Agent 模式**下、切换模型**立即**复现 | 说明是无条件下发，非场景触发 |

### 1.2 不应调用（走别的路径）

| 情况 | 原因 | 去向 |
|---|---|---|
| 400 但消息是 `context_length_exceeded` / `invalid_api_key` / `rate_limit` / `model_not_found` | 与本故障无关 | 常规 API 排障 |
| 报错函数来自**用户自建/第三方 MCP server** | 根因在第三方 server 的 schema，禁用只是掩盖 | 修第三方 server 或向作者报 issue |
| Cursor 版本 ≥ 3.21 且报错已消失 | 官方已修 | 无需处理 |
| 报错发生在普通 Chat / Cmd+K（非 Agent 模式） | 内置工具通常不下发 | 重新定性 |

### 1.3 四个已被实测证伪的路径（**不要重复踩**）

1. **UI 里禁用 → 死路。**
   `cursor-app-control` 是**进程内 Provider**，不是常规 MCP server：
   ```js
   this.environmentService.isGlass && (           // 只有 Glass 窗口才注册
     this.mcpProviderService.registerMcpProvider(this.createProvider()), ...)
   function sWo(e){ function t(i){ return !(Qrp(i)||Zrp(i)||e&&Yrp(i)) } }  // Glass 下写死隐藏
   ```
   → Glass 窗口下被隐藏，**经典窗口下根本不注册**。两个窗口都看不到，UI 路径在设计上不成立。

2. **加 Rules 拦截 → 无效。**
   400 发生在**请求构造阶段**（tools 数组下发时），模型尚未输出。Rules 只注入 system prompt，管不到这一层。

3. **服务器级禁用（`disabledMcpServers`）→ 可能失效。**
   ```js
   if(!r || !this.isServerEnabled(r) && !this.isMcpProviderServer(r)) continue;
   //                                    ↑ Provider 豁免服务器级启用检查
   ```

4. **换模型 → 治标不治本。**
   切 Claude 确实能绕开（Anthropic 协议校验不同），但 GPT 系仍不可用。本方案是让 GPT 恢复。

---

## 2. 故障机理（用于向用户解释，60 秒版）

```
模块加载 ──▶ 工具连同 zod schema 静态注册（无条件）
        ──▶ 请求构造：stringify(zod.toJSONSchema(schema)) 塞进 tools 数组
        ──▶ OpenAI 校验 union/discriminatedUnion 派生结构 → 不合法
        ──▶ HTTP 400，整轮请求被拒（模型还没开口）
```

被拒的是**那几个工具的 schema**，不是模型、不是 API Key、不是网络。

---

## 3. SOP（严格执行，每步有判定标准）

### S1 · 收集报错原文

- **动作**：要求用户粘贴**完整**报错字符串（不要只给函数名）。最少需要 `Invalid schema for function '<full-name>'` 这一段。
- **判定**：能正则抽出 `<server>-<tool>` → 进入 S2；抽不出 → 要求用户重新复制，不要用截图描述。
- **产出**：`ERROR_TEXT`

### S2 · 定性：内置工具 or 第三方

- **动作**：用已知 server 集合比对（见附录 A）。命中即内置。
- **判定**：命中 → 本 Skill 适用；未命中 → 用 §4.2 scan 确认；scan 也确认不了 → 提示用户这可能来自第三方 MCP，转 §6 边界处理。
- **产出**：`SERVER_ID`（默认 `cursor-app-control`）

### S3 · 环境准备（**关键前置**）

- **动作**：
  1. 定位 Cursor 安装目录：优先从用户给的崩溃日志 / 报错堆栈里的 `vscode-file://` 路径提取（形如 `c:/<path>/cursor/resources/app/out/...`）；否则用脚本自动探测常见路径；探测不到则要求用户提供。
  2. 确认 `state.vscdb` 存在（脚本 `status` 会验证）。
  3. **要求用户完全退出 Cursor（含系统托盘图标）**。
- **判定**：`status` 输出 `[0] Cursor 进程数: 0` 且能读出 `mcpDisabledTools` → 就绪。
- **产出**：`CURSOR_OUT_DIR`、`DB_PATH`、就绪确认
- ⚠️ **运行中写入会被 WAL 覆盖**，脚本默认拒绝写入并返回退出码 4。

### S4 · scan（**加速器，可跳过**）

- **动作**：`scan --cursor-dir <CURSOR_OUT_DIR>`
- **判定**：
  - 输出 `[建议禁用]` 非空 → 采用其清单
  - 输出为空/报错 → **降级走 S5 的 `--from-error` 迭代**（这才是主路径，不依赖 scan）
- **产出**：高置信禁用清单 + 待定清单 + 保留清单
- 说明：scan 输出**带置信度**。`[MAYBE]`（proximity 匹配）可能是误判，**默认不要禁用**，仅在实测仍报时才追加。

### S5 · 首次写入

- **动作**（二选一，等价）：
  ```bash
  # A. 用 scan 产出的清单
  apply --server-id <SERVER_ID> --tools <t1,t2,t3>
  # B. 直接从报错解析（无 scan 时的主路径）
  apply --from-error "<ERROR_TEXT>"
  ```
- **判定**：脚本退出码 `0` + `[4] 回读验证: 一致 OK` → 成功
  退出码 `4` = Cursor 未退出；`2` = 路径/解析失败；`5` = 回读不一致
- **产出**：备份文件路径（**必须记录并告知用户**）、当前 `mcpDisabledTools` 全量值

### S6 · 用户实测（**必须由用户执行**）

- **动作**：启动 Cursor → 切到原出错模型 → **新开一个 chat** → 发任意消息
- **判定**：见 §5 验收标准
- ⚠️ **必须新开 chat**：已有会话的工具集在初始化时已固定

### S7 · 迭代判定（核心闭环）

| 实测结果 | 结论 | 下一步 |
|---|---|---|
| 无 400，正常回复 | ✅ 完成 | 收尾（S8） |
| 仍报 400，**函数名变了** | ✅ **过滤生效**，只是还有别的坏工具 | `apply --from-error "<新报错>"`，回到 S6 |
| 仍报 400，**函数名没变** | ❌ 该 key 未生效 | 查 §7 失败模式 2 |
| 迭代 ≥ 2 轮仍报，函数名持续变化 | 坏工具数量多 | `apply --server-id <SERVER_ID> --all-known`（全禁兜底） |
| 全禁后仍报 | ❌ 过滤链路外还有注入点 | 放弃本方案，转 §6 兜底 |

### S8 · 收尾

- 向用户报告：已禁用清单、失去的能力、备份路径、回退方法
- 写入工作区 memory（备份时间戳尤其重要）

---

## 4. 工具用法

脚本：`<skill_dir>/scripts/cursor_mcp_toolctl.py`（Python 3，无第三方依赖）

### 4.1 子命令

| 子命令 | 作用 | 是否要求退出 Cursor | 退出码 |
|---|---|---|---|
| `scan` | 扫描 bundle 判定 schema 风险 | 否 | 0=有高置信项 / 3=无 / 2=路径错 |
| `status` | 只读读取当前禁用清单 | 否 | 0 / 2 |
| `apply` | 备份+合并写入+回读验证 | **是** | 0=成功 / 2=参数错 / 4=未退出 / 5=回读不一致 |
| `restore` | 清空清单或从备份恢复 | **是** | 0 / 2 / 4 |

### 4.2 参数

| 参数 | 说明 |
|---|---|
| `--cursor-dir` | Cursor 的 `resources/app/out` 目录（scan 用） |
| `--server-id` | 内置 server id，默认 `cursor-app-control` |
| `--tools` | 逗号分隔的**工具原始名**（不带 server 前缀） |
| `--from-error` | 粘贴报错原文，自动解析 server/tool |
| `--all-known` | 禁用 `KNOWN_TOOLS` 全部 9 个（多轮失败兜底） |
| `--db` | `state.vscdb` 路径（便携版/非标准位置用） |
| `--out` | 输出文件（默认 `./.cursor_toolctl_out.txt`） |
| `--backup` | restore 时指定备份文件（不含 `-wal`/`-shm` 后缀） |
| `--force` | 跳过进程检查（不推荐） |

> 脚本**所有输出都写入 `--out` 文件**。某些 Windows 环境下 stdout 会被吞掉，以文件为准。

---

## 5. 验收标准与自检清单

### 5.1 成功标准（全部满足才算修好）

1. 启动 Cursor 后进程正常，无异常报错 toast
2. 切到**原出错的模型**（如 GPT-5.x）
3. **新开 chat**，发送任意消息
4. 收到正常回复，**无 400**
5. 连续 3 轮对话无 400 复现

### 5.2 自检清单（交付前逐项确认）

- [ ] 报错原文已取全（含完整函数名，非截图描述）
- [ ] server id 已确认为 Cursor 内置
- [ ] 写入前 `status` 显示进程数 = 0
- [ ] 写入后有备份文件（含 `-wal` / `-shm` 共 3 个）
- [ ] 回读验证显示"一致 OK"
- [ ] 已告知用户必须**新开 chat** 测试
- [ ] 已告知用户备份路径与回退方法
- [ ] 已告知用户被禁用工具对应的能力损失

---

## 6. 边界情况与兜底

| 边界 | 处理 |
|---|---|
| 全禁后仍报 400 | 过滤链路外还有注入点 → 本方案到顶。兜底：**切 Claude 系模型**（走 Anthropic 协议，实测可绕开）或升级 Cursor（官方修复在 3.21+） |
| 用户开了 `update.mode: none` 锁版本 | 不要擅自改该设置；改用本方案或切模型 |
| 报错涉及**另一个**内置 server（如 `cursor-dev-control`） | 换 `--server-id` 重跑，流程不变 |
| 企业/团队管控账号 | UI 开关可能被 `getEffectiveEnabledTools` 早退拦截，但**直写 DB 不受影响** |
| 便携版 / 非标准数据目录 | 用 `--db` 显式指定 |
| Cursor 升级后 | 工具名与 key 可能变化 → 重跑 `scan`；若 `mcpDisabledTools` 键消失，`status` 会报"未定位到键"，需重新定性 |
| 用户机器上无 Python | 用托管解释器绝对路径（见附录 C） |

---

## 7. 常见失败模式

| # | 症状 | 根因 | 处理 |
|---|---|---|---|
| 1 | `apply` 返回 4「Cursor 仍在运行」 | 托盘图标未退出 / 后台进程残留 | 让用户从托盘彻底退出；确认无误仍误报才用 `--force` |
| 2 | 报错函数名**未变化** | ① 工具名误写成带前缀的形态 ② server id 猜错 | 正确格式为 `<server>\|<tool_original_name>`，tool **不带**前缀。例：`cursor-app-control\|move_agent_to_root` |
| 3 | scan 输出"未找到 bundle" | `--cursor-dir` 指错层级 | 必须是 `.../resources/app/out`，不是 `resources/app` |
| 4 | scan 无结果但报错确实存在 | 版本代码形态变了，正则未覆盖 | 走 `--from-error` 迭代（主路径，不依赖 scan） |
| 5 | 写入后 Cursor 启动异常 | 配置损坏 | `restore --backup <备份路径>`（3 个文件一起恢复） |
| 6 | 改完"没效果" | 在**已有 chat** 里测 | 必须新开 chat |
| 7 | 中文界面搜不到设置项 | 第三方汉化补丁运行时注入，搜索匹配的是底层英文 key | 用英文 key 搜索；本方案不依赖 UI |
| 8 | scan 把安全工具标成 MAYBE | proximity 降级误判 | 只信 `[建议禁用]`（高置信）段 |

---

## 8. 最小可运行示例

### 示例 A：从报错原文直接迭代（**主路径，无需 scan**）

**输入**
```
openai: status=400 {"error":{"message":"Invalid schema for function
'cursor-app-control-move_agent_to_root': schema must be a JSON Schema object"}}
```

**执行**（Cursor 已完全退出）
```bash
python cursor_mcp_toolctl.py apply \
  --from-error "Invalid schema for function 'cursor-app-control-move_agent_to_root': schema must be a JSON Schema object"
```

**输出**
```
[i] 从报错解析: server=cursor-app-control tool=move_agent_to_root
[0] Cursor 进程数: 0
[1] 写入前 mcpDisabledTools = []
[2] 已备份 3 个文件 -> ...\state.vscdb.bak_20261003_172730
[3] 已写入: ["cursor-app-control|move_agent_to_root"]
[4] 回读验证: 一致 OK

下一步：启动 Cursor -> 切到出错的模型 -> 新开一个 chat -> 发送任意消息验证。
回退  ：python cursor_mcp_toolctl.py restore --backup ...\state.vscdb.bak_20261003_172730
```

**预期后续**：若用户回报"仍报，但变成 `cursor-app-control-cursor_dialog`"，执行同一条命令、换新的报错原文再跑一次即可（合并式写入，不清空已有项）。

---

### 示例 B：scan 驱动的一次性定位（本机实测输出）

**执行**
```bash
python cursor_mcp_toolctl.py scan --cursor-dir "C:/AItools/Cursor-3.11/cursor/resources/app/out"
```

**输出（实测，已脱敏路径无关部分）**
```
[scan] out dir  : C:/AItools/Cursor-3.11/cursor/resources/app/out
[scan] server id: cursor-app-control
  - workbench.glass.main.js: 工具常量=9, 风险schema=22, 工具=9
  - workbench.desktop.main.js: 工具常量=9, 风险schema=14, 工具=9

== [建议禁用] 高置信 ==
  [DISABLE] cursor_dialog                    schema=JIg      Yt.discriminatedUnion(...)
  [DISABLE] move_agent_to_cloned_root        schema=Svl      Yt.union(...)
  [DISABLE] move_agent_to_root               schema=Svl      Yt.union(...)

== [待定] 低置信：仅邻近匹配，可能是误判 ==
  [MAYBE ] create_project                   schema=IA_      Yt.union(...) (proximity)

== [保留] 未检出高风险 schema ==
  [KEEP  ] get_cursor_user_state   risk=low   schema var not in risk table
  [KEEP  ] open_automation         risk=low   schema var not in risk table
  [KEEP  ] open_resource           risk=low   schema var not in risk table
  [KEEP  ] rename_chat             risk=low   schema var not in risk table
  [KEEP  ] show_confetti           risk=low   schema var not in risk table

== apply 可直接使用的参数 ==
--server-id cursor-app-control --tools cursor_dialog,move_agent_to_cloned_root,move_agent_to_root
```

**随后执行**
```bash
python cursor_mcp_toolctl.py apply --server-id cursor-app-control \
  --tools cursor_dialog,move_agent_to_cloned_root,move_agent_to_root
```

> 实战提示：本机实测中 `open_automation`（`nn.record(string, unknown)` + `.refine()`）虽被 scan 判为 KEEP，但因 schema 形态可疑而**保守一并禁用**。是否对 `open_automation` 采取保守策略由执行者决定——本 Skill 的默认建议是**先按 scan 高置信清单执行，只在实测仍报时才追加**，避免过度禁用。

---

## 附录 A：已知事实表（Cursor 3.11.25 实测）

**cursor-app-control 的 9 个内置工具**

| 工具 | schema 形态 | 实测结论 |
|---|---|---|
| `move_agent_to_root` | `zod.union([...])` | ❌ 实测 400 |
| `move_agent_to_cloned_root` | 同上（共用同一 schema） | ❌ 实测 400 |
| `cursor_dialog` | `zod.discriminatedUnion("action",[...])` | ❌ 实测 400 |
| `open_automation` | `zod.record(string, unknown)` + `.refine()` | ⚠️ 可疑，实战中保守禁用 |
| `create_project` | 最简 `object({}).strict()` | ✅ 保留 |
| `open_resource` | 同上 | ✅ 保留 |
| `get_cursor_user_state` | 同上 | ✅ 保留 |
| `rename_chat` | 同上 | ✅ 保留 |
| `show_confetti` | 同上 | ✅ 保留 |

**关键存储位置**

| 项 | 值 |
|---|---|
| DB | Windows：`%APPDATA%\Cursor\User\globalStorage\state.vscdb`<br>macOS：`~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` |
| 键 | `src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl.persistentStorage.applicationUser` |
| 字段 | `mcpDisabledTools`（JSON 数组） |
| 条目格式 | `"<server-id>|<tool-original-name>"`（工具名**不带** server 前缀） |
| 备份 | 同目录 `state.vscdb.bak_<时间戳>`，含 `-wal` / `-shm` 共 3 个文件 |

**禁用机制代码依据**

```js
// 工具级禁用（UI 与配置同一入口）
toggleToolDisabled(e, t){
  const n = this.toolKey(e, t);                 // "${serverId}|${toolName}"
  const i = new Set(mcpDisabledTools || []);
  i.has(n) ? i.delete(n) : i.add(n);
  reactiveStorageService.setApplicationUserPersistentStorage("mcpDisabledTools", [...i]);
}
// 下发前过滤（两条路径都生效）
for(const a of s) this.mcpService.isToolDisabled(r, a.name) || o.push(a.name);
```

---

## 附录 B：代价说明（必须告知用户）

禁用后失去的能力（按工具）：

- `move_agent_to_root` / `move_agent_to_cloned_root`：agent 自动切换工作区根目录、在 worktree 间迁移会话
- `cursor_dialog`：agent 弹出交互对话框
- `open_automation`：打开自动化 UI 面板

保留：`create_project`、`open_resource`、`get_cursor_user_state`、`rename_chat`、`show_confetti`。

日常编码几乎不受影响；**完全可逆**（清空数组或恢复备份即可）。

---

## 附录 C：Windows 环境坑

| 坑 | 表现 | 绕法 |
|---|---|---|
| Bash 工具 PATH 损坏 | `ls`/`cat`/`head` 报 command not found | 用绝对路径调 Python，不用 shell 内建命令 |
| PowerShell stdout 被吞 | 只回 exit code | 脚本统一把结果写文件，再读文件 |
| GBK 控制台 | 中文 print 抛异常 | 脚本已做 ASCII 转义输出 |
| 无系统 Python | `python` 不可用 | 用托管解释器绝对路径，如 `C:\Users\<user>\.workbuddy\binaries\python\versions\3.13.12\python.exe` |

---

## 附录 D：与其他故障的区分

| 故障 | 报错特征 | 本 Skill 是否适用 |
|---|---|---|
| 本故障 | `Invalid schema for function '<server>-<tool>'` | ✅ |
| MCP server 连接失败 | `Failed to connect to MCP server` / `spawn ENOENT` | ❌ |
| React 渲染崩溃 | `removeChild ... not a child of this node` + Component stack | ❌（Cursor UI bug，Reload Window） |
| API Key / 额度 | `401` / `429` / `insufficient_quota` | ❌ |
| 上下文超限 | `context_length_exceeded` | ❌ |
