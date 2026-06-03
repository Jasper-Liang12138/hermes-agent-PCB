## Why

当前 BGA 识别逻辑依赖封装名称中是否包含 "bga" 字符串，无法识别封装名不规范但实际为高密度 BGA 封装的器件（如某些厂商自定义命名的 FPGA/BGA 器件）。改用 pin 数阈值（>200）判定，覆盖面更广，逻辑更简洁，且 pin 数获取方法已有现成实现可复用。

## What Changes

- `_extract_text_bga_selection`: 移除对 `package_name` 中 "bga" 字符串的匹配，改为统计每个器件块的 pin 数量，pin 数 > 200 即视为 BGA
- `_summarize_board_model`: 移除 `"bga" in ...` 字符串匹配，改为检查 `len(pins) > 200`
- `_extract_rule_bga_selection` 中调用的 `_service.extract_bga_from_txt` 保持不变，作为补充数据源
- 阈值 200 作为模块级常量定义，便于后续调整

## Capabilities

### New Capabilities

- `bga-detection-by-pin-count`: 基于 pin 数阈值识别 BGA 器件，替代原有封装名字符串匹配逻辑

### Modified Capabilities

<!-- No existing capabilities whose requirements change at spec level -->

## Impact

- `tools/pcb_chunking_tool.py`: `_extract_text_bga_selection`、`_summarize_board_model` 两处 BGA 判定逻辑
