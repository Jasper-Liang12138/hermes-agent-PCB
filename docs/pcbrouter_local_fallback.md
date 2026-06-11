# pcbrouter 局部布线兜底说明

本文记录 fanout 分支新增的 `pcbrouter -bga_local_route` 兜底链路。

## 目标

`route_bga` 仍优先使用现有 BJUT / arc / 135 / RL fanout router。若主布线器失败，并且当前平台可执行 `pcbrouter`，Agent 会自动切换到 `pcbrouter` 局部布线兜底。

## 本分支新增文件

```text
tools/pcb_local_router.py
vendor/pcbrouter/bin/pcbrouter
vendor/pcbrouter/bin/pcbrouter_aarch64
tests/fixtures/pcbrouter/inputs_402_retest_20260602.gz
tests/tools/test_pcb_local_router.py
```

`vendor/pcbrouter/bin/pcbrouter` 是 Linux x86-64 动态链接二进制，`vendor/pcbrouter/bin/pcbrouter_aarch64` 是从天翼云 `/work/home/PcbRouter/bin/pcbrouter` 复制出的 AArch64 副本。Mac / Windows 本地不会直接执行它们，单元测试使用 fake script 验证调用链路。

## 输入格式

真实命令格式：

```bash
pcbrouter board.kicad_pcb -bga_local_route bga_local_route_input.csv
```

CSV 支持两类：

```csv
net
NET_A
NET_B
```

或：

```csv
net,route_layer
NET_A,In2.Cu
NET_B,
```

当 `route_bga` 触发兜底时，adapter 会从 `fanoutParams.orderLines` 生成 CSV。若 `orderLines.layer` 能映射到 KiCad 铜层名，会写入 `route_layer`；无法确认时留空，让 `pcbrouter` 自行决定。

若原始 board 文件同目录存在同名 `.kicad_dru`，adapter 会复制给 `pcbrouter` 使用；否则 `pcbrouter` 会使用默认规则。

## 输出

运行目录：

```text
<ROUTER_WORK_DIR>/pcbrouter_local_route/
```

主要过程文件：

```text
pcbrouter_input.kicad_pcb
bga_local_route_input.csv
output/
log/
output_routed/
pcbrouter.stdout.log
pcbrouter.stderr.log
```

adapter 会在运行前预建 `output/`、`log/`、`output_routed/`，否则部分 `pcbrouter` 版本只打印成功日志但不会把 `.kicad_pcb` 落盘。

adapter 会优先寻找 `output_routed/*.kicad_pcb` 作为 `routingResult`。`output_routed/*.csv` 是 `pcbrouter` 的统计报告，不是现有前端 `importLines` 原始走线记录，因此不会作为 `importLinesFilePath` 返回。

## 配置

默认二进制路径：

```text
vendor/pcbrouter/bin/pcbrouter
```

在 Linux AArch64 上会优先选择：

```text
vendor/pcbrouter/bin/pcbrouter_aarch64
```

可用环境变量覆盖：

```bash
export PCBROUTER_BIN=/path/to/pcbrouter
export PCBROUTER_TIMEOUT_SECONDS=300
```

也支持 `PCB_ROUTER_BIN`、`PCB_LOCAL_ROUTER_BIN`、`PCBROUTER_DIR`、`PCB_ROUTER_DIR`、`PCB_LOCAL_ROUTER_DIR`。

## 测试

fake 测试：

```bash
python3 -m pytest tests/tools/test_pcb_local_router.py -q
```

天翼云真实测试建议：

```bash
cd /work/home/hermes-agent-PCB-fanout
python3 -m pytest tests/tools/test_pcb_local_router.py -q -n 0
mkdir -p /tmp/pcbrouter_real && tar -xzf tests/fixtures/pcbrouter/inputs_402_retest_20260602.gz -C /tmp/pcbrouter_real
cd /tmp/pcbrouter_real/inputs
/work/home/hermes-agent-PCB-fanout/vendor/pcbrouter/bin/pcbrouter \
  402Pin_08BGA_8L_S_01141700.kicad_pcb \
  -bga_local_route input_40nets_net_only.csv
```
