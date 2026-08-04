"""Regressions for deterministic junk-campaign classification and its ledger."""

import sqlite3
from pathlib import Path

from apple_mail_fast_mcp.junk_campaigns import (
    JunkCampaignStore,
    fingerprint_sender,
    looks_generated_local_part,
    message_evidence,
)


def _message(message_id: str, sender: str) -> dict[str, object]:
    return {
        "id": message_id,
        "rfc_message_id": f"rfc-{message_id}@example.test",
        "sender": sender,
    }


def test_junk_regression_requires_generated_identity_across_rotating_domains(
    tmp_path: Path,
) -> None:
    """Regression: ordinary senders must stay outside the permanent-delete campaign set."""
    store = JunkCampaignStore(tmp_path / "junk.sqlite3")
    messages = [
        _message("1", "Alert <alert-198.81370@throwaway-one.example>"),
        _message("2", "Alert <alert-198.81370@throwaway-two.example>"),
        _message("3", "Alert <alert-198.81370@throwaway-three.example>"),
        _message("4", "Support <support@legitimate-one.example>"),
        _message("5", "Support <support@legitimate-two.example>"),
        _message("6", "Support <support@legitimate-three.example>"),
    ]
    store.record_messages(account="mail@example.com", mailbox="Spam", messages=messages)

    assert store.qualified_local_parts(
        account="mail@example.com",
        minimum_domains=3,
        minimum_messages=3,
        observation_window_days=30,
    ) == {"alert-198.81370"}


def test_junk_regression_needs_rfc_id_for_provider_deletion() -> None:
    """Regression: a Mail internal id alone can never authorize provider deletion."""
    assert (
        message_evidence(
            account="mail@example.com",
            mailbox="Spam",
            message={"id": "42", "sender": "147719586@one.example"},
        )
        is None
    )


def test_junk_regression_action_ledger_only_suppresses_completed_deletes(tmp_path: Path) -> None:
    """Regression: observation and failure records must remain eligible for a later delete."""
    store = JunkCampaignStore(tmp_path / "junk.sqlite3")
    [message] = store.record_messages(
        account="mail@example.com",
        mailbox="Spam",
        messages=[_message("1", "147719586@one.example")],
    )

    store.record_action(message, status="observed")
    assert store.was_deleted(message) is False
    store.record_action(message, status="failed", detail="temporary provider failure")
    assert store.was_deleted(message) is False
    store.record_action(message, status="deleted", detail="provider copies: 1")
    assert store.was_deleted(message) is True


def test_junk_regression_flagged_domain_survives_local_unflag(tmp_path: Path) -> None:
    """Regression: clearing a flag must preserve its domain-level deletion rule."""
    store = JunkCampaignStore(tmp_path / "junk.sqlite3")
    flagged = _message("flagged-1", "ordinary@campaign.example")
    flagged["flagged"] = True
    store.record_messages(account="mail@example.com", mailbox="Junk Email", messages=[flagged])

    unflagged = {**flagged, "flagged": False}
    store.record_messages(account="mail@example.com", mailbox="Junk Email", messages=[unflagged])

    assert store.auto_delete_domains() == {"campaign.example"}


def test_junk_regression_flagged_domain_is_global_and_durable(tmp_path: Path) -> None:
    """Regression: one auto-flagged junk domain must apply across every account."""
    database = tmp_path / "junk.sqlite3"
    first_store = JunkCampaignStore(database)
    flagged = _message("flagged-1", "ordinary@campaign.example")
    flagged["flagged"] = True
    first_store.record_messages(account="first@example.com", mailbox="Spam", messages=[flagged])

    reopened_store = JunkCampaignStore(database)

    assert reopened_store.auto_delete_domains() == {"campaign.example"}


def test_junk_regression_provider_health_preserves_transition_state(tmp_path: Path) -> None:
    """Regression: stale credentials must alert once and record later recovery."""
    store = JunkCampaignStore(tmp_path / "junk.sqlite3")

    assert (
        store.record_provider_health(
            account="mail@example.com", healthy=False, detail="authentication required"
        )
        is None
    )
    assert (
        store.record_provider_health(
            account="mail@example.com", healthy=False, detail="authentication required"
        )
        is False
    )
    assert store.record_provider_health(account="mail@example.com", healthy=True) is False
    assert store.record_provider_health(account="mail@example.com", healthy=True) is True


def test_junk_regression_generated_shape_is_conservative() -> None:
    """Regression: human mailbox names stay outside generated-local-part matching."""
    assert looks_generated_local_part("147719586") is True
    assert looks_generated_local_part("alert-198.81370") is True
    assert looks_generated_local_part("support") is False
    assert fingerprint_sender("Person <Support@Example.COM>") == fingerprint_sender(
        "support@example.com"
    )


def test_junk_regression_legacy_database_records_current_observation_time(tmp_path: Path) -> None:
    """Regression: migrated sightings must enter the active observation window."""
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            CREATE TABLE junk_campaign_observation (
                account TEXT NOT NULL,
                mailbox TEXT NOT NULL,
                message_id TEXT NOT NULL,
                local_part TEXT NOT NULL,
                domain TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (account, mailbox, message_id)
            )
            """
        )
        connection.commit()
    finally:
        connection.close()
    store = JunkCampaignStore(database)
    store.record_messages(
        account="mail@example.com",
        mailbox="Spam",
        messages=[
            _message("1", "147719586@one.example"),
            _message("2", "147719586@two.example"),
            _message("3", "147719586@three.example"),
        ],
    )

    assert store.qualified_local_parts(
        account="mail@example.com",
        minimum_domains=3,
        minimum_messages=3,
        observation_window_days=30,
    ) == {"147719586"}


def test_junk_regression_legacy_flagged_rows_seed_auto_delete_domains(tmp_path: Path) -> None:
    """Regression: deployed flagged evidence must populate the permanent registry."""
    database = tmp_path / "legacy.sqlite3"
    store = JunkCampaignStore(database)
    flagged = _message("flagged-1", "ordinary@remember.example")
    flagged["flagged"] = True
    store.record_messages(account="mail@example.com", mailbox="Spam", messages=[flagged])
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE junk_auto_delete_domain")
        connection.commit()
    finally:
        connection.close()

    migrated_store = JunkCampaignStore(database)

    assert migrated_store.auto_delete_domains() == {"remember.example"}
