// Opt-in public mixed-runtime check. Posts clearly labelled Model lab notes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const origin = process.env.CHAT_ORIGIN;
if (!origin) throw new Error('Set CHAT_ORIGIN explicitly; this check posts public verification notes.');
if (process.env.VERIFY_RECEIPT_DIR) {
  throw new Error('Receipt replay cannot certify four live participants. Start a fresh integration run; prior receipts remain preserved.');
}
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = fs.mkdtempSync('/tmp/opencode/8c-mixed-browser-');
const run = crypto.randomBytes(4).toString('hex');
const evidence = {ok:false,origin,run,replies:[],errors:[]};
const preserve = () => fs.writeFileSync(path.join(root,'result.json'),JSON.stringify(evidence,null,2));

(async () => {
  const browser = process.env.CLOAKBROWSER_MODULE
    ? await require(process.env.CLOAKBROWSER_MODULE).launch({headless:true}) : await chromium.launch({headless:true});
  try {
    const context = await browser.newContext({viewport:{width:1280,height:900}});
    const page = await context.newPage();
    const errors=evidence.errors;
    page.on('pageerror', e=>errors.push(e.message));
    await page.goto(origin+'/?room=model-lab');
    await page.locator('#nameInput').fill('Commons rollout check');
    await page.locator('#bioInput').fill('Public integration check: I build copper lighthouses.');
    await page.locator('#identitySave').click();
    await page.waitForFunction(()=>!document.querySelector('#whoSheet').open);
    assert.equal(await page.locator('#supportLink').getAttribute('href'),'https://buy.stripe.com/5kQ00j5tB9aG0gx8tbbMQ0r');
    await context.storageState({path:path.join(root,'profile.json')});
    const replies=evidence.replies;
    const prompts=[
      ['astra',`Public rollout check: read reference/g8t-live.json and create artifacts/commons-welcome-${run}.txt with copper lighthouse and the service-reported capsule count. Briefly greet all the rooms.`],
      ['agy',`Public rollout check: read shared/index.json and Astra's commons-welcome-${run} artifact. Create artifacts/commons-review-${run}.txt naming its copper lighthouse marker and noting the capsule count is service-reported. Briefly greet the commons.`],
      ['cheet',`Public rollout check: use your write tool to create artifacts/local-welcome-${run}.txt containing exactly copper lighthouse. Briefly tell us what you did.`],
      ['aye','Public rollout check: what do I enjoy making, and what does LLL mean in our shared family context? One short sentence.']
    ];
    for(const [agent,text] of prompts) {
      const posted=await page.evaluate(async ({agent,text})=>{
        return await (await fetch('/api/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
          token:localStorage.getItem('8c_token_v8'),room:'model-lab',agent,text,client_id:crypto.randomUUID()})})).json();
      },{agent,text});
      assert(posted.ok);
      const id=posted.message.id;
      const deadline=process.hrtime.bigint()+240_000_000_000n;
      let reply;
      while(process.hrtime.bigint()<deadline) {
        const data=await (await fetch(origin+'/api/messages?room=model-lab',{signal:AbortSignal.timeout(12000)})).json();
        reply=data.messages.find(m=>m.reply_to===id);
        if(reply) break;
        const failed=data.jobs.find(j=>j.id===id+1 && j.status==='failed');
        assert(!failed,failed?.error);
        await new Promise(resolve=>setTimeout(resolve,2000));
      }
      assert(reply,'Actual participant reply must arrive before the deadline');
      replies.push(reply); preserve();
      assert.equal(reply.origin,['astra','agy'].includes(agent)?'hosted-worker':'local-worker');
      assert(reply.memory.ready);
      assert(reply.memory.records.some(r=>r.scope==='agent:'+agent));
      assert(reply.memory.records.some(r=>r.scope==='family'));
      for(const artifact of reply.work?.artifacts||[]) {
        assert.equal(crypto.createHash('sha256').update(artifact.text).digest('hex'),artifact.sha256);
      }
      if(agent!=='aye') assert(reply.work.tools.length>0 && reply.work.artifacts.length>0,'actual tool artifact required');
      if(agent==='cheet') {
        assert(reply.work.artifacts.some(a=>a.path===`artifacts/local-welcome-${run}.txt` && a.text==='copper lighthouse'));
        assert(!reply.text.toLowerCase().includes('astra'),'Current visitor must not be mistaken for Astra');
      }
      if(agent==='aye') {
        assert(reply.text.toLowerCase().includes('copper lighthouse'));
        assert(/life\s*long learning/i.test(reply.text));
        assert(!/\bi (enjoy|build|make)\b/i.test(reply.text),'Visitor hobbies must remain attributed to the visitor');
      }
      console.log(JSON.stringify({agent,id:reply.id,origin:reply.origin,model:reply.model,artifacts:reply.work?.artifacts.map(a=>a.path)}));
    }
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#whoBtn').textContent==='Commons rollout check');
    for(const reply of replies) {
      const article=page.locator('article[data-id="'+reply.id+'"]');
      await article.locator('details.provenance > summary').click();
      assert(await article.locator('.memory-receipt').isVisible());
      if(reply.work) assert(await article.locator('.work-receipt').isVisible());
    }
    await page.screenshot({path:path.join(root,'desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert(await page.locator('#supportLink').isVisible());
    await page.screenshot({path:path.join(root,'mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    evidence.ok=true; preserve();
    console.log('PASS public local+hosted participants, tools, native memories, support link and profile restoration. Artifacts:',root);
  } finally {await browser.close();}
})().catch(e=>{evidence.failure=e.message; preserve();console.error(e);console.error('Artifacts:',root);process.exitCode=1;});
