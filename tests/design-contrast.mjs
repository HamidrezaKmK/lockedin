/* Walks the rendered DOM in every theme and reports text that fails WCAG AA against its
 * effective background, plus interactive borders that fail 1.4.11. Real computed styles, so it
 * catches the derived colours (color-mix, rgba overlays) a token table cannot. */
import { spawn } from "node:child_process";
import fs from "node:fs"; import net from "node:net"; import os from "node:os"; import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright-core";
const REPO=process.env.LOCKEDIN_REPO || process.cwd(), CHROME=process.env.LOCKEDIN_E2E_CHROME || "/usr/bin/google-chrome";
const BASEPATH=process.env.BASEPATH||"/";
async function freePort(){const s=net.createServer();await new Promise(r=>s.listen(0,"127.0.0.1",r));const{port}=s.address();await new Promise(r=>s.close(r));return port;}
async function waitFor(b,c,o){const d=Date.now()+40000;while(Date.now()<d){if(c.exitCode!==null)throw new Error("died\n"+o());try{const r=await fetch(b+"/api/health");if(r.status<500)return;}catch{}await delay(150);}throw new Error("timeout");}

const AUDIT = () => {
  const sr=c=>{c/=255;return c<=0.03928?c/12.92:Math.pow((c+0.055)/1.055,2.4);};
  const lum=([r,g,b])=>0.2126*sr(r)+0.7152*sr(g)+0.0722*sr(b);
  const cr=(a,b)=>{const la=lum(a),lb=lum(b),hi=Math.max(la,lb),lo=Math.min(la,lb);return (hi+0.05)/(lo+0.05);};
  const parse=s=>{const m=String(s).match(/rgba?\(([^)]+)\)/);if(!m)return null;
    const p=m[1].split(/[,\s/]+/).filter(Boolean).map(Number);
    return {rgb:[p[0],p[1],p[2]],a:p.length>3?p[3]:1};};
  const over=(fg,bg)=>fg.rgb.map((c,i)=>c*fg.a+bg[i]*(1-fg.a));
  /** Average the stops of a linear/radial gradient — an element painted with `background:
   *  linear-gradient(...)` has a transparent background-COLOR, so reading only that walked
   *  straight past the sidebar and compared its white text against the page canvas. */
  const gradientColor=(img)=>{
    const stops=String(img).match(/rgba?\([^)]+\)/g);
    if(!stops||!stops.length)return null;
    const parsed=stops.map(parse).filter(c=>c&&c.a>0.4);
    if(!parsed.length)return null;
    const n=parsed.length;
    return {rgb:[0,1,2].map(i=>parsed.reduce((a,c)=>a+c.rgb[i],0)/n),a:1};
  };
  function bgOf(el){
    let node=el, stack=[];
    while(node&&node.nodeType===1){
      const cs=getComputedStyle(node);
      const c=parse(cs.backgroundColor);
      if(c&&c.a>0){ stack.push(c); if(c.a>=0.999) break; }
      const g=cs.backgroundImage&&cs.backgroundImage!=="none"?gradientColor(cs.backgroundImage):null;
      if(g){ stack.push(g); break; }
      node=node.parentElement;
    }
    let base=[255,255,255];
    const html=parse(getComputedStyle(document.documentElement).backgroundColor);
    if(html&&html.a>=0.999)base=html.rgb;
    for(let i=stack.length-1;i>=0;i--) base=over(stack[i],base);
    return base;
  }
  const out=[];
  const seen=new Set();
  document.querySelectorAll("body *").forEach(el=>{
    const cs=getComputedStyle(el);
    if(cs.display==="none"||cs.visibility==="hidden"||parseFloat(cs.opacity)<0.15) return;
    const r=el.getBoundingClientRect();
    if(r.width<2||r.height<2) return;
    // direct text only
    const txt=[...el.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent.trim()).join(" ").trim();
    const bg=bgOf(el);
    if(txt.length>1){
      const fg=parse(cs.color);
      if(fg){
        const c=over(fg,bg);
        const size=parseFloat(cs.fontSize), weight=parseInt(cs.fontWeight)||400;
        const large=size>=24||(size>=18.66&&weight>=700);
        const need=large?3:4.5;
        const ratio=cr(c,bg);
        if(ratio<need){
          const key=el.className+"|"+txt.slice(0,24);
          if(!seen.has(key)){ seen.add(key);
            out.push({kind:"text",ratio:+ratio.toFixed(2),need,sel:(el.tagName.toLowerCase()+"."+String(el.className||"").split(" ").filter(Boolean).slice(0,2).join(".")),
                      size:+size.toFixed(1),text:txt.slice(0,46)});
          }
        }
      }
    }
    // interactive boundaries
    if(/^(BUTTON|INPUT|SELECT|TEXTAREA)$/.test(el.tagName)||el.classList.contains("acard")||el.classList.contains("card")){
      const bw=parseFloat(cs.borderTopWidth)||0;
      if(bw>0){
        const bc=parse(cs.borderTopColor);
        if(bc&&bc.a>0.05){
          const outside=bgOf(el.parentElement||document.body);
          const c=over(bc,outside);
          const ratio=cr(c,outside);
          if(ratio<3){
            const key="B|"+el.className+"|"+(el.textContent||"").trim().slice(0,18);
            if(!seen.has(key)){ seen.add(key);
              out.push({kind:"border",ratio:+ratio.toFixed(2),need:3,
                sel:el.tagName.toLowerCase()+"."+String(el.className||"").split(" ").filter(Boolean).slice(0,2).join("."),
                text:(el.textContent||"").trim().slice(0,32)});
            }
          }
        }
      }
    }
  });
  return out;
};

async function main(){
  const dataRoot=fs.mkdtempSync(path.join(os.tmpdir(),"ct-"));
  const port=await freePort(), base=`http://127.0.0.1:${port}`;
  let out="",child,browser;
  try{
    child=spawn("uv",["run","lockedin","serve","--host","127.0.0.1","--port",String(port)],
      {cwd:REPO,env:{...process.env,LOCKEDIN_HOME:dataRoot,LOCKEDIN_INSECURE_COOKIE:"1",PYTHONUNBUFFERED:"1"},stdio:["ignore","pipe","pipe"]});
    child.stdout.on("data",c=>out+=c);child.stderr.on("data",c=>out+=c);
    await waitFor(base,child,()=>out);
    browser=await chromium.launch({executablePath:CHROME,headless:true,args:["--no-sandbox","--disable-dev-shm-usage"]});
    const ctx=await browser.newContext({viewport:{width:1440,height:960}});
    const post=(p,d)=>ctx.request.fetch(base+p,{method:"POST",data:d});
    const user="ct"+Date.now();
    await post("/api/signup",{username:user,password:"temporary-password"});
    const {slug}=await(await post("/api/bubbles",{name:"Manifold priors in diffusion"})).json();
    await post(`/api/bubbles/${slug}/approve`,{instructions:"Estimate local intrinsic dimension."});
    await post("/api/todos",{title:"Re-run the sigma sweep"});
    const p=await ctx.newPage();
    await p.goto(base+BASEPATH,{waitUntil:"networkidle"}); await delay(900);
    let total=0;
    for(const th of ["dark","light","pink","techno","pearl"]){
      await p.evaluate(t=>localStorage.setItem("li_theme",t),th);
      await p.goto(base+BASEPATH,{waitUntil:"networkidle"}); await delay(1400);
      const byView={};
      for(const v of ["home","bubbles","assets","todos","workspace","settings"]){
        await p.click(`.navbtn[data-view="${v}"]`).catch(()=>{}); await delay(1000);
        byView[v]=await p.evaluate(AUDIT);
      }
      await p.click('.navbtn[data-view="bubbles"]'); await delay(700);
      const c=p.locator("#main .grid .acard").first();
      if(await c.count()){ await c.click(); await delay(2400); byView["bubblehome"]=await p.evaluate(AUDIT); }
      const merged=new Map();
      for(const [v,list] of Object.entries(byView))
        for(const f of list) merged.set(f.kind+f.sel+f.text,{...f,view:v});
      const list=[...merged.values()].sort((a,b)=>a.ratio-b.ratio);
      total+=list.length;
      console.log(`\n=== ${th.toUpperCase()} — ${list.length} failures ===`);
      list.slice(0,18).forEach(f=>console.log(
        `  ${f.kind==="border"?"BORDER":"text  "} ${String(f.ratio).padStart(5)}/${f.need}  ${f.view.padEnd(11)} ${f.sel.slice(0,40).padEnd(40)} ${JSON.stringify(f.text)}`));
      if(list.length>18) console.log(`  … and ${list.length-18} more`);
    }
    console.log("\nTOTAL FAILURES:",total);
  } finally { if(browser)await browser.close().catch(()=>{}); if(child)child.kill("SIGTERM"); }
}
main().catch(e=>{console.error(e);process.exit(1);});
