# Changelog

Every change to tools, parameters and permissions. A new permission is called out: users
must approve it.

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
