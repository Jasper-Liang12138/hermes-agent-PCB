PCB intent rule: config.ini, port, logs, package/delivery, Git, frontend debug, and agent support questions are support-chat; do not call getProjectData for them.
§
PCB fanout rule: BGA fanout/routing flow must be getProjectData -> pcb_extract_bga -> generateFanoutParams -> route.
§
PCB fanout rule: after getProjectData returns board data, do not inspect or summarize raw board data; call pcb_extract_bga with board_text="__CACHED_PROJECT_DATA__".
§
PCB fanout rule: after selectedBGA and routerType are known, call generateFanoutParams; pass only selectedBGA, routerType, and user-stated constraints.
§
PCB fanout rule: after fanoutParams exist and the user confirms execution, route is the only allowed next tool; userData must be the confirmed fanoutParams JSON string.
§
PCB reroute rule: selected-trace reroute uses drop_net -> reroute; deletion targets must come from frontend selected traces, not text guesses.
§
PCB intent rule: #全局fanout or #布线 means force the BGA fanout skill; #拆线重布 or #reroute means force the selected-trace reroute skill.
§
PCB intent rule: if the user asks a temporary explanation/chat question during a PCB flow, answer the chat question without discarding the PCB flow state.
§
PCB reroute rule: users may enter reroute before selecting traces; drop_net should ask the frontend for selected traces and report an empty-selection prompt if none are selected.
