# Security

The posture of this build, stated as what is enforced, what is not, and what would be first in a
client environment. Where something is a gap it is named as a gap rather than described in a way
that sounds like a control.

Nothing here is a claim about a client's data, because there is no client and no client data. That
is the first section.

## There is no real data, and that is load-bearing rather than convenient

Every client, engagement, person, invoice and time entry in this repository comes out of
`scripts/seed.py`, which composes names from word lists in `scripts/generator.py`. No real dataset
was ever loaded, so there is nothing to leak and no scrubbing step that could have been skipped.
The repository has been public since day one, which is what turns that from a preference into a
constraint the build had to hold.

**What tokenisation would look like, since a real deployment needs an answer.** The tools return
client and person names because a delivery review that says "engagement 12" and not "Meridian
Partners" is not usable. In a client environment the seam is `db/views/` and not the tools: names
resolve through a view, and a deployment that must not expose them swaps that view for one
returning a stable surrogate key, with the mapping held in a table the `mcp_readonly` role cannot
select from. The tool contract does not change, the workbook does not change, and the pack becomes
readable only by someone who can also open the mapping. That is a half-day of work and it is not
built here, because building it against synthetic names would prove nothing.

## What is actually enforced

**Read-only is a grant, not a promise the code makes.** The server could be rewritten tomorrow to
issue an `UPDATE` and it would still fail. Three layers, and the innermost is the one that counts:

1. The Lambda's IAM policy allows `GetSecretValue` on exactly one secret. It cannot read the
   cluster's master credential.
2. That secret belongs to `mcp_readonly`, a role holding `SELECT` and nothing else.
3. `scripts/bootstrap_aurora.py` connects as that role and attempts a write **on every run**:

```
insert refused         ERROR: permission denied for table clients; SQLState: 42501
create refused         ERROR: permission denied for schema public; Position: 14; SQLState: 42501
```

Generating that evidence as part of the bootstrap rather than going looking for it afterwards means
a future change that widens the grant fails the bootstrap instead of passing quietly.

`publish_pack` is the one write path in the build, and it writes to an S3 archive rather than to the
database. It reads no business data.

**The endpoint requires a Cognito access token, and the checks are specific.** `server/auth.py` is
about sixty lines because the MCP v2 SDK is already a resource server; what it adds is the claim
validation, and each check closes a hole that is invisible when it is missing:

| Check | What it stops |
| --- | --- |
| Signature against the pool JWKS | Without it every other claim is attacker-controlled |
| `iss` equals the configured issuer | A signature check alone accepts a correctly-signed token from **somebody else's** Cognito pool |
| `token_use == "access"` | Cognito signs ID tokens and access tokens with the same keys from the same pool. An ID token is not an API authorization |
| `client_id` in the accepted set | A correctly-signed token minted for a different app in the same pool is still refused |
| `exp`, `nbf` | Enforced by PyJWT during decode |

Scope enforcement (`bizdata/read`) runs through the SDK's own path, so an insufficient-scope refusal
comes back with the right status and the right `WWW-Authenticate` header rather than a hand-rolled
403.

`scripts/check_auth.py` asserts all of this against the live endpoint, 21 assertions, using a
second Cognito app client that can do `client_credentials` and nothing else. The connector's client
can only do `authorization_code`, so the test path and the demo path cannot be confused for one
another.

**Credentials are not in the repository, and that is asserted rather than trusted.**
`scripts/check_plugin.py` includes four assertions that no credential ships in the published plugin
manifest, one of which compares against the real client secret fetched from Terraform. The
`.mcp.json` manifest deliberately has no field for a client secret — secrets belong in the OS
keychain, not in a file people commit — which is why installing the connector takes one credential
supplied out of band.

**Network.** The Lambda is not in a VPC. It reaches Aurora over the RDS Data API, an HTTPS endpoint
authorised with IAM, so there is no subnet, no ENI, no security group and no NAT gateway. Aurora is
in a VPC because an Aurora cluster always is; that VPC has **no internet gateway at all**, and the
cluster's security group has no ingress rule and no egress rule. That is the correct configuration
rather than an unfinished one, because nothing connects to it over the network.

## "Disconnect" does not revoke

This one is measured, not read, and it is the finding most worth a security reviewer's attention
because it is a gap between what a control is *named* and what it *does*.

Disconnecting the connector in Cowork and reconnecting from the plugin alone **reconnected with
nothing entered**, and also **retained the "always allow" tool permissions** set before the
disconnect. The second half is what makes the first half readable: per-tool permissions are
connector-scoped user state, so a Disconnect that preserves them preserved the stored OAuth
credential too.

The protocol closes it independently of any reading of the UI. Both remaining possibilities —
a fresh authorization or a token refresh — require client authentication, measured against Cognito
with everything else held constant:

```
refresh, no secret         400  {"error":"invalid_client","error_description":"invalid_client_secret"}
refresh, secret via Basic  400  {"error":"invalid_grant"}
```

Access tokens last 60 minutes, so a connector still working an hour later is one that can mint new
tokens, and minting requires the secret whichever grant is used. Cowork holds it, and held it
through the Disconnect.

**So a user who disconnects a connector intending to withdraw its access has not withdrawn it, and
nothing in the interface says so.** On this build, revocation means rotating the Cognito app client
— a Terraform change, not a UI action. Anyone treating Disconnect as an off-boarding step should
know that it is not one. Evidence: `docs/evidence/step-10-cowork-run.txt` §2a.

## What is not enforced, worst first

**Per-user data scoping does not exist, and this is the real gap.** There is one Cognito app client
and one `mcp_readonly` database role, so every authenticated caller sees the entire portfolio.
There is no notion of "this partner's engagements" anywhere in the stack. For a services firm that
is exactly wrong — engagement financials are among the more closely held numbers in the business,
and a shared service account returning everything to everyone is the most common real-world flaw in
builds shaped like this one.

The remedy is not speculative, because the surface for it already exists on both sides. Cowork's
connector form offers **"Individual sign-in — each member signs in to connect"**, which is the
product's own answer. Server-side it means carrying the authenticated subject into the query layer:
a `person_id` claim on the token, a scoping predicate applied in `db/sql/` rather than in the tool
handlers, and Postgres row-level security underneath so a missed predicate fails closed instead of
returning the portfolio. The reason it is not built here is that scoping synthetic data to
synthetic users would demonstrate the mechanism and prove nothing about the hard part, which is
deciding who may see what.

**Audit logging is half built, and the missing half is the same gap.** Every tool call already
writes one JSON line to CloudWatch with the run id, the tool, the arguments, the row counts, the
latency and the scoring model version, and every HTTP request writes another with the protocol
version and user agent. What no line records is **who** — `server/reqlog.py` logs `authenticated`
as a bare boolean, deliberately, because the alternative is putting a credential in a log. With no
per-user identity there is nothing else to record. So "the client says this number is wrong" is
answerable today and "who ran this and what were they entitled to see" is not, and both are fixed
by the same piece of work.

**Retention and residency are set but not designed.** Both CloudWatch log groups are at 30-day
retention and the archive bucket carries a lifecycle rule; neither has been reconciled against any
real retention policy, and everything sits in `us-west-2`. In a client environment both are
questions with answers before the first deployment, not after.

**There is no rate limiting beyond the API Gateway throttle** (20 burst / 10 sustained), no WAF,
and no alerting on authentication failures. The throttle is a cost control that happens to be a
crude availability control, and calling it a security measure would be overstating it.

## The egress reality

Worth stating plainly because it is the first thing a client's security team will raise, and
because the answer is not "it's fine."

**Anthropic's cloud is the caller.** The Skills run in a sandbox operated by Anthropic, and the tool
calls originate from Anthropic's infrastructure and arrive at this endpoint over the public
internet. That is not an incidental deployment detail — it is why a laptop cannot be a connector at
all, which this build established the hard way at step 4. A client engagement therefore starts with
an allowlisting conversation rather than ending with one: which egress is permitted, what the
endpoint's exposure is, and whether the data may leave the client's network boundary in the first
place.

For an organisation that answers no to the last one, the honest response is that this architecture
does not fit and a different shape does — the model inside their boundary, or the aggregation layer
inside it with only aggregates crossing. What does help is that this design already returns
aggregates rather than rows: on a full run against the demo period, 40,000 time entries stay in the
database and **113 aggregated rows** cross the boundary — 18 engagement rows, 75 weekly summary
rows, and detail for the 10 engagements a rule selected. That reduces the exposure and does not
eliminate it, and the difference between those two claims is the conversation.

The sandbox's outbound access is also narrower than it looks. It can make an HTTPS `PUT` to a
presigned S3 URL, which is how the archive works, and it **cannot** put a binary file into Google
Drive, whose surface takes content inline as text. Distribution from the archive is currently
manual for that reason.

## What would be first in a client environment

In order, and the order is the argument:

1. **Per-user scoping**, with row-level security underneath so a missed predicate fails closed.
   Everything else is smaller than this.
2. **Attribution in the audit log**, which falls out of (1) and turns the existing tool-call log
   into something an auditor can use.
3. **Tokenisation of names at the view layer**, if the client's classification requires it.
4. **A network posture agreed in writing** before anything is deployed — allowlisting, egress, and
   where the data is permitted to be.
5. **Alerting on authentication failures**, and a documented rotation procedure for the Cognito app
   client, since rotation is currently the only real revocation path.

Secrets are already in Secrets Manager (`bizdata/mcp-readonly`, plus the RDS-managed master), so
that is not on this list. Neither are VPC endpoints, which would matter if the Lambda were in a VPC
and it is not — if a client requires private connectivity, the Lambda moves into the VPC and gains
endpoints for Secrets Manager and the Data API, and that is a networking change with a NAT gateway
or endpoint bill attached rather than a code change.

## Reporting something

This is a portfolio build with synthetic data and no users. If you find something wrong with it,
open an issue on the repository — there is nothing here that warrants a private disclosure channel,
and saying so is more honest than publishing a security address that goes to a personal inbox.
