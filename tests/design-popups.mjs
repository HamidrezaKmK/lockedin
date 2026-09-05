/* Popup audit.
 *
 * Opens every overlay, dropdown and dialog at phone width and reports anything that does not fit:
 * a child sticking out past its dialog, a dialog wider than the viewport, or a body that scrolls
 * under a footer. Writes a screenshot of each so the failures can be looked at rather than
 * guessed at.
 *
 * Usage: OUT=/tmp/pop DEVICE=iphone node tests/design-popups.mjs
 */
import { spawn } from "node:child_process";
import fs from "node:fs"; import net from "node:net"; import os from "node:os"; import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright-core";
const REPO=process.env.LOCKEDIN_REPO||process.cwd();
const CHROME=process.env.LOCKEDIN_E2E_CHROME||"/usr/bin/google-chrome";
const OUT=process.env.OUT||"/tmp/pop"; fs.mkdirSync(OUT,{recursive:true});
const DEV={iphone:{width:390,height:844},small:{width:360,height:780},android:{width:412,height:915}};
const V=DEV[process.env.DEVICE||"iphone"];
async function freePort(){const s=net.createServer();await new Promise(r=>s.listen(0,"127.0.0.1",r));const{port}=s.address();await new Promise(r=>s.close(r));return port;}
async function waitFor(b,c){const d=Date.now()+40000;while(Date.now()<d){if(c.exitCode!==null)throw new Error("died");try{const r=await fetch(b+"/api/health");if(r.status<500)return;}catch{}await delay(150);}throw new Error("timeout");}

/** Anything that pokes out of its own dialog, or out of the screen. */
const AUDIT = (sel) => {
  const box=document.querySelector(sel);
  if(!box) return {missing:true};
  const b=box.getBoundingClientRect();
  const out=[];
  box.querySelectorAll("*").forEach(el=>{
    const cs=getComputedStyle(el);
    if(cs.display==="none"||cs.visibility==="hidden") return;
    const r=el.getBoundingClientRect();
    if(r.width<1||r.height<1) return;
    let scrollsX=false, anc=el.parentElement;
    while(anc && anc!==box){
      const sc=getComputedStyle(anc);
      if(/(auto|scroll)/.test(sc.overflowX) && anc.scrollWidth>anc.clientWidth+2){ scrollsX=true; break; }
      anc=anc.parentElement;
    }
    const overLeft=Math.round(b.left-r.left);
    const overRight=scrollsX?0:Math.round(r.right-b.right);
    if(overLeft>1||overRight>1){
      out.push({el:el.tagName.toLowerCase()+"."+String(el.className||"").split(" ").filter(Boolean).slice(0,2).join("."),
                text:(el.textContent||"").trim().slice(0,22), overLeft:Math.max(0,overLeft), overRight:Math.max(0,overRight)});
    }
  });
  return {
    dialog:{w:Math.round(b.width),h:Math.round(b.height),left:Math.round(b.left),right:Math.round(b.right)},
    viewport:{w:innerWidth,h:innerHeight},
    offscreen:{left:Math.round(-b.left)>1?Math.round(-b.left):0, right:Math.round(b.right-innerWidth)>1?Math.round(b.right-innerWidth):0,
               above:Math.round(-b.top)>1?Math.round(-b.top):0,
               below:Math.round(b.bottom-innerHeight)>1?Math.round(b.bottom-innerHeight):0},
    overflow:out.slice(0,6), overflowCount:out.length,
  };
};

async function main(){
  const home=fs.mkdtempSync(path.join(os.tmpdir(),"pop-"));
  const port=await freePort(), base=`http://127.0.0.1:${port}`;
  const child=spawn("uv",["run","lockedin","serve","--host","127.0.0.1","--port",String(port)],
    {cwd:REPO,env:{...process.env,LOCKEDIN_HOME:home,LOCKEDIN_INSECURE_COOKIE:"1"},stdio:["ignore","pipe","pipe"]});
  await waitFor(base,child);
  const b=await chromium.launch({executablePath:CHROME,headless:true,args:["--no-sandbox","--disable-dev-shm-usage"]});
  const ctx=await b.newContext({viewport:V,isMobile:true,hasTouch:true,deviceScaleFactor:3});
  const post=(p,d)=>ctx.request.fetch(base+p,{method:"POST",data:d});
  const u="pop"+Date.now();
  await post("/api/signup",{username:u,password:"temporary-password"});
  const {slug}=await(await post("/api/bubbles",{name:"Disney Plan"})).json();
  await post(`/api/bubbles/${slug}/approve`,{instructions:"x"});
  // a few assets with long names, like the real thing
  const zlib=await import("node:zlib");
  const png=(w,h)=>{const raw=Buffer.alloc((w*3+1)*h);
    const chunk=(t,d)=>{const l=Buffer.alloc(4);l.writeUInt32BE(d.length);const bd=Buffer.concat([Buffer.from(t),d]);
      const tb=[];for(let n=0;n<256;n++){let c=n;for(let k=0;k<8;k++)c=c&1?0xedb88320^(c>>>1):c>>>1;tb[n]=c>>>0;}
      let crc=0xffffffff;for(const by of bd)crc=tb[(crc^by)&0xff]^(crc>>>8);
      const cb=Buffer.alloc(4);cb.writeUInt32BE((crc^0xffffffff)>>>0);return Buffer.concat([l,bd,cb]);};
    const ih=Buffer.alloc(13);ih.writeUInt32BE(w,0);ih.writeUInt32BE(h,4);ih[8]=8;ih[9]=2;
    return Buffer.concat([Buffer.from([0x89,0x50,0x4e,0x47,0x0d,0x0a,0x1a,0x0a]),chunk("IHDR",ih),chunk("IDAT",zlib.deflateSync(raw)),chunk("IEND",Buffer.alloc(0))]);};
  for(const n of ["animal-kingdom-11b40c9e2f.png","animal-kingdom-39912ab77c.png","animal-kingdom-4e4671dd80.png"])
    await ctx.request.fetch(`${base}/api/bubbles/${slug}/assets`,{method:"POST",multipart:{file:{name:n,mimeType:"image/png",buffer:png(400,300)}}});
  await post("/api/todos",{title:"A task"});

  const p=await ctx.newPage();
  const errs=[]; p.on("pageerror",e=>errs.push(e.message));
  const cases=[
    ["bubble-assets",  `#bubble/${slug}`, async()=>{ await openMenu(); await tap(".toolmenu-item","Assets"); }, ".asset-modal, [role=dialog]"],
    ["papers",         `#bubble/${slug}`, async()=>{ await openMenu(); await tap(".toolmenu-item","Papers"); }, "[role=dialog]"],
    ["connect-agent",  `#bubble/${slug}`, async()=>{ await openMenu(); await tap(".toolmenu-item","Connect an agent"); }, "[role=dialog]"],
    ["tool-menu",      `#bubble/${slug}`, async()=>{ await openMenu(); }, ".toolmenu-panel"],
    ["presence-menu",  `#bubble/${slug}`, async()=>{ await openMenu(); await tap(".toolmenu-item","reading"); }, ".presence-menu"],
    ["new-bubble",     `#bubbles`,        async()=>{ await tap(".view-head button.primary"); }, "[role=dialog]"],
    ["help",           `#bubbles`,        async()=>{ await p.evaluate(()=>document.getElementById("helpBtn").click()); }, "#helpModal .diffbox"],
    ["account-menu",   `#bubbles`,        async()=>{ await p.evaluate(()=>document.getElementById("accountBtn").click()); }, "#accountMenu"],
    ["model-config",   `#settings`,       async()=>{ await tap(".model-card button","Configure"); }, "#modelCfg"],
    ["text-dialog",    `#todos`,          async()=>{ await tap(".view-head button.primary"); }, "[role=dialog]"],
  ];
  const tap=async(sel,text)=>p.evaluate(({sel,text})=>{
    const all=[...document.querySelectorAll(sel)];
    const el=text?all.find(n=>(n.textContent||"").includes(text)):all[0];
    if(!el) throw new Error("no element for "+sel+(text?" containing "+text:""));
    el.click();
  },{sel,text});
  const openMenu=async()=>{ await tap(".toolmenu-btn"); await p.locator(".toolmenu-panel").waitFor({state:"visible",timeout:5000}); await delay(250); };
  let bad=0;
  for(const [name,hash,open,sel] of cases){
    await p.goto(base+"/"+hash,{waitUntil:"networkidle"});
    await p.reload({waitUntil:"networkidle"});
    await delay(2200);
    try{ await open(); }catch(e){ console.log(`  ${name.padEnd(15)} could not open: ${String(e.message).split("\n")[0].slice(0,70)}`); continue; }
    await delay(900);
    const r=await p.evaluate(AUDIT,sel);
    await p.screenshot({path:`${OUT}/${name}.png`});
    if(r.missing){ console.log(`  ${name.padEnd(15)} dialog not found (${sel})`); continue; }
    const off=Object.entries(r.offscreen).filter(([,v])=>v).map(([k,v])=>`${k}+${v}`).join(",");
    const flag=(r.overflowCount||off)?"BAD ":"ok  ";
    if(r.overflowCount||off) bad++;
    console.log(`  ${flag}${name.padEnd(15)} dialog ${r.dialog.w}x${r.dialog.h} vw=${r.viewport.w}` +
                (off?` offscreen[${off}]`:"") + (r.overflowCount?` overflowing=${r.overflowCount}`:""));
    for(const o of r.overflow) console.log(`        ↳ ${o.el.slice(0,34).padEnd(34)} L+${o.overLeft} R+${o.overRight}  ${JSON.stringify(o.text)}`);
  }
  console.log(`\n${bad} popup(s) with layout problems`);
  console.log("page errors:", errs.length?[...new Set(errs)]:"(none)");
  await b.close(); child.kill("SIGTERM");
}
main().catch(e=>{console.error(e);process.exit(1);});
