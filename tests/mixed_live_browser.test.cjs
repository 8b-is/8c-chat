const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const {mkdtempSync, writeFileSync, rmSync} = require('node:fs');
const {tmpdir} = require('node:os');
const path = require('node:path');
const {test} = require('node:test');

for (const previous of [
  {ok:false, replies:[]},
  {ok:false, replies:[{id:1}], failure:'second participant failed'},
  {ok:true, replies:[{id:1}]},
]) {
  test('never certifies a prior incomplete run: ' + JSON.stringify(previous), () => {
    const directory = mkdtempSync(path.join(tmpdir(), '8c-browser-replay-test-'));
    try {
      writeFileSync(path.join(directory,'result.json'), JSON.stringify(previous));
      const result = spawnSync(process.execPath, [path.join(__dirname,'mixed_live_browser.cjs')], {
        env: {...process.env, CHAT_ORIGIN:'http://127.0.0.1:1', VERIFY_RECEIPT_DIR:directory},
        encoding:'utf8', timeout:5000,
      });
      assert.equal(result.status, 1);
      assert.match(result.stderr, /Receipt replay cannot certify four live participants/);
      assert.doesNotMatch(result.stdout, /PASS/);
    } finally {
      rmSync(directory, {recursive:true}); // Only this test's newly created receipt.
    }
  });
}
