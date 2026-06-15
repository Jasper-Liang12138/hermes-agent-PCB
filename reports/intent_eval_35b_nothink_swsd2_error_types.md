# 35B no-think + SWSD2 错误类型统计

数据来源：`reports/intent_eval_35b_nothink_full/swsd2_eval.json`  
统计时间：2026-06-14

## 总体

| 指标 | 数值 |
|---|---:|
| 总样本数 | 500 |
| 正确数 | 433 |
| 错误数 | 67 |
| 准确率 | 86.60% |

## 一、按类别统计

| 类别 | 错误数 | 类内总数 | 说明 |
|---|---:|---:|---|
| chat_consultation | 20 | 100 | 咨询类问题被过度拉进 PCB workflow |
| chat_analysis | 20 | 30 | 分析/解释类问题最容易被误判成 fanout/reroute 入口 |
| unclear_fuzzy | 9 | 30 | 模糊短句的边界仍不稳定 |
| chat_general | 7 | 30 | 礼貌寒暄、泛化表达仍有误判 |
| flow_select | 3 | 25 | 选择阶段的短输入识别不够稳 |
| pcb_entry_fanout | 3 | 75 | 少量真实 fanout 入口被压成 unclear/chat |
| flow_invalid | 2 | 15 | 流程内非法输入仍有少量误判 |
| cancel | 1 | 20 | 少量取消类被误成 unclear |
| edge_fill | 1 | 5 | 边缘样本仍有漏判 |
| flow_confirm | 1 | 25 | 确认阶段仍有极少数边界误判 |

## 二、按 flow_state 统计

| flow_state | 错误数 | 说明 |
|---|---:|---|
| idle | 60 | 绝大多数错误发生在空闲态入口判断 |
| wait_selection | 4 | 主要是 `U23` 一类目标选择短句 |
| wait_confirm | 3 | 主要是弱确认词被误读 |

## 三、按错误维度统计

| 错误维度 | 数量 | 说明 |
|---|---:|---|
| intent mismatch | 67 | 所有错误都至少体现为 intent 错 |
| route_mode mismatch | 40 | 40 条发生了 `chat/pcb` 路由错分 |
| bootstrap mismatch | 26 | 26 条伴随错误触发或漏触发 `getProjectData` 倾向 |

## 四、核心错误模式

| 错误模式 | 数量 | 典型现象 |
|---|---:|---|
| chat 被误路由到 pcb | 38 | 本应 `route_mode=chat`，结果进了 PCB workflow |
| 分析/咨询类被过触发为 PCB | 35 | “分析利弊”“分几步”“列出 BGA”被当成入口 |
| chat 被过保守判成 unclear | 10 | 本可正常 chat，结果落成 unclear |
| unclear 被抹平成 chat | 6 | 本应保留模糊态，结果直接按 chat 返回 |
| 选择阶段短句漏判 | 3 | `U55`、`U23` 这类输入未识别成 `pcb_select_target` |
| 弱确认被误判为 confirm | 3 | “好的”“嗯”一类被推进流程 |
| unclear 被过触发成 pcb_entry | 2 | 模糊输入被硬拉入 PCB 入口 |
| 礼貌 chat 被误读成 cancel | 2 | “麻烦你谢谢”类被误归一成 cancel |

## 五、最常见 intent 混淆

| 期望 intent | 实际 intent | 数量 | 说明 |
|---|---|---:|---|
| chat | pcb_entry | 22 | 最主要问题，fanout 入口触发过强 |
| chat | pcb_reroute_selected | 12 | reroute 入口触发过强 |
| chat | unclear | 10 | 对普通咨询过于保守 |
| unclear | chat | 6 | 模糊语义没有保留下来 |
| pcb_select_target | unclear | 3 | 选择态短输入识别不足 |
| unclear | pcb_confirm_route | 3 | wait_confirm 的弱确认词约束还不够严 |

## 六、最常见 policy reason

| reason | 数量 | 含义 |
|---|---:|---|
| swsd2_fanout_entry | 17 | fanout 入口规则偏积极 |
| swsd2_reroute_entry | 13 | reroute 入口规则偏积极 |
| swsd2_raw_task_intent | 9 | 过度相信 raw task intent |
| swsd2_raw_unclear | 8 | 对 chat/unclear 边界处理偏保守 |
| swsd2_chat | 6 | 把 unclear 直接抹平为 chat |
| swsd2_invalid_selection_turn | 4 | selection 阶段对短输入支持不够 |
| swsd2_fuzzy_idle | 4 | idle 模糊输入分流仍不稳 |

## 七、错误画像总结

| 排名 | 错误画像 | 结论 |
|---:|---|---|
| 1 | 分析/咨询问题被识别成 PCB 执行入口 | 当前最大误差源，集中在 idle |
| 2 | 模糊短句的 `chat / unclear / confirm` 边界不稳定 | 第二大误差源 |
| 3 | 选择阶段短 token 输入支持不够 | 影响较小，但很集中、可定向修 |
| 4 | polite / defer_execution 类语句被误读 | 说明 meta/control intent 还需更强约束 |

## 八、典型错例

| 样本 | 期望 | 实际 | 错误类型 |
|---|---|---|---|
| “嗯，我不确定要不要做reroute，先帮我分析利弊（仅说明）” | `chat/chat` | `pcb_entry/pcb` | 分析类问句被过触发为 PCB |
| “走线太密了，能重布吗（先别执行）” | `chat/chat` | `pcb_reroute_selected/pcb` | defer_execution 未能阻止 reroute 入口 |
| “麻烦你拆线重布一般分几步？” | `chat/chat` | `pcb_reroute_selected/pcb` | 咨询类被误当成执行入口 |
| “帮我U55谢谢” | `pcb_select_target/pcb` | `unclear/pcb` | selection 阶段短输入漏判 |
| “麻烦你好的（先别执行）” | `unclear/pcb` | `pcb_confirm_route/pcb` | 弱确认词被误判为 confirm |
| “麻烦你谢谢（先别执行）” | `chat/chat` | `cancel/chat` | 礼貌语被误读为 cancel |

## 九、直接结论

| 方向 | 优先级 | 原因 |
|---|---:|---|
| 压低 `idle` 状态下的 fanout/reroute 入口触发阈值 | P0 | 60/67 错误都发生在 idle |
| 加强“分析/仅说明/不要调用工具/先别执行”对 chat 的保护 | P0 | 35 条过触发直接来自分析咨询类 |
| 强化 `wait_confirm` 的弱确认词过滤 | P1 | 3 条集中误判，可直接修 |
| 强化 `wait_selection` 的器件名短输入识别 | P1 | 3 条集中漏判，收益直接 |
| 区分 polite chat 与 cancel | P2 | 数量少，但体验上刺眼 |

