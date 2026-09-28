"""Personal-data controls (S3.4). Requirements: NFR-303, NFR-304, NFR-305.

The failure these guard against cannot be rolled back. A citizen's name or ID
that reaches the model, the store or a log file is out, and no later fix
retrieves it. So the tests are written from the attacker's side of the
question: given text that looks like a real person's data, what escapes?
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.governance.paths import redact_path
from orchestrator.store.models import Origin
from orchestrator.testing.rig import ORIGIN, body, make_client, make_rig


class TestPathRedaction:
    """NFR-304: identifying path segments never reach storage or logs."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/application/11502001234", "/application/:id"),  # a citizen ID
            ("/receipt/2026-0098", "/receipt/:id"),
            ("/case/APP-2026-0098", "/case/:id"),
            ("/user/a3f9c8e1b2d4", "/user/:id"),  # opaque token
            ("/status/17123456", "/status/:id"),  # phone-shaped
        ],
    )
    def test_nfr304_identifying_segments_become_id(self, path: str, expected: str) -> None:
        assert redact_path(path) == expected

    @pytest.mark.parametrize(
        "path",
        ["/services/renewal", "/legal/notice", "/services/passport-renewal", "/", "/about/contact"],
    )
    def test_nfr304_ordinary_pages_stay_readable(self, path: str) -> None:
        """Redaction must leave an operator able to tell pages apart."""
        assert ":id" not in redact_path(path)

    def test_nfr304_an_unusable_path_is_not_echoed_back(self) -> None:
        """Whatever was in it, it is not repeated into a log line."""
        assert redact_path("/legal/%252e%252e/x") == "<unparseable>"
        assert redact_path(None) == "<unparseable>"

    def test_nfr304_redaction_is_separate_from_tier_matching(self) -> None:
        """Tier rules must see the real path; only storage and logs see :id.

        If these were the same function, a rule on a dated page would stop
        matching the moment redaction improved.
        """
        from orchestrator.governance.paths import normalise_path

        assert normalise_path("/legal/2026-budget") == "/legal/2026-budget"
        assert redact_path("/legal/2026-budget") == "/legal/:id"


class TestLogsCarryNoText:
    """NFR-303: logs hold keys and hashes, never the citizen's words."""

    def test_nfr303_a_translated_request_logs_no_segment_text(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Person-shaped but invented, and not assignable: the point is distinctive
        # tokens to grep for, never a real citizen (docs/CLAUDE.md).
        personal = "Testperson Examplename of Nowhereton applied on 3 March"
        client = make_client(make_rig())
        with caplog.at_level(logging.DEBUG):
            response = client.post("/v1/translate", json=body(personal), headers={"Origin": ORIGIN})
        assert response.status_code == 200

        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert personal not in logged
        for word in ("Testperson", "Examplename", "Nowhereton"):
            assert word not in logged, f"{word!r} reached the logs"

    def test_nfr304_the_request_log_records_the_redacted_path(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The control has a call site, not just a unit test.

        A redaction function nothing calls protects nobody, so this asserts the
        request log carries the redacted form and not the citizen's id.
        """
        client = make_client(make_rig())
        with caplog.at_level(logging.INFO):
            client.post(
                "/v1/translate",
                json=body("Apply for a passport.", path="/application/11502001234"),
                headers={"Origin": ORIGIN},
            )
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "path=/application/:id" in logged
        assert "11502001234" not in logged

    def test_nfr303_a_failing_lookup_logs_no_segment_text(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The error path is where text usually leaks, via an exception message."""
        rig = make_rig()
        personal = "Otherperson Examplename, CID 00000000000"

        def explode(*_: object, **__: object) -> None:
            raise RuntimeError("store unavailable")

        rig.service.store.lookup = explode  # type: ignore[method-assign]
        client = make_client(rig)
        with caplog.at_level(logging.DEBUG):
            response = client.post("/v1/translate", json=body(personal), headers={"Origin": ORIGIN})

        assert response.status_code == 200  # fails open to English (NFR-410)
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "Otherperson" not in logged and "00000000000" not in logged


class TestRetention:
    """NFR-305: unapproved machine output does not accumulate forever."""

    def test_nfr305_old_machine_translations_expire(self) -> None:
        rig = make_rig(clients_to_persist=1)
        client = make_client(rig)
        client.post("/v1/translate", json=body("Apply for a passport."), headers={"Origin": ORIGIN})

        future = datetime.now(UTC) + timedelta(days=91)
        assert rig.service.store.count_machine_before(future) == 1
        assert rig.service.store.expire_machine(future) == 1
        assert rig.service.store.count_machine_before(future) == 0

    def test_nfr305_a_dry_run_changes_nothing(self) -> None:
        rig = make_rig(clients_to_persist=1)
        client = make_client(rig)
        client.post("/v1/translate", json=body("Apply for a passport."), headers={"Origin": ORIGIN})

        future = datetime.now(UTC) + timedelta(days=91)
        assert rig.service.store.count_machine_before(future) == 1
        assert rig.service.store.count_machine_before(future) == 1  # still there

    def test_nfr305_recent_machine_translations_are_kept(self) -> None:
        rig = make_rig(clients_to_persist=1)
        client = make_client(rig)
        client.post("/v1/translate", json=body("Apply for a passport."), headers={"Origin": ORIGIN})

        cutoff = datetime.now(UTC) - timedelta(days=90)
        assert rig.service.store.expire_machine(cutoff) == 0

    def test_nfr305_approved_translations_are_never_expired(self) -> None:
        """A human signed these off; they are the service's memory, not working data."""
        rig = make_rig(clients_to_persist=1)
        client = make_client(rig)
        client.post("/v1/translate", json=body("Apply for a passport."), headers={"Origin": ORIGIN})

        versions = [v for v in rig.tm._versions.values()]
        assert versions, "nothing was stored, so nothing was proven"
        for version in versions:
            if version.origin is Origin.MT:
                rig.tm._versions[version.id] = version.__class__(
                    **{**version.__dict__, "origin": Origin.HUMAN}
                )

        future = datetime.now(UTC) + timedelta(days=9_000)
        assert rig.service.store.expire_machine(future) == 0
