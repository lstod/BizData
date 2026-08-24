"""Four read tools, and one action.

Four, and the number is a design constraint rather than an accident: it is the number that
keeps the analysis legible, and every one of them aggregates server side so the model never
sees a raw table. ``publish_pack`` arrived at step 9 as a fifth tool and the cap still
holds, because the cap is on the *read* surface and publishing is an action — the only write
in the system, and the one place a confirmation step belongs. Keeping it visibly separate
from the read surface is better design than folding it into an existing signature to
preserve a number.

Each module here owns one tool: its Pydantic response models, the query parameters it
builds, and nothing else. The SQL lives in db/sql/ and the registration in server/app.py.
"""

from server.tools import (
    get_engagement_burn,
    get_financials,
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

WRITE_TOOLS = (publish_pack.publish_pack,)

TOOLS = (*READ_TOOLS, *WRITE_TOOLS)

__all__ = [
    "TOOLS",
    "READ_TOOLS",
    "WRITE_TOOLS",
    "list_engagements",
    "get_engagement_burn",
    "get_time_summary",
    "get_financials",
    "publish_pack",
]
