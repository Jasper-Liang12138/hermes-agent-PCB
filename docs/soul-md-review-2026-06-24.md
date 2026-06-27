# SOUL.md 工业稳定性与功能 Review 报告

- 日期：2026-06-24
- 范围：本次迁入的 PCB Agent 系统提示词 `SOUL.md`，及其加载链路、部署链路、稳定性保护
- 结论：**内容合格，加载链路通畅，隔离设计健壮；存在一处升级覆盖隐患需处理**

---

## 1. 审查对象与方法

审查了以下内容：

- `SOUL.md` 文本本身（identity / 沟通风格 / 协作原则 / PCB 行为 / 边界）
- 加载链路：`agent/prompt_builder.py` → `run_agent.py::_build_system_prompt`
- 部署链路：`scripts/package-delivery-windows.ps1` → `.github/delivery/install.bat` → `hermes_constants.py::get_hermes_home`
- 稳定性保护：注入扫描、字符截断、去重、缓存

---

## 2. 加载链路（已验证通畅）

```
仓库根 SOUL.md
  └─ package-delivery-windows.ps1:138   复制到交付包 OutputDir
       └─ install.bat:30-37             复制到 %USERPROFILE%\.hermes\SOUL.md
            └─ get_hermes_home()        默认即 ~/.hermes  (hermes_constants.py:17)
                 └─ load_soul_md()      读取 (prompt_builder.py:886)
                      └─ _build_system_prompt  作为 identity slot #1 注入 (run_agent.py:4080-4083)
```

路径一致，链路闭合。SOUL.md 会作为 system prompt 的第一层身份注入，优先于 `DEFAULT_AGENT_IDENTITY` 兜底文本。

---

## 3. 工业稳定性评估

| 保护点 | 实现位置 | 现状判定 |
|---|---|---|
| 提示词注入扫描 | `prompt_builder.py:36-73` `_scan_context_content` | 当前文本干净，不触发任何威胁模式或不可见 Unicode，原样加载 |
| 字符截断上限 | `CONTEXT_FILE_MAX_CHARS = 20000` | 当前文件约 1.5K 字符，远不触发截断 |
| 二次注入去重 | `run_agent.py:4188` `skip_soul=True` | SOUL 作 identity 加载后不再被 `build_context_files_prompt` 重复注入，逻辑正确 |
| 系统提示缓存稳定 | `_build_system_prompt` 每会话构建一次并缓存 | SOUL 不会每轮重读，最大化前缀缓存命中 |
| 读取异常兜底 | `load_soul_md` try/except 返回 None | 文件缺失/不可读时回退到 `DEFAULT_AGENT_IDENTITY`，不会崩溃 |

整体稳定性达标，没有发现会导致运行期失败或事实链污染的缺陷。

---

## 4. 功能与隔离设计

`SOUL.md` 的 `# Boundaries` 段落明确声明：仅塑造语气、解释、建议、摘要，**不控制** workflow state、工具执行、router 选择、结构化协议字段。

该声明与代码实现一致：`_build_system_prompt` 只把 SOUL 文本拼入 prompt，完全不触碰：

- SWSD 工作流状态
- RuntimeBridge 工具执行
- ResponseBuilder 的 protocol 字段

即使 SOUL 措辞被改坏，也无法污染事实链（facts chain）。这是一个健壮的关注点隔离设计，建议保留并在后续演进中维持该边界。

内容质量方面：身份定位清晰，沟通风格可操作（结构化摘要、直接命名不确定性），PCB 行为段把 fanout / reroute / DRC / import / restore / 报告等核心动作都覆盖到了，且强调"区分工具产生的事实与解释/建议"，与隔离设计相互呼应。

---

## 5. 问题与改进建议

### 5.1 【高优先级】升级不会覆盖旧 SOUL.md

**问题**：`install.bat:31` 使用 `if not exist ... copy` 模式。对已安装过旧版本的现场机器，重装**不会更新** SOUL.md，仅打印 `SOUL.md exists, skipped`。本次迁入的新内容在存量机器上不会生效。

**影响**：工业交付中导致"代码已更新、现场仍跑旧提示词"的不一致，排查困难。`memories\MEMORY.md` 的 `install.bat:21` 同样是该模式，存在相同隐患。

**建议**（二选一）：

- 方案 A（推荐，保留用户自定义同时支持升级）：改为带版本/内容校验的安全覆盖——检测到内置版本与已安装版本不同时，先备份旧文件（如 `SOUL.md.bak-<timestamp>`）再覆盖，并在日志中提示已升级与备份位置。
- 方案 B（最低成本）：保持 `if not exist` 现状，但在交付 README 中明确写出"升级 SOUL/MEMORY 需手动删除 `~/.hermes\SOUL.md` 后重装"。

### 5.2 【中优先级】打包缺失时仅告警不阻断

**问题**：`package-delivery-windows.ps1:143` 在 SOUL.md 缺失时只 `Write-Warn`，打包仍继续产出交付包。

**建议**：SOUL.md 属于 PCB Agent 必需身份资产，建议缺失时改为 `throw` 终止打包，避免产出"无身份"的残缺交付包流向现场。

### 5.3 【低优先级】内容健壮性增强

- 当前 SOUL.md 无 YAML frontmatter。`load_soul_md` 不会剥离 frontmatter（与 `_load_hermes_md` 不同），若未来在 SOUL.md 顶部加配置块会被原样注入。维持纯正文即可，若需配置请走独立机制。
- 建议在文档中固定一条约定：SOUL.md 仅承载"语气/解释/建议/摘要"，任何涉及状态、执行、字段的规则一律不得写入 SOUL，以防边界被无意破坏。

---

## 6. 总体结论

- 内容：合格，定位清晰，覆盖 PCB 核心工作流，边界声明诚实。
- 稳定性：达标，注入扫描 / 截断 / 去重 / 缓存 / 异常兜底齐备。
- 隔离设计：健壮，SOUL 无法污染事实链，建议长期保留该边界。
- 待办：优先处理 5.1 的升级覆盖隐患（影响现场一致性），其次 5.2 打包阻断。
