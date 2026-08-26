"""Four tools for triage, and two that bracket a run.

Four, and the number is a design constraint rather than an accident: it is the number that
keeps the analysis legible, and every one of them aggregates server side so the model never
sees a raw table. The cap is on the **read** surface — on how many things a caller has to
choose between when deciding what to look at — and it still holds at six tools, because
neither of the other two answers a question about an engagement.

``publish_pack`` arrived at step 9 and closes a run out: the only write in the system, and
the one place a confirmation step belongs. ``get_run_ledger`` arrived at step 12 and opens
one, answering whether the run should happen at all. Keeping both visibly outside the read
surface is better design than folding either into an existing signature to preserve a
number, and it is a more honest description of the shape than calling the ledger a fifth
way to query the data. It queries the archive, and only reaches the database to fingerprint
what the archive should be compared against.

Each module here owns one tool: its Pydantic response models, the query parameters it
builds, and nothing else. The SQL lives in db/sql/ and the registration in server/app.py.
"""

from server.tools import (
    get_engagement_burn,
    get_financials,
    get_run_ledger,
    get_time_summary,
    list_engagements,
    publish_pack,
)

READ_TOOLS = (
    list_engagements.list_engagements,
    get_engagement_burn.get_engagement_burn,
    get_time_summary.get_time_summary,
    get_financials.get_financials,
)

# Ordered the way a run uses them: the ledger decides whether to start, publish_pack ends it.
RUN_TOOLS = (
    get_run_ledger.get_run_ledger,
    publish_pack.publish_pack,
)

# Retained because scripts/check_publish.py and scripts/check_tools.py import it, and
# because "which tool can write" is a question worth being able to ask in one place.
WRITE_TOOLS = (publish_pack.publish_pack,)

TOOLS = (*READ_TOOLS, *RUN_TOOLS)

__all__ = [
    "TOOLS",
    "READ_TOOLS",
    "RUN_TOOLS",
    "WRITE_TOOLS",
    "list_engagements",
    "get_engagement_burn",
    "get_time_summary",
    "get_financials",
    "get_run_ledger",
    "publish_pack",
]
