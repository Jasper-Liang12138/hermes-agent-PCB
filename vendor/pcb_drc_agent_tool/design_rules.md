## DRC rules
### 规则总框架
#### 第 0 层：Task / Interface rules

- 当前板上哪个 BGA 是目标 BGA
- 当前检查范围是不是只包含信号网络
- power net 是否被正确过滤
- 差分对是否能被正确识别
- board 是否满足当前任务前提，比如“挖空区对应的 net 是否存在”
precheck
scope_check
pair_extract_check

#### 第 1 层：Hard connectivity / legality rules

- H1. 起始逃逸缺失

pad 没有起步 fanout。
/HR_CONNECT_PAD_NOT_ESCAPED。

- H2. 逃逸路径未完成

从 BGA pad 出发没有真正逃到 BGA 区外。
你现在已有 HR_CONNECT_BRANCH_INCOMPLETE 思路。

- H3. 逃逸拓扑非法
一个 pad 多个初始逃逸
BGA 区内 fork
多个外部终点

HR_TOPO_MULTIPLE_ESCAPE
HR_CONNECT_ESCAPE_PATH_FORK（未启用）
HR_TOPO_ENDPOINT_NOT_UNIQUE（未启用）

- H4. 几何非法
异网同层 crossing / overlap
未来可扩到最小间距明显违规、非法接触、segment 自交
你现在已经有 HR_DRC_SEGMENT_CROSSING。
- H5. 关键差分对结构损坏
这一类应该放到 hard，而不是 diff optimization。
因为如果差分对已经“失去成对关系”，那不是质量差，而是任务失败。
例如：

只布了一根，另一根没逃逸
两根线分配到完全不对称层序
一边有完整路径、一边根本没有可匹配路径

#### 第 2 层：Escape quality / optimization rules

- O1. Fanout style quality
    - inner pad should use via
    - first-hop 是否符合预期 fanout 策略
    - outer ring 是否不必要地过早打 via
- O2. Fanout geometry quality
    - via 是否靠 cell center
    - via 是否接近 45° 方向
    - 初始扇出是否朝合理方向
- O3. Manufacturing proxy quality
    - via size / drill 合理性   
    - top fanout width

#### 第 3 层：Differential rules


- D1. Pair existence / pairing integrity

差分对两根 net 都存在
两根都被识别为同一 pair
两边都从目标 BGA 成功逃逸
两边都有可抽取路径


- D2. Pair symmetry

对内总长度失配
过孔数量失配
换层次数失配
层序不对称
fanout 起始方向不对称
同组同层同边逃逸

- D3. Pair coupling quality

间距一致性
非耦合段比例
单边绕行长度
局部偏离过大
耦合中断次数
