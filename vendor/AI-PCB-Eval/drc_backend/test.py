from api import evaluate_drc_score
import json

pcb_path = r"D:\pcb_drc_demo\samples\fault_center.kicad_pcb"

res = evaluate_drc_score(pcb_path)

print("\n===== DRC SCORE SUMMARY =====")
print("ok           :", res["ok"])
print("score_name   :", res["score_name"])
print("score        :", res["score"])
print("pass         :", res["pass"])

details = res.get("details", {})
print("\n===== DETAILS =====")
print("hard_penalty :", details.get("hard_penalty"))
print("issue_count  :", details.get("hard_issue_count"))
print("rule_counts  :")
for k, v in details.get("hard_rule_counts", {}).items():
    print(f"  - {k}: {v}")

timing = res.get("artifacts", {}).get("timing", {})
print("\n===== TIMING =====")
for k, v in timing.items():
    print(f"{k}: {v:.6f}")

issues = res.get("artifacts", {}).get("issues", [])
print("\n===== ISSUES =====")
for i, issue in enumerate(issues, 1):
    print(f"{i:02d}. {issue['rule']} | {issue['message']}")

# 如果你还想看完整 JSON，再手动打开这段
# print("\n===== RAW JSON =====")
# print(json.dumps(res, indent=2, ensure_ascii=False))