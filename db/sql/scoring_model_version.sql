-- The live scoring model version, on its own.
--
-- Every other file here reads the version as a scalar subquery alongside the figures it is
-- already computing, because those tools are answering a question about the data and the
-- version is context on the answer. publish_pack answers no question about the data — it
-- writes files — but its ledger entry has to record which model was in force when the pack
-- it is archiving was built, or the archive cannot settle the argument it exists to settle.
--
-- Read on the call rather than cached in the process, for the reason in the docstring of
-- server/tools/common.py: the weights are data precisely so they can change without a
-- redeploy, and a cached version string would quietly outlive the change.
--
-- No placeholders. db/checks/health_v1.sql already asserts that exactly one model is active,
-- and db/seeds/scoring_weights.sql enforces it with a partial unique index, so this returns
-- one row or none.

select version as scoring_model_version
from scoring_model
where is_active;
