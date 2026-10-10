"""Contract validation tests."""

from __future__ import annotations

import pytest

from memory_agent.contracts.events import (
    CONVERSATION_EVENT_SCHEMA_VERSION,
    ConversationEvent,
    SourceLocator,
    SourceRef,
    event_from_dict,
    event_to_dict,
)
from memory_agent.contracts.batches import (
    BATCH_SCHEMA_VERSION,
    BatchMember,
    BatchSpec,
    BatchPolicy,
)
from memory_agent.contracts.candidates import (
    Candidate,
    CandidateSet,
    EvidenceRef,
    Scope,
)
from memory_agent.contracts.memory import (
    MEMORY_SCHEMA_VERSION,
    MemoryAtom,
)
from memory_agent.contracts.commands import (
    COMMAND_API_VERSION,
    CommandResponse,
    ResponseError,
    RequestEnvelope,
)
from memory_agent.contracts.query import (
    QUERY_SCHEMA_VERSION,
    RecallResult,
    RecallItem,
    QueryTrace,
    QueryVariant,
)


def _make_event(**overrides):
    defaults = dict(
        schema_version=CONVERSATION_EVENT_SCHEMA_VERSION,
        event_id="evt_x",
        logical_message_id="m1",
        revision_hash="rh1",
        source=SourceRef(
            connector="claude-code",
            adapter_version="reader-v0.1",
            host_id="local-mac",
            source_file_id="src_x",
            generation=1,
            session_id="s1",
            branch_id="main",
            turn_key="u01",
            project_id="proj_test",
            locator=SourceLocator(byte_start=0, byte_end=12, record_ordinal=1),
            source_time="2026-10-08T10:00:00Z",
            discovered_at="2026-10-08T10:00:01Z",
        ),
        role="user",
        kind="message",
        text="hello",
        origin="user_session",
    )
    defaults.update(overrides)
    return ConversationEvent(**defaults)


class TestEventsContract:
    def test_roundtrip(self):
        ev = _make_event()
        d = event_to_dict(ev)
        ev2 = event_from_dict(d)
        assert ev2 == ev

    def test_unknown_field_rejected(self):
        d = event_to_dict(_make_event())
        d["rogue"] = "x"
        with pytest.raises(Exception):
            event_from_dict(d)

    def test_wrong_version_rejected(self):
        d = event_to_dict(_make_event())
        d["schema_version"] = 999
        with pytest.raises(Exception):
            event_from_dict(d)

    def test_canonical_event_key_is_stable(self):
        ev = _make_event()
        k1 = ev.canonical_event_key()
        ev2 = _make_event()
        k2 = ev2.canonical_event_key()
        assert k1 == k2


class TestBatchContract:
    def test_minimal_batch(self):
        spec = BatchSpec(
            schema_version=BATCH_SCHEMA_VERSION,
            batch_id="b1",
            session_id="s1",
            branch_id="main",
            project_id=None,
            connector="claude-code",
            policy=BatchPolicy(),
            input_hash="h",
            members=[BatchMember(unit_id="u1", purpose="target", order=1, epoch=1)],
        )
        assert spec.batch_id == "b1"

    def test_empty_members_rejected(self):
        with pytest.raises(Exception):
            BatchSpec(
                schema_version=BATCH_SCHEMA_VERSION,
                batch_id="b1",
                session_id="s1",
                branch_id="main",
                project_id=None,
                connector="claude-code",
                policy=BatchPolicy(),
                input_hash="h",
                members=[],
            )


class TestCandidateContract:
    def test_minimal_candidate(self):
        cand = Candidate(
            candidate_id="c1",
            kind="requirement",
            statement="Use Python.",
            scope=Scope(level="global"),
            evidence=[EvidenceRef(
                source_event_id="e1",
                source_revision_hash="r1",
                role="user",
                origin="user_session",
                quote="Use Python.",
                span_start=0,
                span_end=12,
            )],
        )
        cs = CandidateSet(
            schema_version=1,
            candidate_set_id="cs1",
            batch_id="b1",
            candidates=[cand],
            prompt_version="v1",
            model="fake-model",
            raw_payload_sha256="abc",
        )
        assert cs.candidates[0].candidate_id == "c1"


class TestCommandContract:
    def test_response_shape(self):
        resp = CommandResponse(
            api_version=COMMAND_API_VERSION,
            request_id="req_x",
            operation="init",
            status="ok",
            result={"ok": True},
        )
        d = resp.model_dump(mode="json")
        assert d["status"] == "ok"
        assert d["api_version"] == COMMAND_API_VERSION


class TestQueryContract:
    def test_recall_result_roundtrip(self):
        r = RecallResult(
            schema_version=QUERY_SCHEMA_VERSION,
            snapshot_id="s1",
            query_trace=QueryTrace(
                trace_id="t",
                fingerprint="f",
                variants=[QueryVariant(variant_id="original", origin="original", text="hi", accepted=True)],
                snapshot_id="s1",
            ),
            items=[RecallItem(memory_id="m", revision=1, statement="s", score=0.5,
                              quote="q", locator={}, scope={})],
        )
        d = r.model_dump(mode="json")
        assert d["items"][0]["memory_id"] == "m"


class TestMemoryContract:
    def test_active_windowing(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc).isoformat()
        mem = MemoryAtom(
            schema_version=MEMORY_SCHEMA_VERSION,
            memory_id="m1",
            revision=1,
            status="active",
            kind="requirement",
            abstraction="principle",
            statement="Use files.",
            subject="proj_test",
            predicate=None,
            value=None,
            modality="must",
            scope=Scope(level="project", project_ids=["proj_test"]),
            primary_domain="dom_x",
            domains=["dom_x"],
            tags=[],
            horizon="persistent",
            asserted_at=now,
            recorded_at=now,
            valid_from=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            valid_until=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            evidence=[],
        )
        assert mem.is_active_at()
        # Before valid_from -> False
        past = datetime.now(timezone.utc) - timedelta(days=2)
        assert not mem.is_active_at(as_of=past)
        # After valid_until -> False
        future = datetime.now(timezone.utc) + timedelta(days=2)
        assert not mem.is_active_at(as_of=future)