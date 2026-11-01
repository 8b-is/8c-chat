// Run after cargo build. Playwright is a development tool, not a server dependency.
const assert = require("node:assert/strict");
const {spawn} = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const directory = fs.mkdtempSync(path.join(process.env.TMPDIR || "/tmp/opencode", "8c-browser-"));
const port = process.env.TEST_PORT || "18898";
const origin = "http://127.0.0.1:" + port;
const key = "browser-tests-worker-key-not-a-production-secret";
const errors = [];

async function run() {
  const server = spawn(path.resolve("target/debug/8c-chat"), [], {env: {...process.env, BIND_ADDR: "127.0.0.1:" + port,
    JOURNAL_PATH: path.join(directory,"chat.jsonl"), WORKER_KEY: key}, stdio: ["ignore","ignore","pipe"]});
  const started = new Promise((resolve,reject) => {
    server.stderr.on("data", chunk => { if (chunk.toString().includes("listening on")) resolve(); });
    server.on("exit", code => reject(new Error("Server exited " + code)));
  });
  let browser;
  try {
    await started;
    browser = process.env.CLOAKBROWSER_MODULE
      ? await require(process.env.CLOAKBROWSER_MODULE).launch({headless:true})
      : await chromium.launch({headless:true});
    const desktop = await browser.newContext({viewport:{width:1280,height:860}});
    const mobile = await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
    const a = await desktop.newPage(), b = await mobile.newPage();
    for (const page of [a,b]) page.on("pageerror", error => errors.push(error.message));
    async function join(page,name) {
      await page.goto(origin);
      await page.locator("#nameInput").fill(name);
      await page.locator("#bioInput").fill("I enjoy building little wooden boats.");
      await page.locator("#identitySave").click();
      await page.waitForFunction(() => !document.querySelector("#whoSheet").open);
      await page.locator("#ayeMode").selectOption("no");
    }
    await Promise.all([join(a,"Éva 東京 💙"),join(b,"Browser visitor two")]);
    assert.equal(await a.locator("#whoBtn").textContent(),"Éva 東京 💙");
    await a.reload(); await a.waitForFunction(() => document.querySelector("#whoBtn").textContent==="Éva 東京 💙");
    await a.locator("#whoBtn").click(); assert.equal(await a.locator("#bioInput").inputValue(),"I enjoy building little wooden boats."); await a.locator("#identityClose").click();
    await a.locator("#ayeMode").selectOption("no");
    await a.locator("#msgInput").fill("Hello from the desktop 💙"); await a.locator("#sendBtn").click();
    await b.getByText("Hello from the desktop 💙",{exact:true}).waitFor();
    await b.locator("#msgInput").fill("Hello back from mobile"); await b.locator("#sendBtn").click();
    await a.getByText("Hello back from mobile",{exact:true}).waitFor();
    assert.ok(await b.evaluate(() => document.documentElement.scrollWidth <= 390), "mobile content must not expand the viewport");

    // Lost acknowledgement: the server accepts a note, the browser sees a failed fetch.
    let loseAck = true;
    await a.route("**/api/messages", async route => {
      if (route.request().method() === "POST" && loseAck) { loseAck=false; await route.fetch(); await route.abort("connectionfailed"); }
      else await route.continue();
    });
    await a.locator("#msgInput").fill("Keep this draft after lost acknowledgement"); await a.locator("#sendBtn").click();
    await a.waitForFunction(() => document.querySelector("#sendStatus").textContent.includes("draft is kept"));
    assert.equal(await a.locator("#msgInput").inputValue(),"Keep this draft after lost acknowledgement");
    await a.reload(); await a.waitForFunction(() => document.querySelector("#whoBtn").textContent==="Éva 東京 💙");
    assert.equal(await a.locator("#msgInput").inputValue(),"Keep this draft after lost acknowledgement");
    await a.locator("#ayeMode").selectOption("no"); await a.locator("#sendBtn").click();
    await a.waitForFunction(() => document.querySelector("#msgInput").value==="");
    const notes = await (await fetch(origin+"/api/messages?room=humayian-lounge")).json();
    assert.equal(notes.messages.filter(m => m.text==="Keep this draft after lost acknowledgement").length,1);
    await a.unroute("**/api/messages");

    // A send in flight cannot erase text typed for the next message.
    let release, intercepted;
    const received = new Promise(resolve => intercepted=resolve);
    const gate = new Promise(resolve => release=resolve);
    await a.route("**/api/messages", async route => {
      if (route.request().method() === "POST") { const response=await route.fetch(); intercepted(); await gate; await route.fulfill({response}); }
      else await route.continue();
    });
    await a.locator("#msgInput").fill("First note in flight"); await a.locator("#sendBtn").click(); await received;
    await a.locator("#msgInput").fill("My next draft must survive"); release();
    await a.waitForFunction(() => !document.querySelector("#sendBtn").disabled);
    assert.equal(await a.locator("#msgInput").inputValue(),"My next draft must survive");
    await a.unroute("**/api/messages");
    await a.locator('[data-room="hot-tub"]').click(); await a.locator("#msgInput").fill("A separate hot-tub draft");
    await a.locator("#favoriteBtn").click(); await a.waitForFunction(() => document.querySelector("#favoriteBtn").getAttribute("aria-pressed")==="true");
    await a.reload(); await a.waitForFunction(() => document.querySelector("#whoBtn").textContent==="Éva 東京 💙");
    assert.equal(await a.locator("#msgInput").inputValue(),"A separate hot-tub draft");
    assert.equal(await a.locator("#favoriteBtn").getAttribute("aria-pressed"),"true");
    await a.locator('[data-room="humayian-lounge"]').click();
    assert.equal(await a.locator("#msgInput").inputValue(),"My next draft must survive");

    // Old-room poll response is deliberately delivered after switching rooms.
    let delayed, releasePoll;
    const delayedSeen = new Promise(resolve => delayed=resolve);
    const pollGate = new Promise(resolve => releasePoll=resolve);
    await a.route("**/api/messages?room=humayian-lounge", async route => {
      const response=await route.fetch(); delayed(); await pollGate;
      try { await route.fulfill({response}); } catch {}
    });
    await delayedSeen; await a.locator('[data-room="hot-tub"]').click(); releasePoll();
    await a.waitForFunction(() => document.querySelector("#roomTitle").textContent==="Hot tub");
    assert.equal(await a.getByText("Hello from the desktop 💙",{exact:true}).count(),0);
    await a.unroute("**/api/messages?room=humayian-lounge");
    await a.locator("#tubBtn").click(); await a.waitForFunction(() => document.querySelector("#tubBtn").textContent==="Resume hot tub");

    // The worker result route is exercised with an explicitly test-only reply.
    await a.locator('[data-room="aye-partner"]').click(); await a.locator("#ayeMode").selectOption("aye");
    await a.locator("#msgInput").fill("Exercise queued reply and retry controls"); await a.locator("#sendBtn").click();
    await a.getByText("Aye: queued for a turn…",{exact:true}).waitFor();
    const ref=scope=>({scope,ref:"c".repeat(64),event_id:1,payload_sha256:"d".repeat(64),snippet_sha256:"e".repeat(64),bytes:10});
    const memory={ready:true,format:"MEM8EV02",binary_sha256:"b".repeat(64),records:[ref("family"),ref("agent:aye")],written:ref("agent:aye")};
    const proof={worker_key:key,agent:"aye",runtime:"ollama",model:"aye-deepseek-pqm:latest",digest:"a".repeat(64),memory};
    const post = async (url,data) => (await fetch(origin+url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)})).json();
    let job=(await post("/api/worker/poll",proof)).job;
    await post("/api/worker/result",{...proof,id:job.id,lease:job.lease,error:"Test runtime interruption"});
    await a.getByRole("button",{name:"Retry reply",exact:true}).click();
    job=(await post("/api/worker/poll",proof)).job;
    assert.ok(job);
    await post("/api/worker/result",{...proof,id:job.id,lease:job.lease,text:"Browser test reply, not an inference claim",elapsed_ms:10,output_tokens:8});
    await a.getByText("Browser test reply, not an inference claim",{exact:true}).waitFor();
    await a.locator("article .provenance summary").click();
    await a.getByText(/Aurora MEM8EV02/).waitFor();
    assert.match(await a.locator(".memory-receipt").textContent(),/family.*agent:aye/);
    await a.locator("#ayeMode").selectOption("no");
    await a.locator("#msgInput").fill("### Rich Note\n\nCheck `code` and **bold**:\n\n```mermaid\nflowchart LR\n  A[Visitor] --> B[Mermaid SVG]\n```");
    await a.locator("#sendBtn").click();
    await a.locator("article svg.mermaid-svg").waitFor();
    assert.equal(await a.locator("article .md-h3").textContent(), "Rich Note");
    assert.equal(await a.locator("article code.md-code").textContent(), "code");
    await a.screenshot({path:path.join(directory,"desktop.png")}); await b.screenshot({path:path.join(directory,"mobile.png")});

    // A delayed profile update must not restore someone who has signed out.
    let releaseProfile, profileIntercepted;
    const profileReceived = new Promise(resolve => profileIntercepted=resolve);
    const profileGate = new Promise(resolve => releaseProfile=resolve);
    await a.route("**/api/profile", async route => {
      const response = await route.fetch(); profileIntercepted(); await profileGate;
      await route.fulfill({response});
    });
    await a.locator("#favoriteBtn").click(); await profileReceived;
    await a.locator("#whoBtn").click(); await a.locator("#switchPerson").click();
    await a.locator("#nameInput").fill("Replacement visitor"); await a.locator("#identitySave").click();
    await a.waitForFunction(() => !document.querySelector("#whoSheet").open);
    releaseProfile();
    await a.waitForFunction(() => !document.querySelector("#favoriteBtn").disabled);
    assert.equal(await a.locator("#whoBtn").textContent(), "Replacement visitor", "late profile updates must not replace the current person");
    await a.unroute("**/api/profile");

    // The maximum accepted name must still fit a narrow phone screen.
    await b.locator("#whoBtn").click(); await b.locator("#switchPerson").click();
    await b.locator("#nameInput").fill("W".repeat(48)); await b.locator("#identitySave").click();
    await b.waitForFunction(() => !document.querySelector("#whoSheet").open);
    await b.setViewportSize({width:320,height:844});
    await b.locator("#ayeMode").selectOption("no");
    await b.locator("#msgInput").fill("A message with a maximum-length author name"); await b.locator("#sendBtn").click();
    await b.getByText("A message with a maximum-length author name", {exact:true}).waitFor();
    assert.ok(await b.evaluate(() => document.documentElement.scrollWidth <= 320), "long names must not expand a phone viewport");
    assert.ok(await b.locator("#messages").evaluate(box => box.scrollWidth <= box.clientWidth), "long author names must not create sideways message scrolling");
    await b.screenshot({path:path.join(directory,"mobile-long-author.png")});
    await b.locator('[data-room="aye-partner"]').click();
    assert.ok(await b.evaluate(() => document.documentElement.scrollWidth <= 320), "long room titles and names must fit together");
    await b.screenshot({path:path.join(directory,"mobile-long-name.png")});
    assert.deepEqual(errors,[]);
    console.log("PASS: two visitors, restored identity/introduction, mobile and long-name layout, lost-ack retry, newer draft preservation, per-room drafts, favorites, stale polls/profile updates, hot-tub pause, queued reply retry.");
    console.log("Browser artifacts:",directory);
  } finally { if (browser) await browser.close(); server.kill("SIGTERM"); }
}
run().catch(error => {console.error(error); process.exitCode=1;});
