// Explicit, opt-in end-to-end check against an already running server and actual worker.
// Posts two clearly labelled verification messages to Model lab.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const origin = process.env.CHAT_ORIGIN;
if (!origin) throw new Error("Set CHAT_ORIGIN explicitly; this check posts public verification notes.");
const output = fs.mkdtempSync(path.join(process.env.TMPDIR || "/tmp/opencode","8c-live-browser-"));
async function run() {
  const browser = await chromium.launch({headless:true});
  const errors = [];
  try {
    const contexts = await Promise.all([browser.newContext({viewport:{width:1280,height:860}}),browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true})]);
    const pages = await Promise.all(contexts.map(c => c.newPage()));
    const names = ["Chat verification · Astra","Chat verification · Visitor"];
    const ids = await Promise.all(pages.map(async (page,index) => {
      page.on("pageerror", e => errors.push(e.message));
      await page.goto(origin+"/?room=model-lab");
      await page.locator("#nameInput").fill(names[index]);
      await page.locator("#bioInput").fill("I enjoy making wooden boats. This profile is checking the public chat connection.");
      await page.locator("#identitySave").click();
      await page.waitForFunction(() => !document.querySelector("#whoSheet").open);
      await page.locator("#ayeMode").selectOption(index===0 ? "aye" : "cheet");
      await page.locator("#msgInput").fill("Chat connection check: please greet me using my chosen name and mention what I enjoy making. One short sentence is plenty.");
      const response = page.waitForResponse(r => r.url().endsWith("/api/messages") && r.request().method()==="POST");
      await page.locator("#sendBtn").click();
      const accepted = await (await response).json(); assert.equal(accepted.ok,true);
      return accepted.message.id;
    }));
    const deadline=process.hrtime.bigint()+180_000_000_000n;
    let replies=[];
    while (process.hrtime.bigint()<deadline) {
      const response=await fetch(origin+"/api/messages?room=model-lab",{signal:AbortSignal.timeout(12000)});
      const data=await response.json();
      replies=ids.map(id => data.messages.find(m => m.reply_to===id && m.origin==="local-worker"));
      if (replies.every(Boolean)) break;
      await new Promise(resolve => setTimeout(resolve,2000));
    }
    assert.ok(replies.length===2 && replies.every(Boolean),"both actual worker replies must arrive before the deadline");
    for (const reply of replies) {
      assert.equal(reply.runtime,"ollama"); assert.match(reply.model_digest,/^[a-f0-9]{64}$/);
      assert.ok(reply.text.length>0); assert.ok(reply.output_tokens>0);
      if (process.env.REQUIRE_MEM8 === "1") {
        assert.equal(reply.memory.format,"MEM8EV02");
        const scopes = reply.memory.records.map(r => r.scope);
        assert.ok(scopes.includes("family") && scopes.includes("room:model-lab"));
        assert.ok(scopes.includes("agent:"+reply.sender_handle.split("@")[0]));
        assert.ok(scopes.some(s => s.startsWith("visitor:")));
        assert.equal(reply.memory.written.event_id,1);
      }
    }
    for (let index=0;index<pages.length;index+=1) {
      const page=pages[index];
      for (const reply of replies) await page.locator('article[data-id="'+reply.id+'"]').waitFor();
      if (process.env.REQUIRE_MEM8 === "1") {
        await page.locator('article[data-id="'+replies[index].id+'"] summary').click();
        await page.locator('article[data-id="'+replies[index].id+'"] .memory-receipt').waitFor();
      }
      await page.reload(); await page.waitForFunction(name => document.querySelector("#whoBtn").textContent===name,names[index]);
      await page.locator("#whoBtn").click(); assert.match(await page.locator("#bioInput").inputValue(),/wooden boats/); await page.locator("#identityClose").click();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth>innerWidth),false);
      await page.screenshot({path:path.join(output,index===0 ? "desktop.png" : "mobile.png")});
    }
    assert.deepEqual(errors,[]);
    const receipt={ok:true,origin,ids,replies,profiles_restored:true,independent_sessions:2};
    fs.writeFileSync(path.join(output,"receipt.json"),JSON.stringify(receipt,null,2),{mode:0o600});
    console.log(JSON.stringify(receipt,null,2)); console.log("Artifacts:",output);
  } finally { await browser.close(); }
}
run().catch(error => {console.error(error);process.exitCode=1;});
