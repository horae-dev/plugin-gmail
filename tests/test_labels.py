# SPDX-License-Identifier: Apache-2.0
"""Label names: `labels.list` once per run, names resolved to ids, and the ways a name can
fail. Synthetic data only."""

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import FakeGoogle, mail
from horae_gmail import labels as label_lookup
from test_triage import BILL, PROMO, run, setup

RECEIPT = mail(
    "18f0c0000000c001",
    sender="shop@example.org",
    subject="Your order",
    labels=("INBOX", "Label_101"),
)
ACME = mail(
    "18f0c0000000c002",
    sender="boss@example.net",
    subject="Quarterly plan",
    labels=("INBOX", "Label_102"),
)


def labels_calls(google: FakeGoogle) -> int:
    return google.routes().count("labels")


def test_system_labels_need_no_labels_call(tmp_path: Path) -> None:
    google, path = setup(tmp_path, PROMO, BILL)
    assert run(google, path).error is None
    assert labels_calls(google) == 0


def test_a_rule_can_name_a_label_by_name(tmp_path: Path) -> None:
    google, path = setup(tmp_path, RECEIPT)
    rules = [{"name": "receipts", "action": "ignore", "label": ["Receipts"]}]
    result = run(google, path, rules=rules).data
    assert result["decisions"][0]["rule"] == "receipts"


def test_a_name_is_matched_ignoring_case_when_the_exact_case_is_unknown(tmp_path: Path) -> None:
    google, path = setup(tmp_path, RECEIPT)
    rules = [{"name": "receipts", "action": "ignore", "label": ["receipts"]}]
    assert run(google, path, rules=rules).data["decisions"][0]["rule"] == "receipts"


def test_a_rule_can_still_name_a_label_by_id(tmp_path: Path) -> None:
    google, path = setup(tmp_path, RECEIPT)
    rules = [{"name": "receipts", "action": "ignore", "label": ["Label_101"]}]
    assert run(google, path, rules=rules).data["decisions"][0]["rule"] == "receipts"


def test_a_wildcard_is_tried_against_names_too(tmp_path: Path) -> None:
    google, path = setup(tmp_path, ACME)
    rules = [{"name": "clients", "action": "ignore", "label": ["Clients/*"]}]
    assert run(google, path, rules=rules).data["decisions"][0]["rule"] == "clients"


def test_labels_are_listed_once_per_run_however_many_references(tmp_path: Path) -> None:
    google, path = setup(tmp_path, RECEIPT, ACME)
    rules = [
        {"name": "a", "action": "ignore", "label": ["Receipts", "Clients/Acme"]},
        {"name": "b", "action": "ignore", "label": ["Clients/*"]},
    ]
    result = run(google, path, rules=rules, label_id="Receipts")
    assert result.error is None
    assert labels_calls(google) == 1


def test_label_id_accepts_a_name_and_reads_history_of_its_id(tmp_path: Path) -> None:
    google, path = setup(tmp_path, RECEIPT)
    assert run(google, path, label_id="Receipts").error is None
    history = next(e for e in google.seen if e["route"] == "history")
    assert parse_qs(urlsplit(history["url"]).query)["labelId"] == ["Label_101"]


def test_labels_list_is_a_get_of_ids_and_names_only(tmp_path: Path) -> None:
    google, path = setup(tmp_path, RECEIPT)
    run(google, path, label_id="Receipts")
    seen = next(e for e in google.seen if e["route"] == "labels")
    assert urlsplit(str(seen["url"])).path == "/gmail/v1/users/me/labels"
    assert parse_qs(urlsplit(str(seen["url"])).query) == {"fields": ["labels(id,name)"]}


@pytest.mark.parametrize("params", [{"label_id": "Nope"}, {"rules": "missing"}])
def test_a_missing_label_refuses_the_run_and_keeps_the_state(
    tmp_path: Path, params: dict[str, Any]
) -> None:
    google, path = setup(tmp_path, RECEIPT)
    before = path.read_text("utf-8")
    if "rules" in params:
        params = {"rules": [{"name": "x", "action": "ignore", "label": ["Nope"]}]}
    replayed = run(google, path, **params)
    assert replayed.error is not None
    assert replayed.error.startswith("label_not_found: ")
    assert "'Nope'" in replayed.error
    assert "history" not in google.routes()
    assert path.read_text("utf-8") == before


def test_an_ambiguous_name_is_refused_and_lists_the_ids(tmp_path: Path) -> None:
    google, path = setup(tmp_path, RECEIPT)
    google.labels.append({"id": "Label_104", "name": "RECEIPTS"})
    rules = [{"name": "x", "action": "ignore", "label": ["receipts"]}]
    replayed = run(google, path, rules=rules)
    assert replayed.error is not None
    assert replayed.error.startswith("label_ambiguous: ")
    assert "Label_101" in replayed.error
    assert "Label_104" in replayed.error
    assert "history" not in google.routes()


def test_the_exact_case_breaks_a_tie_and_an_id_beats_a_name(tmp_path: Path) -> None:
    known = label_lookup.Labels({"Label_1": "Work", "Label_2": "work", "Work": "Elsewhere"})
    assert known.resolve("Work") == "Work"  # an id first
    known = label_lookup.Labels({"Label_1": "Work", "Label_2": "work"})
    assert known.resolve("work") == "Label_2"  # exact case first
    with pytest.raises(label_lookup.LabelError, match="label_ambiguous"):
        known.resolve("WORK")


def test_a_labels_call_that_fails_fails_the_run_with_a_fixed_code(tmp_path: Path) -> None:
    google = FakeGoogle({"labels": [(403, b'{"error": {"message": "secret detail"}}')]})
    google.mailbox[RECEIPT["id"]] = RECEIPT
    google.history = [{"id": "4001", "messagesAdded": [{"message": {"id": RECEIPT["id"]}}]}]
    path = tmp_path / "gmail-state.json"
    from horae_gmail import state

    state.save(str(path), {"mail": "4000"})
    replayed = run(google, path, label_id="Receipts")
    assert replayed.error == "http_403"


def test_a_malformed_label_entry_is_refused_not_guessed(tmp_path: Path) -> None:
    google = FakeGoogle({"labels": [(200, b'{"labels": ["x"]}')]})
    path = tmp_path / "gmail-state.json"
    from horae_gmail import state

    state.save(str(path), {"mail": "4000"})
    replayed = run(google, path, label_id="Receipts")
    assert replayed.error == "bad_response"


def test_entries_with_a_bad_id_or_name_are_ignored(tmp_path: Path) -> None:
    google, path = setup(tmp_path, RECEIPT)
    google.labels.extend([{"id": "bad id!", "name": "Receipts"}, {"id": "Label_9", "name": ""}])
    assert run(google, path, label_id="Receipts").error is None
