/* Screenshots of a chalk talk: macros, tables, wikilinks and the slide pager.
 *
 * Usage: OUT=/tmp/talkshots node tests/talk-render-shots.mjs
 */
import { spawn } from "node:child_process";
import fs from "node:fs"; import net from "node:net"; import os from "node:os"; import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright-core";
const REPO=process.env.LOCKEDIN_REPO||process.cwd(), CHROME=process.env.LOCKEDIN_E2E_CHROME||"/usr/bin/google-chrome";
const OUT=process.env.OUT||"/tmp/talkshots"; fs.mkdirSync(OUT,{recursive:true});
async function freePort(){const s=net.createServer();await new Promise(r=>s.listen(0,"127.0.0.1",r));const{port}=s.address();await new Promise(r=>s.close(r));return port;}
async function waitFor(b,c,o){const d=Date.now()+40000;while(Date.now()<d){if(c.exitCode!==null)throw new Error("died\n"+o());try{const r=await fetch(b+"/api/health");if(r.status<500)return;}catch{}await delay(150);}throw new Error("timeout\n"+o());}

const PAGE_MD = String.raw`# Pretrained VAMP

The optimality theorem lives here: $\E[\psi(X_T)] = \Var[\psi]^{1/2}$, and the bound is on
[[Estimator variance]] — a link written as the human title, not the slug.

| observable | estimator | bias |
|---|---|---|
| reward | VAMP chart | $O(\epsilon)$ |
| class | entropy clock | none |

$$\E[\psi] = \int \psi \, dp$$
`;

const RICH = String.raw`<!-- slide: kind=derivation -->
# The clock, and what it reads

*Every clock needs a thing to read it off*

The bridge expectation $\E[\psi(X_T) \mid X_s]$ is the object the sampler actually
evaluates, and $\Var[\psi]$ is what the estimator's variance is written in.

$$\E\!\left[\psi(X_T)\,\middle|\,X_s\right] = \int \psi(x)\, p_{T|s}(x \mid X_s)\, dx$$

| clock | what it needs | what it resolves | on which process |
|---|---|---|---|
| Biroli speciation time | data covariance, Gaussian-mixture theory | class | ideal score, in theory |
| Li-Chen critical windows | bounds for log-concave mixtures | class or a named feature | theory only |
| Handke entropy clock | class labels, online Bayes classifier | class, or a partition of it | a trained model |
| VAMP chart readout | endpoint pairs from rollouts | any observable, including a reward | whichever sampler you ran |

- **What we bring beyond the table.** The optimality theorem on [[pretrained-vamp]]
  gives the closed form, and [[Estimator variance|the variance page]] carries the bound.
- A link that points nowhere: [[no such page here]].

> The estimator is unbiased whenever $\E[\psi]$ exists.
`;

function slide(i){
  return `<!-- slide: kind=setup -->\n# Slide number ${i}\n\n*A filler slide so the deck is long*\n\nBody text for slide ${i}. Inline math $\\E[X_${i}]$ rides along.\n`;
}

async function main(){
  const dataRoot=fs.mkdtempSync(path.join(os.tmpdir(),"tk-"));
  const port=await freePort(), base=`http://127.0.0.1:${port}`;
  let out="",child,browser;
  try{
    child=spawn("uv",["run","lockedin","serve","--host","127.0.0.1","--port",String(port)],
      {cwd:REPO,env:{...process.env,LOCKEDIN_HOME:dataRoot,LOCKEDIN_INSECURE_COOKIE:"1",PYTHONUNBUFFERED:"1"},stdio:["ignore","pipe","pipe"]});
    child.stdout.on("data",c=>out+=c);child.stderr.on("data",c=>out+=c);
    await waitFor(base,child,()=>out); console.log("server up",base);
    browser=await chromium.launch({executablePath:CHROME,headless:true,args:["--no-sandbox","--disable-dev-shm-usage"]});

    const seed=await browser.newContext();
    const user="tk"+Date.now();
    const req=(p,d,m)=>seed.request.fetch(base+p,{method:m||"POST",data:d});
    await req("/api/signup",{username:user,password:"temporary-password"});
    await req("/api/settings/math",{macros:{"\\E":"\\mathbb{E}","\\Var":"\\operatorname{Var}"}},"PUT");
    const {slug}=await(await req("/api/bubbles",{name:"Speciation clocks"})).json();
    await req(`/api/bubbles/${slug}/approve`,{instructions:"Read the clock off a sampler."});
    // Two extra pages so a wikilink has somewhere to land.
    const mkPage=async(title,content)=>{
      const {page_slug}=await(await req(`/api/bubbles/${slug}/pages`,{title})).json();
      const cur=await(await seed.request.fetch(`${base}/api/bubbles/${slug}/pages/${page_slug}`)).json();
      await req(`/api/bubbles/${slug}/pages/${page_slug}`,{content,base_mtime:cur.mtime??null},"PUT");
      return page_slug;
    };
    const vampSlug=await mkPage("Pretrained VAMP",PAGE_MD);
    await mkPage("Estimator variance","# Estimator variance\n\nThe bound.\n");
    console.log("vamp page slug:",vampSlug);
    await req(`/api/bubbles/${slug}/premise`,{
      abstract:"Read a speciation clock off any sampler. The readout is $\\E[\\psi(X_T)\\mid X_s]$, "+
               "and the derivation is on [[Pretrained VAMP]].",
      goal:"Get $\\Var[\\psi]$ below the Handke bound."},"PUT");
    const deck=[RICH,...Array.from({length:9},(_,i)=>slide(i+2))].join("\n\n---\n\n");
    const {id:talkId}=await(await req(`/api/bubbles/${slug}/talks`,{title:"Reading the clock",body:deck})).json();
    const shortDeck=[slide(1),slide(2),slide(3)].join("\n\n---\n\n");
    const {id:shortId}=await(await req(`/api/bubbles/${slug}/talks`,{title:"A short deck",body:shortDeck})).json();
    const cookies=await seed.cookies(); await seed.close();

    const errs=[];
    const ctx=await browser.newContext({viewport:{width:1440,height:960}});
    await ctx.addCookies(cookies);
    const p=await ctx.newPage();
    p.on("pageerror",e=>errs.push("pageerror: "+e.message));
    p.on("console",m=>{if(m.type()==="error")errs.push("console: "+m.text().slice(0,160));});
    const shot=async(name,sel)=>{
      const el=sel?await p.$(sel):null;
      await (el||p).screenshot({path:path.join(OUT,name+".png")});
      console.log("wrote",name);
    };
    const goSlide=async n=>{
      await p.goto(`${base}/#bubble/${slug}/talk/${encodeURIComponent(talkId)}/slide/${n}`,{waitUntil:"networkidle"});
      await p.waitForSelector(".tk-slide",{timeout:15000}); await delay(700);
    };
    await goSlide(1);
    await shot("01-rich-slide",".tk-stage");
    await shot("01-rich-full");
    // What the math actually became, and whether the wikilinks became links.
    const probe=await p.evaluate(()=>{
      const md=document.querySelector(".tk-slide .tk-md");
      return {
        katexErrors:[...md.querySelectorAll(".katex-error")].map(e=>e.textContent).slice(0,5),
        tables:md.querySelectorAll("table").length,
        tableBorder:(()=>{const td=md.querySelector("td");return td?getComputedStyle(td).borderTopWidth:null;})(),
        wikiLinks:[...md.querySelectorAll("a.tk-wikilink")].map(a=>a.textContent),
        rawWiki:(md.textContent.match(/\[\[[^\]]+\]\]/g)||[]),
        macros:JSON.stringify((window.S&&window.S.mathMacros)||null),
      };
    });
    console.log("PROBE slide1:",JSON.stringify(probe,null,1));
    // Clicking a resolved wikilink has to leave the deck and open that page.
    await p.click(".tk-slide a.tk-wikilink[data-page]");
    await p.waitForFunction(()=>location.hash.includes("/pretrained-vamp"),{timeout:8000});
    await delay(700); await shot("02-clicked-wikilink");
    console.log("PROBE nav:",p.url().split("#")[1]);
    await goSlide(1);
    // The broken one must not navigate; it says so instead.
    await p.click(".tk-slide a.tk-wikilink.unresolved"); await delay(500);
    console.log("PROBE broken-click hash:",p.url().split("#")[1],
                "| toast:",await p.evaluate(()=>{const t=document.querySelector(".tk-toast");return t?t.textContent:null;}));
    await shot("03-broken-wikilink",".tk-stage");
    for(const n of [7,8,10]){ await goSlide(n); await shot(`pager-${n}`,".tk-foot"); }
    const dots=await p.evaluate(()=>{
      const d=document.querySelector(".tk-dots"), on=d.querySelector(".tk-dot.on");
      const r=d.getBoundingClientRect(), o=on.getBoundingClientRect();
      return {stripW:Math.round(r.width),scrollW:Math.round(d.scrollWidth),
              activeLeft:Math.round(o.left-r.left),activeVisible:o.left>=r.left-1&&o.right<=r.right+1};
    });
    console.log("PROBE dots slide10:",JSON.stringify(dots));
    // A document page: index.html's own renderer, checked for the same three things.
    await p.goto(`${base}/#bubble/${slug}/${vampSlug}`,{waitUntil:"networkidle"}); await delay(900);
    await shot("page-vamp");
    console.log("PROBE page:",JSON.stringify(await p.evaluate(()=>{
      const pv=document.querySelector("#previewWrap")||document.body;
      return {katexErrors:[...pv.querySelectorAll(".katex-error")].map(e=>e.textContent).slice(0,3),
              tables:pv.querySelectorAll("table").length,
              wiki:[...pv.querySelectorAll("a.wikilink")].map(a=>a.textContent+(a.className.includes("unresolved")?" (broken)":"")),
              raw:(pv.textContent.match(/\[\[[^\]]+\]\]/g)||[])};
    })));
    // A short deck: the dots alone say where you are, so no counter beside them.
    await p.goto(`${base}/#bubble/${slug}/talk/${encodeURIComponent(shortId)}/slide/2`,{waitUntil:"networkidle"});
    await p.waitForSelector(".tk-slide",{timeout:15000}); await delay(700);
    await shot("pager-short",".tk-foot");
    // And the edit-mode footer, which carries the same pager.
    await p.goto(`${base}/#bubble/${slug}/talk/${encodeURIComponent(talkId)}/slide/9/edit`,{waitUntil:"networkidle"});
    await p.waitForSelector(".tk-editcard",{timeout:15000}); await delay(1200);
    await shot("pager-edit",".tk-foot");
    console.log("PROBE edit dots:",JSON.stringify(await p.evaluate(()=>{
      const d=document.querySelector(".tk-dots"),on=d.querySelector(".tk-dot.on");
      const r=d.getBoundingClientRect(),o=on.getBoundingClientRect();
      return {visible:o.left>=r.left-1&&o.right<=r.right+1,fade:d.dataset.fade};
    })));
    // Home page of the bubble: abstract also goes through the same markdown pipeline.
    await p.goto(`${base}/#bubble/${slug}`,{waitUntil:"networkidle"}); await delay(900);
    await shot("home");
    console.log("PROBE home:",JSON.stringify(await p.evaluate(()=>{
      const a=document.querySelector(".tk-abstract");
      return {wiki:[...a.querySelectorAll("a.tk-wikilink")].map(x=>x.textContent),
              raw:(a.textContent.match(/\[\[[^\]]+\]\]/g)||[]),
              katexErrors:[...a.querySelectorAll(".katex-error")].length,
              html:a.innerHTML.slice(0,120)};
    })));
    // Narrow: the pager is tightest on a phone, which is where clipped dots first showed up.
    const mob=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true,deviceScaleFactor:2});
    await mob.addCookies(cookies);
    const mp=await mob.newPage();
    await mp.goto(`${base}/#bubble/${slug}/talk/${encodeURIComponent(talkId)}/slide/6`,{waitUntil:"networkidle"});
    await mp.waitForSelector(".tk-slide",{timeout:15000}); await delay(900);
    await mp.screenshot({path:path.join(OUT,"mobile-slide6.png")});
    console.log("PROBE mobile dots:",JSON.stringify(await mp.evaluate(()=>{
      const d=document.querySelector(".tk-dots"),on=d.querySelector(".tk-dot.on");
      const r=d.getBoundingClientRect(),o=on.getBoundingClientRect();
      return {visible:o.left>=r.left-1&&o.right<=r.right+1,fade:d.dataset.fade};
    })));
    // The rich slide on a narrow card: the table has to scroll inside the card, not past it.
    await mp.goto(`${base}/#bubble/${slug}/talk/${encodeURIComponent(talkId)}/slide/1`,{waitUntil:"networkidle"});
    await mp.waitForSelector(".tk-slide",{timeout:15000}); await delay(900);
    await mp.screenshot({path:path.join(OUT,"mobile-slide1.png"),fullPage:true});
    console.log("PROBE mobile slide1:",JSON.stringify(await mp.evaluate(()=>{
      const card=document.querySelector(".tk-slide"),w=document.querySelector(".tk-tablewrap");
      return {cardW:Math.round(card.getBoundingClientRect().width),
              tableSpills:w.getBoundingClientRect().right>card.getBoundingClientRect().right+1,
              tableScrolls:w.scrollWidth>w.clientWidth+1};
    })));
    await mob.close();
    console.log(errs.length?"ERRORS:\n"+errs.join("\n"):"no page errors");
    await ctx.close();
  } finally {
    if(browser)await browser.close();
    if(child)child.kill("SIGINT");
  }
}
main().catch(e=>{console.error(e);process.exit(1);});
