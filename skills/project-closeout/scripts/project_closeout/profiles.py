"""Task-scoped, reversible policies. A role is separate from a keep/move decision."""
import re
from collections import Counter
from pathlib import PurePosixPath
from .core import CloseoutError, source_or_record, temp_evidence

TIERS = {
    "results": ("只留结果", "保留选定的交付件和已识别的必要依赖。"),
    "milestones": ("结果和重要节点", "另留可继续编辑的源稿、决策和关键阶段记录。"),
    "clear": ("全部清理", "连同结果一起移出当前任务，可撤销。"),
}
ROLES = {"result":"结果文件","milestone":"重要节点","process":"过程文件","unknown":"待辨认"}

def judgment(r):
    path=r["path"]; p=PurePosixPath(path.lower()); s=str(p)
    if r["kind"]!="file":return "excluded",r["reason"]
    if r.get("reason")=="你指定的最终成果":return "result","由你指定为最终成果"
    a=r.get("model_advice",{})
    if a.get("role"):
        quote=a.get("evidence","")
        if a["role"]!="unknown" and a.get("confidence",0)>=.9 and quote and quote in r.get("excerpt",""):
            return a["role"],"AI 根据文本片段判断："+a["reason"]+"；原文：“"+quote+"”"
        return "unknown","AI 没有给出足以核对的明确证据，请确认用途"
    if temp_evidence(path) or re.search(r"预览|试稿|草稿|待核|核验|试排|preview|draft|thumbnail",s):
        return "process","本地判断：位置或名称显示它可能是预览、试稿或缓存"
    if re.search(r"旧版|弃用|已撤回|作废|(?:^|[/_. -])(?:discarded|superseded)(?:[/_. -]|$)",s):
        return "process","本地判断：路径带有旧版或撤回标记"
    if re.search(r"用户确认|制作说明|分段尺寸|选定方案|决策|复盘|原始|milestone|decisions/|制作记录|源文件|关键节点|已确认|里程碑|阶段成果|尺寸表|调整说明|修订说明",s):
        return "milestone","本地判断：文件名或所在目录标明了原始材料、决策或制作过程"
    if re.search(r"最终交付|定稿|终稿|交稿版|打印版|(?:^|[/_. -])final(?:[/_. -]|$)",s):
        return "result","本地判断：路径有交付或定稿标记，未据此确认实际提交版本"
    if len(p.parts)==2 and p.parts[0] in {"outputs","成果","交付"} and not re.search(r"(?:^|[_ -])v\d",p.name) and p.suffix in {".md",".txt",".pdf",".png",".jpg",".docx",".pptx",".xlsx",".mp4",".zip"}:
        return "result","本地判断：位于成果目录顶层，格式可用于交付，尚未确认采用版本"
    if a.get("confidence",0)>=.9:
        if a.get("action")=="candidate":return "process","模型建议（未提供原句）："+a["reason"]
        if a.get("action")=="keep" and a.get("retention") in {"essential","important"}:return "milestone","模型建议（未提供原句）："+a["reason"]
    if source_or_record(path):return "milestone","本地判断：文件类型或目录提示它可能是源稿、数据或工作记录"
    return "unknown","现有内容和路径不足以判断它在任务中的用途"

def classify(r):
    return judgment(r)[0]

def closure(paths,by):
    keep=set(paths); queue=list(keep)
    while queue:
        for ref in by[queue.pop()].get("references",[]):
            if ref in by and ref not in keep:keep.add(ref);queue.append(ref)
    return keep

def build_profiles(base, scope="", overrides=None):
    by={r["path"]:r for r in base["records"]}
    inside={p for p in by if not scope or p.startswith(scope+"/")}
    if scope and not inside: raise CloseoutError("任务范围不存在")
    overrides=overrides or {}
    facts={p:judgment(r) for p,r in by.items()}
    roles={p:overrides.get(p) or fact[0] for p,fact in facts.items()}
    external=closure(set(by)-inside,by)&inside
    plans={};summaries=[]
    for tier,(label,description) in TIERS.items():
        chosen={"result"} if tier=="results" else {"result","milestone"} if tier=="milestones" else set()
        seeds={p for p in inside if roles[p] in chosen or roles[p]=="excluded"}|external
        keep=closure(seeds,by); moved=[]; amount=0
        plan=dict(base,tier=tier,scope=scope,records=[], warnings=[w for w in base["warnings"] if "--final" not in w])
        for p in sorted(inside):
            r=dict(by[p]);role=roles[p];r["role"]=role
            if r["kind"]!="file":r["decision"]="protected"
            else:
                retain=p in keep
                origin=("你已手动标为"+ROLES[role]) if p in overrides else facts[p][1]
                ref=next((x for x in r.get("referenced_by",[]) if x in keep or x not in inside),None)
                if p in external or (retain and p not in seeds):
                    why="保留：被"+("其他任务或其依赖" if p in external else "留下的文件")+"引用"+("（"+ref+"）" if ref else "")
                elif tier=="clear":why="移出：全部清理包括"+ROLES[role]
                else:why=("保留：" if retain else "移出：")+ROLES[role]+("属于" if retain else "不属于")+"本方案保留范围"
                r.update(decision="keep" if retain else "candidate",reason=why+"。"+origin)
                if not retain:moved.append(p);amount+=r["size"]
            plan["records"].append(r)
        blocked=tier!="clear" and not any(roles[p] in chosen for p in inside)
        def sample(records):
            return [{"path":r["path"],"reason":r["reason"]} for r in records[:3]]
        kept=[r for r in plan["records"] if r["decision"]=="keep"]
        outgoing=[r for r in plan["records"] if r["decision"]=="candidate"]
        priority={"result":0,"milestone":1,"process":2,"unknown":3}
        explanation={"keep":sample(sorted(kept,key=lambda r:priority[r["role"]])),
                     "move":sample(sorted(outgoing,key=lambda r:priority[r["role"]])),
                     "uncertain":sum(r["role"]=="unknown" for r in outgoing),
                     "unknown_examples":[r["path"] for r in outgoing if r["role"]=="unknown"][:3],
                     "dependencies":sample([r for r in kept if r["reason"].startswith("保留：被")]),
                     "excluded":sum(r["kind"]!="file" for r in plan["records"])}
        plans[tier]=plan
        summaries.append(dict(tier=tier,label=label,description=description,keep_count=len(inside)-len(moved),
            move_count=len(moved),move_bytes=amount,blocked=blocked,recommended=False,explanation=explanation,
            move_description="、".join(f"{ROLES[k]} {v} 项" for k,v in Counter(roles[p] for p in moved).items()),
            examples=moved[:4]))
    return plans,summaries

def overview(base,summaries):
    records=base["records"];roles=Counter(r.get("role") or classify(r) for r in records)
    return dict(file_count=sum(r["kind"]=="file" for r in records),artifact_examples=[r["path"] for r in records if (r.get("role") or classify(r))=="result"][:5],
        sentences=[f"识别到 {roles['result']} 个结果文件、{roles['milestone']} 个重要节点。",f"{roles['unknown']} 个文件还需辨认。可以在清单中指定结果、节点或过程；未指定的待辨认文件会列入移出清单。"])
