# bga-router-constraint-files Specification

## Purpose
TBD - created by archiving change optimize-bga-escape-flow. Update Purpose after archive.
## Requirements
### Requirement: 135 routing family does not require constrain file
系统 SHALL 在执行 135 family 布线器时不创建、不传递、不依赖 `constrain.txt`。135 family 包含 `135`、`rl` 和 `rl_135` routerType。

#### Scenario: 135 route runs without constrain.txt
- **WHEN** `routerType` 为 `135`、`rl` 或 `rl_135`
- **THEN** 系统调用 135 main router 时 MUST 不把 `constrain.txt` 作为参数传入

#### Scenario: 135 route cleanup removes stale constrain.txt
- **WHEN** router work 目录中存在上一次 arc 运行遗留的 `constrain.txt`，本次执行 `routerType=135`
- **THEN** 系统 SHALL 在执行前清理该残留文件，且 135 布线路径不重新生成该文件

### Requirement: Arc routing family uses router profile constrain file
系统 SHALL 在执行 arc family 布线器时使用 arc 布线器目录中的 `constrain.txt`，并将该文件复制到 router work 目录供 native arc router 使用。arc family 包含 `arc` 和 `rl_arc` routerType。

#### Scenario: Arc route copies constrain.txt from router directory
- **WHEN** `routerType` 为 `arc` 或 `rl_arc`，且 resolved router directory 中存在 `constrain.txt`
- **THEN** 系统 SHALL 将该文件复制到 router work 目录中的 `constrain.txt` 后再调用 arc main router

#### Scenario: Arc route fails clearly when constrain.txt is missing
- **WHEN** `routerType` 为 `arc` 或 `rl_arc`，但 resolved router directory 中不存在 `constrain.txt`
- **THEN** 系统 MUST 不调用 arc main router，并返回说明缺少 `constrain.txt` 的错误

#### Scenario: Arc route passes constrain file to arc main
- **WHEN** arc family route 进入 native main router 阶段
- **THEN** 系统 SHALL 使用 `order_input.txt`、layout 文件和 `constrain.txt` 作为 arc main router 输入

### Requirement: Constraint handling is profile driven
系统 MUST 不根据用户 fanoutParams 中的 `constraints` 动态生成 arc `constrain.txt`；arc 约束文件内容 SHALL 以 router profile 自带文件为准。

#### Scenario: User constraints do not rewrite arc constrain file
- **WHEN** 用户回传 fanoutParams 中包含 `constraints` 且 `routerType=arc`
- **THEN** 系统 SHALL 保留从 router directory 复制的 `constrain.txt` 内容，不用用户 `constraints` 重写该文件

#### Scenario: Constraints remain available in fanoutParams
- **WHEN** 用户回传 fanoutParams 中包含 `constraints`
- **THEN** 系统 MAY 在结构化 `fanoutParams` 中保留该字段，但 MUST 不把它作为生成 arc `constrain.txt` 的来源

