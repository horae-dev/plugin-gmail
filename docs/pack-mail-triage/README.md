# pack-mail-triage (proposal)

> **Proposed content for a new repository, `horae-dev/pack-mail-triage`.** It lives under
> `docs/pack-mail-triage/` of plugin-gmail until the maintainer creates that repository
> ([#6](https://github.com/horae-dev/plugin-gmail/issues/6), item 3). To publish: create the empty
> repository, copy this directory's contents to its root, and add what every pack repository has
> (`LICENSE`, `AGENTS.md`, `CLAUDE.md`, `graft.yaml`, `renovate.json`, `.github/`, `pyproject.toml`
> and `uv.lock` for the checks, `tests/`), as in
> [pack-daily-brief](https://github.com/horae-dev/pack-daily-brief). The checks of these files
> are in plugin-gmail's `tests/test_pack.py`; move them with the pack (they read `ROOT / "plugin.yaml"`).

Horae pipeline pack: Gmail triage into tasks. The `mail-triage` pipeline and the
`mail-triage/triage-mail` skill its `llm` step runs.

A pack has no runtime: it ships files, so it needs no network, secret or path. Its one intent,
`task.create`, is what the skill proposes. The mail is read, and the rules applied, by
[plugin-gmail](https://github.com/horae-dev/plugin-gmail) (`gmail.triage`, 0.3.0 or later); the
tasks are written by a tasks sink (`obsidian.write_task` in the pipeline).

| File | What it is |
|---|---|
| `plugin.yaml` | the manifest: provides the skill and the pipeline; the intent `task.create` |
| `pipelines/mail-triage.yaml` | `collect` (gmail.triage, your rules) then `llm` then `act` |
| `skills/mail-triage/triage-mail/` | the skill: prompt, output schema, input view, 13 golden cases |

## Install and configure

1. Install plugin-gmail and the tasks sink, and approve their permissions as usual.
2. `horae plugin add pack-mail-triage` copies the pipeline to `pipelines/pack-mail-triage/`.
3. **Edit your copy** before the first run: `state_path` (a file in a directory granted for
   writing to plugin-gmail), your `rules`, and `account_index`. An edited file needs its
   approval again (the lock pins each pipeline's SHA-256).
4. `link_prefix` of the tasks entry must cover `https://mail.google.com/mail/u/<account_index>/`.
5. A model alias named `default` in `horae.yaml` (or change `model:`).

If you already copied plugin-gmail's `examples/triage-mail.pipeline.yaml`, keep it: it keeps
working with plugin-gmail's own `gmail/triage-mail` skill. The pack's names differ on purpose
(`mail-triage`, `mail-triage/triage-mail`) so that both can exist.

## Evals

`horae eval mail-triage/triage-mail --plugin-dir .` runs the 13 golden cases offline. The
recorded answers are hand-written (`claude-fixture-1`); their wording is for the maintainer to
judge (see plugin-gmail's release evals).

## License

Apache-2.0
