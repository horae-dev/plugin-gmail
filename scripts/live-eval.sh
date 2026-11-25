#!/usr/bin/env bash
# The live eval of the release workflow (.github/workflows/release-evals.yml): the golden cases
# whose expected guard result is `pass` are sent to the real model, one `horae eval --live` per
# case, under one spend cap. The `injected-*` cases are left out on purpose: their recorded
# answer is a model that obeyed an injection, which a live model is not expected to reproduce.
#
# Inputs (environment):
#   HORAE_CORE         the pip requirement of the core (`horae @ git+...@<sha>`; not in uv.lock)
#   ANTHROPIC_API_KEY  the model key (read through the instance's `{env: ...}` secret reference)
#   EVAL_MODEL         the Anthropic model id
#   EVAL_PRICE_IN, EVAL_PRICE_OUT  US dollars per million input / output tokens of that model
#   EVAL_MAX_USD       the cap for the whole eval: each case is refused (exit 3) when its worst
#                      case does not fit what is left of it
#   EVAL_OUT           the file the merged JSON report is written to
#   RUNNER_TEMP        scratch directory (the instance and the per-case reports)
#
# Exit: 0 every case passed; 1 the eval could not be run or a case failed.
set -euo pipefail

skill="gmail/triage-mail"
horae=(uv run --with "${HORAE_CORE:?}" python -m horae)
work="${RUNNER_TEMP:?}/live-eval"
instance="${work}/instance"
mkdir -p "${instance}" "${work}/cases"

cat > "${instance}/horae.yaml" <<YAML
schema_version: 1
timezone: UTC
state_dir: state
secrets:
  refs:
    ANTHROPIC_API_KEY: { env: ANTHROPIC_API_KEY }
models:
  default:
    backend: anthropic
    model: ${EVAL_MODEL:?}
    secret: ANTHROPIC_API_KEY
    price: { input_per_mtok: ${EVAL_PRICE_IN:?}, output_per_mtok: ${EVAL_PRICE_OUT:?} }
YAML

# The cases a live model is expected to pass as they are (the offline report knows them).
mapfile -t cases < <(
  "${horae[@]}" eval "${skill}" --plugin-dir . --json | jq -r '.cases[] | select(.guard == "pass") | .case'
)
if [ "${#cases[@]}" -eq 0 ]; then
  echo "no case to evaluate live" >&2
  exit 1
fi

remaining="${EVAL_MAX_USD:?}"
failed=0
for name in "${cases[@]}"; do
  report="${work}/cases/${name}.json"
  echo "::group::${name} (cap left: ${remaining} USD)"
  status=0
  "${horae[@]}" eval "${skill}" -i "${instance}" --plugin-dir . --alias default \
    --case "${name}" --live --yes --max-usd "${remaining}" --json > "${report}" || status=$?
  echo "exit ${status}"
  echo "::endgroup::"
  if [ ! -s "${report}" ] || ! jq -e '.cases' "${report}" > /dev/null 2>&1; then
    echo "${name}: no report (exit ${status}): refused, over the cap, or could not run" >&2
    rm -f "${report}"
    failed=1
    break
  fi
  [ "${status}" -eq 0 ] || failed=1
  remaining="$(jq -n --argjson cap "${remaining}" --slurpfile r "${report}" \
    '[$cap - ($r[0].summary.usage.usd // 0), 0] | max')"
done

# No case ran (refused, over the cap): there is nothing to attach, and the job fails.
if ! compgen -G "${work}/cases/*.json" > /dev/null; then
  echo "no case was evaluated" >&2
  exit 1
fi

# One report for the release: every case, a summary recomputed over them.
jq -s --arg cap "${EVAL_MAX_USD}" --arg model "${EVAL_MODEL}" '
  {
    report: "live-eval",
    model: $model,
    max_usd: ($cap | tonumber),
    cases: [.[].cases[]],
    summary: {
      cases: ([.[].cases[]] | length),
      passed: ([.[].cases[] | select(.status == "pass")] | length),
      failed: ([.[].cases[] | select(.status == "fail")] | length),
      errors: ([.[].cases[] | select(.status != "pass" and .status != "fail")] | length),
      usd: ([.[].summary.usage.usd // 0] | add // 0)
    }
  }' "${work}"/cases/*.json > "${EVAL_OUT:?}"

jq -r '.cases[] | "\(.status)  \(.case)"' "${EVAL_OUT}"
jq -r '.summary | "cases=\(.cases) passed=\(.passed) failed=\(.failed) errors=\(.errors) usd=\(.usd)"' "${EVAL_OUT}"
jq -e '.summary.cases == '"${#cases[@]}"' and .summary.failed == 0 and .summary.errors == 0' "${EVAL_OUT}" > /dev/null || failed=1
exit "${failed}"
