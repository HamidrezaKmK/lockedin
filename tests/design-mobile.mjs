/* Mobile layout sweep.
 *
 * Renders every view at phone and small-tablet widths, in whichever themes are asked for, and
 * writes both a viewport shot and a full-height shot so vertical rhythm can be judged rather
 * than guessed. Also dumps measured gaps between the major blocks of each view.
 *
 * Usage: OUT=/tmp/mob THEMES=dark,light node tests/design-mobile.mjs
 */
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs"; import net from "node:net"; import os from "node:os"; import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright-core";
const REPO=process.env.LOCKEDIN_REPO||process.cwd();
const CHROME=process.env.LOCKEDIN_E2E_CHROME||"/usr/bin/google-chrome";
const OUT=process.env.OUT||"/tmp/mob"; fs.mkdirSync(OUT,{recursive:true});
const BP=process.env.BASEPATH||"/";
/* Point at an existing data root with real content, or leave unset and the sweep seeds a
   throwaway one via scripts/seed_talks_demo.py (user `talks`, password `talks`). */
const HOME=process.env.LOCKEDIN_HOME||fs.mkdtempSync(path.join(os.tmpdir(),"design-mobile-"));
const SEEDED=!process.env.LOCKEDIN_HOME;
const THEMES=(process.env.THEMES||"dark").split(",");
const DEVICE=process.env.DEVICE||"iphone";
const DEVICES={
  iphone:{viewport:{width:390,height:844},isMobile:true,hasTouch:true,deviceScaleFactor:3},
  small:{viewport:{width:360,height:780},isMobile:true,hasTouch:true,deviceScaleFactor:3},
  android:{viewport:{width:412,height:915},isMobile:true,hasTouch:true,deviceScaleFactor:2.5},
  ipad:{viewport:{width:834,height:1112},hasTouch:true,deviceScaleFactor:2},
};
async function freePort(){const s=net.createServer();await new Promise(r=>s.listen(0,"127.0.0.1",r));const{port}=s.address();await new Promise(r=>s.close(r));return port;}
async function waitFor(b,c){const d=Date.now()+40000;while(Date.now()<d){if(c.exitCode!==null)throw new Error("died");try{const r=await fetch(b+"/api/health");if(r.status<500)return;}catch{}await delay(150);}throw new Error("timeout");}

/** Vertical gaps between the top-level blocks a view renders, so "awkward" becomes a number. */
const GAPS = () => {
  const main=document.getElementById("main"); if(!main) return null;
  const kids=[...main.children].filter(e=>e.offsetParent!==null||getComputedStyle(e).position==="fixed");
  const rows=[];
  for(let i=0;i<kids.length;i++){
    const r=kids[i].getBoundingClientRect();
    const label=kids[i].tagName.toLowerCase()+(kids[i].className?"."+String(kids[i].className).split(" ").filter(Boolean).slice(0,2).join("."):"");
    const next=kids[i+1]?kids[i+1].getBoundingClientRect():null;
    rows.push({el:label.slice(0,34), h:Math.round(r.height), gapAfter:next?Math.round(next.top-r.bottom):null});
  }
  const pad=getComputedStyle(main);
  return {padding:pad.padding, blocks:rows};
};

async function main(){
  if(SEEDED){
    console.log("seeding a demo workspace in",HOME);
    const r=spawnSync("uv",["run","python","scripts/seed_talks_demo.py"],
      {cwd:REPO,env:{...process.env,LOCKEDIN_HOME:HOME},encoding:"utf8"});
    // The seeder currently dies on its last step (scripts/seed_talks_demo.py calls
    // talks.revise_slide, which no longer exists). It gets far enough to produce a bubble with
    // pages and decks, which is all this sweep needs, so carry on and say so plainly.
    if(r.status!==0){
      const why=(r.stderr||"").trim().split("\n").pop();
      console.log("  seed script stopped early ("+why+") — continuing with what it created");
    }
  }
  const port=await freePort(), base=`http://127.0.0.1:${port}`;
  const child=spawn("uv",["run","lockedin","serve","--host","127.0.0.1","--port",String(port)],
    {cwd:REPO,env:{...process.env,LOCKEDIN_HOME:HOME,LOCKEDIN_INSECURE_COOKIE:"1"},stdio:["ignore","pipe","pipe"]});
  await waitFor(base,child);
  const b=await chromium.launch({executablePath:CHROME,headless:true,args:["--no-sandbox","--disable-dev-shm-usage"]});
  const ctx=await b.newContext(DEVICES[DEVICE]);
  const lr=await ctx.request.fetch(base+"/api/login",{method:"POST",data:{username:"talks",password:"talks"}});
  if(!lr.ok()) console.log("login failed",lr.status());
  const bl=await (await ctx.request.fetch(base+"/api/bubbles")).json();
  const slug=(bl.bubbles.find(x=>x.slug!=="tutorial")||bl.bubbles[0]).slug;
  const det=await (await ctx.request.fetch(base+`/api/bubbles/${slug}`)).json();
  const page=(det.bubble.pages||[])[1]?.page_slug||det.bubble.home;
  const tk=await (await ctx.request.fetch(base+`/api/bubbles/${slug}/talks`)).json();
  const talk=(tk.talks||[])[0]?.id;
  const p=await ctx.newPage();
  const errs=[];
  p.on("pageerror",e=>errs.push(e.message));
  const VIEWS=[
    ["home","#home"],["bubbles","#bubbles"],["library","#assets"],["todos","#todos"],
    ["workspace","#workspace"],["settings","#settings"],
    ["bubblehome","#bubble/"+slug],
    ["page","#bubble/"+slug+"/"+page],
    ...(talk?[["deck","#bubble/"+slug+"/talk/"+encodeURIComponent(talk)+"/slide/1"]]:[]),
  ];
  for(const th of THEMES){
    await p.goto(base+BP,{waitUntil:"networkidle"});
    await p.evaluate(t=>localStorage.setItem("li_theme",t),th);
    for(const [name,hash] of VIEWS){
      await p.goto(base+(BP==="/"?"":BP)+hash,{waitUntil:"networkidle"});
      await p.reload({waitUntil:"networkidle"});
      await delay(name==="deck"||name==="page"||name==="bubblehome"?3000:1600);
      await p.screenshot({path:`${OUT}/${DEVICE}-${th}-${name}.png`});
      try{ await p.screenshot({path:`${OUT}/${DEVICE}-${th}-${name}-full.png`,fullPage:true}); }catch(e){}
      if(th===THEMES[0]){
        const g=await p.evaluate(GAPS);
        if(g) console.log(`\n[${name}] main padding ${g.padding}`),
              g.blocks.forEach(r=>console.log(`   ${String(r.el).padEnd(34)} h=${String(r.h).padStart(5)}  gap-after=${r.gapAfter===null?"-":r.gapAfter}`));
      }
    }
    // logged-out landing
    const anon=await b.newContext(DEVICES[DEVICE]);
    const ap=await anon.newPage();
    await ap.goto(base+BP,{waitUntil:"networkidle"}); await delay(1500);
    await ap.screenshot({path:`${OUT}/${DEVICE}-${th}-landing.png`});
    await ap.screenshot({path:`${OUT}/${DEVICE}-${th}-landing-full.png`,fullPage:true});
    await anon.close();
    console.log("theme done:",th);
  }
  console.log("\npage errors:",errs.length?[...new Set(errs)].join(" | "):"(none)");
  await b.close(); child.kill("SIGTERM");
}
main().catch(e=>{console.error(e);process.exit(1);});
