"""Built-in job handlers. `system.noop` exercises the whole job contract and is used by the tests.

input_scope keys (all optional):
  items (int, default 3, max 1000)   number of items to process
  fail_items (list[int])             item indexes that fail -> job ends partially_completed
  raise_until_retry (int)            raise while the job's retry_count is below this -> exercises automatic
  retry
  always_raise (bool)                always raise -> exhausts retries -> failed
  sleep_per_item (float, seconds)    slow items, to exercise cancellation / heartbeat
"""

from __future__ import annotations

import time

from anticipa.jobs.context import JobContext
from anticipa.jobs.registry import register_job

MAX_NOOP_ITEMS = 1000


@register_job("system.noop")
def noop(ctx: JobContext) -> None:
    scope = ctx.job.input_scope
    items = max(0, min(int(scope.get("items", 3)), MAX_NOOP_ITEMS))
    fail_items = {int(i) for i in scope.get("fail_items", [])}
    if scope.get("always_raise"):
        raise RuntimeError("noop: always_raise")
    if ctx.job.retry_count < int(scope.get("raise_until_retry", 0)):
        raise RuntimeError("noop: transient failure")
    delay = float(scope.get("sleep_per_item", 0))

    ctx.progress(total=items)
    for i in range(items):
        ctx.raise_if_cancelled()
        item = ctx.item(f"item-{i}")
        if not item.done:  # idempotent: a retry skips what already succeeded
            previously_failed = item.status == "failed"
            if i in fail_items:
                item.fail("noop: planned failure")
                if not previously_failed:
                    ctx.progress(processed=1, failed=1)
            else:
                item.succeed()
                ctx.progress(
                    processed=0 if previously_failed else 1,
                    succeeded=1,
                    failed=-1 if previously_failed else 0,
                )
        ctx.checkpoint({"last_index": i})
        if delay:
            time.sleep(delay)
