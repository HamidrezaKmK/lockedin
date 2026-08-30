/* Interaction smoke test for the visual design.
 *
 * Boots a disposable server, signs in, and drives the SPA: the icon sprite loads, no OS emoji
 * remain in the app chrome, the theme switcher cycles all five palettes, every nav view renders,
 * and the controls whose markup changed (TODO done toggle, archive, close) still work and keep
 * an accessible name.
 *
 * Usage:  node tests/design-smoke.mjs                    # tests the app at /
 *         BASEPATH=/preview node tests/design-smoke.mjs   # point at another mount
 */
import { spawn } from "node:child_process";
import fs from "node:fs"; import net from "node:net"; import os from "node:os"; import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright-core";
const REPO=process.env.LOCKEDIN_REPO || process.cwd(), CHROME=process.env.LOCKEDIN_E2E_CHROME || "/usr/bin/google-chrome";
const BP=process.env.BASEPATH||"/";
async function freePort(){const s=net.createServer();await new Promise(r=>s.listen(0,"127.0.0.1",r));const{port}=s.address();await new Promise(r=>s.close(r));return port;}
async function waitFor(b,c,o){const d=Date.now()+40000;while(Date.now()<d){if(c.exitCode!==null)throw new Error("died\n"+o());try{const r=await fetch(b+"/api/health");if(r.status<500)return;}catch{}await delay(150);}throw new Error("timeout");}
let pass=0,fail=0;
const ok=(c,m)=>{ if(c){pass++;console.log("  ok   "+m);} else {fail++;console.log("  FAIL "+m);} };
async function main(){
  const dataRoot=fs.mkdtempSync(path.join(os.tmpdir(),"sm-"));
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
    const u="sm"+Date.now(); await post("/api/signup",{username:u,password:"temporary-password"});
    const {slug}=await(await post("/api/bubbles",{name:"Smoke bubble"})).json();
    await post(`/api/bubbles/${slug}/approve`,{instructions:"x"});
    await post("/api/todos",{title:"A task to open"});
    const errs=[];
    const p=await ctx.newPage();
    p.on("pageerror",e=>errs.push(e.message));
    await p.goto(base+BP,{waitUntil:"networkidle"}); await delay(1200);

    ok(await p.locator("#li-icon-sprite").count()===1,"icon sprite injected once");
    ok(await p.locator("svg.li-ic").count()>8,"icons rendered ("+await p.locator("svg.li-ic").count()+" on first paint)");
    // no emoji left in the app chrome
    const leftover=await p.evaluate(()=>{
      const re=/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}]/u;
      const hits=[];
      const w=document.createTreeWalker(document.getElementById("app"),NodeFilter.SHOW_TEXT);
      let n; while((n=w.nextNode())){ const t=n.textContent; if(re.test(t)) hits.push(t.trim().slice(0,40)); }
      return hits;
    });
    ok(leftover.length===0,"no emoji left in app chrome"+(leftover.length?": "+JSON.stringify(leftover.slice(0,5)):""));

    // theme cycling through all five
    const seen=[];
    for(let i=0;i<6;i++){
      seen.push(await p.evaluate(()=>document.body.className));
      await p.click("#themeCycle"); await delay(320);
    }
    ok(new Set(seen).size===5,"theme switcher cycles all five ("+new Set(seen).size+")");
    ok(await p.locator("#themeCycle svg.li-ic").count()===1,"theme button shows an icon after cycling");
    // The top bar reveals itself on any interaction with it and covers the first nav row until
    // its idle timer fires. Step away and let it retract before driving the sidebar.
    await p.mouse.move(720,600); await delay(3200);

    // nav
    for(const v of ["home","bubbles","assets","todos","workspace","settings"]){
      await p.click(`.navbtn[data-view="${v}"]`); await delay(700);
      ok(await p.locator("#main").innerText().then(t=>t.trim().length>0),`nav: ${v} renders`);
    }
    // theme swatches in settings
    ok(await p.locator(".theme-swatch").count()===5,"settings shows five palette swatches");
    ok(await p.locator('input[type=checkbox]').first().isVisible(),"custom checkbox visible");

    // todo detail + done toggle label
    await p.click('.navbtn[data-view="todos"]'); await delay(800);
    await p.locator("#main .home-row").first().click(); await delay(900);
    const doneTxt=await p.locator('button:has-text("Mark done")').first().innerText().catch(()=>"");
    ok(/Mark done/.test(doneTxt),"TODO done button reads 'Mark done' (icon + label, not [object])");

    // bubble home + agent dialog
    await p.click('.navbtn[data-view="bubbles"]'); await delay(800);
    await p.locator("#main .grid .acard").first().click(); await delay(2600);
    ok(await p.locator(".tk-band").count()>0,"bubble home rendered by the forked talks.js");
    ok(await p.locator(".tk-empty").count()>0,"chalk-talks empty state present");
    const emptyTxt=await p.locator(".tk-empty").first().innerText();
    ok(!/seed the demo data/.test(emptyTxt),"empty state no longer says 'seed the demo data'");

    // Emoji in menus and dialogs. The first sweep only walked what was on screen at load, which
    // is exactly how a whole menu of them survived: nothing renders it until you open it.
    const scanEmoji = async (label) => {
      const hits = await p.evaluate(() => {
        const re = /[\u{1F000}-\u{1FAFF}\u{2190}-\u{21FF}\u{2300}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE0F}]/u;
        const out = [];
        const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let n; while ((n = w.nextNode())) {
          if (!n.parentElement || !n.parentElement.offsetParent) continue;
          const t = n.textContent; if (re.test(t)) out.push(t.trim().slice(0, 40));
        }
        return out;
      });
      ok(hits.length === 0, `no emoji in ${label}` + (hits.length ? ": " + JSON.stringify(hits.slice(0, 6)) : ""));
    };
    // Home is a workspace digest now, not the marketing page.
    await p.click('.navbtn[data-view="home"]'); await delay(1800);
    const homeTxt=await p.locator("#main").innerText();
    ok(!/built out of my fascination|Bring your own AI sub|Open source, yours to shape/.test(homeTxt),
       "Home no longer renders the marketing page");
    ok(await p.locator("#main .home-band, #main .home-empty").count() > 0,
       "Home shows the workspace digest");

    // TODOs is a list of rows, not a grid of cards.
    await p.click('.navbtn[data-view="todos"]'); await delay(1200);
    ok(await p.locator("#main .home-row").count() > 0, "TODOs render as rows");
    ok(await p.locator("#main .grid .acard").count() === 0, "TODOs are no longer a card grid");

    // the bubble toolbar's ⋮ menu — Bubble home / Papers / Assets / Overleaf / sharing
    await p.click('.navbtn[data-view="bubbles"]'); await delay(700);
    await p.locator("#main .grid .acard").first().click(); await delay(2400);
    const more = p.locator(".toolmenu-host button, [class*=toolmenu] button").first();
    if (await more.count()) { await more.click(); await delay(500); }
    ok(await p.locator(".toolmenu-panel").count() > 0, "bubble tool menu opens");
    await scanEmoji("the bubble tool menu");
    ok(await p.locator(".toolmenu-panel .toolmenu-icon svg.li-ic").count() >= 4,
       "tool-menu rows carry icons (" + await p.locator(".toolmenu-panel .toolmenu-icon svg.li-ic").count() + ")");
    ok(await p.locator('.toolmenu-panel img[src^="http"]').count() === 0,
       "no icon is fetched from a third-party origin");
    await p.keyboard.press("Escape"); await p.mouse.click(700, 600); await delay(300);

    // the presence cluster's own menus
    for (const sel of ['[class*=presence] button', '#accountBtn']) {
      const el2 = p.locator(sel).first();
      if (await el2.count()) { await el2.click().catch(()=>{}); await delay(450); await scanEmoji(sel); await p.mouse.click(700, 600); await delay(250); }
    }

    // archive is now an icon button and still labelled for screen readers
    await p.click('.navbtn[data-view="bubbles"]'); await delay(800);
    ok(await p.locator('button[aria-label*="Archive this bubble"] svg.li-ic').count()>0,"archive is an icon button with an aria-label");
    ok(await p.locator('button[aria-label="Delete this bubble"]').count()>0,"delete keeps an accessible name");

    console.log("\npage errors: "+(errs.length?JSON.stringify([...new Set(errs)]):"(none)"));
    if(errs.length) fail++;
    console.log(`\n${pass} passed, ${fail} failed`);
    process.exitCode = fail?1:0;
  } finally { if(browser)await browser.close().catch(()=>{}); if(child)child.kill("SIGTERM"); }
}
main().catch(e=>{console.error(e);process.exit(1);});
