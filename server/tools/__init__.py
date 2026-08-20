"""The four read tools.

Four, and the number is a design constraint rather than an accident: it is the number that
keeps the analysis legible, and every one of them aggregates server side so the model never
sees a raw table. ``publish_pack`` arrives at step 9 as a fifth tool and the cap still
holds, because the cap is on the *read* surface and publishing is an action.

Each module here owns one tool: its Pydantic response models, the query parameters it
builds, and nothing else. The SQL lives in db/sql/ and the registration in server/app.py.
"""

from server.tools import get_engagement_burn, get_financials, get_time_summary, list_engagements

TOOLS = (
    list_engagements.list_engagements,
    get_engagement_burn.get_engagement_burn,
    get_time_summary.get_time_summary,
    get_financials.get_financials,
)

__all__ = ["TOOLS", "list_engagements", "get_engagement_burn", "get_time_summary", "get_financials"]
