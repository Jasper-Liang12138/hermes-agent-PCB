# 5 PreCheck模块

## 5.1 模块目标

PreCheck模块位于PCB解析之后、规则检查之前，用于完成目标BGA识别、网络预处理和差分对预识别工作，为后续规则检查提供统一输入。

整体流程如下：

```text
Board
 ↓
Target BGA识别
 ↓
Signal Net过滤
 ↓
Candidate Diff Pair识别
 ↓
PreCheck Summary生成
```

---

## 5.2 Target BGA识别

### 设计目的

对于包含多个BGA器件的PCB，需要确定当前检查对象。

支持两种模式：

#### 指定模式

```bash
--target-bga U67
```

直接指定目标器件。

#### 自动模式

若未指定目标器件：

```text
统计所有BGA器件Pad数量
选择Pad数量最多的BGA
```

原因：

```text
Pad数量最多的器件通常为CPU、FPGA或SOC，
也是逃逸布线最复杂的区域。
```

---

## 5.3 Signal Net过滤

### 设计目的

Hard Rule主要针对信号网络。

以下网络通常不参与检查：

```text
GND
AGND
DGND
VCC
1V8
1V2
3V3
VBAT
```

因此需要进行网络预过滤。

### 输出

```python
board.signal_nets
board.filtered_out_nets
```

---

## 5.4 差分对候选识别

### 支持格式

```text
PCIE_TX0_P / PCIE_TX0_N

USB_DP / USB_DN

CLK+ / CLK-

DDR3_EDQSP_2 / DDR3_EDQSN_2
```

### 输出

```python
board.candidate_diff_pairs
```

用于后续差分规则检查。

---

## 5.5 PreCheck输出

最终生成：

```python
board.precheck_summary
```

包含：

```json
{
  "target_bga": "U67",
  "target_bga_pad_count": 900,
  "all_net_count": 1800,
  "signal_net_count": 1400,
  "filtered_out_net_count": 400,
  "candidate_diff_pair_count": 62
}
```

---

## 5.6 当前实现能力

当前PreCheck已经具备：

### BGA自动识别

支持：

```text
单BGA场景
多BGA场景
指定BGA场景
```

### 信号网络过滤

支持：

```text
电源网络过滤
地网络过滤
非命名网络过滤
```

### 差分对自动识别

支持：

```text
PCIe
USB
SATA
DDR
SERDES
通用P/N命名
```

---

## 5.7 当前存在的问题

### 问题1：电源网络识别依赖命名规则

当前主要通过：

```text
GND
VCC
PWR
1V8
3V3
```

等关键字进行判断。

对于特殊命名网络：

```text
VDD_CORE
AVCC_PLL
```

存在误分类可能。

---

### 问题2：无法读取Net Class

当前未解析：

```text
NetClass
```

信息。

因此无法直接利用：

```text
线宽
线距
差分约束
```

进行分类。

---

### 问题3：仅支持单目标BGA

当前规则检查对象为：

```python
board.target_bga
```

多BGA协同检查尚未实现。

---

### 问题4：差分对识别依赖命名

当前采用：

```text
P/N
+/-
DP/DN
```

规则识别。

尚不能通过拓扑结构自动推断差分对关系。

---

## 5.8 后续优化方向

### 电源网络配置文件

新增：

```json
power_nets.json
```

实现：

```text
Signal Net
Power Net
Ground Net
```

显式分类。

---

### 多BGA检查

支持：

```text
单BGA模式
多BGA模式
指定BGA模式
```

三种工作模式。

---

### 差分对配置优先级

建立：

```text
外部配置 > 自动识别
```

机制。

减少误识别问题。

---

### Net Class解析

直接读取：

```text
NetClass
```

实现与EDA规则一致的网络分类体系。

---

# 6 Hard Rule检查

## 6.1 模块目标

Hard Rule用于检查会直接导致BGA逃逸布线失败或产生明显设计错误的问题。

其特点为：

```text
必须满足
发现即报错
```

当前已启用四条规则：

```text
HR_CONNECT_PAD_NOT_ESCAPED
HR_TOPO_MULTIPLE_ESCAPE
HR_DRC_SEGMENT_CROSSING
HR_CONNECT_BRANCH_INCOMPLETE
```

---

## 6.2 当前实现能力

### P1 Pad逃逸检查

检查：

```text
BGA Pad是否存在初始逃逸连接
```

发现：

```text
未连接Segment
```

则报错。

---

### P2 多逃逸路径检查

检查：

```text
Pad是否存在多个初始逃逸方向
```

发现：

```text
多个Escape Choice
```

则报错。

---

### P7 Segment交叉检查

检查：

```text
同层
异网
Segment
```

是否发生交叉或重叠。

---

### H3 不完整逃逸路径检查

检查：

```text
Pad发出的逃逸路径
```

是否成功离开BGA区域。

若路径中断则报错。

---

## 6.3 当前未启用规则

### H4 多终点检查

目标：

```text
一个Pad只能对应一个逃逸终点
```

当前已实现但未启用。

---

### H5 Fork检查

目标：

```text
逃逸路径不能在BGA内部产生分叉
```

当前已实现但未启用。

---

## 6.4 当前存在的问题

### 不检查完整网络连通性

目前检查：

```text
是否逃出BGA
```

不检查：

```text
最终是否连接到目标器件
```

---

### 不检查网络拓扑正确性

例如：

```text
逃逸后连接到错误网络
```

当前无法识别。

---

### 不检查过孔切层合理性

例如：

```text
Layer Transition
```

目前未纳入检查。

---

## 6.5 后续规划

新增：

```text
H4 Endpoint检查
H5 Fork检查
完整连通性检查
目标器件到达检查
```

构建完整逃逸路径验证体系。

---

# 7 Optimization Rule检查

## 7.1 模块目标

Optimization Rule用于检查布线质量。

特点：

```text
非致命错误
用于优化建议
不影响Hard Pass
```

---

## 7.2 当前实现能力

### OR_INNER_PAD_PREFER_VIA

检查：

```text
内圈Pad是否优先使用Via逃逸
```

---

### OR_VIA_NOT_AT_CELL_CENTER

检查：

```text
Fanout Via是否位于Cell Center附近
```

---

### OR_VIA_NOT_45_DEG

检查：

```text
Via逃逸方向是否接近45°
```

---

### OR_FANOUT_VIA_SIZE_INVALID

检查：

```text
Via尺寸
Drill尺寸
```

是否符合Pitch要求。

---

## 7.3 当前存在的问题

### 缺少线宽优化规则

已实现：

```text
OR_TOP_FANOUT_WIDTH_TOO_SMALL
```

但未启用。

---

### 缺少拥塞优化检查

未检查：

```text
通道利用率
拥塞区域
```

---

### 缺少层分配优化

未检查：

```text
逃逸层是否合理
```

---

## 7.4 后续规划

新增：

```text
通道利用率检查
拥塞检查
层分配检查
逃逸方向一致性检查
```

---

# 8 Differential Rule检查

## 8.1 模块目标

Differential Rule用于检查高速差分对布线质量。

规则来源：

```text
diff_pairs.json
routing_rules.json
diff_groups.json
```

---

## 8.2 当前实现能力

### D1 差分对网络存在性检查

检查：

```text
P端网络
N端网络
```

是否存在于PCB中。

---

### D2 Via数量匹配检查

检查：

```text
P侧Via数量
N侧Via数量
```

是否一致。

---

### D3 长度匹配检查

检查：

```text
Length Match
```

是否满足：

```json
tolerance_mm
```

约束。

---

### D4 差分线宽检查

检查：

```text
Segment Width
```

是否满足：

```json
width_mm
width_tol_mm
```

要求。

---

### D5 差分间距检查

检查：

```text
Pair Gap
```

是否满足：

```json
pair_gap_mm
pair_gap_tol_mm
```

要求。

---

## 8.3 当前存在的问题

### 未检查Layer Transition一致性

例如：

```text
P侧换层
N侧未换层
```

无法识别。

---

### 未检查Skew

未检查：

```text
Propagation Delay
```

差异。

---

### 未检查Group Length Match

diff_groups目前仅完成解析。

尚未参与规则检查。

---

### 未检查Phase Matching

例如：

```text
耦合段长度差异
```

当前无法检测。

---

## 8.4 后续规划

新增：

```text
D6 Layer Transition Match
D7 Group Length Match
D8 Skew Check
D9 Phase Match
```

构建完整高速差分规则体系。

---

# 9 外部约束系统

## 9.1 设计目标

实现：

```text
规则与代码解耦
```

使不同项目能够通过配置文件快速切换约束。

---

## 9.2 diff_pairs.json

定义：

```text
差分对关系
```

示例：

```json
{
  "name":"CLK",
  "p_net":"CLK+",
  "n_net":"CLK-"
}
```

---

## 9.3 routing_rules.json

定义：

```text
线宽
间距
长度匹配
```

规则。

---

## 9.4 diff_groups.json

定义：

```text
差分组
```

例如：

```text
CLK Group
DDR Group
PCIe Group
```

---

## 9.5 当前限制

目前：

```text
diff_pairs
routing_rules
```

已实际参与检查。

而：

```text
diff_groups
```

尚未真正参与规则验证。

---

# 11 当前能力总结

## 已实现能力

### PCB解析

支持：

```text
Net
Pad
Via
Segment
Module
```

解析。

---

### BGA逃逸检查

支持：

```text
Pad逃逸
多分支
交叉
不完整逃逸
```

检查。

---

### 差分规则检查

支持：

```text
长度
线宽
间距
Via数量
```

检查。

---

### 优化规则检查

支持：

```text
Via位置
Via尺寸
45°逃逸
```

检查。

---

### 评分系统

支持：

```text
Hard Score
Hard Pass
```

生成。

---

### JSON输出

支持：

```text
board_result
issue_report
ui_payload
```

输出。

---

## 当前工具边界

暂不支持：

```text
SI分析
PI分析
阻抗分析
时序分析
串扰分析
EMI分析
```

---

## 当前整体成熟度评估

```text
PCB解析           ★★★★★
BGA逃逸检查       ★★★★☆
差分规则检查      ★★★☆☆
优化规则检查      ★★★☆☆
高速设计规则      ★★☆☆☆
AI闭环支持        ★★★★☆
```

当前工具已经具备：

```text
大模型BGA布线结果评测
规则定位
错误解释
质量评分
```

能力，可作为后续AI布线系统的验证与评估核心模块。
