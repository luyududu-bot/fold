"use strict";
const token=location.hash.slice(1), $=id=>document.getElementById(id);
let projects=[], tab="project", selectedProject=null, currentJob=null, result=null, tier="milestones", lastRun=null, revision=0, rows=null;
const extraKeeps=new Map();
const coverUrls=new Map();
const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
function icon(name){const n=document.createElementNS('http://www.w3.org/2000/svg','svg');n.classList.add('icon');n.setAttribute('aria-hidden','true');const use=document.createElementNS(n.namespaceURI,'use');use.setAttribute('href','#i-'+name);n.append(use);return n;}
function fileType(name){const suffix=name.includes('.')?name.split('.').pop().toUpperCase():'';return suffix&&suffix.length<=8?suffix:'FILE';}
function coverFallback(p,index){const cover=node('span',undefined,'cover');cover.dataset.tone=String(index%4);cover.setAttribute('aria-hidden','true');
  const examples=(p.output_examples||[]).slice(0,3),stack=node('span',undefined,'file-stack'+(examples.length?'':' no-files'));
  if(examples.length){for(let i=0;i<3;i++){const name=examples[i%examples.length],paper=node('span',undefined,'mini-paper');paper.append(node('span',fileType(name),'paper-type'),node('span',undefined,'paper-lines'),node('span',undefined,'paper-block'),node('span',name,'paper-file'));stack.append(paper);}}
  else stack.append(node('span',undefined,'folder-sculpture'));
  cover.append(stack,node('span',examples.length?'项目文件':'本地项目','cover-type'));return cover;
}
async function loadCover(p,cover){
  try{if(!coverUrls.has(p.id))coverUrls.set(p.id,fetch('/api/preview/'+p.id,{headers:{'X-Closeout-Token':token}}).then(async r=>r.ok&&r.status!==204?URL.createObjectURL(await r.blob()):null).catch(()=>null));
    const url=await coverUrls.get(p.id);if(!url||!cover.isConnected)return;const img=new Image();img.alt='';img.onload=()=>{cover.querySelector('.file-stack')?.remove();cover.prepend(img);cover.querySelector('.cover-type').textContent='本地图片预览';};img.src=url;
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
  const matches=projects.filter(p=>category(p)===tab&&[p.display_name,...p.threads.map(t=>t.title)].join(" ").toLocaleLowerCase().includes(q));
  $("cards").replaceChildren();
  matches.forEach((p,index)=>{const card=node('button',undefined,'card');card.type='button';card.title=p.display_name;card.setAttribute('aria-label','分析项目：'+p.display_name);
    const cover=coverFallback(p,index),info=node('span',undefined,'card-info'),title=node('span',undefined,'card-title-row'),arrow=node('span',undefined,'card-arrow');arrow.append(icon('arrow'));title.append(node('strong',p.display_name,'card-title'),arrow);
    const files=(p.output_examples||[]).join(' · '),meta=node('span',undefined,'card-meta'),kind=node('span');kind.append(node('span',undefined,'meta-dot'),document.createTextNode(p.threads.length?`${p.threads.length} 个相关聊天`:'本地文件夹'));meta.append(kind,node('span',activity(p.last_activity)));
    info.append(title,node('span',files||'选中后识别项目文件','card-files'),meta);card.append(cover,info);card.addEventListener('click',()=>choose(p));$('cards').append(card);loadCover(p,cover);});
  const label={project:'我的项目',chat:'单独聊天',folder:'其他文件夹'}[tab];$('picker-title').textContent=label;$('workspace-crumb').textContent=label;$('project-count').textContent=projects.filter(p=>category(p)==='project').length;
  $('empty-state').hidden=matches.length>0;
  $("list-note").textContent=q?`${matches.length} 个匹配结果`:`${matches.length} ${tab==='chat'?'个聊天':'个项目'}`;
  for(const b of $("tabs").querySelectorAll("button")){b.classList.toggle("active",b.dataset.tab===tab);b.setAttribute('aria-current',b.dataset.tab===tab?'page':'false');}
}
async function load(refresh=false){clearError();const d=refresh?await api("/api/refresh",{}):await api("/api/projects");projects=d.projects;renderProjects();}
async function choose(p){
  const view=++revision;selectedProject=p;currentJob=null;result=null;rows=null;extraKeeps.clear();clearError();
  $("picker").hidden=true;$("project-view").hidden=false;$("project-title").textContent=p.display_name;$("project-stage").textContent="正在分析";
  $('workspace-crumb').textContent='项目 / 整理建议';
  $("results").hidden=true;$("completed").hidden=true;$("retry").hidden=true;$("busy").hidden=false;$("progress").textContent="正在识别项目文件…";window.scrollTo(0,0);
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
  $("project-stage").textContent=failed?"AI 分析未完成":d.mode==="ai"?"文本片段分析完成":"本地盘点结果";$("findings-title").textContent=failed?"AI 分析未完成":"整理建议";$("analysis-actions").hidden=!failed;$("decision-bar").hidden=false;$("decision-note").hidden=false;
  $("findings-text").replaceChildren(...d.overview.sentences.map(t=>node("p",t)));$("result-mode").textContent=failed?"仅本地盘点":d.mode==="ai"?"文本片段分析":"本地分析";
  if(d.overview.artifact_examples.length){const line=node("p","结果候选：","artifact-line");for(const p of d.overview.artifact_examples.slice(0,3))line.append(node("span",p.split("/").pop(),"artifact-chip"));$("findings-text").prepend(line);}
  $("mode-note").textContent=d.mode_message;
  const coverage=d.analysis;$("analysis-coverage").hidden=!coverage;
  if(coverage)$("analysis-coverage").textContent=`共 ${coverage.file_count} 个文件：提取文本 ${coverage.text_count} 项，AI 判断文本片段 ${coverage.ai_file_count} 项；${coverage.unread_count} 项未读取内容`+(coverage.truncated_count?`，${coverage.truncated_count} 项文本仅部分读取`:"")+`。未读项的去留以所选方案为准。`+(coverage.budget_skipped_count?`其中 ${coverage.budget_skipped_count} 项受读取额度限制。`:"");$("transmission").hidden=!(d.has_transmission??(d.mode==="ai"));$("report-location").textContent="分析报告："+d.report_path;
  $("scope").replaceChildren(...["",...d.scopes].map(v=>new Option(v||"整个项目",v)));$("scope").value=d.scope;
  $("comparison-note").textContent="只留结果时，先确认最终交付件；全部清理也会移出结果。私密文件、链接、未展开的目录和其他范围仍引用的文件留在原处。";
  renderPlans();if(open) loadRows();

}
async function revise(change){const view=revision;$("results").inert=true;try{const d=await api("/api/task",{job:currentJob,scope:result.scope,...change});if(view===revision)showResults(d,true);}catch(e){error(e);$("scope").value=result.scope;if($("review-panel").open)await loadRows();}finally{$("results").inert=false;}}
$("scope").onchange=()=>revise({scope:$("scope").value});
function activeProfile(){return result.profiles.find(p=>p.tier===tier);}
function exclusions(){const candidates=rows?new Set(rows.filter(r=>r.decision==="candidate").map(r=>r.path)):null;return [...extraKeeps.keys()].filter(p=>!candidates||candidates.has(p));}
function impact(){const p=activeProfile(),kept=exclusions();return{keep:p.keep_count+kept.length,move:p.move_count-kept.length,bytes:Math.max(0,p.move_bytes-kept.reduce((n,path)=>n+(extraKeeps.get(path)||0),0))};}
function renderPlans(){
  $("plans").replaceChildren();
  for(const p of result.profiles){const card=node("button",undefined,"plan-card"+(p.tier===tier?" selected":""));card.type="button";card.dataset.tier=p.tier;card.setAttribute("role","radio");card.setAttribute("aria-checked",String(p.tier===tier));card.tabIndex=p.tier===tier?0:-1;
    const head=node("div",undefined,"plan-head");head.append(node("strong",p.label));if(p.recommended&&result.analysis?.status!=="failed")head.append(node("small","推荐"));const marker=node('span',undefined,'plan-marker');marker.append(icon('check'));head.append(marker);
    card.append(head,node("p",p.description,"plan-description"));const counts=node("div",undefined,"plan-counts");
    counts.append(node("strong",String(p.keep_count)),node("span","项保留"),node("span",`移出 ${p.move_count} 项 · ${size(p.move_bytes)}`,"move-count"));card.append(counts);
    card.append(node("p",p.move_count?"主要移出："+p.move_description:"这份方案保留当前全部文件","plan-examples"));
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
function updateDecision(){explainChoice();const p=activeProfile(),i=impact();$("chosen-title").textContent="采用“"+p.label+"”";$("chosen-impact").textContent=`保留 ${i.keep} 项，移出 ${i.move} 项（${size(i.bytes)}）`+(extraKeeps.size?` · 已额外保留 ${extraKeeps.size} 项`:"");$("apply").disabled=i.move===0||p.blocked;if(p.blocked)$("chosen-impact").textContent="请先在文件清单中标出要留下的结果或节点。";$("apply").textContent=i.move?"按此方案整理":"全部保留，无需整理";}
async function loadRows(){const view=revision,selected=tier;try{const d=await api(`/api/records/${currentJob}/${selected}`);if(view!==revision||selected!==tier)return;rows=d.records;$("warnings").replaceChildren(...d.warnings.map(t=>node("li",t)));renderRows();}catch(e){error(e);}}
function renderRows(){
  if(!rows)return;const f=$("filter").value,q=$("file-search").value.trim().toLowerCase(),filtered=rows.filter(r=>r.path.toLowerCase().includes(q)&&(f==="all"||(f==="unknown"?r.role==="unknown"&&r.decision==="candidate"&&!extraKeeps.has(r.path):f==="keep"?r.decision!=="candidate":r.decision==="candidate")));$("files").replaceChildren();
  for(const r of filtered.slice(0,visibleRows)){const tr=node("tr"),check=node("td");if(r.decision==="candidate"){const cb=node("input");cb.type="checkbox";cb.checked=!extraKeeps.has(r.path);cb.setAttribute("aria-label","移出 "+r.path);cb.addEventListener("change",()=>{cb.checked?extraKeeps.delete(r.path):extraKeeps.set(r.path,r.size);updateDecision();renderRows();});check.append(cb);}
    const role=node("td");if(r.kind==="file"){const sel=node("select");sel.setAttribute("aria-label","文件归类："+r.path);for(const [v,l] of Object.entries({result:"结果文件",milestone:"重要节点",process:"过程文件",unknown:"待辨认",auto:"恢复自动判断"})){const op=new Option(l,v);op.disabled=v==="unknown";sel.add(op);}sel.value=r.role;sel.onchange=()=>revise({path:r.path,role:sel.value});role.append(sel);}else role.textContent="不在清理范围";
    tr.append(check,node("td",r.path),role,node("td",extraKeeps.has(r.path)?"额外保留":r.decision==="candidate"?"移入废纸篓":"保留"),node("td",extraKeeps.has(r.path)?"你要求留下这个文件。":r.reason),node("td",size(r.size)));$("files").append(tr);}
  $("more-rows").hidden=filtered.length<=visibleRows;$("more-rows").onclick=()=>{visibleRows+=150;renderRows();};$("row-limit").textContent=`共 ${filtered.length} 项`+(filtered.length>visibleRows?`，已显示前 ${visibleRows} 项。`:"");
}
$("tabs").addEventListener("click",e=>{const b=e.target.closest("button");if(b){tab=b.dataset.tab;$('search').value='';$('back').click();renderProjects();}});$("search").addEventListener("input",renderProjects);$("refresh").addEventListener("click",()=>load(true).catch(error));
$("back").addEventListener("click",()=>{revision++;currentJob=null;$("project-view").hidden=true;$("picker").hidden=false;clearError();renderProjects();});$("brand").addEventListener("click",e=>{e.preventDefault();tab='project';$('search').value='';$("back").click();});
$("review-panel").addEventListener("toggle",()=>{if($("review-panel").open&&!rows)loadRows();});$("filter").addEventListener("change",renderRows);$("file-search").oninput=()=>{visibleRows=150;renderRows();};
$('plans').addEventListener('keydown',e=>{const order=result?.profiles.map(p=>p.tier);if(!order||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(e.key))return;e.preventDefault();const index=order.indexOf(tier),next=e.key==='Home'?0:e.key==='End'?order.length-1:(index+(['ArrowRight','ArrowDown'].includes(e.key)?1:-1)+order.length)%order.length;selectTier(order[next]);});
$("transmission").addEventListener("toggle",async()=>{if($("transmission").open){try{const d=await api("/api/transmission/"+currentJob);$("payload-text").textContent=JSON.stringify(d.manifest,null,2);}catch(e){error(e);}}});
$("apply").addEventListener("click",async()=>{clearError();const scope=result.scope||selectedProject.display_name;const i=impact();if(!window.confirm(`「${scope}」采用“${activeProfile().label}”：移出 ${i.move} 项，保留 ${i.keep} 项。确认移入 Mac 废纸篓？`))return;const view=revision;$("apply").disabled=true;$("apply").textContent="正在整理…";try{const d=await api("/api/apply",{job:currentJob,tier,version:result.version||0,confirm_clear:tier==="clear",keep_extra:exclusions()});if(view!==revision)return;lastRun=d.run;$("results").hidden=true;$("completed").hidden=false;$("project-stage").textContent="整理完成";$("complete-title").textContent=d.count?(d.destination==="trash"?"已移到废纸篓":"未确认移到废纸篓"):"全部文件已保留";$("complete-description").textContent=d.count?(d.destination==="trash"?`${d.count} 项文件已移到 Mac 废纸篓，其他文件留在原处。清空前可以撤销。`:"系统未确认转移成功。本次记录已保留，可撤销后检查废纸篓权限。"):"这份方案没有需要移出的文件。";$("undo").hidden=!lastRun;$("undo").disabled=false;}catch(e){if(view===revision){updateDecision();error(e);}}});
$("undo").addEventListener("click",async()=>{clearError();$("undo").disabled=true;try{await api("/api/undo",{run:lastRun});$("complete-title").textContent="文件已经恢复";$("complete-description").textContent="本次移出的文件已恢复到原来的位置。";}catch(e){$("undo").disabled=false;error(e);}});
$("analysis-settings").addEventListener("click",()=>$("settings").click());
for(const id of ["retry","reanalyze","analysis-retry"])$(id).addEventListener("click",()=>choose(selectedProject));
function renderSettings(p){$("analysis-mode").textContent=p.enabled&&p.has_key?"自动 AI 分析":"本地分析";}
$("settings").addEventListener("click",async()=>{try{const p=await api("/api/settings");renderSettings(p);$("enable-ai").checked=p.enabled;$("provider").value=p.provider;$("model").value=p.model;$("api-key").value="";$("api-key").placeholder=p.has_key?"已有 Key；需要更换时填写":"仅在本地填写";$("remember-key").checked=p.remember;$("api-settings").hidden=!p.enabled;$("settings-error").textContent="";$("settings-dialog").showModal();}catch(e){error(e);}});
$("enable-ai").addEventListener("change",()=>{$("api-settings").hidden=!$("enable-ai").checked;});$("cancel-settings").addEventListener("click",()=>{$("api-key").value="";$("settings-dialog").close();});
$("settings-dialog").addEventListener("close",()=>{$("api-key").value="";});
$("save-settings").addEventListener("click",async()=>{$("save-settings").disabled=true;$("settings-error").textContent="";try{const p=await api("/api/settings",{enabled:$("enable-ai").checked,provider:$("provider").value,model:$("model").value.trim(),key:$("api-key").value.trim(),remember:$("remember-key").checked});$("api-key").value="";renderSettings(p);$("settings-dialog").close();}catch(e){$("settings-error").textContent=e.message;}finally{$("save-settings").disabled=false;}});
load().catch(error);api("/api/settings").then(renderSettings).catch(error);
