## ADDED Requirements

### Requirement: BGA detection by pin count threshold

系统 SHALL 在解析 PCB 版图数据时，通过统计器件的 pin 数量来判定该器件是否为 BGA 封装。pin 数大于 200 的器件 SHALL 被识别为 BGA 候选。

当 S 表达式文本中存在 `(component "REFDES" ...)` 或 `(component (name "REFDES") ...)` 格式的器件块时，系统 SHALL 统计每个器件块内的 `(pin ...)` 条目数，若数量超过 200 则将该器件加入 BGA 候选列表。

#### Scenario: Component with more than 200 pins identified as BGA

- **WHEN** 版图数据中包含一个名为 U27 的器件，其 pin 数为 400
- **THEN** U27 出现在 BGA 候选 selection 列表中，detail 包含封装名和 pin 数

#### Scenario: Component with 200 or fewer pins not identified as BGA

- **WHEN** 版图数据中包含一个名为 U5 的器件，其 pin 数为 144
- **THEN** U5 不出现在 BGA 候选 selection 列表中（除非封装名明确包含 "bga" 且被 pcb_chunk_service 的 extract_bga_from_txt 单独识别）

#### Scenario: Component with no package name prefix but high pin count

- **WHEN** 版图数据中包含一个器件，其封装名不包含 "bga" 字符串但 pin 数为 676
- **THEN** 系统仍将该器件识别为 BGA 候选并加入 selection

#### Scenario: Empty board text

- **WHEN** 版图数据为空字符串
- **THEN** 系统返回空 selection 列表，不抛出异常
