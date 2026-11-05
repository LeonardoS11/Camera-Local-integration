// Abas "Avisos" (cada um configura o próprio aparelho) e "Admin" do painel (31/10).
// Usa os utilitários do index.html: $, api, post, esc, EU, CASA, pode, desde, hhmm, aplicarCasa.
const put=(u,b)=>api(u,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
const del=u=>api(u,{method:'DELETE'});
const dataHora=t=>t?new Date(t*1000).toLocaleString('pt-BR',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
// "1.10" >= "1.7" falha como texto; compara maior.menor numericamente
const versaoMin=(v,maj,min)=>{const [a,b]=String(v||'').split('.').map(x=>parseInt(x,10)||0);return a>maj||(a===maj&&b>=min)};
function uaNome(ua){ua=ua||'';const so=/iPhone/.test(ua)?'iPhone':/iPad/.test(ua)?'iPad':/Android/.test(ua)?'Android':/Windows/.test(ua)?'Windows':/Mac OS/.test(ua)?'Mac':/Linux/.test(ua)?'Linux':'?';
  const nav=/Edg\//.test(ua)?'Edge':/SamsungBrowser/.test(ua)?'Samsung':/Firefox/.test(ua)?'Firefox':/Chrome/.test(ua)?'Chrome':/Safari/.test(ua)?'Safari':/python|curl|casa-/.test(ua)?'script':'';return `${so} ${nav}`.trim()}

// ================= Avisos =================
const ehIOS=/iPhone|iPad|iPod/.test(navigator.userAgent)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
const ehAndroid=/Android/.test(navigator.userAgent);
const instalado=()=>matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
const suportaPush=()=>'serviceWorker' in navigator&&'PushManager' in window&&'Notification' in window;
let PUSHCFG=null;
function b64url(s){const p='='.repeat((4-s.length%4)%4), b=atob((s+p).replace(/-/g,'+').replace(/_/g,'/'));return Uint8Array.from(b,c=>c.charCodeAt(0))}
async function inscricaoAtual(){if(!suportaPush())return null;const r=await navigator.serviceWorker.ready;return r.pushManager.getSubscription()}
async function carregarAvisos(){PUSHCFG=await api('/api/push/config');await estadoPush();prefsPush();aparelhosPush();ntfyInfo();historicoPush()}
const hm=t=>new Date(t*1000).toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
async function historicoPush(){const r=await api('/api/push/historico'), agora=Date.now()/1000;let perdidos=0;
  const linha=a=>{const env=a.envios.map(e=>{
      if(e.recebido)return`<span class="pill ok">${esc(e.aparelho)}: chegou ${hm(e.recebido)}</span>`;
      if(!(e.status>=200&&e.status<300))return`<span class="pill" style="color:var(--red)">${esc(e.aparelho)}: falhou (${e.status})</span>`;
      if(agora-e.ts<120)return`<span class="pill">${esc(e.aparelho)}: a caminho…</span>`;
      perdidos++;return`<span class="pill" style="color:var(--red)">${esc(e.aparelho)}: enviado, o celular não confirmou</span>`}).join(' ');
    const sem=a.filtrado?'<span class="mut">desligado nas suas preferências</span>':a.envios.length?'':'<span class="mut">nenhum aparelho ativo na hora</span>';
    return`<div class="ctl"><span>${dataHora(a.ts)} · <b>${esc(a.titulo)}</b><br>${env}${sem}${a.ntfy?' <span class="mut">· ntfy ✓</span>':''}</span></div>`};
  const h=r.avisos.map(linha).join('');
  $('#pushhist').innerHTML=(h||'<p class="mut">Nenhum aviso nos últimos 7 dias.</p>')+(perdidos?`<p class="mut" style="margin-top:8px"><b>"Enviado, o celular não confirmou"</b> = o Google/Apple aceitou o aviso, mas ele não chegou ao navegador do celular.
    ${ehAndroid?'Quase sempre é economia de bateria: Configurações → Apps → <b>Chrome</b> → Bateria → <b>Sem restrições</b>; no Samsung, tire o Chrome de "Apps em suspensão" e "Apps em suspensão profunda" (Cuidados com o dispositivo → Bateria → Limites de uso em segundo plano).':'No iPhone, confira Ajustes → Notificações → Casas, e o modo Foco.'}</p>`:'')}
async function estadoPush(){const el=$('#pushestado'), passo=(n,t)=>`<div class="ctl"><span><b>${n}.</b> ${t}</span></div>`;
  if(ehIOS&&!instalado()){el.innerHTML=`<p>No iPhone os avisos chegam pelo <b>ícone do painel na tela de início</b> (iOS 16.4 ou mais novo):</p>
    ${passo(1,'Abra este site no <b>Safari</b>.')}${passo(2,'Toque em <b>Compartilhar</b> (o quadrado com a seta ↑) → <b>Adicionar à Tela de Início</b> → Adicionar.')}
    ${passo(3,'Abra o ícone <b>Casas</b> que apareceu, entre com seu usuário e volte aqui em <b>Avisos</b>.')}
    ${passo(4,'Toque em <b>Ativar avisos</b> e depois em <b>Permitir</b>.')}`;return}
  if(!suportaPush()){el.innerHTML=`<p class="mut">Este navegador não recebe avisos. No Android use o <b>Chrome</b>; no iPhone, o <b>Safari</b> com o ícone na tela de início.</p>`;return}
  if(Notification.permission==='denied'){el.innerHTML=`<p><b style="color:var(--red)">As notificações deste site estão bloqueadas.</b></p>
    <p class="mut">${ehIOS?'Ajustes do iPhone → Notificações → Casas → Permitir.':ehAndroid?'Toque no cadeado ao lado do endereço → Permissões → Notificações → Permitir (ou Configurações → Apps → Chrome → Notificações).':'Clique no cadeado ao lado do endereço → Notificações → Permitir.'} Depois recarregue esta página.</p>`;return}
  const sub=await inscricaoAtual(), minha=sub&&PUSHCFG.aparelhos.find(a=>a.endpoint===sub.endpoint);
  if(minha){el.innerHTML=`<p><span class="pill ok">ativo</span> Este aparelho (<b>${esc(minha.aparelho)}</b>) recebe os avisos.</p>
    <div class="grid"><button class="b p" onclick="testePush(true)">Enviar aviso de teste</button><button class="b" onclick="desativarPush()">Desativar neste aparelho</button></div><p id="testeres" class="mut"></p>`;return}
  el.innerHTML=`<p>Receba aqui os avisos de pessoa, atividade suspeita e câmera fora do ar, com foto, mesmo com o celular bloqueado.</p>
    <div class="grid"><input id="pushnome" placeholder="Nome deste aparelho (opcional)" style="flex:1"><button class="b p" onclick="ativarPush()">Ativar avisos neste aparelho</button></div>
    ${ehAndroid&&!instalado()?'<p class="mut">Dica: no Chrome, menu ⋮ → <b>Adicionar à tela inicial</b> (ou "Instalar app") para abrir o painel como um app.</p>':''}
    <p class="mut">Se o celular economiza bateria, deixe o ${ehAndroid?'Chrome':'navegador'} sem restrição de bateria para os avisos não atrasarem.</p><p id="testeres" class="mut"></p>`}
async function ativarPush(){const nome=$('#pushnome')?.value.trim()||'';
  const perm=await Notification.requestPermission();if(perm!=='granted'){estadoPush();return}
  const reg=await navigator.serviceWorker.ready;let sub=await reg.pushManager.getSubscription();
  const opc={userVisibleOnly:true,applicationServerKey:b64url(PUSHCFG.chave)};
  try{if(!sub)sub=await reg.pushManager.subscribe(opc)}catch(e){if(sub)await sub.unsubscribe();sub=await reg.pushManager.subscribe(opc)}
  await post('/api/push/inscrever',{inscricao:sub.toJSON(),aparelho:nome});await carregarAvisos();testePush(true)}
async function desativarPush(){const sub=await inscricaoAtual();if(sub){await post('/api/push/remover',{endpoint:sub.endpoint});await sub.unsubscribe()}carregarAvisos()}
async function testePush(soEste){const sub=soEste?await inscricaoAtual():null;const r=await post('/api/push/teste',{endpoint:sub?sub.endpoint:''});
  const t=r.enviados.map(e=>`${esc(e.aparelho)}: ${e.ok?'enviado ✓':'falhou ('+e.status+')'}`).join(' · ')||'nenhum aparelho ativo';
  const el=$('#testeres');if(el)el.innerHTML=t+(r.enviados.some(e=>e.ok)?' — deve chegar em alguns segundos.':'');aparelhosPush();
  historicoPush();setTimeout(historicoPush,8000)}
function prefsPush(){const c=PUSHCFG, p=c.prefs, casas=p.casas||c.casas.map(x=>x.id);
  $('#pushprefs').innerHTML=(c.casas.length>1?`<div class="mut">Casas:</div><div class="grid">${c.casas.map(x=>`<label><input type="checkbox" data-pc="${x.id}" ${casas.includes(x.id)?'checked':''}> ${esc(x.nome)}</label>`).join('')}</div>`:'')+
    `<div class="mut" style="margin-top:6px">Tipos:</div>${Object.entries(c.categorias).map(([k,t])=>`<div class="ctl"><span>${esc(t)}${k==='suspeito'?' <span class="mut">(recomendado deixar ligado)</span>':''}</span><div class="v"><input type="checkbox" data-pt="${k}" ${p.categorias[k]!==false?'checked':''}></div></div>`).join('')}`}
async function salvarPrefs(){const casas=PUSHCFG.casas.length>1?[...document.querySelectorAll('[data-pc]:checked')].map(i=>i.dataset.pc):null;
  const categorias={};document.querySelectorAll('[data-pt]').forEach(i=>categorias[i.dataset.pt]=i.checked);
  await put('/api/push/prefs',{casas,categorias});$('#prefres').textContent='Salvo.';setTimeout(()=>$('#prefres').textContent='',2000)}
async function aparelhosPush(){const c=await api('/api/push/config'), sub=await inscricaoAtual().catch(()=>null);PUSHCFG=c;
  $('#pushaparelhos').innerHTML=c.aparelhos.map(a=>`<div class="ctl"><span><b>${esc(a.aparelho)}</b>${sub&&sub.endpoint===a.endpoint?' <span class="pill ok">este</span>':''}
    <span class="mut">· ativado ${dataHora(a.criado)}${a.ultimo?` · último aviso ${dataHora(a.ultimo)} ${a.status>=200&&a.status<300?'✓':'(falhou '+a.status+')'}`:''}</span></span>
    <div class="v"><button class="b" onclick="removerAparelho(${a.id})">Remover</button></div></div>`).join('')+
    (c.aparelhos.length?`<div class="grid" style="margin-top:6px"><button class="b" onclick="testePush(false)">Testar em todos</button></div>`:'<p class="mut">Nenhum aparelho recebe avisos ainda.</p>')}
async function removerAparelho(id){await post('/api/push/remover',{id});carregarAvisos()}
function ntfyInfo(){const n=PUSHCFG.ntfy;$('#ntfyinfo').innerHTML=`<p>App <b>ntfy</b> (Android: F-Droid/Play; iPhone: App Store) → adicionar servidor <code>${esc(n.servidor)}</code>, entrar com o usuário do ntfy e assinar ${Object.entries(n.topicos).map(([c,t])=>`<code>${esc(t)}</code>`).join(' e ')}.</p>
  <p>O usuário do ntfy é separado do painel. ${pode('admin')?'Para criar um (a senha você digita), na VPS: <code>docker compose exec ntfy ntfy user add NOME</code> e <code>docker compose exec ntfy ntfy access NOME TOPICO ro</code>.':'Peça um ao administrador.'}</p>`}

// ================= Admin =================
let ADM='usuarios', PERFIS=null, CASAS_ADM=null, US_EDIT=null, PF_EDIT=null, CE=null, XI=[];
document.querySelectorAll('#admnav button').forEach(b=>b.onclick=()=>{ADM=b.dataset.a;
  document.querySelectorAll('#admnav button').forEach(x=>x.classList.toggle('p',x===b));
  document.querySelectorAll('[data-ap]').forEach(d=>d.hidden=d.dataset.ap!==ADM);carregarAdmin()});
function carregarAdmin(){vigiarPontes();return ({usuarios:admUsuarios,perfis:admPerfis,casas:admCasas,pontes:admPontes,sistema:admSistema,acessos:admAcessos})[ADM]()}
// Admin > Pontes se atualiza a cada 30 s enquanto estiver visível; o vigia se desliga ao sair da aba
let PONTES_T=null;
function vigiarPontes(){clearInterval(PONTES_T);PONTES_T=null;if(ADM!=='pontes')return;
  PONTES_T=setInterval(()=>{if(ADM!=='pontes'||$('#admin').hidden){clearInterval(PONTES_T);PONTES_T=null;return}
    if(document.hidden)return;QUIETO=true;try{admPontes().catch(()=>{})}finally{QUIETO=false}},30000)}
const nomeCasaAdm=id=>id==='*'?'todas':(CASAS_ADM?.[id]?.nome||id);

// ---- usuários
async function admUsuarios(){const [us,pf,ca]=await Promise.all([api('/api/usuarios'),api('/api/perfis'),api('/api/admin/casas')]);PERFIS=pf;CASAS_ADM=ca.casas;
  window._US=us;
  $('#uslista').innerHTML=us.map(u=>`<div class="ctl"><span><b>${esc(u.nome||u.login)}</b> ${u.ativo?'':'<span class="pill off">bloqueado</span>'}<br>
    <span class="mut">${esc(u.login)} · ${esc(u.perfil||'sem perfil')} · vê: ${u.casas.map(nomeCasaAdm).map(esc).join(', ')} · ${u.ultimo?'entrou há '+desde(u.ultimo):'nunca entrou'} · ${u.sessoes} aparelho(s) conectado(s) · avisos em ${u.avisos}</span></span>
    <div class="v"><button class="b" onclick="editarUsuario(${u.id})">Editar</button>${u.login===EU.login?'':`<button class="b" onclick="apagarUsuario(${u.id},${js(u.login)})">✕</button>`}</div></div>`).join('');
  $('#u-perfil').innerHTML=pf.perfis.map(p=>`<option value="${p.id}">${esc(p.nome)}</option>`).join('');
  if(!US_EDIT){const fam=pf.perfis.find(p=>p.nome==='Só assistir')||pf.perfis[pf.perfis.length-1];$('#u-perfil').value=fam?.id}
  casasUsuario(US_EDIT?us.find(u=>u.id===US_EDIT).casas:[CASA])}
function casasUsuario(sel){$('#u-casas').innerHTML='<span class="mut">Vê as casas:</span>'+Object.entries(CASAS_ADM).map(([id,c])=>`<label><input type="checkbox" value="${id}" ${sel.includes(id)||sel.includes('*')?'checked':''}> ${esc(c.nome)}</label>`).join('')+
  `<label title="inclui casas criadas no futuro"><input type="checkbox" value="*" ${sel.includes('*')?'checked':''}> Todas, inclusive futuras</label>`}
function editarUsuario(id){const u=window._US.find(x=>x.id===id);US_EDIT=id;$('#ustitulo').textContent='Editar '+(u.nome||u.login);
  $('#u-login').value=u.login;$('#u-login').disabled=true;$('#u-nome').value=u.nome||'';$('#u-senha').value='';$('#u-senha').placeholder='Nova senha (deixe vazio para manter)';
  $('#u-perfil').value=u.perfil_id;casasUsuario(u.casas);$('#u-ativo-l').hidden=u.login===EU.login;$('#u-ativo').checked=u.ativo;$('#usres').textContent='';$('#usform').scrollIntoView({behavior:'smooth'})}
function limparUsuario(){US_EDIT=null;$('#ustitulo').textContent='Novo usuário';$('#u-login').disabled=false;['login','nome','senha'].forEach(k=>$('#u-'+k).value='');
  $('#u-senha').placeholder='Senha (mín. 8)';$('#u-ativo-l').hidden=true;$('#usres').textContent='';admUsuarios()}
async function salvarUsuario(){let casas=[...document.querySelectorAll('#u-casas input:checked')].map(i=>i.value);if(casas.includes('*'))casas=['*'];
  const b={nome:$('#u-nome').value,perfil_id:+$('#u-perfil').value,casas};if($('#u-senha').value)b.senha=$('#u-senha').value;
  if(US_EDIT){if(!$('#u-ativo-l').hidden)b.ativo=$('#u-ativo').checked;await put('/api/usuarios/'+US_EDIT,b)}
  else{b.login=$('#u-login').value;b.senha=$('#u-senha').value;await post('/api/usuarios',b)}
  const nome=b.nome||b.login;limparUsuario();$('#usres').textContent=`${nome} salvo. Passe o usuário e a senha para a pessoa; no primeiro acesso ela pode trocar a senha no menu do nome e ativar os avisos em "Avisos".`}
async function apagarUsuario(id,login){if(confirm(`Apagar o usuário ${login}? (para só bloquear, use Editar → Ativo)`)){await del('/api/usuarios/'+id);admUsuarios()}}

// ---- perfis
async function admPerfis(){PERFIS=await api('/api/perfis');const P=PERFIS.permissoes;
  $('#pflista').innerHTML=PERFIS.perfis.map(p=>`<div class="ctl"><span><b>${esc(p.nome)}</b> <span class="mut">· ${p.usuarios} usuário(s)</span><br>
    <span class="mut">${p.fixo?'tudo':p.permissoes.map(k=>esc(P[k]||k)).join(' · ')||'nada'}</span></span>
    <div class="v">${p.fixo?'<span class="mut">fixo</span>':`<button class="b" onclick="editarPerfil(${p.id})">Editar</button><button class="b" onclick="apagarPerfil(${p.id},${js(p.nome)})">✕</button>`}</div></div>`).join('');
  if(!PF_EDIT)formPerfil([])}
function formPerfil(sel){$('#pf-perms').innerHTML=Object.entries(PERFIS.permissoes).filter(([k])=>k!=='admin').map(([k,t])=>`<label style="display:block;padding:3px 0"><input type="checkbox" value="${k}" ${sel.includes(k)?'checked':''}> ${esc(t)}</label>`).join('')+
  '<p class="mut">Administração (usuários, perfis, casas) fica só no perfil Administrador.</p>'}
function editarPerfil(id){const p=PERFIS.perfis.find(x=>x.id===id);PF_EDIT=id;$('#pftitulo').textContent='Editar perfil '+p.nome;$('#pf-nome').value=p.nome;formPerfil(p.permissoes)}
function limparPerfil(){PF_EDIT=null;$('#pftitulo').textContent='Novo perfil';$('#pf-nome').value='';formPerfil([])}
async function salvarPerfil(){const b={nome:$('#pf-nome').value,permissoes:[...document.querySelectorAll('#pf-perms input:checked')].map(i=>i.value)};
  if(PF_EDIT)await put('/api/perfis/'+PF_EDIT,b);else await post('/api/perfis',b);limparPerfil();admPerfis()}
async function apagarPerfil(id,nome){if(confirm(`Apagar o perfil ${nome}?`)){await del('/api/perfis/'+id);admPerfis()}}

// ---- casas e câmeras
const slug=t=>t.normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,24)||'cam';
const urlXiaomi=a=>`xiaomi://${a.uid}:cn@${a.localip}?did=${a.did}&model=${a.model}`;
const didDe=u=>(String(u).match(/did=(\d+)/)||[])[1];
async function admCasas(){const r=await api('/api/admin/casas');CE=JSON.parse(JSON.stringify(r.casas));XI=r.xiaomi;
  $('#casasres').textContent=r.xiaomi_ts?'':'(a lista de câmeras da conta Xiaomi chega quando o PC de casa liga o agente)';renderCasas()}
function renderCasas(){
  $('#casaslista').innerHTML=Object.entries(CE).map(([id,c])=>{const ponte=c.ponte||{};
   return `<div class="card"><div class="grid" style="align-items:center"><b style="margin-right:auto">${esc(c.nome)}</b> <span class="mut">id ${esc(id)}</span>
      ${Object.keys(c.cameras).length?'':`<button class="b" onclick="removerCasa('${id}')">Apagar casa</button>`}</div>
    <div class="grid" style="margin-top:8px"><input value="${esc(c.nome)}" placeholder="Nome" onchange="CE['${id}'].nome=this.value">
      <input value="${esc(c.topico||id)}" placeholder="tópico do ntfy" title="tópico do ntfy" onchange="CE['${id}'].topico=this.value">
      <label><input type="checkbox" ${c.robo?'checked':''} onchange="CE['${id}'].robo=this.checked"> tem o robô</label>
      <label><input type="checkbox" ${c.presenca?'checked':''} onchange="CE['${id}'].presenca=this.checked"> celulares na rede (PC)</label></div>
    ${Object.entries(c.cameras).map(([cam,nome])=>{const viaPonte=cam in ponte;return `<div class="ctl"><span style="flex:1"><input value="${esc(nome)}" onchange="CE['${id}'].cameras['${cam}']=this.value" style="width:100%;max-width:220px"> <span class="mut">${esc(cam)}</span>
      ${viaPonte?`<br><select onchange="escolherXiaomi('${id}','${cam}',this.value)" style="max-width:100%;margin-top:4px">${opcoesXiaomi(didDe(ponte[cam]))}</select>
        <br><input value="${esc(ponte[cam])}" onchange="CE['${id}'].ponte['${cam}']=this.value" class="mut" style="width:100%;margin-top:4px;font-size:12px">`:''}</span>
      <div class="v" style="flex-wrap:wrap;justify-content:flex-end"><select onchange="origem('${id}','${cam}',this.value)"><option value="pc" ${viaPonte?'':'selected'}>pelo PC</option><option value="ponte" ${viaPonte?'selected':''}>pelo celular-ponte</option></select>
        <label title="girar pelo painel"><input type="checkbox" ${(c.ptz||[]).includes(cam)?'checked':''} onchange="lista('${id}','ptz','${cam}',this.checked)"> giro</label>
        <label title="ajustes pelo painel (só pelo PC)"><input type="checkbox" ${(c.ajustes||[]).includes(cam)?'checked':''} onchange="lista('${id}','ajustes','${cam}',this.checked)"> ajustes</label>
        <button class="b" onclick="removerCamera('${id}','${cam}')">✕</button></div></div>`}).join('')||'<p class="mut">Sem câmeras.</p>'}
    <div class="grid" style="margin-top:8px"><select id="add-${id}"><option value="">+ câmera da conta Xiaomi…</option>${XI.filter(a=>!a.usada).map(a=>`<option value="${a.did}">${esc(a.name)} · ${esc(a.localip)}${a.ssid?' · '+esc(a.ssid):''}</option>`).join('')}<option value="pc">+ câmera pelo PC (nome)</option></select>
      <button class="b" onclick="adicionarCamera('${id}')">Adicionar</button></div></div>`}).join('')}
function opcoesXiaomi(did){return `<option value="">câmera da conta Xiaomi…</option>`+XI.map(a=>`<option value="${a.did}" ${String(a.did)===String(did)?'selected':''}>${esc(a.name)} · ${esc(a.localip)}</option>`).join('')}
function escolherXiaomi(id,cam,did){const a=XI.find(x=>String(x.did)===did);if(a){CE[id].ponte[cam]=urlXiaomi(a);renderCasas()}}
function origem(id,cam,v){const c=CE[id];c.ponte=c.ponte||{};if(v==='ponte'){c.ponte[cam]=c.ponte[cam]||'xiaomi://';c.ajustes=(c.ajustes||[]).filter(x=>x!==cam)}else delete c.ponte[cam];renderCasas()}
function lista(id,k,cam,on){const c=CE[id];c[k]=(c[k]||[]).filter(x=>x!==cam);if(on)c[k].push(cam)}
function idNovo(base){const todos=new Set(Object.values(CE).flatMap(c=>Object.keys(c.cameras)));let id=slug(base),n=2;while(todos.has(id))id=slug(base)+n++;return id}
function adicionarCamera(id){const v=$('#add-'+id).value;if(!v)return;const c=CE[id];
  if(v==='pc'){const nome=prompt('Nome da câmera (o PC de casa precisa ser configurado para enviá-la com o mesmo id):');if(!nome)return;c.cameras[idNovo(nome)]=nome}
  else{const a=XI.find(x=>String(x.did)===v);const nome=prompt('Nome da câmera no painel:',a.name);if(!nome)return;const cam=idNovo(nome);c.cameras[cam]=nome;c.ponte=c.ponte||{};c.ponte[cam]=urlXiaomi(a);a.usada=true}
  renderCasas()}
function removerCamera(id,cam){if(!confirm(`Tirar a câmera ${CE[id].cameras[cam]}? As gravações dela ficam até vencer.`))return;const c=CE[id];delete c.cameras[cam];if(c.ponte)delete c.ponte[cam];
  ['ptz','ajustes'].forEach(k=>c[k]=(c[k]||[]).filter(x=>x!==cam));renderCasas()}
function novaCasa(){const nome=prompt('Nome da nova casa (ex.: Praia):');if(!nome)return;const id=slug(nome);if(CE[id])return alert('Já existe.');CE[id]={nome,topico:id,cameras:{},ponte:{}};renderCasas()}
function removerCasa(id){if(confirm(`Apagar a casa ${CE[id].nome}?`)){delete CE[id];renderCasas()}}
async function salvarCasas(){const r=await put('/api/admin/casas',{casas:CE});$('#casasres').textContent='Salvo · go2rtc: '+r.go2rtc;await recarregarEu()}
async function recarregarEu(){EU=await api('/api/eu');if(!EU.casas.some(c=>c.id===CASA))CASA=EU.casas[0]?.id;
  $('#casa').innerHTML=EU.casas.map(c=>`<option value="${c.id}" ${c.id===CASA?'selected':''}>${esc(c.nome)}</option>`).join('');
  $('#casa').hidden=EU.casas.length<2;$('#titulo').hidden=EU.casas.length>1;aplicarCasa()}

// ---- pontes
async function admPontes(){const [ps,ca]=await Promise.all([api('/api/pontes'),api('/api/admin/casas')]);CASAS_ADM=ca.casas;
  $('#pontelista').innerHTML=ps.map(p=>{const i=p.info||{}, NC=CASAS_ADM[p.casa]?.cameras||{}, cams=Object.entries(i.cameras||{});
    return `<div class="ctl"><span><b>${esc(p.nome)}</b> <span class="pill ${p.online?'ok':'off'}">${p.online?'online':'sem sinal'}</span> <span class="mut">${esc(nomeCasaAdm(p.casa))} · visto há ${desde(p.visto)}${i.rede?.ip?` · IP ${esc(i.rede.ip)}`:''}${i.bateria!=null?` · bateria ${i.bateria}%${i.carregando?' (carregando)':''}`:''}${i.temperatura!=null?` · ${Math.round(i.temperatura)}°C`:''}${versaoPonte(p)}</span><br>
      <span class="mut">${i.login_xiaomi===false?'<b style="color:var(--red)">falta entrar na conta Xiaomi no celular</b> · ':''}${cams.map(([c,v])=>cameraPonte(p,c,v,NC)).join(' · ')||'sem câmeras ainda'}</span>${cartaoPonte(i.cartao,NC)}${comandosPonte(p,NC)}</span>
      <div class="v" style="flex-wrap:wrap;justify-content:flex-end">${p.online&&versaoMin(i.versao,1,7)?`<button class="b" title="o celular testa se cada câmera responde na rede local" onclick="comandoPonte(${p.id},'rede')">Testar rede</button><button class="b" onclick="comandoPonte(${p.id},'log')">Log</button><button class="b" onclick="comandoPonte(${p.id},'reiniciar_go2rtc')">Reiniciar go2rtc</button><button class="b" onclick="comandoPonte(${p.id},'reiniciar_ponte')">Reiniciar ponte</button>`:''}<button class="b" onclick="revogarPonte(${p.id},${js(p.nome)})">Revogar</button></div></div>`}).join('')||'<p class="mut">Nenhum celular pareado.</p>'}
// uma câmera da ponte: estado, IP do endereço, e por que parou (erro do go2rtc do celular; "lan" = respondeu à busca na rede local)
function cameraPonte(p,c,v,NC){const ip=(String((p.urls||{})[c]||'').match(/@([^?/]+)/)||[])[1]||'';
  if(v.enviando)return `${esc(NC[c]||c)}: enviando (${(v.mb||0).toFixed(0)} MB)`;
  const outra=(p.outra_rede||{})[c];
  const causa=outra?`<b style="color:var(--red)" title="a câmera recebeu IP do modem: um cabo junta a rede do modem com a do Deco (satélite e modem no mesmo switch). Modem só na WAN do Deco principal, switch numa LAN dele; depois tire a câmera da tomada por 10 s">em outra rede: ${esc(outra[0])} (celular em ${esc(outra[1])})</b>`:v.lan===false?`<b style="color:var(--red)" title="o celular mandou a busca P2P para esse IP e nada voltou: confira o IP atual da câmera e se ela está no mesmo Wi-Fi do celular">não respondeu na rede do celular${ip?' ('+esc(ip)+')':''}</b>`:v.lan===true?'responde na rede, mas sem vídeo':'';
  const err=v.erro?` <span title="${esc(v.erro)}">(${esc(String(v.erro).slice(0,70))}${String(v.erro).length>70?'…':''})</span>`:'';
  return `${esc(NC[c]||c)}: <b>parada</b>${v.falhando_desde?' há '+desde(v.falhando_desde):''} ${causa}${err}${p.online&&versaoMin((p.info||{}).versao,1,7)?` <button class="b" style="padding:0 6px" onclick="comandoPonte(${p.id},'recriar','${c}')">Reconectar</button>`:''}`}
function comandosPonte(p,NC){const l=(p.comandos||[]).slice(0,3);if(!l.length)return'';const A={recriar:'reconectar',reiniciar_go2rtc:'reiniciar go2rtc',reiniciar_ponte:'reiniciar ponte',rede:'testar rede',log:'log'};
  return '<br><span class="mut">Comandos: '+l.map(c=>`${A[c.acao]||c.acao}${c.camera?' '+esc(NC[c.camera]||c.camera):''} (${dataHora(c.criado)}): ${c.fim?`<span style="color:var(${c.ok?'--grn':'--red'})">${c.ok?'ok':'falhou'}</span>${c.resultado?` <details style="display:inline"><summary style="display:inline;cursor:pointer">ver</summary><pre style="white-space:pre-wrap;font-size:11px;max-height:240px;overflow:auto">${esc(c.resultado)}</pre></details>`:''}`:c.enviado?'executando…':'aguardando o celular'}`).join(' · ')+'</span>'}
async function comandoPonte(id,acao,camera){if((acao==='reiniciar_ponte'||acao==='reiniciar_go2rtc')&&!confirm('As câmeras dessa ponte param por alguns segundos. Continuar?'))return;
  try{const r=await post(`/api/pontes/${id}/comando`,{acao,camera:camera||''});$('#pontelista').insertAdjacentHTML('afterbegin',`<p class="mut">${esc(r.aviso)}</p>`)}catch(e){alert(e.message||e)}
  setTimeout(admPontes,2000);setTimeout(admPontes,70000)}
// versão e resultado da última atualização automática (ponte 1.5+)
function versaoPonte(p){const i=p.info||{}, a=i.atualizacao||{}, v=i.versao;if(!v)return'';
  let t=` · versão ${esc(v)}`;
  if(p.versao_publicada&&v!==p.versao_publicada)t+=a.ok===false&&a.para===p.versao_publicada?` <b style="color:var(--red)" title="${esc(a.erro||'')}">(a ${esc(a.para)} falhou; voltou)</b>`:` (atualizando para ${esc(p.versao_publicada)}…)`;
  else if(a.ok&&a.fim&&Date.now()/1e3-a.fim<86400)t+=` (atualizada há ${desde(a.fim)})`;
  return t}
// buracos do ao vivo que a ponte busca no cartão SD (ponte 1.2+)
function cartaoPonte(c,NC){const m=s=>s>=3600?`${(s/3600).toFixed(1).replace('.',',')} h`:`${Math.round(s/60)} min`;
  const l=Object.entries(c||{}).filter(([,v])=>v.recuperado_s||v.perdido_s||v.pendente_s||v.ultimo_erro);if(!l.length)return'';
  return '<br><span class="mut">Cartão SD: '+l.map(([k,v])=>`${esc(NC[k]||k)}: ${v.recuperado_s?m(v.recuperado_s)+' recuperados':''}${v.pendente_s?`${v.recuperado_s?', ':''}${m(v.pendente_s)} na fila`:''}${v.perdido_s>=60?` · ${m(v.perdido_s)} sem gravação`:''}${v.ultimo_erro&&!v.pendente_s?'':v.ultimo_erro?` <span title="${esc(v.ultimo_erro)}">(${esc(String(v.ultimo_erro).slice(0,60))})</span>`:''}`).join(' · ')+'</span>'}
async function revogarPonte(id,nome){if(!confirm(`Revogar ${nome}? Ele para de enviar vídeo até ser pareado de novo.`))return;await del('/api/pontes/'+id);admPontes()}

// ---- sistema
async function admSistema(){const r=await api('/api/admin/sistema'), c=r.config;
  $('#sy-modelo').innerHTML=Object.entries(r.modelos).map(([k,t])=>`<option value="${k}" ${k===c.ia_modelo_evento?'selected':''}>${esc(t)}</option>`).join('');
  $('#sy-conhecidos').checked=c.ia_analisar_conhecidos;$('#sy-resumo').checked=c.resumo_diario;$('#sy-hora').value=c.resumo_hora;$('#syres').textContent=''}
async function salvarSistema(){await put('/api/admin/sistema',{ia_modelo_evento:$('#sy-modelo').value,ia_analisar_conhecidos:$('#sy-conhecidos').checked,
  resumo_diario:$('#sy-resumo').checked,resumo_hora:$('#sy-hora').value});$('#syres').textContent='Salvo.'}

// ---- acessos
async function admAcessos(){const r=await api('/api/admin/acessos');
  $('#sesslista').innerHTML=r.sessoes.map(s=>`<div class="ctl"><span><b>${esc(s.nome||s.login)}</b> ${s.esta?'<span class="pill ok">este aparelho</span>':''}<br>
    <span class="mut">${esc(uaNome(s.ua))} · IP ${esc(s.ip)} · entrou ${dataHora(s.criado)} · vale até ${dataHora(s.expira)}</span></span>
    <div class="v">${s.esta?'':`<button class="b" onclick="encerrarSessao('${s.id}')">Encerrar</button>`}</div></div>`).join('')||'<p class="mut">Nenhuma.</p>';
  $('#acelista').innerHTML=r.acessos.map(a=>`${dataHora(a.ts)} · <b style="color:var(${a.ok?'--grn':'--red'})">${a.ok?'entrou':'recusado'}</b> · ${esc(a.login)} · ${esc(a.ip)} · ${esc(uaNome(a.ua))}`).join('<br>')||'—'}
async function encerrarSessao(id){if(confirm('Encerrar esta sessão? O aparelho precisa entrar de novo.')){await del('/api/admin/sessoes/'+id);admAcessos()}}
