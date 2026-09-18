"""Background MT worker (backlog S2.4). Requirements: FR-155, FR-156, FR-143, FR-141, FR-142.

loop:
  sweep expired leases (crashed workers' jobs go back to pending)
  claim up to N jobs, one reserved-quota token each (FR-156: live traffic
    can never starve the worker, and the worker never uses live's share)
  for each job:
    model version changed?  ─► fail (no retry): the key belongs to another model
    translate (masked text only; the worker never sees real values, FR-143)
    upstream error          ─► fail with retry + backoff
    validate on masked text ─► fail (no retry, "validation:<cause>"): a deterministic
                               model would give the same output; enqueue remembers
                               this for 24 h so page views don't re-queue it
    store machine translation (masked) ─► complete
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field

from orchestrator.pipeline.segment import ModelFormat, SegmentError, parse
from orchestrator.pipeline.validate import validate_output
from orchestrator.queue.jobs import VALIDATION_FAILURE_PREFIX, ClaimedJob, WorkQueue
from orchestrator.store.keys import SegmentKeys
from orchestrator.store.lookup import TranslationStore
from orchestrator.testing.mock_nmt import UpstreamError
from orchestrator.upstream.quota import QuotaManager
from orchestrator.upstream.translator import Translator

log = logging.getLogger(__name__)


@dataclass
class WorkerReport:
    swept: int = 0
    claimed: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)


class Worker:
    def __init__(
        self,
        *,
        queue: WorkQueue,
        store: TranslationStore,
        translator: Translator,
        model_format: ModelFormat,
        quota: QuotaManager,
        worker_id: str,
        lease_seconds: float = 60.0,
        call_timeout_seconds: float = 30.0,
    ) -> None:
        self.queue = queue
        self.store = store
        self.translator = translator
        self.fmt = model_format
        self.quota = quota
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.call_timeout = call_timeout_seconds

    async def run_once(self, batch: int = 10) -> WorkerReport:
        report = WorkerReport(swept=self.queue.sweep())
        tokens = 0
        while tokens < batch and self.quota.try_worker():
            tokens += 1
        if tokens == 0:
            report.outcomes["no_worker_quota"] += 1
            return report
        claimed = self.queue.claim(self.worker_id, tokens, self.lease_seconds)
        report.claimed = len(claimed)
        results = await asyncio.gather(*(self._process(c) for c in claimed))
        report.outcomes.update(results)
        return report

    async def run_forever(self, idle_sleep: float = 1.0, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                report = await self.run_once()
            except Exception:  # noqa: BLE001 - keep the worker alive; leases recover lost jobs
                log.exception("worker iteration failed")
                report = WorkerReport()
            if report.claimed == 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=idle_sleep)
                except TimeoutError:
                    pass

    async def _process(self, claimed: ClaimedJob) -> str:
        job = claimed.job
        if job.model_version and job.model_version != self.translator.model_version:
            self.queue.fail(claimed.id, self.worker_id, "model_changed", retry=False)
            return "model_changed"
        try:
            source = parse(job.masked_source, allow_entities=True)
        except SegmentError as err:
            self.queue.fail(
                claimed.id,
                self.worker_id,
                f"{VALIDATION_FAILURE_PREFIX}source:{err.cause}",
                retry=False,
            )
            return "bad_source"
        try:
            raw = await asyncio.wait_for(
                self.translator.translate(self.fmt.encode(source), source), self.call_timeout
            )
        except (UpstreamError, TimeoutError) as err:
            self.queue.fail(
                claimed.id, self.worker_id, f"upstream:{type(err).__name__}", retry=True
            )
            return "upstream_retry"
        try:
            decoded = self.fmt.decode(raw, source)
            validate_output(decoded, source)
        except SegmentError as err:
            cause = getattr(err, "reason", None) or err.cause
            self.queue.fail(
                claimed.id, self.worker_id, f"{VALIDATION_FAILURE_PREFIX}{cause}", retry=False
            )
            return f"invalid:{cause}"
        try:
            self.store.record_machine(
                keys=SegmentKeys(job.segment_key, job.approved_key, job.machine_key, job.gfp),
                masked_source=job.masked_source,
                masked_target=decoded.to_wire(),
                model_version=self.translator.model_version,
                term_ids=job.term_ids,
                tag_integrity=True,
            )
        except Exception as err:  # noqa: BLE001 - storage down: retry later
            self.queue.fail(claimed.id, self.worker_id, f"store:{type(err).__name__}", retry=True)
            return "store_retry"
        self.queue.complete(claimed.id, self.worker_id)
        return "stored"
