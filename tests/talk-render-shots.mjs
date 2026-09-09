/* Screenshots of a chalk talk: macros, tables, wikilinks and the slide pager.
 *
 * Usage: OUT=/tmp/talkshots node tests/talk-render-shots.mjs
 */
import { spawn } from "node:child_process";
import fs from "node:fs"; import net from "node:net"; import os from "node:os"; import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright-core";
import assert from "node:assert/strict";
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
# The clock $\E[\psi]$, and what it reads

*Figure from [[Pretrained VAMP]] §2.1. Held-out error on the vertical axis (log scale, roughly $2\times10^{-3}$ to $6\times10^{-2}$), source time on the horizontal axis ($0$ to $0.9$).*

The bridge expectation $\E[\psi(X_T) \mid X_s]$ is the object the sampler actually
evaluates, and $\Var[\psi]$ is what the estimator's variance is written in.

$$\E\!\left[\psi(X_T)\,\middle|\,X_s\right] = \int \psi(x)\, p_{T|s}(x \mid X_s)\, dx$$

\begin{theorem}[Bridge readout]\label{thm:bridge}
The conditional expectation is sufficient for the endpoint observable.
\end{theorem}

\begin{lemma}\label{lem:clock}
The clock is measurable. This uses \thmref{thm:bridge}.
\end{lemma}

Thus \thmref{lem:clock} applies within this talk.

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
        theoremTitles:[...md.querySelectorAll(".tk-theorem-title")].map(x=>x.textContent),
        theoremRefs:[...md.querySelectorAll(".tk-thm-ref")].map(x=>x.textContent),
        rawTheorem:/\\begin\{(?:theorem|lemma)\}/.test(md.textContent),
        macros:JSON.stringify((window.S&&window.S.mathMacros)||null),
      };
    });
    assert.deepEqual(probe.theoremTitles,["Theorem 1 (Bridge readout)","Lemma 1"]);
    assert.deepEqual(probe.theoremRefs,["Theorem 1","Lemma 1"]);
    assert.equal(probe.rawTheorem,false,"theorem source syntax must not leak onto the slide");
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
    // Title and subtitle are slide content: maths and page links have to render there too.
    await goSlide(1);
    console.log("PROBE headings:",JSON.stringify(await p.evaluate(()=>{
      const t=document.querySelector(".tk-slide h2"),sub=document.querySelector(".tk-slide .sub");
      return {titleMath:t.querySelectorAll(".katex").length, titleRaw:/\$/.test(t.textContent),
              subMath:sub.querySelectorAll(".katex").length, subRaw:/\$|\[\[/.test(sub.textContent),
              subLinks:[...sub.querySelectorAll("a.tk-wikilink")].map(a=>a.textContent)};
    })));
    await shot("04-headings",".tk-slide");
    // A subtitle is markable slide source. Rendering it must not break quote anchoring: select
    // a phrase that straddles the rendered formula and check the mark picker opens.
    console.log("PROBE sub-mark:",JSON.stringify(await p.evaluate(async()=>{
      const sub=document.querySelector(".tk-slide .sub");
      const r=document.createRange(); r.selectNodeContents(sub);
      const sel=getSelection(); sel.removeAllRanges(); sel.addRange(r);
      sub.closest(".tk-slide").dispatchEvent(new MouseEvent("mouseup",{bubbles:true}));
      await new Promise(res=>setTimeout(res,300));
      const pop=document.querySelector(".tk-pop");
      const toast=document.querySelector(".tk-toast");
      return {picker:!!pop, toast:toast?toast.textContent:null};
    })));
    // Pin it, and check it paints back onto the subtitle: quotePattern has to bridge the
    // rendered formula and the rendered link the same way it bridges maths in the body.
    await p.click('.tk-pop .tk-kb[data-k="q"]');
    await p.fill(".tk-pop textarea","Which held-out split?");
    await p.click(".tk-pop [data-pin]");
    await p.waitForSelector(".tk-gutter .tk-note, .tk-gutter [data-note]",{timeout:10000}).catch(()=>{});
    await delay(1200);
    console.log("PROBE mark-paints:",JSON.stringify(await p.evaluate(()=>{
      const sub=document.querySelector(".tk-slide .sub");
      const marks=[...document.querySelectorAll(".tk-slide mark.tk-anno")];
      return {inSub:marks.some(m=>sub.contains(m)),
              total:marks.length,
              gutter:document.querySelectorAll(".tk-gutter .tk-note").length};
    })));
    await shot("06-sub-mark",".tk-stage");

    // Ctrl+Shift+` toggles this deck's notes pane. At 1440px wide the pane is a column gated
    // by S.notes ("no-notes" on the overlay), not the mobile drawer ("notes-open").
    assert.ok(!(await p.evaluate(()=>document.querySelector(".tk-overlay").classList.contains("no-notes"))),
      "the notes pane starts open");
    await p.keyboard.press("Control+Shift+`");
    await p.waitForFunction(()=>document.querySelector(".tk-overlay").classList.contains("no-notes"),{timeout:2000});
    assert.ok(await p.evaluate(()=>document.querySelector(".tk-overlay").classList.contains("no-notes")),
      "Ctrl+Shift+` must hide the deck's notes pane");
    await p.keyboard.press("Control+Shift+`");
    await p.waitForFunction(()=>!document.querySelector(".tk-overlay").classList.contains("no-notes"),{timeout:2000});
    assert.ok(!(await p.evaluate(()=>document.querySelector(".tk-overlay").classList.contains("no-notes"))),
      "pressing it again must bring the notes pane back");
    console.log("PROBE notes-chord: toggled the deck's pane and back");

    // The open/all-closed pill in the gutter header jumps to the all-slides view (S.view
    // "sheet"), the same surface the crumb's "all slides" link reaches. It must not also
    // toggle the pane via the header's own click handler (gh.onclick), which is the
    // stopPropagation regression: capture the drawer/notes-open state first and recheck it
    // is unchanged after the click.
    const pill=await p.$(".tk-gh [data-jump-sheet]");
    assert.ok(pill,"the open/all-closed pill must be a real element in the gutter header");
    const pillText=await pill.evaluate(n=>n.textContent);
    console.log("PROBE pill text:",pillText);
    const notesOpenBefore=await p.evaluate(()=>document.querySelector(".tk-overlay").classList.contains("notes-open"));
    await pill.click();
    await p.waitForSelector(".tk-sheet",{timeout:5000});
    assert.equal(await p.locator(".tk-slide").count(),0,"the deck slide must be gone once the sheet is showing");
    const notesOpenAfter=await p.evaluate(()=>document.querySelector(".tk-overlay").classList.contains("notes-open"));
    assert.equal(notesOpenAfter,notesOpenBefore,
      "clicking the pill must not also toggle the notes pane via the header's click handler");
    await shot("pill-all-slides",".tk-stage");
    console.log("PROBE pill-click: switched to the all-slides view without also toggling the pane");
    await goSlide(1);   // back to the deck for the rest of this script's existing flow

    // The contact sheet shows the same two lines.
    await p.click(".tk-crumb .back"); await delay(700);
    await shot("05-contact-sheet",".tk-sheet");
    console.log("PROBE sheet:",JSON.stringify(await p.evaluate(()=>{
      const m=document.querySelector(".tk-mini");
      return {math:m.querySelectorAll(".katex").length, raw:/\$|\[\[/.test(m.textContent)};
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
