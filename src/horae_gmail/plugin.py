# SPDX-License-Identifier: Apache-2.0
"""The plugin: one source, `gmail.recent`, the ids and headers of the latest messages.

The core's plugin host starts this module as an MCP server over stdio (`python -m
horae_gmail`). The plugin only reads, and returns data: it proposes no intent (the triage into
tasks is story #3).

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
from collections.abc import Mapping
from typing import Any

from horae_sdk import Call, Output, Permissions, Plugin, PluginError

from horae_gmail import gmail, oauth
from horae_gmail.fetch import Api, FetchError, HttpFetcher, check_url

NAME = "gmail"
VERSION = "0.1.0"
HORAE = ">=0,<0.2"
COMMAND = ["python3", "-m", "horae_gmail"]
"""`runtime.command` of `plugin.yaml`. When installing, pin an absolute interpreter that has
this package (the host's `PATH` is fixed and its working directory empty)."""
HOSTS = ("gmail.googleapis.com", "oauth2.googleapis.com")
"""The hosts declared in `plugin.yaml`; the only ones the plugin contacts."""
PERMISSIONS = Permissions(network=list(HOSTS), secrets=list(oauth.SECRETS))
"""Declare the least you need: no intent (the source proposes none), two hosts, three
secrets."""

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
        description="Recent Gmail message ids and headers (From, Subject, Date), read-only.",
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
        credentials = oauth.credentials(env)
        if credentials is None:
            raise PluginError(
                "secret_missing: " + ", ".join(oauth.SECRETS) + " must each be set, without spaces"
            )
        for endpoint in (oauth.TOKEN_URL, gmail.API_URL):  # the hosts this call contacts
            check_url(endpoint, HOSTS)
            if call.host_allowed(endpoint) is False:
                raise PluginError(f"host_not_approved: {endpoint.split('/')[2]}")
        tokens = oauth.AccessTokens(client, credentials)
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

    return plugin


def _message(reason: str) -> str:
    """The fixed error text of a failure: `auth_failed` tells the user what to do."""
    return oauth.AUTH_FAILED_MESSAGE if reason == "auth_failed" else reason


def manifest() -> dict[str, Any]:
    """The `plugin.yaml` document of this plugin (`python -m horae_gmail --manifest`)."""
    return build().manifest(command=COMMAND, horae=HORAE, license="Apache-2.0")


__all__ = ["COMMAND", "HOSTS", "NAME", "build", "manifest"]
