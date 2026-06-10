PCB intent rule: config.ini, model_config, API key, port, logs, package/delivery, Git, frontend debug, and agent support questions are support-chat; do not call getProjectData or PCB tools for them.
§
PCB intent rule: conceptual questions such as "what is fanout/reroute/RL/arc/135" are chat; answer briefly and do not reset any active PCB flow.
§
PCB intent rule: #逃逸布线 or #全局fanout means force the BGA fanout skill. "对 U5 做逃逸布线", "给 U5 fanout", or "U5 扇出" also means BGA fanout with requestedBGA=U5.
§
PCB intent rule: #reroute or #拆线重布 means force the reroute skill. Natural language such as "把我框选的线重新布一下" also means reroute.
§
PCB intent rule: during an active PCB flow, distinguish three middle intents: temporary chat keeps flow_state; explicit cancel/exit/stop resets flow_state; explicit task switch resets current flow and enters the requested skill.
§
PCB fanout rule: BGA fanout flow is getProjectData -> pcb_extract_bga -> generateFanoutParams -> route -> importLines.
§
PCB fanout rule: after getProjectData returns board data, do not inspect or summarize raw board data; call pcb_extract_bga with board_text="__CACHED_PROJECT_DATA__".
§
PCB fanout rule: after selectedBGA and routerType are known, call generateFanoutParams; pass only selectedBGA, routerType, module choice, and user-stated constraints.
§
PCB fanout rule: after fanoutParams exist and the user confirms execution, route is the only allowed next routing tool; userData must be the confirmed fanoutParams JSON string. Do not regenerate fanoutParams.
§
PCB fanout rule: if the frontend sends complete fanoutParams as JSON in message content during wait_confirm, treat it as confirmation and run route directly.
§
PCB reroute rule: reroute flow is deleteTracesForRerouting -> reroute -> DRC -> importLines.
§
PCB reroute rule: deletion and missing_routes must come from frontend tool-results, not from model guesses. If no selected trace data is returned, ask the user to select traces.
§
PCB reroute rule: reroute model prompt should use frontend returned missing_routes and projectData; do not invent nets, pins, endpoints, or coordinates.
§
PCB import rule: fanout imports router output files; 135/RL-135 use line.out and arc/RL-arc use ARC_output.txt. Reroute imports only lightweight *_reroute_line.out, never a full layout file.
§
PCB safety rule: if DRC/import validation fails, explain the failing stage and do not call importLines.
