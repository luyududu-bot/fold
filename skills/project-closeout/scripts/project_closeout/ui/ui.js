"use strict";
const token=location.hash.slice(1), $=id=>document.getElementById(id);
let projects=[], tab="project", selectedProject=null, currentJob=null, result=null, tier="milestones", lastRun=null, revision=0, rows=null;
const extraKeeps=new Map();
const coverUrls=new Map();
const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
function icon(name){const n=document.createElementNS('http://www.w3.org/2000/svg','svg');n.classList.add('icon');n.setAttribute('aria-hidden','true');const use=document.createElementNS(n.namespaceURI,'use');use.setAttribute('href','#i-'+name);n.append(use);return n;}
let recent=[];try{recent=JSON.parse(localStorage.getItem("shouwei.recent")||"[]");if(!Array.isArray(recent))recent=[];}catch{recent=[];}
function coverFallback(p){const cover=node("span",undefined,"cover");cover.setAttribute("aria-hidden","true");cover.append(icon(category(p)==="chat"?"chat":p.output_examples?.length?"file":"folder"));return cover;}
function caption(p){
  const files=p.output_examples?.slice(0,2).join(" · ");if(files)return files;
  const hint=[[/建筑设计|院宅/,"建筑设计项目"],[/托福|语言学习/,"语言学习项目"],[/学术|科研/,"学术研究项目"],[/课.*作业/,"课程作业项目"]].find(([pattern])=>pattern.test(p.display_name));
  return hint?hint[1]:category(p)==="chat"?"聊天与工作文件":"项目文件与整理记录";
}
async function loadCover(p,cover){
  try{if(!coverUrls.has(p.id))coverUrls.set(p.id,fetch('/api/preview/'+p.id,{headers:{'X-Closeout-Token':token}}).then(async r=>r.ok&&r.status!==204?URL.createObjectURL(await r.blob()):null).catch(()=>null));
    const url=await coverUrls.get(p.id);if(!url||!cover.isConnected)return;const img=new Image();img.alt='';img.onload=()=>{cover.replaceChildren(img);cover.classList.add('photo');};img.src=url;
  }catch{/* A missing preview never blocks project selection. */}
}
function activity(value){if(!Number.isFinite(value)||value<=0)return '本地项目';const d=new Date(value*1000);return new Intl.DateTimeFormat('zh-CN',{month:'short',day:'numeric'}).format(d)+' 更新';}
function error(e){$("error").textContent=e.message||String(e);$("error").hidden=false;}
function clearError(){$("error").hidden=true;}
async function api(path,body){const o={headers:{"X-Closeout-Token":token}};if(body!==undefined){o.method="POST";o.headers["Content-Type"]="application/json";o.body=JSON.stringify(body);}const response=await fetch(path,o);const data=await response.json();if(!response.ok)throw new Error(data.error||"操作未完成");return data;}
function size(b){return b<1024?b+" B":b<1048576?(b/1024).toFixed(1)+" KB":(b/1048576).toFixed(1)+" MB";}
function category(p){return p.display_kind==="shared"?"project":p.display_kind;}
function renderProjects(){
  const q=$("search").value.trim().toLocaleLowerCase();
  const matches=projects.filter(p=>(q||category(p)===tab)&&[p.display_name,...p.threads.map(t=>t.title)].join(" ").toLocaleLowerCase().includes(q));
  $("cards").replaceChildren();
  matches.sort((a,b)=>{const rank=p=>recent.includes(p.id)?recent.indexOf(p.id):1e6;return rank(a)-rank(b)||(b.last_activity||0)-(a.last_activity||0);});
  matches.forEach(p=>{const card=node("button",undefined,"card");card.title=p.display_name;card.setAttribute("aria-label","分析项目："+p.display_name);
    const cover=coverFallback(p),info=node("span",undefined,"card-info"),arrow=node("span",undefined,"card-arrow");arrow.append(icon("arrow"));
    info.append(node("strong",p.display_name),node("span",caption(p)),node("small",p.threads.length?p.threads.length+" 个相关聊天":"本地项目"));
    card.append(cover,info,arrow);card.onclick=()=>choose(p);$("cards").append(card);loadCover(p,cover);});
  const label={project:"我的项目",chat:"单独聊天",folder:"其他文件夹"}[tab];$("picker-title").textContent=label;$("list-heading").textContent=q?"搜索结果":{project:"全部项目",chat:"全部聊天",folder:"全部文件夹"}[tab];
  $('empty-state').hidden=matches.length>0;
  $("list-note").textContent=q?`${matches.length} 个匹配结果`:`${matches.length} ${tab==='chat'?'个聊天':tab==='folder'?'个文件夹':'个项目'}`;
  for(const b of $("tabs").querySelectorAll("button")){b.classList.toggle("active",b.dataset.tab===tab);b.setAttribute('aria-current',b.dataset.tab===tab?'page':'false');}
}
async function load(refresh=false){clearError();const d=refresh?await api("/api/refresh",{}):await api("/api/projects");projects=d.projects;renderProjects();}
async function choose(p){
  const view=++revision;selectedProject=p;currentJob=null;result=null;rows=null;extraKeeps.clear();clearError();
  $("picker").hidden=true;$("project-view").hidden=false;$("project-title").textContent=p.display_name;$("project-stage").textContent="正在分析";
  recent=[p.id,...recent.filter(id=>id!==p.id)].slice(0,30);try{localStorage.setItem("shouwei.recent",JSON.stringify(recent));}catch{}
  document.body.classList.remove("nav-open");$("stage-count").textContent="";$("scope-line").hidden=true;
  const cover=coverFallback(p),info=node("div");info.append(node("strong",p.display_name),node("p",caption(p)));$("project-hero").replaceChildren(cover,icon("folder"),info);loadCover(p,cover);

  $("results").hidden=true;$("completed").hidden=true;$("retry").hidden=true;$("busy").hidden=false;$("progress").textContent="正在识别项目文件…";document.querySelector(".workspace").scrollTop=0;
  try{const started=await api("/api/start",{project:p.id});if(revision!==view)return;currentJob=started.job;
    while(revision===view){const d=await api("/api/jobs/"+currentJob);if(revision!==view)return;
      if(d.status==="error")throw new Error(d.error);if(d.status==="done"){showResults(d);return;}
      $("progress").textContent=d.progress;await new Promise(r=>setTimeout(r,700));}}
  catch(e){if(revision===view){$("busy").hidden=true;$("retry").hidden=false;error(e);}}
}
let visibleRows=150;
function showResults(d,refresh=false){
  if(!refresh){$("file-search").value="";$("filter").value="all";}const prev=tier, open=refresh&&$("review-panel").open;visibleRows=150;
  result=d;const failed=d.analysis?.status==="failed"||d.mode_message?.startsWith("AI 分析未完成");tier=refresh?prev:"milestones";rows=null;extraKeeps.clear();$("busy").hidden=true;$("project-stage").textContent="选择保留方案";$("results").hidden=false;$("review-panel").open=open;$("transmission").open=false;
  $("project-stage").textContent=failed?"AI 分析未完成":d.mode==="ai"?"文本片段分析完成":"本地分析完成";$("findings-title").textContent=failed?"AI 分析未完成":"整理建议";$("analysis-actions").hidden=!failed;$("decision-bar").hidden=false;$("decision-note").hidden=false;
  $("findings-text").replaceChildren(...d.overview.sentences.slice(0,1).map(t=>node("p",t)),node("p",failed?d.mode_message:"你可以只留结果，也可以留下重要节点和制作依据。"));
  $("scope-line").hidden=false;$("stage-count").textContent=" · "+d.overview.file_count+" 个文件";$("review-count").textContent="查看 "+d.overview.file_count+" 个文件";
  if(!refresh)$("limits").open=false;
  $("mode-note").textContent=d.mode_message;
  const coverage=d.analysis;$("analysis-coverage").hidden=!coverage;
  if(coverage)$("analysis-coverage").textContent=`共 ${coverage.file_count} 个文件：提取文本 ${coverage.text_count} 项，AI 判断文本片段 ${coverage.ai_file_count} 项；${coverage.unread_count} 项未读取内容`+(coverage.truncated_count?`，${coverage.truncated_count} 项文本仅部分读取`:"")+`。未读项的去留以所选方案为准。`+(coverage.budget_skipped_count?`其中 ${coverage.budget_skipped_count} 项受读取额度限制。`:"");$("transmission").hidden=!(d.has_transmission??(d.mode==="ai"));$("report-location").textContent="分析报告："+d.report_path;
  $("scope").replaceChildren(...["",...d.scopes].map(v=>new Option(v||"整个项目",v)));$("scope").value=d.scope;
  $("comparison-note").textContent="用途不明的文件，可在清单中额外保留。";
  renderPlans();if(open||$("limits").open) loadRows();

}
async function revise(change){const view=revision;$("results").inert=true;try{const d=await api("/api/task",{job:currentJob,scope:result.scope,...change});if(view===revision)showResults(d,true);}catch(e){error(e);$("scope").value=result.scope;if($("review-panel").open)await loadRows();}finally{$("results").inert=false;}}
$("scope").onchange=()=>revise({scope:$("scope").value});
function activeProfile(){return result.profiles.find(p=>p.tier===tier);}
function exclusions(){const candidates=rows?new Set(rows.filter(r=>r.decision==="candidate").map(r=>r.path)):null;return [...extraKeeps.keys()].filter(p=>!candidates||candidates.has(p));}
function impact(){const p=activeProfile(),kept=exclusions();return{keep:p.keep_count+kept.length,move:p.move_count-kept.length,bytes:Math.max(0,p.move_bytes-kept.reduce((n,path)=>n+(extraKeeps.get(path)||0),0))};}
function renderPlans(){
  $("plans").replaceChildren();
  for(const p of result.profiles){const card=node("button",undefined,"plan-card"+(p.tier===tier?" selected":""));card.type="button";card.dataset.tier=p.tier;card.setAttribute("role","radio");card.setAttribute("aria-checked",String(p.tier===tier));card.tabIndex=p.tier===tier?0:-1;
    const marker=node("span",undefined,"plan-marker");if(p.tier===tier)marker.append(icon("check"));const copy=node("span",undefined,"plan-copy");
    copy.append(node("strong",p.label),node("small",p.description));const name=node("span",undefined,"plan-name");name.append(marker,copy);
    card.append(name,node("span",p.keep_count+" 项","qty"),node("span",p.move_count+" 项","qty"));
    card.addEventListener("click",()=>selectTier(p.tier));$("plans").append(card);}
  updateDecision();
}
async function selectTier(value){visibleRows=150;tier=value;rows=null;extraKeeps.clear();clearError();renderPlans();$('plans').querySelector(`[data-tier="${value}"]`)?.focus({preventScroll:true});if($("review-panel").open)await loadRows();}
function explainChoice(){
  const p=activeProfile(), e=p.explanation, i=impact(), extra=new Set(exclusions());
  $("explanation-title").textContent="为什么这样整理 · "+p.label;
  $("explanation-lead").textContent=tier==="clear"?
    "你选择结束这项任务，交付件也在移出范围内。移出不表示文件没有价值。":
    tier==="results"?"这份清单以交付为准。结果和已发现的依赖会留下；未被引用的阶段稿会移出。":
    "这份清单既保留交付件，也保留有助于继续修改或说明决策的节点。";
  const box=$("explanation-files");box.replaceChildren();
  const add=(title,items)=>{if(!items.length)return;const group=node("div");group.append(node("h3",title));for(const r of items.slice(0,2)){const line=node("p");line.append(node("strong",r.path),document.createTextNode("　"+r.reason));group.append(line);}box.append(group);};
  add("会留下", [...extra].slice(0,2).map(path=>({path,reason:"你已选择额外保留。"})).concat(e.keep));
  add("为避免断开引用",e.dependencies);
  let move=e.move.filter(r=>!extra.has(r.path));if(!move.length&&i.move&&rows)move=rows.filter(r=>r.decision==="candidate"&&!extra.has(r.path));add("会移出",move);
  const n=e.uncertain-(rows||[]).filter(r=>r.role==="unknown"&&extra.has(r.path)).length;
  $("explanation-caution").textContent=(n?`移出项中有 ${n} 个文件用途尚不明确，建议先核对。`:"")+(e.excluded?`有 ${e.excluded} 项受保护，未纳入清理。`:"")+"依赖检查只识别可读文本中的路径，未验证设计工程能否独立打开。确认后移入 Mac 废纸篓，不会清空。";
  $("inspect-unknown").hidden=n<=0;
}
$("inspect-unknown").onclick=async()=>{$("filter").value="unknown";$("file-search").value="";$("review-panel").open=true;visibleRows=150;if(rows)renderRows();else await loadRows();$("review-panel").scrollIntoView({block:"start"});};
function updateDecision(){explainChoice();const p=activeProfile(),i=impact();
  if(result.analysis?.status!=="failed"){
    $("findings-title").textContent={milestones:"保留成果和重要过程",results:"只保留最终成果",clear:"清理这项任务的文件"}[tier];
    $("findings-text").lastElementChild.textContent=p.description;
  }
  const unknown=p.explanation.uncertain-(rows||[]).filter(r=>r.role==="unknown"&&extraKeeps.has(r.path)).length;
  $("comparison-note").textContent=unknown?`待移出文件中有 ${unknown} 项用途未明，可在清单中核对并保留。`:"可在文件清单中核对判断依据，并额外保留文件。";
$("chosen-title").textContent="采用“"+p.label+"”";$("chosen-impact").textContent=`保留 ${i.keep} 项，移出 ${i.move} 项（${size(i.bytes)}）`+(extraKeeps.size?` · 已额外保留 ${extraKeeps.size} 项`:"");$("apply").disabled=i.move===0||p.blocked;if(p.blocked)$("chosen-impact").textContent="请先在文件清单中标出要留下的结果或节点。";$("apply").textContent=i.move?"按此方案整理":"全部保留，无需整理";}
async function loadRows(){const view=revision,selected=tier;try{const d=await api(`/api/records/${currentJob}/${selected}`);if(view!==revision||selected!==tier)return;rows=d.records;$("warnings").replaceChildren(...d.warnings.map(t=>node("li",t)));renderRows();}catch(e){error(e);}}
function renderRows(){
  if(!rows)return;const f=$("filter").value,q=$("file-search").value.trim().toLowerCase(),filtered=rows.filter(r=>r.path.toLowerCase().includes(q)&&(f==="all"||(f==="unknown"?r.role==="unknown"&&r.decision==="candidate"&&!extraKeeps.has(r.path):f==="keep"?r.decision!=="candidate":r.decision==="candidate")));$("files").replaceChildren();
  for(const r of filtered.slice(0,visibleRows)){const tr=node("tr"),check=node("td");if(r.decision==="candidate"){const cb=node("input");cb.type="checkbox";cb.checked=!extraKeeps.has(r.path);cb.setAttribute("aria-label","移出 "+r.path);cb.addEventListener("change",()=>{cb.checked?extraKeeps.delete(r.path):extraKeeps.set(r.path,r.size);updateDecision();renderRows();});check.append(cb);}
    const role=node("td");if(r.kind==="file"){const sel=node("select");sel.setAttribute("aria-label","文件归类："+r.path);for(const [v,l] of Object.entries({result:"结果文件",milestone:"重要节点",process:"过程文件",unknown:"待辨认",auto:"恢复自动判断"})){const op=new Option(l,v);op.disabled=v==="unknown";sel.add(op);}sel.value=r.role;sel.onchange=()=>revise({path:r.path,role:sel.value});role.append(sel);}else role.textContent="不在清理范围";
    tr.append(check,node("td",r.path),role,node("td",extraKeeps.has(r.path)?"额外保留":r.decision==="candidate"?"移入废纸篓":"保留"),node("td",extraKeeps.has(r.path)?"你要求留下这个文件。":r.reason),node("td",size(r.size)));$("files").append(tr);}
  $("more-rows").hidden=filtered.length<=visibleRows;$("more-rows").onclick=()=>{visibleRows+=150;renderRows();};$("row-limit").textContent=`共 ${filtered.length} 项`+(filtered.length>visibleRows?`，已显示前 ${visibleRows} 项。`:"");
}
$("tabs").addEventListener("click",e=>{const b=e.target.closest("button");if(b?.dataset.tab){tab=b.dataset.tab;$('search').value='';$('back').click();renderProjects();}});$("search").addEventListener("input",renderProjects);$("refresh").addEventListener("click",()=>load(true).catch(error));
$("back").addEventListener("click",()=>{revision++;currentJob=null;$("project-view").hidden=true;$("picker").hidden=false;clearError();renderProjects();});$("brand").addEventListener("click",e=>{e.preventDefault();tab='project';$('search').value='';$("back").click();});
$("review-panel").addEventListener("toggle",()=>{if($("review-panel").open&&!rows)loadRows();});$("filter").addEventListener("change",renderRows);$("file-search").oninput=()=>{visibleRows=150;renderRows();};
$('plans').addEventListener('keydown',e=>{const order=result?.profiles.map(p=>p.tier);if(!order||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(e.key))return;e.preventDefault();const index=order.indexOf(tier),next=e.key==='Home'?0:e.key==='End'?order.length-1:(index+(['ArrowRight','ArrowDown'].includes(e.key)?1:-1)+order.length)%order.length;selectTier(order[next]);});
$("transmission").addEventListener("toggle",async()=>{if($("transmission").open){try{const d=await api("/api/transmission/"+currentJob);$("payload-text").textContent=JSON.stringify(d.manifest,null,2);}catch(e){error(e);}}});
$("apply").addEventListener("click",async()=>{clearError();const scope=result.scope||selectedProject.display_name;const i=impact();if(!window.confirm(`「${scope}」采用“${activeProfile().label}”：移出 ${i.move} 项，保留 ${i.keep} 项。确认移入 Mac 废纸篓？`))return;const view=revision;$("apply").disabled=true;$("apply").textContent="正在整理…";try{const d=await api("/api/apply",{job:currentJob,tier,version:result.version||0,confirm_clear:tier==="clear",keep_extra:exclusions()});if(view!==revision)return;lastRun=d.run;$("results").hidden=true;$("completed").hidden=false;$("project-stage").textContent="整理完成";$("complete-title").textContent=d.count?(d.destination==="trash"?"已移到废纸篓":"未确认移到废纸篓"):"全部文件已保留";$("complete-description").textContent=d.count?(d.destination==="trash"?`${d.count} 项文件已移到 Mac 废纸篓，其他文件留在原处。清空前可以撤销。`:"系统未确认转移成功。本次记录已保留，可撤销后检查废纸篓权限。"):"这份方案没有需要移出的文件。";$("undo").hidden=!lastRun;$("undo").disabled=false;}catch(e){if(view===revision){updateDecision();error(e);}}});
$("undo").addEventListener("click",async()=>{clearError();$("undo").disabled=true;try{await api("/api/undo",{run:lastRun});$("complete-title").textContent="文件已经恢复";$("complete-description").textContent="本次移出的文件已恢复到原来的位置。";}catch(e){$("undo").disabled=false;error(e);}});
$("analysis-settings").addEventListener("click",()=>$("settings").click());
for(const id of ["retry","reanalyze","analysis-retry"])$(id).addEventListener("click",()=>choose(selectedProject));
let savedConfig=null;
function updateSettingsMode(){
  const on=$("enable-ai").checked;
  $("api-settings").hidden=!on;
  $("analysis-mode").textContent=on?"必要的文件片段会发送给所选服务商，可能产生费用。":"使用本地规则分析，文件内容不会发送给服务商。";
}
function setKeyHint(){
  const hasKey=savedConfig?.has_key && $("provider").value===savedConfig.provider;
  $("api-key").placeholder=hasKey?"已配置，留空沿用现有 Key":"输入此服务商的 API Key";
}
$("settings").addEventListener("click",async()=>{try{
  const p=await api("/api/settings");savedConfig=p;
  $("enable-ai").checked=p.enabled;$("provider").value=p.provider;$("model").value=p.model;
  $("api-key").value="";$("remember-key").checked=p.remember;$("settings-error").textContent="";
  setKeyHint();updateSettingsMode();$("settings-dialog").showModal();
}catch(e){error(e);}});
$("enable-ai").addEventListener("change",updateSettingsMode);
$("provider").addEventListener("change",()=>{$("api-key").value="";$("model").value=$("provider").value===savedConfig?.provider?savedConfig.model:"";setKeyHint();});
$("cancel-settings").addEventListener("click",()=>{$("api-key").value="";$("settings-dialog").close();});
$("settings-dialog").addEventListener("close",()=>{$("api-key").value="";});
$("settings-form").addEventListener("submit",async e=>{e.preventDefault();$("save-settings").disabled=true;$("settings-error").textContent="";try{const p=await api("/api/settings",{enabled:$("enable-ai").checked,provider:$("provider").value,model:$("model").value.trim(),key:$("enable-ai").checked?$("api-key").value.trim():"",remember:$("remember-key").checked});$("api-key").value="";savedConfig=p;$("settings-dialog").close();}catch(e){$("settings-error").textContent=e.message;}finally{$("save-settings").disabled=false;}});
$("search-nav").onclick=()=>{$("back").click();document.body.classList.remove("nav-open");$("search").focus();};
$("more").onclick=()=>$("settings").click();
function navToggle(){if(matchMedia("(max-width:700px)").matches)document.body.classList.toggle("nav-open");else document.body.classList.toggle("nav-fold");}
$("collapse").onclick=()=>{navToggle();$("top-home").focus();};
$("top-home").onclick=()=>{navToggle();$("collapse").focus();};
$("limits").ontoggle=()=>{if($("limits").open&&!rows)loadRows();};
$("tabs").addEventListener("click",()=>document.body.classList.remove("nav-open"));
document.addEventListener("keydown",e=>{if(e.key==="Escape")document.body.classList.remove("nav-open");});
load().catch(error);
