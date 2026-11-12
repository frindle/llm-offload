"""Single self-contained HTML page for the docsum web UI (inline CSS and JS only, no external resources)."""


def page_html():
    body = '''<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>DocSum &mdash; documents stay on this Mac</title>
<style>
body{font-family:system-ui,sans-serif;max-width:56rem;margin:0 auto;padding:1rem;background:#fff;color:#222}
@media(prefers-color-scheme:dark){body{background:#1a1a1a;color:#ddd}}
#drop{border:2px dashed #888;border-radius:8px;padding:2rem;text-align:center;cursor:pointer;position:relative}
#drop.dragging{border-color:#0af}
table{width:100%;border-collapse:collapse;margin-top:1rem}
th,td{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #ccc}
.queued{color:#888}.running{color:#c90}.done{color:#080}.failed{color:#c00}
button,input,textarea,select{font-size:1rem;padding:.4rem;border:1px solid #aaa;border-radius:4px}
#jobs td a{margin-right:.5rem}
</style></head><body>
<h1>DocSum &mdash; documents stay on this Mac</h1>
<div id="drop" onclick="document.getElementById('file').click()">Drop a PDF here or choose one, max 100 MB
<input type="file" id="file" accept="application/pdf,.pdf" style="display:none"></div>
<span id="fname" style="display:block;margin:.5rem 0"></span>
<input type="text" id="refname" maxlength="100" placeholder="Name (optional)" size="40" style="display:block;margin-bottom:.5rem">
<textarea id="scenario" placeholder="Optional: who I am and what I need to decide" rows="3" cols="50"></textarea>
<button id="go">Submit</button>
<span id="msg"></span>
<table id="jobs"><thead><tr><th>Id</th><th>Name</th><th>Status</th><th>Pages</th><th>Elapsed</th><th>Actions</th></tr></thead><tbody></tbody></table>
<script>
(function(){
var drop=document.getElementById("drop"),fileEl=document.getElementById("file"),fnameEl=document.getElementById("fname");
function prefill(n){document.getElementById("refname").value=n.replace(/\\.[^.]*$/,"")}
drop.addEventListener("dragover",function(e){e.preventDefault();drop.classList.add("dragging")});
drop.addEventListener("dragleave",function(){drop.classList.remove("dragging")});
drop.addEventListener("drop",function(e){e.preventDefault();drop.classList.remove("dragging");if(e.dataTransfer.files.length){fileEl.files=e.dataTransfer.files;fnameEl.textContent=e.dataTransfer.files[0].name;prefill(e.dataTransfer.files[0].name)}});
fileEl.addEventListener("change",function(){if(fileEl.files.length){fnameEl.textContent=fileEl.files[0].name;prefill(fileEl.files[0].name)}else{fnameEl.textContent=""}});
document.getElementById("go").addEventListener("click",function(){
if(!fileEl.files.length){document.getElementById("msg").textContent="Please choose a PDF";return}
var fd=new FormData();fd.append("file",fileEl.files[0]);fd.append("name",document.getElementById("refname").value);fd.append("scenario",document.getElementById("scenario").value);
fetch("api/upload",{method:"POST",body:fd}).then(function(r){return r.json()}).then(function(j){
if(j.error){document.getElementById("msg").textContent="Error: "+j.error}else{document.getElementById("msg").textContent="queued "+j.id;refresh()}});
});
function fmt(s){var m=Math.floor(s/60);return m+"m "+(s%60).toString().padStart(2,"0")+"s"}
function refresh(){fetch("api/jobs").then(function(r){return r.json()}).then(function(j){
var tb=document.querySelector("#jobs tbody");tb.textContent="";var trs=[];(j.jobs||[]).forEach(function(jo){var tr=document.createElement("tr");
var td1=document.createElement("td");td1.textContent=jo.id;tr.appendChild(td1);
var tdn=document.createElement("td");tdn.textContent=jo.title||jo.name||"";if(jo.title&&jo.name){tdn.title=jo.name}
var rb=document.createElement("button");rb.textContent="Rename";rb.style.marginLeft=".5rem";rb.onclick=(function(jo){return function(){var v=prompt("Name",jo.title||"");if(v===null)return;fetch("api/rename/"+jo.id,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:v})}).then(refresh)}})(jo);tdn.appendChild(rb);tr.appendChild(tdn);
var td2=document.createElement("td");td2.className=jo.status;td2.textContent=jo.status;if(jo.code&&jo.status==="failed"){td2.textContent+=" ("+jo.code+")"}tr.appendChild(td2);
var td3=document.createElement("td");td3.textContent=jo.pages!==null?jo.pages:"-";tr.appendChild(td3);
var td4=document.createElement("td");td4.textContent=jo.elapsed!==null?fmt(jo.elapsed):"-";tr.appendChild(td4);
var td5=document.createElement("td");
if(jo.has_pdf){var a1=document.createElement("a");a1.href="job/"+jo.id+"/pdf";a1.download="";a1.textContent="Download PDF";td5.appendChild(a1)}
if(jo.has_md){var a2=document.createElement("a");a2.href="job/"+jo.id+"/md";a2.download="";a2.textContent="Download MD";td5.appendChild(a2)}
var b=document.createElement("button");b.textContent="Delete";b.onclick=(function(id){return function(){fetch("api/delete/"+id,{method:"POST"}).then(refresh)}})(jo.id);td5.appendChild(b);tr.appendChild(td5);
trs.push(tr)});trs.forEach(function(tr){tb.appendChild(tr)})})};
setInterval(refresh,3000);refresh();
})();
</script></body></html>'''
    return body
