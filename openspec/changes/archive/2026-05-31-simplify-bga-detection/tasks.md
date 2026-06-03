## 1. Core Implementation

- [x] 1.1 在 `tools/pcb_chunking_tool.py` 模块级添加 `_BGA_MIN_PIN_COUNT = 200` 常量
- [x] 1.2 修改 `_extract_text_bga_selection`：移除 `"bga" in package_text.lower()` 和 `"bga" not in package_name.lower()` 判定，改为 `pin_count > _BGA_MIN_PIN_COUNT`
- [x] 1.3 修改 `_summarize_board_model`：移除 `"bga" in f"{footprint} {part}".lower()` 判定，改为 `len(pins) > _BGA_MIN_PIN_COUNT`

## 2. Tests

- [x] 2.1 更新 `tests/tools/test_pcb_chunking_tool.py` 中 BGA 识别相关测试用例：验证 pin 数 > 200 的器件被识别为 BGA，pin 数 <= 200 的器件不被识别
- [x] 2.2 添加边界测试：pin 数恰好为 200 的器件不被识别、pin 数为 201 的器件被识别
- [x] 2.3 运行 `python -m pytest tests/tools/test_pcb_chunking_tool.py -v` 确认全部通过

## 3. Validation

- [x] 3.1 运行 `python -m pytest tests/ -v -k "pcb"` 确保无相关回归
