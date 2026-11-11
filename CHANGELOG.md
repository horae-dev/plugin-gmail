# Changelog

Every change to tools, parameters and permissions. A new permission is called out: users
must approve it.

## 0.3.0

Triage follow-ups (#6, items 1 and 2). **No new permission, host, secret, intent or scope**
(`plugin.yaml` changes only its version); **the plugin makes no LLM call**.

- **Labels by name.** `label_id` and the rules' `label` matchers take a label id or a label
  name. Names are resolved with `users.labels.list` (a GET that `gmail.readonly` and
  `gmail.metadata` already allow), at most once per run and only when a name or custom id must
  be resolved: system label ids need no call. Resolution: exact id, then exact-case name, then
  name ignoring case. A missing reference fails the run with `label_not_found`, an ambiguous
  one with `label_ambiguous` (the ids are listed); the state does not move. Wildcard matchers
  (`Clients/*`) are also tried against label names. `label_id` is no longer limited to the id
  characters (a name may hold spaces and `/`).
- **Behaviour change**: a literal `label` id that is not a system label (`Label_12`) is now
  checked against the mailbox, so one that no longer exists fails the run instead of never
  matching.
- **Multi-account links.** New `gmail.triage` parameter `account_index` (0 to 99, default
  `0`): task links are `https://mail.google.com/mail/u/<account_index>/#all/<message id>`.
  The fingerprint does not change. The pipeline's `link_prefix` must cover that path:
  `https://mail.google.com/mail/u/<n>/` for one account, `https://mail.google.com/mail/u/` for
  all. The manifest declares no `link_prefix`, so it is unchanged; the example pipeline
  documents the rule.
- Not done here (left in #6): the move to `pack-mail-triage` and live evals.

## 0.2.1

Schemas decision 67 (#7). No new permission, host, secret or intent; **the plugin makes no
LLM call**.

- Skill `gmail/triage-mail`: `task_fingerprint: {ids: "[].data.needs_interpretation[].id",
  prefix: "gmail:"}`. The core's guard refuses a `task.create` whose fingerprint is not
  `gmail:` + a viewed mail id. The prompt states the rule.
- `examples/triage-mail.pipeline.yaml`: `skip_when_empty: ["[].data.needs_interpretation"]` on
  the `llm` step (no call and no cost when the rules decided every mail).
- New golden case `invented-id` (a guard violation) and a pipeline-level test of the skip.
  Recordings re-synced (digests only). Locked core and `horae-sdk` bumped.

## 0.2.0

Rules-first triage into tasks (#3).

- **New permission** (users must approve it): intent `task.create`. No new host or secret.
  The since-state file's directory is granted per instance in `horae.lock` (`paths.write`),
  not declared. **The plugin makes no LLM call.**
- **`gmail.triage` source**: the mail added to a label since the last run (`historyId` in a
  JSON file at `state_path`; the first run baselines and handles nothing). Parameters:
  `state_path`, `rules`, `account`, `label_id`, `max_messages`, `body_kib`, `snippet_chars`,
  `update_state`. User rules (`from`, `subject`, `label`, `list_id` globs; actions `ignore`,
  `task`, `interpret`; first match wins) give each message one decision, reported with
  `decided`. A `task` rule proposes `task.create` with fingerprint `gmail:<message id>` and
  the link `https://mail.google.com/mail/u/0/#all/<message id>` (routed by `link_prefix`).
  Undecided mail is returned as `needs_interpretation`: escaped, capped snippets of the
  plain-text body; no HTML, no attachment.
- **Skill `gmail/triage-mail`** (`provides.skills`): strict output schema, `links: none`,
  `task.create` up to 25, 12 golden cases run offline with `horae eval` and hand-written
  recorded answers. `scripts/sync_recordings.py` refreshes their digests.
- **Example pipeline** `examples/triage-mail.pipeline.yaml`.
- Dev: `horae` (the core) as a dev dependency, for the golden-case tests.

## 0.1.0

First release (#2): Gmail OAuth and the token lifecycle.

- **New permissions** (users must approve them): network `gmail.googleapis.com` and
  `oauth2.googleapis.com`; secrets `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` and
  `GMAIL_REFRESH_TOKEN`. No intent, path or LLM call.
- **`gmail.recent` source**: the ids and `From`, `Subject` and `Date` headers of the latest
  1 to 25 messages (`max_results`, default 10). No message body is requested. Headers are
  escaped and capped.
- **Read-only scopes**: `gmail.readonly` by default (the triage of #3 needs bodies, which
  `gmail.metadata` cannot read); `gmail.metadata` can be chosen. No modify, compose or send
  scope exists in the plugin.
- **Token lifecycle**: a refresh token is exchanged for an access token, kept in memory for
  one call. A 401 makes one refresh and one retry; `invalid_grant` is `auth_failed` with a
  fixed message. No redirects, timeouts, and only the two declared hosts, also checked with
  `Call.host_allowed` (requires horae-sdk 0.5).
- **`python -m horae_gmail google-auth`**: the one-time consent helper (loopback redirect,
  PKCE). It requests one read-only scope and refuses any other.
- Docs: Google Cloud setup, publishing to production (Testing-mode refresh tokens expire
  after 7 days), restricted-scope verification notes.
