# Smart Tree PR audit through tai.is

The first workflow is an audit of an existing PR. It collects evidence before
deciding whether a merge, version change, or deployment is still needed. A merged
PR is not proof that its code is in the public download or a running service.

In a tai room with workbench access, request:

> Use the run tool to audit Smart Tree PR 82 with
> `gh pr view 82 --repo 8b-is/smart-tree --json number,state,mergedAt,mergeCommit,headRefOid,statusCheckRollup`.
> Save the observed result in artifacts/smart-tree-pr-82.json. Report command
> failures as failures and distinguish CodeQL from functional tests.

The operator can exercise the same installed command runner directly:

```sh
/usr/local/libexec/8c-run gh pr view 82 --repo 8b-is/smart-tree \
  --json number,state,mergedAt,mergeCommit,headRefOid,statusCheckRollup
```

The runner's outer JSON contains `exit_code`, `timed_out`, `truncated`, and
`output`. Its own process exit alone does not establish command success. Refuse
to treat truncated output as a complete audit. GitHub CLI resolution checks only
`/usr/bin/gh` and `/usr/local/bin/gh`; job PATH entries cannot select a host
executable. A missing CLI produces an explicit error before command execution.
The existing host command allowlist still controls what the CLI may do.

For release evidence, inspect `https://i1.is/api/releases/live` and the immutable
build manifest named there. Compare the PR merge commit with the manifest's
`source_revision` using Git ancestry. Record the build ID, asset SHA-256, and
observation time. On each deployment target, compare the running executable's
hash with the installation receipt and preserve any local source patch. Version
text alone cannot distinguish two builds or a patched runtime.

## Remaining workflow implementation

The intended sequence is: inspect/fetch into an isolated checkout, update and
test the change, review the exact revision, merge, select the version, commit and
push the version change, build immutable artifacts, publish to a release channel,
verify the target, and retain rollback information. A tai channel should retain
the job, stage results, exact revisions, artifact hashes, and failures. Retries
must reconcile an earlier action before repeating it.

This pilot does not implement that mutation runner. Before enabling it, connect
the authenticated id1 subject to repository/action grants. Bind authorization to
the exact revision, artifact, channel and target, with expiry and replay
protection. A chat profile or a personal-memory challenge is not that identity.
Keep merge, publication, and service replacement independently observable.

The existing Smart Tree release script needs separate build and publication
stages: it currently publishes without interpreting `--publish`, and its Mac
build also changes installed executables. Its publisher has a single `current`
pointer. These behaviors must be corrected before a tai job can offer a reliable
staging or preview operation. Publishing an i1 download must not silently replace
a separately patched inference service.
