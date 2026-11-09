<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-gmail

Horae plugin: rules-first Gmail triage that turns requests into tasks.

> **0.1.0: OAuth and token lifecycle only.** This version signs in to Gmail with read-only
> scopes, keeps the token fresh, and ships one minimal source that proves it works. The
> triage into tasks is the next story ([#3](../../issues/3)).

It declares every host and secret it uses in `plugin.yaml`, reads nothing but message
headers, and proposes no intents.

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

## Which scope

The default scope is **`https://www.googleapis.com/auth/gmail.readonly`**.

| | `gmail.metadata` | `gmail.readonly` |
|---|---|---|
| Message ids, labels, headers | yes | yes |
| Message bodies and attachments | **no** | yes |
| `q` search parameter | **no** (the API refuses it) | yes |
| Can modify or send | no | no |

Story #3 sends mail that rules do not decide to the core's LLM step, to turn a request
into a task. That needs the body, which `gmail.metadata` cannot read, so the metadata scope
cannot do what #3 needs and the default is `gmail.readonly`. Both are read-only, and both
are Google *restricted* scopes (see [Verification](#restricted-scopes-and-verification)).

`gmail.recent` itself only asks for `format=metadata` with the `From`, `Subject` and `Date`
headers, so it works with either scope. If you want the least access while the triage is not
built, choose metadata when you run the helper (`--scope metadata`). No other scope can be
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
- At most 1 MiB per answer, after decompression, and at most 25 messages per call.

## Permissions

| | |
|---|---|
| Network | `gmail.googleapis.com`, `oauth2.googleapis.com` |
| Secrets | `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` |
| Intents | none |
| Paths | none |
| LLM calls | none |

## Development

```
uv sync
uv lock --check
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest --cov                                        # coverage gate 85%
HORAE_SDK_RECORD=1 uv run pytest                           # rewrite tests/recordings (review the diff)
uv run python -m horae_gmail --manifest > plugin.yaml      # regenerate the manifest
```

The tests use synthetic Gmail and OAuth responses recorded in `tests/fixtures/google` and a
fake Google on `127.0.0.1`; they never touch the network, and no fixture holds a real
address or token.

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
