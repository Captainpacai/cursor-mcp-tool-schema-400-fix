# cursor-mcp-tool-schema-400-fix

修复 Cursor Agent 模式下的报错：

```
openai: status=400 {"error":{"message":"Invalid schema for function
'cursor-app-control-move_agent_to_root': schema must be a JSON Schema object"}}
```

## 这是什么

Cursor 的内置 MCP Provider（`cursor-app-control` 等）在模块加载时静态注册工具，其中部分工具的 zod schema（`union` / `discriminatedUnion` / `record` / `refine` / `transform`）转成 JSON Schema 后不被 OpenAI 接受，导致走 OpenAI 协议的模型（GPT 系）**每次请求都被 400 拒绝**。

本 Skill 利用 Cursor 自带的**工具级禁用机制**，把坏 schema 工具写进 `state.vscdb` 的 `mcpDisabledTools`。它们在请求构造阶段即被过滤，不再进入 `tools` 数组 → 400 消失 → GPT 恢复可用。

已在 **Cursor 3.11.25 / Windows 11** 上完整实测闭环。macOS 路径为按惯例推断，未实测。

## 安装

```bash
# WorkBuddy 用户级（推荐）
cp -r cursor-mcp-tool-schema-400-fix ~/.workbuddy/skills/

# 或项目级
cp -r cursor-mcp-tool-schema-400-fix .workbuddy/skills/
```

**要求**：Python ≥ 3.8。**无第三方依赖**（仅用标准库）。

安装后，当对话中出现上述报错时会自动触发；也可直接按下面用法手动执行。

## 用法

脚本位于 `scripts/cursor_mcp_toolctl.py`，四个子命令：

| 子命令 | 作用 | 需要退出 Cursor |
|---|---|---|
| `scan` | 扫描 Cursor 安装包，判定各内置工具的 schema 风险 | 否 |
| `status` | 只读读取当前禁用清单 | 否 |
| `apply` | 备份 + 合并写入 + 回读验证 | **是** |
| `restore` | 清空清单或从备份恢复 | **是** |

### 1. 查看当前状态

```bash
python scripts/cursor_mcp_toolctl.py status
```

```
[0] Cursor 进程数: 0
[1] key: src.vs.platform.reactivestorage...applicationUser
[2] mcpDisabledTools = []
```

### 2. 从报错原文直接修复（主路径）

```bash
python scripts/cursor_mcp_toolctl.py apply \
  --from-error "Invalid schema for function 'cursor-app-control-move_agent_to_root': schema must be a JSON Schema object"
```

```
[i] 从报错解析: server=cursor-app-control tool=move_agent_to_root
[0] Cursor 进程数: 0
[1] 写入前 mcpDisabledTools = []
[2] 已备份 3 个文件 -> .../state.vscdb.bak_<时间戳>
[3] 已写入: ["cursor-app-control|move_agent_to_root"]
[4] 回读验证: 一致 OK
```

然后：启动 Cursor → 切到出错的模型 → **新开一个 chat** → 验证。

若仍报 400 但函数名变了，说明过滤已生效，用新的报错原文再跑一次即可（合并式写入，不清空已有项）。

### 3. 一次性扫描定位（加速器）

```bash
python scripts/cursor_mcp_toolctl.py scan --cursor-dir "<CURSOR_INSTALL>/resources/app/out"
```

输出按置信度分级：`[建议禁用]` 可直接采用，`[待定]` 为邻近匹配可能是误判。

### 4. 回退

```bash
# 从备份恢复（推荐）
python scripts/cursor_mcp_toolctl.py restore --backup <state.vscdb.bak_时间戳>

# 或清空禁用清单
python scripts/cursor_mcp_toolctl.py restore
```

## 代价

禁用后 agent 失去：自动切换工作区根目录（`move_agent_to_root` / `move_agent_to_cloned_root`）、弹出交互对话框（`cursor_dialog`）、打开自动化 UI（`open_automation`）。

保留：`create_project`、`open_resource`、`get_cursor_user_state`、`rename_chat`、`show_confetti`。

完全可逆。

## 不适用的情况

- 报错来自第三方 MCP server（应修 server 本身）
- `context_length_exceeded` / `invalid_api_key` / `rate_limit` 等其它 400
- 普通 Chat / Cmd+K 模式下的报错

详见 `SKILL.md` §1.2 与附录 D。

## License

MIT
