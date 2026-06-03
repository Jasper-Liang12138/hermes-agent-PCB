## ADDED Requirements

### Requirement: BGA target selection is explicit for all detected counts
系统 SHALL 在 BGA 逃逸分析识别到一个或多个 BGA 候选时，始终通过 `selection` 字段向前端返回候选列表，并等待用户选择目标 BGA 后再进入布线器类型选择。

#### Scenario: Single BGA still requires target selection
- **WHEN** 用户发起 BGA 逃逸且版图分析只返回一个 BGA 候选 `U22`
- **THEN** 系统返回包含 `U22` 的 `selection` 列表，并保持流程在目标选择阶段

#### Scenario: Router type is requested after selecting the single BGA
- **WHEN** 系统处于目标选择阶段且候选列表只包含 `U22`，用户回复 `选择 U22`
- **THEN** 系统记录 `selectedBGA=U22`，并提示用户选择 `135` 或 `arc` 以及 `RL` 或 `北科大`

#### Scenario: Confirm before target selection is rejected
- **WHEN** 系统处于目标选择阶段且用户回复 `确认`
- **THEN** 系统 MUST 不执行布线，并提示用户先选择目标 BGA

### Requirement: Fanout parameter JSON confirms routing when orderLines are present
系统 SHALL 在 fanoutParams 确认阶段接受用户回传的 fanout 参数 JSON；当解析结果包含非空 `orderLines` 时，系统 MUST 将其视为用户同意本配置，并使用更新后的参数执行布线。

#### Scenario: User JSON updates cached fanoutParams and starts routing
- **WHEN** 系统处于 fanoutParams 确认阶段，用户回传 `{"orderLines":[{"layer":"Top","net":"GND","order":1}],"routerType":"rl","selectedBGA":"U22"}`
- **THEN** 系统更新缓存中的 `orderLines`、`routerType` 和 `selectedBGA`，并调用布线器执行布线

#### Scenario: User JSON may omit confirmation text
- **WHEN** 系统处于 fanoutParams 确认阶段，用户回传的内容是包含 `orderLines` 的 JSON 且不包含“确认”文字
- **THEN** 系统 SHALL 将该回传视为确认执行，不再要求用户额外回复“确认”

#### Scenario: JSON without orderLines does not confirm routing
- **WHEN** 系统处于 fanoutParams 确认阶段，用户回传 JSON 但其中没有 `orderLines`
- **THEN** 系统 MUST 不执行布线，并提示用户回复确认、取消或提供有效 fanout 参数

### Requirement: User fanout parameters are validated against session context
系统 MUST 在执行布线前校验并归一化用户回传的 fanout 参数，使 `selectedBGA` 属于已识别候选，`routerType` 属于支持枚举，`orderLines` 为可执行的有序列表。

#### Scenario: Invalid selectedBGA is rejected
- **WHEN** 系统处于 fanoutParams 确认阶段，已识别候选为 `U22`，用户回传包含 `selectedBGA=U99` 和非空 `orderLines` 的 JSON
- **THEN** 系统 MUST 不执行布线，并提示目标 BGA 无效或需要重新选择

#### Scenario: Missing selectedBGA uses selected session target
- **WHEN** 系统处于 fanoutParams 确认阶段，session 已记录 `selectedBGA=U22`，用户回传包含非空 `orderLines` 但缺少 `selectedBGA`
- **THEN** 系统 SHALL 使用 session 中的 `U22` 补齐参数后执行布线

#### Scenario: Returned orderLines are normalized
- **WHEN** 用户回传的 `orderLines` 中 `order` 字段不是连续编号
- **THEN** 系统 SHALL 按 order 排序并重新生成从 1 开始的连续编号后传给布线器
