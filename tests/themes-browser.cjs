// Actual frontend, synthetic API only. No Rust/model worker or public server required.
// PLAYWRIGHT_MODULE=/existing/path/to/playwright node tests/themes-browser.cjs
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "..");
const artifacts = fs.mkdtempSync(path.join(os.tmpdir(), "8c-theme-ui-"));
const presets = ["none", "qwave-whisper-v1", "violet-night-v1"];
const rooms = [{id:"humayian-lounge",name:"Lounge",description:"Synthetic theme fixture"},{id:"hot-tub",name:"Hot tub",description:"Second fixture room"}];
const themes = new Map(rooms.map(room => [room.id,{preset:"none",revision:0,updated_at:0,actor_name:""}]));
const posts = [];
let rejectTheme = false;
const profile = {name:"Theme tester",handle:"fixture",bio:"Synthetic profile",favorites:[]};
const server = http.createServer(async (req,res) => {
  res.setHeader("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");
  const url = new URL(req.url,"http://127.0.0.1");
  const json = (data,status=200) => {res.writeHead(status,{"Content-Type":"application/json"});res.end(JSON.stringify(data));};
  if (url.pathname === "/" || url.pathname === "/app.js") {
    res.writeHead(200,{"Content-Type":url.pathname === "/" ? "text/html" : "text/javascript"});
    return res.end(fs.readFileSync(path.join(root,"src",url.pathname === "/" ? "page.html" : "app.js")));
  }
  if (url.pathname === "/api/config") return json({ok:true,rooms,agents:[]});
  if (url.pathname === "/api/introduce") return json({ok:true,profile,token:"fixture-token"});
  if (url.pathname === "/api/messages" && req.method === "GET") {
    const room = url.searchParams.get("room");
    return json({ok:true,room,theme:themes.get(room),agents:[],messages:[{id:1,sender_name:"Fixture visitor",sender_handle:"fixture",sender_kind:"human",timestamp_secs:1,text:"A very quiet background leaves our conversation readable."}],jobs:[],online:[],hottub:{running:false,budget_left:0}});
  }
  if (url.pathname === "/api/room-theme") {
    let body = ""; for await (const chunk of req) body += chunk;
    const input = JSON.parse(body); posts.push(input);
    const current = themes.get(input.room);
    if (rejectTheme) return json({ok:false,error:"Please wait before changing the room again."},400);
    if (!current || !presets.includes(input.preset) || input.expected_revision !== current.revision || input.token !== "fixture-token") return json({ok:false,error:"The room theme changed. Try again."},409);
    const theme = {preset:input.preset,revision:current.revision+1,updated_at:1,actor_name:profile.name};
    themes.set(input.room,theme); return json({ok:true,theme});
  }
  res.writeHead(404); res.end();
});
const setTheme = (room,preset,actor_name="Synthetic Aye") => {const old=themes.get(room); themes.set(room,{preset,revision:old.revision+1,updated_at:1,actor_name});};
async function run() {
  await new Promise(resolve => server.listen(0,"127.0.0.1",resolve));
  const origin = "http://127.0.0.1:" + server.address().port;
  let browser;
  try {
    browser = await chromium.launch({headless:true});
    const errors=[];
    const context = await browser.newContext({viewport:{width:1280,height:860}});
    await context.addInitScript(() => localStorage.setItem("8c_token_v8","fixture-token"));
    const page = await context.newPage(); page.on("pageerror",e=>errors.push(e.message));
    const surface = page.locator("#roomSurface");
    const waitTheme = preset => page.waitForFunction(p=>document.querySelector("#roomSurface").dataset.theme===p,preset);
    const refresh = () => page.evaluate(()=>window.dispatchEvent(new Event("online")));
    await page.goto(origin);
    await page.waitForFunction(()=>document.querySelector("#whoBtn").textContent==="Theme tester");
    await page.locator("#themeBtn").click();
    await page.waitForFunction(()=>!document.querySelector("#themeApply").disabled);
    assert.equal(await page.locator("#themeMotion").inputValue(),"still");
    await page.locator("#roomTheme").selectOption("qwave-whisper-v1"); await page.locator("#themeApply").click();
    await waitTheme("qwave-whisper-v1");
    assert.equal(posts[0].expected_revision,0);
    assert.equal(await surface.getAttribute("data-motion"),"still");
    assert.equal(await page.locator(".room-art svg").evaluate(e=>getComputedStyle(e).opacity),"0.04");
    await page.locator("#themeMotion").selectOption("gentle");
    assert.equal(await surface.getAttribute("data-motion"),"gentle");
    await page.locator("#themePause").click();
    assert.equal(await surface.getAttribute("data-motion"),"still");
    await page.reload(); await page.waitForFunction(()=>document.querySelector("#whoBtn").textContent==="Theme tester");
    await page.locator("#themeBtn").click(); assert.equal(await page.locator("#themePause").getAttribute("aria-pressed"),"true");
    await page.locator("#themePause").click();
    await page.emulateMedia({reducedMotion:"reduce"});
    await page.waitForFunction(()=>document.querySelector("#roomSurface").dataset.motion==="still");
    assert.equal(await page.locator(".room-art svg").evaluate(e=>getComputedStyle(e).animationName),"none");
    await page.emulateMedia({reducedMotion:"no-preference"});
    await page.waitForFunction(()=>document.querySelector("#roomSurface").dataset.motion==="gentle");
    await page.evaluate(()=>window.dispatchEvent(new Event("pagehide")));
    assert.equal(await surface.getAttribute("data-motion"),"still");
    await page.evaluate(()=>window.dispatchEvent(new Event("pageshow")));
    assert.equal(await surface.getAttribute("data-motion"),"gentle");
    await page.evaluate(()=>{Object.defineProperty(document,"hidden",{configurable:true,get:()=>true});document.dispatchEvent(new Event("visibilitychange"));});
    assert.equal(await surface.getAttribute("data-motion"),"still");
    await page.evaluate(()=>{delete document.hidden;document.dispatchEvent(new Event("visibilitychange"));});
    await page.emulateMedia({forcedColors:"active"}); await waitTheme("none");
    await page.emulateMedia({forcedColors:"none"}); await waitTheme("qwave-whisper-v1");
    await page.locator("#themeMotion").selectOption("off"); await waitTheme("none");
    setTheme("humayian-lounge","violet-night-v1"); await refresh();
    await page.waitForFunction(()=>document.querySelector("#roomTheme").value==="violet-night-v1");
    assert.equal(await surface.getAttribute("data-theme"),"none","Aye changes cannot override local Off");
    await page.locator("#themeLocalReset").click(); await waitTheme("violet-night-v1");
    await page.locator("#themeClose").click();
    await page.locator("#msgInput").fill("Do not clear my draft");
    await page.locator("#themeBtn").click(); rejectTheme=true;
    await page.locator("#themeReset").click();
    await page.waitForFunction(()=>document.querySelector("#themeStatus").textContent.includes("Could not change"));
    assert.equal(await page.locator("#msgInput").inputValue(),"Do not clear my draft"); rejectTheme=false;
    await page.locator("#themeReset").click(); await waitTheme("none");
    // Unknown future or hostile presets never become DOM style/class values.
    setTheme("humayian-lounge","url(https://invalid.example/x)"); await refresh();
    await page.waitForResponse(r=>r.url().includes("/api/messages?"));
    assert.equal(await surface.getAttribute("data-theme"),"none");
    setTheme("humayian-lounge","qwave-whisper-v1"); await refresh(); await waitTheme("qwave-whisper-v1");
    await page.locator("#themeClose").click();
    await page.screenshot({path:path.join(artifacts,"desktop-qwave.png")});
    // Deliver an old-room snapshot despite abort: the generation/room guard must win.
    await page.evaluate(()=>{
      const original=window.fetch; let held=false;
      window.fetch=async (...args)=>{
        const response=await original(...args);
        if (!held && String(args[0]).includes("/api/messages?room=humayian-lounge")) {
          held=true; const body=await response.text();
          return new Promise(resolve=>{window.releaseOldTheme=()=>{window.fetch=original;resolve(new Response(body,{status:200,headers:{"Content-Type":"application/json"}}));};});
        }
        return response;
      };
    });
    await refresh(); await page.waitForFunction(()=>typeof window.releaseOldTheme==="function");
    await page.locator('[data-room="hot-tub"]').click();
    await page.waitForFunction(()=>document.querySelector("#themeAttribution").textContent.includes("room default"));
    await page.evaluate(async ()=>{window.releaseOldTheme();await new Promise(resolve=>setTimeout(resolve,0));});
    assert.equal(await surface.getAttribute("data-theme"),"none","late old-room theme must not leak into the new room");
    await page.locator('[data-room="humayian-lounge"]').click(); await waitTheme("qwave-whisper-v1");
    // Delay the new room: the old atmosphere must disappear before its response.
    let release, seen;
    const gate=new Promise(resolve=>release=resolve), intercepted=new Promise(resolve=>seen=resolve);
    await page.route("**/api/messages?room=hot-tub",async route=>{seen();await gate;await route.continue();});
    await page.locator('[data-room="hot-tub"]').click(); await intercepted;
    assert.equal(await surface.getAttribute("data-theme"),"none");
    const roomResponse=page.waitForResponse(r=>r.url().includes("/api/messages?room=hot-tub")); release(); await roomResponse;
    await page.waitForFunction(()=>document.querySelector("#themeAttribution").textContent.includes("room default"));
    await page.unroute("**/api/messages?room=hot-tub");
    setTheme("hot-tub","violet-night-v1"); await refresh(); await waitTheme("violet-night-v1");
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:path.join(artifacts,"mobile-violet.png")});
    await page.locator("#themeBtn").click();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.equal(await page.locator("#themeSheet").evaluate(e=>e.scrollWidth>e.clientWidth),false,"dialog controls must fit on mobile");
    await page.screenshot({path:path.join(artifacts,"mobile-controls.png")});
    // Storage denial is local, visible, and does not break controls.
    await page.evaluate(()=>{Storage.prototype.setItem=()=>{throw new Error("blocked fixture");};});
    await page.locator("#themeMotion").selectOption("off"); await waitTheme("none");
    assert.match(await page.locator("#themeStorageStatus").textContent(),/could not be saved/);
    // Unintroduced visitors still control their own view, not the room.
    const guest=await browser.newPage({viewport:{width:390,height:844}});
    await guest.addInitScript(()=>{Storage.prototype.getItem=()=>{throw new Error("blocked fixture");};});
    await guest.goto(origin); await guest.locator("#identityClose").click(); await guest.locator("#themeBtn").click();
    assert.equal(await guest.locator("#themeApply").isDisabled(),true);
    assert.match(await guest.locator("#themeStorageStatus").textContent(),/could not be restored/);
    await guest.locator("#themeMotion").selectOption("off");
    assert.equal(await guest.locator("#roomSurface").getAttribute("data-theme"),"none");
    assert.deepEqual(errors,[]);
    console.log("PASS: synthetic themes, CAS request, manual reset, drafts, allowlist, local persistence/pause/off, reduced motion, forced colors, visibility, room reset, mobile layout, storage denial, guest controls.");
    console.log("Synthetic browser screenshots:",artifacts);
  } finally {if(browser) await browser.close(); await new Promise(resolve=>server.close(resolve));}
}
run().catch(error=>{console.error(error);process.exitCode=1;});
