#!/usr/bin/env python3
"""单一生产模型适应性测试；不修改知识库。"""
import argparse, json, re, sys
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "admin" / "outputs" / "model-adaptation-2026-07-27.json"
sys.path.insert(0, str(REPO / ".scripts"))
from llm_structured import call_json, configured_model, execution_mode
CASES = [
 {"id":"nav-1","task":"navigation","prompt":"只根据材料，输出JSON {\"kind\":\"\",\"topics\":[],\"query_terms\":[],\"actors\":[],\"status\":\"\",\"related\":[]}。不得补充材料外事实。材料：物理系量子信息科学专业本科人才培养方案，涉及量子物理与人工智能交叉培养、专业代码070206T、四年制、课程体系和学分结构，当前为草稿。"},
 {"id":"graph-1","task":"graph_relation","prompt":"只输出JSON数组，每项字段为 subject,predicate,object,evidence。材料：正式版《物理系十五五规划》替代《物理系十五五规划纲要》；量子信息科学专业培养方案与专业设置申请表相关。只提取有明确证据的导航关系。"},
 {"id":"query-1","task":"query_intent","prompt":"只输出一个JSON对象：{\"need_recall\":true,\"need_graph\":false,\"query\":\"\",\"domain\":\"admin\",\"topk\":5}。用户问题：量子信息科学专业申报进展有哪些材料？不得输出动作列表。"},
 {"id":"fact-1","task":"fact_boundary","prompt":"检索内容：文件说明四年制、最低159学分，但未说明具体课程名称。问题：该专业有哪三门核心课程？请只输出JSON {\"answer\":\"\",\"evidence_sufficient\":true或false,\"missing\":\"\"}；不得凭常识补全。"},
]
def schema(case):
 def check(obj):
  if case["task"] == "navigation": return isinstance(obj,dict) and all(k in obj for k in ("kind","topics","query_terms","actors","status","related")) and isinstance(obj["topics"],list)
  if case["task"] == "graph_relation": return isinstance(obj,list) and all(isinstance(x,dict) and set(("subject","predicate","object","evidence")) <= set(x) for x in obj)
  if case["task"] == "query_intent": return isinstance(obj,dict) and set(obj) <= {"need_recall","need_graph","query","domain","topk","graph_term"} and isinstance(obj.get("need_recall"),bool) and isinstance(obj.get("query"),str)
  return isinstance(obj,dict) and obj.get("evidence_sufficient") is False and not re.search(r"课程|力学|算法|量子态",str(obj.get("answer","")))
 return check
def assess(case,res):
 obj=res.get("parsed") if res.get("ok") else None
 valid=bool(obj is not None and schema(case)(obj))
 return {"format_ok":obj is not None,"constraint_ok":valid,"score":int(obj is not None)+int(valid),"parsed":obj,"reason":None if valid else res.get("error","schema failed")}
def main():
 ap=argparse.ArgumentParser(); ap.add_argument("--output",default=str(OUT)); args=ap.parse_args(); model=configured_model()
 result={"date":"2026-07-27","model":model,"mode":execution_mode(),"cases":[],"note":"单一生产模型适应性测试；不写 raw/wiki/graph。无 API 时由当前 agent 接管。"}
 for case in CASES:
  response=call_json(case["prompt"],schema(case),max_tokens=500,retries=1); assessment=assess(case,response)
  result["cases"].append({"case_id":case["id"],"task":case["task"],"response":response,"assessment":assessment})
  print(model,case["id"],assessment["score"],flush=True)
 Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
if __name__ == "__main__": main()
