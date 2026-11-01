// Opt-in fresh public checks using an existing verification visitor's identity.
// Identity reuse tests continuity; no prior reply/receipt can satisfy assertions.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const origin = process.env.CHAT_ORIGIN;
const profile = process.env.PROFILE_STATE;
if (!origin || !profile) throw new Error('Set CHAT_ORIGIN and PROFILE_STATE explicitly; this check posts public verification requests.');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = fs.mkdtempSync('/tmp/opencode/8c-cross-room-browser-');
const run = crypto.randomBytes(4).toString('hex');
const evidence = {ok:false,origin,run,replies:[],errors:[]};
const preserve = () => fs.writeFileSync(path.join(root,'result.json'),JSON.stringify(evidence,null,2));

(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const context = await browser.newContext({storageState:profile,viewport:{width:1280,height:900}});
    const page = await context.newPage();
    page.on('pageerror',error=>evidence.errors.push(error.message));
    await page.goto(origin+'/?room=tokyo-lab',{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>document.querySelector('#whoBtn')?.textContent==='Commons rollout check');
    const post = async text => {
      const posted = await page.evaluate(async text=>await (await fetch('/api/messages',{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
          token:localStorage.getItem('8c_token_v8'),room:'tokyo-lab',agent:'aye',text,client_id:crypto.randomUUID()})
      })).json(),text);
      assert(posted.ok);
      const end = process.hrtime.bigint()+180_000_000_000n;
      while (process.hrtime.bigint()<end) {
        const data = await (await fetch(origin+'/api/messages?room=tokyo-lab',{signal:AbortSignal.timeout(12000)})).json();
        const reply = data.messages.find(m=>m.reply_to===posted.message.id);
        if (reply) {
          assert.equal(reply.origin,'local-worker');
          assert(reply.memory.ready);
          for (const scope of ['family','agent:aye','visitor:'+posted.message.sender_handle]) {
            assert(reply.memory.records.some(record=>record.scope===scope),'Missing recalled scope '+scope);
          }
          evidence.replies.push(reply); preserve();
          return reply;
        }
        const failed = data.jobs.find(job=>job.id===posted.message.id+1 && job.status==='failed');
        assert(!failed,failed?.error);
        await new Promise(resolve=>setTimeout(resolve,2000));
      }
      throw new Error('Fresh cross-room reply did not arrive');
    };
    const recall = await post('Public continuity check after service restart: what do I enjoy making, and what does LLL mean in our shared family context? One short sentence.');
    assert(/copper lighthouse/i.test(recall.text));
    assert(/life long learning/i.test(recall.text));
    assert(!/\bi (enjoy|build|make)\b/i.test(recall.text));
    assert.equal(recall.work.tools.length,0);
    const filename = `artifacts/aye-continuity-${run}.txt`;
    const work = await post(`Public continuity check: use your write tool to create ${filename} containing exactly amber observatory. Briefly report the result.`);
    assert(work.work.tools.some(tool=>tool.name==='write' && tool.status==='completed'));
    const artifact = work.work.artifacts.find(artifact=>artifact.path===filename);
    assert(artifact);
    assert.equal(artifact.text,'amber observatory');
    assert.equal(artifact.sha256,crypto.createHash('sha256').update(artifact.text).digest('hex'));
    await page.reload({waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>document.querySelector('#whoBtn')?.textContent==='Commons rollout check');
    for (const reply of evidence.replies) {
      const article = page.locator(`article[data-id="${reply.id}"]`);
      await article.locator('details.provenance > summary').click();
      assert(await article.locator('.memory-receipt').isVisible());
      assert(await article.locator('.work-receipt').isVisible());
    }
    await page.screenshot({path:path.join(root,'cross-room.png'),fullPage:true});
    assert.deepEqual(evidence.errors,[]);
    evidence.ok = true; preserve();
    console.log(JSON.stringify({ok:true,message_ids:evidence.replies.map(reply=>reply.id),artifact:filename}));
    console.log('PASS fresh cross-room recall, prior-profile restoration and Aye exact write. Artifacts:',root);
  } finally {await browser.close();}
})().catch(error=>{evidence.failure=error.message;preserve();console.error(error);console.error('Artifacts:',root);process.exitCode=1;});
