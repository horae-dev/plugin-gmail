<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-gmail

Horae plugin: rules-first Gmail triage that turns requests into tasks.

> **0.2.0: rules-first triage into tasks** ([#3](../../issues/3)). On top of 0.1.0's OAuth and
> token lifecycle, `gmail.triage` reads the new mail, lets **your rules** decide it, and hands
> only the rest to the core's LLM step through the skill `gmail/triage-mail`. The recorded
> answers of the skill's golden cases are **hand-written, not a model's**; the wording of
> the tasks has not been judged by the maintainer yet.

It declares every host, secret and intent it uses in `plugin.yaml`. `gmail.recent` reads
nothing but message headers and proposes no intent; `gmail.triage` reads the plain-text body
and proposes `task.create` only for mail one of your rules made a task. **The plugin never
calls a model**: the model step belongs to the core's pipeline.

## `gmail.recent`

A source: the ids and three headers of the most recent messages. **No message body is
requested or read.**

| Parameter | Default | Meaning |
|---|---|---|
| `max_results` | `10` | How many messages, 1 to 25 |

Output (`data`):

```json
{
  "messages": [
    {
      "id": "18f0a1b2c3d4e5f1",
      "thread_id": "18f0a1b2c3d4e5f1",
      "from": "Ada Example \\<ada@example.org\\>",
      "subject": "Quarterly report draft",
      "date": "Mon, 02 Nov 2026 08:15:00 +0100"
    }
  ],
  "count": 1,
  "skipped": 0
}
```

**Headers are untrusted text**, written by whoever sent the mail. Each of `from`, `subject`
and `date` is made one inert line (control and format characters removed, Obsidian Tasks
signifiers and Markdown escaped, by the SDK's `escape_title`) and capped at 200, 200 and 64
characters. A header the message does not have is `""`. `skipped` counts messages deleted
between the list and the read, or whose answer could not be read.

### Failures

Every failure is a fixed code; Google's own text, and any token, never reach an error.

| Error | Meaning |
|---|---|
| `auth_failed: ...` | Google refused the refresh token (`invalid_grant`: revoked, expired, or the app is in **Testing** and the token is older than 7 days; `invalid_client` or `unauthorized_client`: wrong client id or secret), or still answered 401 after a fresh token. The message tells you to run the consent helper again. |
| `scope_not_read_only` | The token Google issued carries a scope other than the two read-only ones; it is refused. |
| `secret_missing: ...` | One of the three `GMAIL_*` secrets is unset, empty or holds spaces. The message names the secrets, never their values. |
| `host_not_approved: <host>` | The core's `horae.lock` does not approve that Google host; nothing was contacted. |
| `rate_limited`, `http_<status>`, `token_http_<status>`, `bad_response` | The API answered with that status, or with something unreadable. |
| `timeout`, `connect_failed`, `tls_failed`, `redirect_refused`, `too_large`, `transport_failed` | The call itself failed (see [Bounds](#bounds)). |

## Triage

`gmail.triage` + the skill `gmail/triage-mail` + the example pipeline
[`examples/triage-mail.pipeline.yaml`](examples/triage-mail.pipeline.yaml):

```
gmail.triage ──► your rules decide ──► ignore                      (nothing)
 (new mail)                        ├─► task ──► task.create        (fingerprint, Gmail link)
                                   └─► interpret / no rule ──► needs_interpretation
                                                   │
                       core llm step: skill gmail/triage-mail ──► task.create (no link)
```

### `gmail.triage`

A source. It reads the mail added to a label (default `INBOX`) **since the last run**, applies
your rules, and returns one decision per message.

| Parameter | Default | Meaning |
|---|---|---|
| `state_path` | required | absolute path of the JSON file that keeps the since-state; its directory must be approved for writing in `horae.lock` (`paths.write`) |
| `rules` | none | the rules below; with none, every message needs interpretation |
| `account` | `mail` | a label (A-Z a-z 0-9 . _ -, at most 32): the state key |
| `label_id` | `INBOX` | the Gmail label id whose new mail is read |
| `max_messages` | `25` | 1 to 100 messages per call |
| `body_kib` | `16` | 1 to 64 KiB of the plain-text body read |
| `snippet_chars` | `300` | 50 to 1000 characters of snippet sent to the LLM step |
| `update_state` | `true` | `false` reads without moving the state (a dry run) |

Output (`data`), per message exactly one decision:

```json
{
  "account": "mail",
  "baselined": false,
  "reset": false,
  "decisions": [
    {"id": "18f0c0000000b001", "thread_id": "18f0c0000000b001", "action": "ignore", "decided": true, "rule": "promotions"},
    {"id": "18f0c0000000b002", "thread_id": "18f0c0000000b002", "action": "task", "decided": true, "rule": "bills"},
    {"id": "18f0c0000000b003", "thread_id": "18f0c0000000b003", "action": "interpret", "decided": false, "rule": null}
  ],
  "counts": {"ignore": 1, "task": 1, "interpret": 1, "decided": 2},
  "needs_interpretation": [
    {"id": "18f0c0000000b003", "from": "Marc Dupont \\<marc@example.net\\>", "subject": "Lease scan",
     "date": "Mon, 02 Nov 2026 08:15:00 +0100", "snippet": "Can you send me the scan of the lease?"}
  ],
  "truncated": false,
  "skipped": 0,
  "state_updated": true
}
```

- **`decided`** says whether the rules settled the message. `needs_interpretation` holds
  only the messages with `decided: false`: when the rules decide everything it is `[]`, and
  nothing of any message body leaves the plugin.
- A message a rule decided is **never** in the LLM step's input (the skill's view shows
  only `needs_interpretation`).

### Rules

Deterministic, no LLM. A rule has a `name`, an `action` and at least one matcher; **the first
rule that matches wins**, so a message gets one decision.

```yaml
rules:
  - name: promotions                 # 1 to 40 of A-Z a-z 0-9 . _ -
    action: ignore                   # ignore | task | interpret
    label: [CATEGORY_PROMOTIONS]     # Gmail label ids (system labels, or Label_12 for yours)
  - name: newsletters
    action: ignore
    list_id: ["*"]                   # "*" means "has a List-Id header"
  - name: bills
    action: task
    from: ["billing@orange.example"] # the address, never the display name
    subject: ["*invoice*", "*facture*"]
    task_prefix: "Pay: "             # optional, before the subject in the title
    due_in_days: 14                  # optional, days after the message's day
  - name: school
    action: interpret                # always to the model; placed before a later ignore
    from: ["*@school.example.org"]
```

- Matchers (`from`, `subject`, `label`, `list_id`) are lists of case-insensitive `fnmatch`
  globs (`*`, `?`, `[...]`), not regular expressions, on at most 300 characters of the field.
  A field matches when any pattern does; a rule matches when every field it names does. A
  field the message does not have matches nothing, `*` included.
- `ignore`: nothing happens. `task`: a `task.create` (below). `interpret`: to the LLM step.
  A message no rule matches **needs interpretation** too.

### Tasks from rules

A `task` rule proposes a `task.create` with the title `task_prefix` + the subject (escaped by
the SDK), a `due` of `due_in_days` after the message's day when set, the fingerprint
**`gmail:<message id>`** (stable across runs: the core does not create it twice) and the link
`https://mail.google.com/mail/u/0/#all/<message id>`. The link is built from the id, never
from anything in the mail. The core routes it only to a `mode: tasks` entry whose trusted
`link_prefix` covers it (`https://mail.google.com/mail/u/0/`), or the step fails with
`intent_unrouted`. The plugin never contacts `mail.google.com`.

### Since-state

Per `account` label, the `historyId` the last run reached, in the JSON file at `state_path`
(`{"version": 1, "accounts": {"mail": {"history_id": "5000"}}}`; no mail, no secret). It is
written once, atomically, after all the mail was read; a run that raises writes nothing, so
its mail is read again next time. A mail whose task the core later drops is not retried.

- **First run (no state):** nothing is handled; the state starts at the mailbox's current
  `historyId` (`baselined: true`), so old mail never floods the task list.
- **`max_messages`:** the state stops after the last history record taken and `truncated` is
  true; the next run continues.
- **Expired `historyId`** (Google keeps history about a week; a 404): the state is reset to
  now and `reset: true` says that mail may have been missed. Run the pipeline more often than
  weekly.
- A message deleted before it was read, or over 1 MiB, is counted in `skipped`.
- A corrupt state file is refused (`state_invalid`), never silently re-baselined.

### What reaches the LLM step (untrusted mail)

Only messages with `decided: false`, as `id`, `from`, `subject`, `date` and `snippet`:

- `from`, `subject` and `date` are escaped, one line, capped at 200, 200 and 64 characters.
- the **snippet** is the start of the first inline `text/plain` part: at most `body_kib` KiB
  are decoded, quoted lines (`>`) are dropped, and it is made one inert line (control
  characters removed, Obsidian Tasks signifiers, Markdown, wikilinks and links neutralised by
  `escape_title`) of at most `snippet_chars` characters.
- **no HTML** is read or returned (an HTML-only mail has an empty snippet), and **no
  attachment** is fetched or returned.
- nothing in a mail is ever fetched or followed; a link in a mail is neutralised and never
  becomes a task link.

### The skill `gmail/triage-mail`

Shipped in this repository (`skills/gmail/triage-mail/`, listed in `provides.skills`), not in a
separate pack: the skill reads this plugin's output shape and has no other user, so the two
change together and are tested together. A `pack-mail-triage` that bundles the pipeline
(with rules for IMAP mail too) is a follow-up, once the core installs a pack's pipelines.

- **Input view:** `data.needs_interpretation[].{id, from, subject, date, snippet}` and
  `data.truncated`; the rules, decisions, thread ids and paths are not shown.
- **Output (strict schema):** `summary`; `triaged`, one entry per message
  `{message_id, outcome: task | no_task | suspicious, confidence: high | medium | low, title, due}`
  (`due` optional, a date only when the mail states it); `intents`, a `task.create`
  `{title, due, fingerprint: "gmail:<message id>"}` for each `task` that is not `low`
  confidence. `links: none`: the guard refuses every link and URL, so an LLM task has no
  link. At most 25 intents.
- **Prompt injection:** the prompt says the input is data; a mail that tries to instruct the
  model is `suspicious`. The guard (decision 55) is the second line.
- **References:** the answer must cite only ids that were in the input. The golden-case tests
  check it (`tests/test_skill.py`); see the follow-ups for the core side.

### Golden cases (evals)

`skills/gmail/triage-mail/evals/` holds 12 cases: an invoice due, a meeting request, a school
notice (French, `14/11/2026`), a newsletter, a shipping notice, a personal request, a
phishing-style "urgent" mail (flagged, no task), a vague mail (low confidence), a mixed batch
of four, an empty batch (the rules decided everything), a prompt-injection mail answered as
data, and the same mail answered by a model that **obeyed** (a URL in a task title), which the
guard refuses. They assert properties (counts, references, `matches` on fingerprints,
`not_contains` on titles), never exact text.

- **Offline, on every pull request** (the `test` job runs `tests/test_skill.py`):
  `horae eval --json` over every case with the core's public replay: no network, no secret,
  no cost. A stale recording fails.
- The recorded answers (`evals/recorded/anthropic/*.json`, model `claude-fixture-1`) are
  **hand-written**. After a prompt, schema or view change, refresh their digests with
  `uv run python scripts/sync_recordings.py`. **No live eval is run here**; re-recording
  with a real model (`horae eval --live --record`) is for the maintainer, with a spend cap.

### Pipeline

[`examples/triage-mail.pipeline.yaml`](examples/triage-mail.pipeline.yaml) is an example to
copy into your instance (the core does not install a plugin's pipelines yet): collect with
your rules, the `llm` step, and the tasks sink with `link_prefix:
"https://mail.google.com/mail/u/0/"`. **No LLM call when rules decide:** the plugin returns
no LLM input then, and with the same (empty) input the step's `on_unchanged_input: skip` makes
no call after the first run. The core has no way yet to skip an `llm` step whose view is
empty on its first run (follow-up).

### Follow-ups

- Core: skip an `llm` step when its viewed input holds no item (a `skip_if_empty` option).
- Core: the guard checks a `task.update_status` fingerprint against the viewed input but not a
  `task.create` one, so a model that invents a message id is caught only by the evals here.
- Core: a task link derived from the fingerprint, so that an LLM-made task (which cannot
  carry a link under `links: none`) links to its mail.
- `pack-mail-triage`: the pipeline and skill as a pack, with IMAP mail through the same skill.
- Label names (`label: [Receipts]`) need `labels.list`; today labels are matched by id.
- A multi-account link (`/mail/u/1/`) and a live eval on a release tag.

## Which scope

The default scope is **`https://www.googleapis.com/auth/gmail.readonly`**.

| | `gmail.metadata` | `gmail.readonly` |
|---|---|---|
| Message ids, labels, headers | yes | yes |
| Message bodies and attachments | **no** | yes |
| `q` search parameter | **no** (the API refuses it) | yes |
| Can modify or send | no | no |

`gmail.triage` sends mail that rules do not decide to the core's LLM step, to turn a request
into a task. That needs the body, which `gmail.metadata` cannot read, so the metadata scope
cannot do what the triage needs and the default is `gmail.readonly`. Both are read-only, and both
are Google *restricted* scopes (see [Verification](#restricted-scopes-and-verification)).

`gmail.recent` itself only asks for `format=metadata` with the `From`, `Subject` and `Date`
headers, so it works with either scope; `gmail.triage` needs `gmail.readonly`. If you only use
`gmail.recent`, choose metadata when you run the helper (`--scope metadata`). No other scope can be
requested: modify, compose, send, insert, settings and full-mailbox scopes appear nowhere in
this plugin, the consent helper refuses them, and an access token that carries one is
refused.

## Set up the Google Cloud project (once)

The plugin reads your own mailbox with your own OAuth client; nothing is shared with anyone.

1. In the [Google Cloud console](https://console.cloud.google.com/) create a project (or pick
   one) and enable the **Gmail API** (APIs and services, Library).
2. **OAuth consent screen** (Google Auth platform, Branding and Audience): user type
   **External** for a personal Gmail account (**Internal** only inside a Google Workspace
   organisation). Fill the app name and your e-mail.
3. **Scopes** (Data access): add only `https://www.googleapis.com/auth/gmail.readonly` (or
   `gmail.metadata`, see above). Do not add any other.
4. **Publish the app** (next section).
5. **Credentials, Create credentials, OAuth client ID**, application type **Desktop app**
   (the installed-app flow with a loopback redirect: no redirect URI to register). Note the
   client id and the client secret. For a Desktop app the secret is not confidential in the
   OAuth sense, but the plugin still treats it as a secret.
6. Get the refresh token with the [consent helper](#run-the-consent-helper).

## Publish the app to production

Set the publishing status to **"In production"** (Audience, Publish app). This is the step
that matters: an app left in **"Testing"** issues refresh tokens that **expire after 7
days**, so the pipeline would break every week with `auth_failed` and you would have to run
the helper again. A test user does not change that.

Publishing an app that uses a restricted scope shows an "unverified app" warning at consent,
which you accept for your own account (Advanced, Go to the app). An unverified app is limited
to 100 users, which is far more than one person needs. Publish **before** you run the helper:
a refresh token obtained while the app was in Testing keeps its 7-day life, so get a new one
after publishing.

## Restricted scopes and verification

Every Gmail scope that reads message content or metadata, `gmail.readonly` and
`gmail.metadata` included, is classed by Google as **restricted**. For an app that other
people use, Google requires the app to pass OAuth verification (a brand and domain review, a
privacy policy, and an annual third-party security assessment). **That is not needed for
personal use**: one person authorising their own client, in production and unverified, is
what this plugin is built for. Plan for verification only if you want to give this OAuth
client to other people. Google's rules change, so check the current
[Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) and
[restricted scope verification](https://support.google.com/cloud/answer/13464325)
pages when you set up.

## Run the consent helper

Run this **on your machine, once**; it is not part of the plugin server and is not run by
Horae. It needs the plugin's package (`uv run` in a clone, or the interpreter you pin in
`plugin.yaml`):

```
umask 077
GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... uv run python -m horae_gmail google-auth > gmail.env
```

Without the variables it asks for the client id and secret (the secret without echo).
`--no-browser` only prints the consent URL instead of opening a browser. `--scope readonly`
(the default) or `--scope metadata` (also `GMAIL_SCOPE`) chooses the scope; **any other value
is refused before anything is opened or sent**, and so is a token Google grants with a scope
other than the one requested.

The helper starts a server on `127.0.0.1` on a free port, opens Google's consent page
(`accounts.google.com`, which only this helper talks to), receives the answer on that port
with a one-time `state` and a PKCE challenge, exchanges the code, and prints **three lines on
stdout**: `GMAIL_CLIENT_ID=...`, `GMAIL_CLIENT_SECRET=...` and `GMAIL_REFRESH_TOKEN=...`.
Everything else (the consent URL, errors) goes to stderr, and nothing is stored by the
command. If Google returns no refresh token (the app had already been authorised), remove its
access at [myaccount.google.com/permissions](https://myaccount.google.com/permissions) and run
it again. Put each value in the matching secret of your Horae instance (mode 600), and delete
`gmail.env`.

## The credentials are secrets

The three secrets are declared in `plugin.yaml` and injected by the host as environment
variables; the plugin reads them at call time and never puts them in `params`, output,
errors, logs or the repository.

- Each call exchanges the refresh token at `oauth2.googleapis.com` for a short-lived access
  token. **It lives in memory for that call only**: never written, logged or returned.
- A `401` from the Gmail API makes **one** fresh exchange and **one** retry; a second `401`
  is `auth_failed`.
- `invalid_grant` is `auth_failed` with a fixed message that tells you to rerun the consent
  helper.
- The `repr` of the credentials hides them, and an unexpected error is logged by its type
  only.

## Bounds

- Hosts: **`oauth2.googleapis.com`** (the token exchange) and **`gmail.googleapis.com`** (the
  API), declared in `plugin.yaml`, checked in code (`https`, default port, no user info) and,
  when the core sends its approved list, against `horae.lock` with `Call.host_allowed`
  (`host_not_approved: <host>`).
- **No redirects at all**: a 3xx is `redirect_refused`, so a bearer token or client secret can
  never be sent anywhere else. `trust_env=False`: no proxy or CA bundle from the environment.
- Timeouts: 5 s to connect, 8 s per read, 9 s per attempt, 20 s per request, 20 s for the
  whole read. One retry after a timeout, a connection error or a 500, 502, 503 or 504.
- At most 1 MiB per answer, after decompression; at most 25 messages per `gmail.recent` call and 100 per `gmail.triage` call.

## Permissions

| | |
|---|---|
| Network | `gmail.googleapis.com`, `oauth2.googleapis.com` |
| Secrets | `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` |
| Intents | `task.create` (only `gmail.triage`, only for mail a rule made a task) |
| Paths | none declared; the state file's directory is granted per instance in `horae.lock` (`paths.write`) |
| LLM calls | none by the plugin; the pipeline's core `llm` step runs the shipped skill `gmail/triage-mail` |

## Development

```
uv sync
uv lock --check
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest --cov                                        # coverage gate 85%
HORAE_SDK_RECORD=1 uv run pytest                           # rewrite tests/recordings (review the diff)
uv run python -m horae_gmail --manifest > plugin.yaml      # regenerate the manifest
uv run horae eval gmail/triage-mail --plugin-dir .         # the golden cases, offline
uv run python scripts/sync_recordings.py                   # refresh the hand-written answers' digests
```

The tests use synthetic Gmail and OAuth responses recorded in `tests/fixtures/google` and a
fake Google on `127.0.0.1`; they never touch the network, and no fixture holds a real
address or token.

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
