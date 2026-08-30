/* Screenshot sweep for the visual design.
 *
 * Renders every theme across desktop, wide desktop, iPad, an Android tablet, iPhone and an
 * Android phone, signed in and signed out, and writes PNGs for review.
 *
 * Usage:  OUT=/tmp/shots node tests/design-shots.mjs
 *         
 */
import { spawn } from "node:child_process";
import fs from "node:fs"; import net from "node:net"; import os from "node:os"; import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright-core";
const REPO=process.env.LOCKEDIN_REPO || process.cwd(), CHROME=process.env.LOCKEDIN_E2E_CHROME || "/usr/bin/google-chrome";
const OUT=process.env.OUT||"/tmp/nd"; fs.mkdirSync(OUT,{recursive:true});
const BASEPATH=process.env.BASEPATH||"/";
async function freePort(){const s=net.createServer();await new Promise(r=>s.listen(0,"127.0.0.1",r));const{port}=s.address();await new Promise(r=>s.close(r));return port;}
async function waitFor(b,c,o){const d=Date.now()+40000;while(Date.now()<d){if(c.exitCode!==null)throw new Error("died\n"+o());try{const r=await fetch(b+"/api/health");if(r.status<500)return;}catch{}await delay(150);}throw new Error("timeout\n"+o());}
const PAGE_MD=`# Score matching under a manifold prior

The generator concentrates on a $d$-dimensional manifold $\\mathcal{M}\\subset\\mathbb{R}^D$ with $d\\ll D$.
Off-manifold the score $\\nabla_x\\log p_t(x)$ blows up as $t\\to 0$.

## What we measured

| noise | est. dim | LID error | wall clock |
|---|---|---|---|
| 0.01 | 7.9 | 0.11 | 4m12s |
| 0.05 | 8.2 | 0.09 | 3m48s |
| 0.10 | 9.6 | 0.41 | 3m30s |

The estimate is stable at the two smallest noise levels and degrades at 0.10.
`;
const DEVICES={
  desktop:{viewport:{width:1440,height:960}},
  wide:{viewport:{width:1920,height:1080}},
  ipad:{viewport:{width:834,height:1112},hasTouch:true,deviceScaleFactor:2},
  androidtab:{viewport:{width:800,height:1280},hasTouch:true,deviceScaleFactor:2},
  iphone:{viewport:{width:390,height:844},isMobile:true,hasTouch:true,deviceScaleFactor:3,
    userAgent:"Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"},
  android:{viewport:{width:412,height:915},isMobile:true,hasTouch:true,deviceScaleFactor:2.6,
    userAgent:"Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"},
};
async function main(){
  const dataRoot=fs.mkdtempSync(path.join(os.tmpdir(),"nd-"));
  const port=await freePort(), base=`http://127.0.0.1:${port}`;
  let out="",child,browser;
  try{
    child=spawn("uv",["run","lockedin","serve","--host","127.0.0.1","--port",String(port)],
      {cwd:REPO,env:{...process.env,LOCKEDIN_HOME:dataRoot,LOCKEDIN_INSECURE_COOKIE:"1",PYTHONUNBUFFERED:"1"},stdio:["ignore","pipe","pipe"]});
    child.stdout.on("data",c=>out+=c);child.stderr.on("data",c=>out+=c);
    await waitFor(base,child,()=>out); console.log("server up",base);
    browser=await chromium.launch({executablePath:CHROME,headless:true,args:["--no-sandbox","--disable-dev-shm-usage"]});

    // seed once, via a request context we reuse for every browser context's cookies
    const seed=await browser.newContext();
    const user="nd"+Date.now();
    const post=(p,d)=>seed.request.fetch(base+p,{method:"POST",data:d});
    await post("/api/signup",{username:user,password:"temporary-password"});
    const {slug}=await(await post("/api/bubbles",{name:"Manifold priors in diffusion"})).json();
    await post(`/api/bubbles/${slug}/approve`,{instructions:"Estimate local intrinsic dimension from a trained score network."});
    await post("/api/bubbles",{name:"Normalizing flows for OOD detection"});
    await post("/api/bubbles",{name:"Sampler schedules"});
    await post("/api/todos",{title:"Re-run the sigma sweep with the fixed seed"});
    await post("/api/todos",{title:"Ask about the LID estimator's bias term"});
    await post("/api/todos",{title:"Draft the related-work paragraph"});
    const d0=await(await seed.request.fetch(`${base}/api/bubbles/${slug}`)).json();
    const home=d0.bubble.home||"overview";
    const cur=await(await seed.request.fetch(`${base}/api/bubbles/${slug}/pages/${home}`)).json();
    await seed.request.fetch(`${base}/api/bubbles/${slug}/pages/${home}`,{method:"PUT",data:{content:PAGE_MD,base_mtime:cur.mtime??null}});
    const cookies=await seed.cookies(); await seed.close();

    const errs=[];
    async function shootSet(devName, themes, views, tag, anon){
      const ctx=await browser.newContext(DEVICES[devName]);
      if(!anon) await ctx.addCookies(cookies);
      const p=await ctx.newPage();
      p.on("pageerror",e=>errs.push(`${devName}: ${e.message}`));
      p.on("console",m=>{if(m.type()==="error")errs.push(`${devName} console: ${m.text().slice(0,140)}`);});
      await p.goto(base+BASEPATH,{waitUntil:"networkidle"}); await delay(900);
      for(const th of themes){
        await p.evaluate(t=>localStorage.setItem("li_theme",t),th);
        await p.goto(base+BASEPATH,{waitUntil:"networkidle"}); await delay(1500);
        for(const v of views){
          if(v==="landing"){
            await p.evaluate(()=>{const a=document.getElementById("auth");if(a)a.scrollTop=0;});
            await delay(400); await p.screenshot({path:`${OUT}/${tag}-${th}-landing.png`});
            const H=await p.evaluate(()=>{const a=document.getElementById("auth");return a?a.scrollHeight:0;});
            const vh=DEVICES[devName].viewport.height;
            for(let i=1,y=vh;y<H&&i<7;i++,y+=vh){
              await p.evaluate(y=>{document.getElementById("auth").scrollTop=y;},y);
              await delay(350); await p.screenshot({path:`${OUT}/${tag}-${th}-landing-${i}.png`});
            }
            continue;
          }
          if(v==="bubblehome"){
            await p.click('.navbtn[data-view="bubbles"]').catch(()=>{}); await delay(800);
            const c=p.locator("#main .grid .acard").first();
            if(await c.count()){ await c.click(); await delay(2600); }
            await p.screenshot({path:`${OUT}/${tag}-${th}-bubblehome.png`});
            continue;
          }
          await p.click(`.navbtn[data-view="${v}"]`).catch(()=>{}); await delay(1100);
          await p.screenshot({path:`${OUT}/${tag}-${th}-${v}.png`});
        }
      }
      await ctx.close();
    }
    const ALL=["dark","light","pink","techno","pearl"];
    await shootSet("desktop",ALL,["bubbles","bubblehome","todos","settings","assets"],"d");
    await shootSet("desktop",ALL,["landing"],"d",true);
    await shootSet("iphone",["light","dark"],["landing"],"ip",true);
    await shootSet("ipad",["pearl"],["landing"],"pad",true);
    await shootSet("iphone",["light","dark","pearl"],["bubbles","bubblehome","todos","settings"],"ip");
    await shootSet("ipad",["pearl","light"],["bubbles","bubblehome","settings"],"pad");
    await shootSet("android",["pink"],["bubbles","bubblehome","todos"],"an");
    await shootSet("androidtab",["techno"],["bubbles","bubblehome","settings"],"at");
    await shootSet("wide",["light"],["bubbles","bubblehome","settings"],"w");
    console.log("\n--- page errors ---");
    console.log(errs.length?[...new Set(errs)].join("\n"):"(none)");
    console.log("\nshots:",fs.readdirSync(OUT).length,"->",OUT);
  } finally { if(browser)await browser.close().catch(()=>{}); if(child)child.kill("SIGTERM"); }
}
main().catch(e=>{console.error(e);process.exit(1);});
