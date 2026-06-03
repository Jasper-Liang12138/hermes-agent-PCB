## Context

当前 BGA 识别分布在 `tools/pcb_chunking_tool.py` 两个函数中：
- `_extract_text_bga_selection`（文本直解析路径）：解析 S 表达式文本中的 `(component ...)` 块，通过 `"bga" in package_name.lower()` 过滤
- `_summarize_board_model`（结构化解析路径）：通过 `parse_txt_to_board_model` 解析后，同样用 `"bga" in ...` 字符串匹配过滤

两条路径都已具备 pin 数统计能力（通过正则 `r'\(pin...'` 或 `len(comp.pins)`），只是目前仅用于生成 detail 文本，未参与判定。

## Goals / Non-Goals

**Goals:**
- 将 BGA 判定逻辑统一为 pin 数 > 200
- 保留已有的 pin 数统计代码，复用其方法
- 保持两条解析路径的行为一致

**Non-Goals:**
- 不修改 `pcb_chunk_service` 轮子内部的 `extract_bga_from_txt` 行为
- 不改变 `selection`、`boardSummary`、`fanoutContext` 的 JSON 输出结构
- 不修改 WebSocket 流程或前端协议

## Decisions

### 阈值：200 pins

选择 200 作为判定阈值。一般 BGA 封装 pin 数在 200 以上（如 256、400、676、1024 等），低于 200 的 QFP/QFN 等封装不应被误判为 BGA。阈值定义为模块级常量 `_BGA_MIN_PIN_COUNT = 200`，便于后续调整。

### 保留 pcb_chunk_service 的 extract_bga_from_txt 作为补充

`_extract_rule_bga_selection` 中 `_service.extract_bga_from_txt` 的返回结果仍参与合并（通过 `_merge_selection`），不会被替换。两条路径（rule-based text + structured model）各自独立改为 pin-count 判定。

### detail 文本生成调整

原 detail 格式为 `{package_name} ({pin_count} pins)`，改为 pin count 判定后，对于非 BGA 封装名但 pin 数超阈值的器件，detail 仍展示实际封装名和 pin 数，不做额外修饰。

## Risks / Trade-offs

- **pin 数统计误差**：S 表达式文本正则 `r'\(pin...'` 可能因格式变体漏计或重复计数 → `_find_sexpr_end` 已确保正确截取 block 边界，正则匹配与结构化解析的两套 pin 计数方法均已验证
- **阈值边界**：200 pin 阈值可能对某些边界器件（如 196 pin 的 BGA）产生漏判 → 当前业务场景中此类器件极少见，且阈值可通过常量快速调整
