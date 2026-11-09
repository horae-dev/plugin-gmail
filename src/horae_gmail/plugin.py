# SPDX-License-Identifier: Apache-2.0
"""The plugin: two sources, `gmail.recent` (the ids and headers of the latest messages) and
`gmail.triage` (the new mail, decided by the user's rules first).

The core's plugin host starts this module as an MCP server over stdio (`python -m
horae_gmail`). The plugin only reads. `gmail.recent` proposes no intent; `gmail.triage`
proposes a `task.create` only for mail a user rule turned into a task, and hands the rest to
the core's LLM step as data (the skill `gmail/triage-mail`): **a plugin never calls a model**.

**Scopes**: read-only only (`oauth.READ_ONLY_SCOPES`); this plugin names no modify or send
scope. **Hosts**: `oauth2.googleapis.com` (the token exchange) and `gmail.googleapis.com`,
declared in `plugin.yaml` and checked in code and, when the core sent a list, against
`horae.lock` with `Call.host_allowed`. **Secrets**: `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`
and `GMAIL_REFRESH_TOKEN`, read from the environment at call time. Access tokens live in
memory for one call and are never written, logged or returned.

A message's headers are written by the sender, so they are **untrusted**: see `gmail.py`.
"""

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from horae_sdk import Call, Output, Permissions, Plugin, PluginError, intents
from horae_sdk import manifest as build_manifest

from horae_gmail import gmail, mail, oauth, state, triage
from horae_gmail import rules as rule_engine
from horae_gmail.fetch import Api, FetchError, HttpFetcher, check_url

NAME = "gmail"
VERSION = "0.2.0"
SKILL = "gmail/triage-mail"
"""The skill the plugin ships (`skills/gmail/triage-mail`)."""
LINK = "https://mail.google.com/mail/u/0/#all/"
"""A task's link is this and the message id. Google is never contacted at this address (it is
not in `HOSTS`): the core only routes the link under the `tasks` entry's `link_prefix`."""
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
LABEL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_MESSAGES = 100
"""Per call (the SDK refuses more than 256 intents)."""
MAX_BODY_KIB = 64
MAX_SNIPPET = 1000
HORAE = ">=0,<0.2"
COMMAND = ["python3", "-m", "horae_gmail"]
"""`runtime.command` of `plugin.yaml`. When installing, pin an absolute interpreter that has
this package (the host's `PATH` is fixed and its working directory empty)."""
HOSTS = ("gmail.googleapis.com", "oauth2.googleapis.com")
"""The hosts declared in `plugin.yaml`; the only ones the plugin contacts."""
PERMISSIONS = Permissions(intents=["task.create"], network=list(HOSTS), secrets=list(oauth.SECRETS))
"""Declare the least you need: `task.create` (only for mail a rule made a task), two hosts,
three secrets. The since-state file's directory is granted per instance in `horae.lock`
(`paths.write`), not declared here."""

_log = logging.getLogger(__name__)


def build(api: Api | None = None, *, environ: Mapping[str, str] | None = None) -> Plugin:
    """The plugin, calling Google with `api` (default `HttpFetcher()`) and reading the
    secrets from `environ` (default `os.environ`, read at call time)."""
    client: Api = api if api is not None else HttpFetcher()
    env: Mapping[str, str] = environ if environ is not None else os.environ
    plugin = Plugin(
        NAME,
        version=VERSION,
        permissions=PERMISSIONS,
        description="Gmail triage: user rules first, then the core LLM step; read-only.",
    )

    @plugin.source(f"{NAME}.recent")
    async def recent(call: Call, max_results: int = gmail.DEFAULT_MESSAGES) -> Output:
        """The ids and `From`, `Subject` and `Date` headers of the `max_results` most recent
        messages (1 to 25), with no body. A header is untrusted text: escaped and capped."""
        if (
            type(max_results) is not int
            or isinstance(max_results, bool)
            or not 1 <= max_results <= gmail.MAX_MESSAGES
        ):
            raise PluginError(
                f"config_invalid: max_results must be a whole number from 1 to {gmail.MAX_MESSAGES}"
            )
        tokens = _tokens(call, client, env)
        try:
            messages, skipped = await gmail.recent(client, tokens, max_results)
        except oauth.GoogleError as error:
            raise PluginError(_message(error.reason)) from None
        except FetchError as error:
            raise PluginError(error.reason) from None
        except Exception as error:
            # The type only: a message could hold a token.
            _log.error("gmail.recent: unexpected %s", type(error).__name__)
            raise PluginError("internal_error") from None
        return Output({"messages": messages, "count": len(messages), "skipped": skipped})

    @plugin.source(f"{NAME}.triage")
    async def triage_source(
        call: Call,
        state_path: str | None = None,
        account: str = "mail",
        label_id: str = "INBOX",
        rules: list[dict[str, Any]] | None = None,
        max_messages: int = 25,
        body_kib: int = 16,
        snippet_chars: int = 300,
        update_state: bool = True,
    ) -> Output:
        """The mail that arrived in `label_id` since the last run (the `historyId` kept in
        `state_path`), read-only. Each message gets exactly one decision from `rules`: ignore,
        task (a `task.create` intent) or interpret, and a message no rule matches is
        `interpret`. Only the `interpret` messages are returned for the core's LLM step, as
        capped, escaped snippets of the plain-text body (no HTML, no attachment)."""
        opts = _options(
            call,
            {
                "state_path": state_path,
                "account": account,
                "label_id": label_id,
                "rules": rules,
                "max_messages": max_messages,
                "body_kib": body_kib,
                "snippet_chars": snippet_chars,
                "update_state": update_state,
            },
        )
        tokens = _tokens(call, client, env)
        api = gmail.Gmail(client, tokens)
        try:
            return await _triage(api, opts)
        except oauth.GoogleError as error:
            raise PluginError(_message(error.reason)) from None
        except FetchError as error:
            raise PluginError(error.reason) from None
        except PluginError:
            raise
        except Exception as error:
            _log.error("gmail.triage: unexpected %s", type(error).__name__)
            raise PluginError("internal_error") from None

    return plugin


def _tokens(call: Call, client: Api, env: Mapping[str, str]) -> oauth.AccessTokens:
    """The token source of a call, once the secrets are set and the hosts approved."""
    credentials = oauth.credentials(env)
    if credentials is None:
        raise PluginError(
            "secret_missing: " + ", ".join(oauth.SECRETS) + " must each be set, without spaces"
        )
    for endpoint in (oauth.TOKEN_URL, gmail.API_URL):  # the hosts this call contacts
        check_url(endpoint, HOSTS)
        if call.host_allowed(endpoint) is False:
            raise PluginError(f"host_not_approved: {endpoint.split('/')[2]}")
    return oauth.AccessTokens(client, credentials)


@dataclass(frozen=True)
class Options:
    """The validated parameters of one `gmail.triage` call."""

    state_path: str
    account: str
    label_id: str
    rules: tuple[rule_engine.Rule, ...]
    max_messages: int
    body_bytes: int
    snippet_chars: int
    update_state: bool


def _bounded(value: object, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise PluginError(f"config_invalid: {name} is a whole number from {low} to {high}")
    return value


def _options(call: Call, params: Mapping[str, Any]) -> Options:
    """Check every parameter. Nothing here touches the network."""
    state_path = params["state_path"]
    if not isinstance(state_path, str):
        raise PluginError("config_invalid: state_path is required (an absolute path)")
    real = call.paths.allowed(state_path, write=True)
    if real is None:
        raise PluginError("path_not_approved: state_path is not under a path approved for writing")
    account, label_id = params["account"], params["label_id"]
    if not isinstance(account, str) or not LABEL.fullmatch(account):
        raise PluginError("config_invalid: account is 1 to 32 of A-Z a-z 0-9 . _ -")
    if not isinstance(label_id, str) or not LABEL_ID.fullmatch(label_id):
        raise PluginError("config_invalid: label_id is a Gmail label id such as INBOX")
    if params["update_state"] is not True and params["update_state"] is not False:
        raise PluginError("config_invalid: update_state is true or false")
    return Options(
        state_path=real,
        account=account,
        label_id=label_id,
        rules=rule_engine.parse_rules(params["rules"]),
        max_messages=_bounded(params["max_messages"], "max_messages", 1, MAX_MESSAGES),
        body_bytes=_bounded(params["body_kib"], "body_kib", 1, MAX_BODY_KIB) * 1024,
        snippet_chars=_bounded(params["snippet_chars"], "snippet_chars", 50, MAX_SNIPPET),
        update_state=params["update_state"],
    )


def _task(message: mail.Message, rule: rule_engine.Rule) -> dict[str, Any]:
    """The `task.create` of a message a rule made a task: the fingerprint and the link come
    from the message id, the title from the subject (escaped by the SDK)."""
    due = None
    if rule.due_in_days is not None and message.day is not None:
        due = message.day + timedelta(days=rule.due_in_days)
    title = rule.task_prefix + (message.subject or "(no subject)")
    return intents.task_create(
        title,
        key=intents.fingerprint(NAME, message.id),
        due=due,
        link=f"{LINK}{message.id}",
    )


async def _triage(api: gmail.Gmail, opts: Options) -> Output:
    previous = state.load(opts.state_path)
    start = previous.get(opts.account)
    reset = baselined = False
    truncated = False
    found: list[mail.Message] = []
    skipped = 0
    if start is None:
        reached, baselined = await triage.current(api), True
    else:
        try:
            ids, reached, truncated = await triage.new_ids(
                api, start, opts.label_id, opts.max_messages
            )
        except triage.HistoryExpired:
            reached, reset = await triage.current(api), True
        else:
            found, skipped = await triage.fetch(api, ids, opts.body_bytes)
    decisions: list[dict[str, Any]] = []
    interpret: list[dict[str, str]] = []
    proposed: list[dict[str, Any]] = []
    counts = {"ignore": 0, "task": 0, "interpret": 0}
    for message in found:
        decision = rule_engine.decide(opts.rules, message)
        counts[decision.action] += 1
        decisions.append(
            {
                "id": message.id,
                "thread_id": message.thread_id,
                "action": decision.action,
                "decided": decision.decided,
                "rule": decision.rule.name if decision.rule else None,
            }
        )
        if decision.action == "task" and decision.rule is not None:
            proposed.append(_task(message, decision.rule))
        elif decision.action == "interpret":
            interpret.append(
                {
                    "id": message.id,
                    "from": message.from_line,
                    "subject": message.subject_line,
                    "date": message.date_line,
                    "snippet": mail.snippet(message, opts.snippet_chars),
                }
            )
    if opts.update_state:
        state.save(opts.state_path, {**previous, opts.account: reached})
    return Output(
        {
            "account": opts.account,
            "baselined": baselined,
            "reset": reset,
            "decisions": decisions,
            "counts": {**counts, "decided": counts["ignore"] + counts["task"]},
            "needs_interpretation": interpret,
            "truncated": truncated,
            "skipped": skipped,
            "state_updated": opts.update_state,
        },
        intents=proposed,
    )


def _message(reason: str) -> str:
    """The fixed error text of a failure: `auth_failed` tells the user what to do."""
    return oauth.AUTH_FAILED_MESSAGE if reason == "auth_failed" else reason


def manifest() -> dict[str, Any]:
    """The `plugin.yaml` document of this plugin (`python -m horae_gmail --manifest`)."""
    plugin = build()
    return build_manifest(
        name=plugin.name,
        version=plugin.version,
        provides={**plugin.provides(), "skills": [SKILL]},
        permissions=plugin.permissions,
        command=COMMAND,
        horae=HORAE,
        description=plugin.description,
        license="Apache-2.0",
    )


__all__ = ["COMMAND", "HOSTS", "NAME", "SKILL", "build", "manifest"]
