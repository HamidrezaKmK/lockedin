#!/usr/bin/env node
/**
 * Large-upload regression: a file bigger than the proxy in front of the server will carry.
 *
 * Production is reached through a Cloudflare tunnel, which rejects any request body over 100 MB
 * with a 413 — the origin itself has no such limit. Big figures are therefore sliced by the
 * browser and reassembled server-side. This drives the real Bubble assets dialog in system
 * Chrome, through a stand-in edge that enforces the same cap, and checks that a 140 MB file
 * arrives intact, that one ring reports progress across the whole file, and that closing the
 * dialog mid-flight both stops the upload and leaves no staging behind.
 *
 * Screenshots land in LOCKEDIN_E2E_SHOTS when set.
 */
import { spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs"; import net from "node:net"; import os from "node:os";
import path from "node:path"; import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright-core";

const REPO = process.cwd();
const CHROME = process.env.LOCKEDIN_E2E_CHROME || "/usr/bin/google-chrome";
const SHOTS = process.env.LOCKEDIN_E2E_SHOTS || "";

// A stand-in for the Cloudflare edge: forwards to the origin, but answers anything whose
// Content-Length exceeds the cap with an immediate 413, exactly as the real edge does. Written
// out at run time so the test stays a single file.
const EDGE_PY = String.raw`"""Stand-in for the Cloudflare edge: forwards to the origin, but answers any request whose
Content-Length exceeds LIMIT with an immediate 413, exactly as the real edge does."""
import asyncio, re, sys
LIMIT = 100 * 1024 * 1024
ORIGIN_PORT, LISTEN_PORT = int(sys.argv[1]), int(sys.argv[2])
BODY = (b"<html><head><title>413 Payload Too Large</title></head><body><center>"
        b"<h1>413 Payload Too Large</h1></center><hr><center>cloudflare</center></body></html>")
rejected = 0

async def handle(cr, cw):
    global rejected
    try:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await cr.read(65536)
            if not chunk: cw.close(); return
            head += chunk
        hdr, rest = head.split(b"\r\n\r\n", 1)
        cl = re.search(rb"content-length:\s*(\d+)", hdr, re.I)
        if cl and int(cl.group(1)) > LIMIT:
            rejected += 1
            print(f"[edge] 413 {hdr.split(chr(13).encode())[0][:60]!r} cl={cl.group(1).decode()}", flush=True)
            cw.write(b"HTTP/1.1 413 Payload Too Large\r\nContent-Type: text/html\r\n"
                     b"Connection: close\r\nContent-Length: %d\r\n\r\n" % len(BODY) + BODY)
            await cw.drain(); cw.close(); return
        orr, orw = await asyncio.open_connection("127.0.0.1", ORIGIN_PORT)
        # One request per connection so every request is re-parsed, not pumped blind through a
        # keep-alive socket (which is how a browser would slip past the size check).
        hdr = re.sub(rb"\r\nconnection:[^\r\n]*", b"", hdr, flags=re.I) + b"\r\nConnection: close"
        orw.write(hdr + b"\r\n\r\n" + rest); await orw.drain()
        async def pump(r, w):
            try:
                while True:
                    b = await r.read(65536)
                    if not b: break
                    w.write(b); await w.drain()
            except Exception: pass
            finally:
                try: w.close()
                except Exception: pass
        await asyncio.gather(pump(cr, orw), pump(orr, cw))
    except Exception: pass

async def main():
    s = await asyncio.start_server(handle, "127.0.0.1", LISTEN_PORT)
    async with s: await s.serve_forever()
asyncio.run(main())
`;

async function freePort(){const s=net.createServer();await new Promise(r=>s.listen(0,"127.0.0.1",r));const{port}=s.address();await new Promise(r=>s.close(r));return port;}
const api=async(req,base,method,p,data)=>(await req.fetch(`${base}${p}`,{method,data})).json();
const sha=f=>{const h=crypto.createHash("sha256");h.update(fs.readFileSync(f));return h.digest("hex");};

const dataRoot=fs.mkdtempSync(path.join(os.tmpdir(),"lockedin-chunk-e2e-"));
const origin=await freePort(), edge=await freePort();
const baseUrl=`http://127.0.0.1:${edge}`;      // the browser only ever talks to the capped "edge"
let child,proxy,browser,out="",failures=0;
const check=(n,c,d="")=>{ if(!c)failures++; console.log(`chunked-upload-e2e: ${c?"ok":"FAILED"} — ${n}`+(c?"":` :: ${d}`)); };
try{
  child=spawn("uv",["run","lockedin","serve","--host","127.0.0.1","--port",String(origin)],
    {cwd:REPO,env:{...process.env,LOCKEDIN_HOME:dataRoot,LOCKEDIN_INSECURE_COOKIE:"1",PYTHONUNBUFFERED:"1"},stdio:["ignore","pipe","pipe"]});
  child.stdout.on("data",c=>out+=c); child.stderr.on("data",c=>out+=c);
  const edgeScript=path.join(dataRoot,"fake-edge.py");
  fs.writeFileSync(edgeScript,EDGE_PY);
  proxy=spawn("python3",[edgeScript,String(origin),String(edge)],{stdio:"inherit"});
  const dl=Date.now()+40000;
  for(;;){ if(Date.now()>dl) throw new Error("server timeout\n"+out);
    try{ const r=await fetch(`${baseUrl}/api/health`); if(r.status<500) break; }catch(e){} await delay(150); }
  console.log("chunked-upload-e2e: origin + 100 MB-capped edge ready");

  browser=await chromium.launch({executablePath:CHROME,headless:true,args:["--no-sandbox","--disable-dev-shm-usage"]});
  const ctx=await browser.newContext({viewport:{width:1400,height:900}});
  await api(ctx.request,baseUrl,"POST","/api/signup",{username:`u${Date.now()}`,password:"temporary-password"});
  const {slug}=await api(ctx.request,baseUrl,"POST","/api/bubbles",{name:"Magic Kingdom"});
  await api(ctx.request,baseUrl,"POST",`/api/bubbles/${slug}/approve`,{instructions:""});
  const d=await api(ctx.request,baseUrl,"GET",`/api/bubbles/${slug}`);

  const page=await ctx.newPage();
  const errors=[]; page.on("pageerror",e=>errors.push(String(e)));
  await page.goto(`${baseUrl}/#bubble/${slug}/${d.bubble.home}`,{waitUntil:"domcontentloaded"});
  const openModal=async()=>{
    await page.locator(".hdr-cluster .toolmenu-btn").click();
    await page.locator(".toolmenu-item",{hasText:"Assets"}).click();
    await page.locator(".asset-modal-footer").waitFor({state:"visible",timeout:10000});
  };
  // Throttle the uplink to something tunnel-like, so progress and cancellation are observable
  // rather than finishing before the first sample.
  const cdp=await ctx.newCDPSession(page);
  await cdp.send("Network.enable");
  await cdp.send("Network.emulateNetworkConditions",
    {offline:false,latency:25,downloadThroughput:-1,uploadThroughput:10*1024*1024});

  await openModal();

  // A 140 MB zip: comfortably over the edge's 100 MB cap, so it can only arrive in slices.
  const big=path.join(os.tmpdir(),"magic kingdom.zip");
  const buf=Buffer.alloc(140*1024*1024);
  for(let i=0;i<buf.length;i+=4096) buf.writeUInt32LE(i>>>0,i);   // varied, so truncation shows
  fs.writeFileSync(big,buf);
  const want=sha(big);

  await page.setInputFiles('.asset-modal-footer input[type=file]',big);
  await page.locator(".asset-modal-footer button.primary").click();

  const ring=page.locator(".upload-ring");
  const seen=[]; let shot=false;
  for(let i=0;i<1200;i++){
    if(!(await ring.isVisible().catch(()=>false))) break;
    const t=(await ring.innerText().catch(()=>"")).trim();
    if(t&&t!==seen[seen.length-1]) seen.push(t);
    if(SHOTS&&!shot&&/^[3-6]\d%$/.test(t)){ shot=true; fs.mkdirSync(SHOTS,{recursive:true});
      await page.locator(".asset-modal-footer").screenshot({path:path.join(SHOTS,"chunked-midway.png")}); }
    await delay(120);
  }
  console.log(`chunked-upload-e2e: ring reported ${seen.length} distinct percentages, ${seen[0]} → ${seen[seen.length-1]}`);
  check("the ring advanced past 0%",seen.filter(p=>p!=="0%").length>3,seen.join(" "));
  check("the ring reached a high percentage",seen.some(p=>parseInt(p)>=90),seen.join(" "));

  await page.locator(".asset-file-name").first().waitFor({timeout:120000});
  const listed=await page.locator(".asset-file-name").allInnerTexts();
  check("the file is listed after upload",listed.join()==="magic-kingdom.zip",listed.join());

  // Byte-for-byte: download it back through the edge and hash it.
  const got=path.join(os.tmpdir(),"roundtrip.zip");
  const resp=await ctx.request.fetch(`${baseUrl}/api/bubbles/${slug}/assets/magic-kingdom.zip`);
  fs.writeFileSync(got,Buffer.from(await resp.body()));
  check("round-trips byte-for-byte",sha(got)===want,`${sha(got).slice(0,12)} vs ${want.slice(0,12)}`);
  check("size on disk is exact",fs.statSync(got).size===buf.length,`${fs.statSync(got).size} vs ${buf.length}`);
  fs.rmSync(got,{force:true});

  // Cancelling mid-flight must abort AND leave no staging behind.
  await page.locator(".asset-modal-header button").click();
  await openModal();
  await page.setInputFiles('.asset-modal-footer input[type=file]',big);
  await page.locator(".asset-modal-footer button.primary").click();
  await ring.waitFor({state:"visible",timeout:10000});
  await delay(3000);
  await page.locator(".asset-modal-header button").click();     // the ✕, mid-upload
  await delay(4000);
  const after=await api(ctx.request,baseUrl,"GET",`/api/bubbles/${slug}/assets`);
  const names=(after.assets||[]).map(a=>a.name);
  check("cancelling leaves no second copy",names.filter(n=>n.startsWith("magic-kingdom")).length===1,names.join());
  const staging=[];
  (function walk(dir){ for(const e of fs.readdirSync(dir,{withFileTypes:true})){
    const p=path.join(dir,e.name);
    if(e.isDirectory()){ if(e.name===".uploads") staging.push(...fs.readdirSync(p).map(x=>path.join(p,x))); else walk(p); } } })(dataRoot);
  check("cancelling cleans up staging",staging.length===0,staging.join());

  // The production failure: the tab goes away mid-upload. The bytes already accepted must
  // survive, and re-offering the same file must carry on rather than start from zero.
  await openModal();
  await page.setInputFiles('.asset-modal-footer input[type=file]',big);
  await page.locator(".asset-modal-footer button.primary").click();
  await ring.waitFor({state:"visible",timeout:10000});
  for(let i=0;i<200&&parseInt(await ring.innerText().catch(()=>"0"))<25;i++) await delay(120);
  const reachedPct=parseInt(await ring.innerText());
  await page.reload({waitUntil:"domcontentloaded"});     // kill it the way a closed tab would
  await delay(1500);
  const staged=[];
  (function walk(dir){ for(const e of fs.readdirSync(dir,{withFileTypes:true})){
    const p=path.join(dir,e.name);
    if(e.isDirectory()){ if(e.name===".uploads") staged.push(...fs.readdirSync(p).map(x=>path.join(p,x))); else walk(p); } } })(dataRoot);
  const stagedBytes=staged.length?fs.statSync(path.join(staged[0],"part.tmp")).size:0;
  check("an interrupted upload keeps its staged bytes",stagedBytes>0,`${stagedBytes} bytes at ~${reachedPct}%`);

  await openModal();
  await page.setInputFiles('.asset-modal-footer input[type=file]',big);
  await page.locator(".asset-modal-footer button.primary").click();
  await ring.waitFor({state:"visible",timeout:10000});
  await delay(600);
  const resumedAt=parseInt(await ring.innerText().catch(()=>"0"));
  check("re-offering the file resumes rather than restarting",resumedAt>=reachedPct-5,
        `resumed at ${resumedAt}%, was interrupted at ${reachedPct}%`);
  for(let i=0;i<1200;i++){ if(!(await ring.isVisible().catch(()=>false))) break; await delay(200); }
  const finalNames=(await api(ctx.request,baseUrl,"GET",`/api/bubbles/${slug}/assets`)).assets.map(a=>a.name);
  check("the resumed upload completes",finalNames.includes("magic-kingdom-2.zip"),finalNames.join());
  const rt=await ctx.request.fetch(`${baseUrl}/api/bubbles/${slug}/assets/magic-kingdom-2.zip`);
  const rtPath=path.join(os.tmpdir(),"resumed.zip");
  fs.writeFileSync(rtPath,Buffer.from(await rt.body()));
  check("the resumed file is byte-identical",sha(rtPath)===want,`${sha(rtPath).slice(0,12)} vs ${want.slice(0,12)}`);
  fs.rmSync(rtPath,{force:true});

  check("no page errors",errors.length===0,errors.join(" | "));
  fs.rmSync(big,{force:true});
}finally{
  console.log(failures?`chunked-upload-e2e: ${failures} check(s) FAILED`:"chunked-upload-e2e: all large-upload checks passed");
  if(browser) await browser.close().catch(()=>{});
  if(proxy) proxy.kill("SIGKILL");
  if(child&&child.exitCode===null){child.kill("SIGTERM");await Promise.race([new Promise(r=>child.once("exit",r)),delay(3000)]);}
  fs.rmSync(dataRoot,{recursive:true,force:true});
  process.exitCode=failures?1:0;
}
