# ---------------------------------------------------------------------------
# The mcp_readonly credential.
#
# This is where the read-only claim stops being a promise and becomes a
# permission. There are two secrets against this cluster and they are not
# interchangeable:
#
#   - the master secret, created and rotated by RDS, which the seed script on
#     the laptop uses to create the schema and load 40,000 rows
#   - this one, mcp_readonly, which is the only credential the Lambda's IAM
#     policy lets it read
#
# The deployed server therefore cannot write, and not because the code declines
# to. The Postgres role has SELECT and nothing else, granted in
# db/seeds/mcp_readonly.sql, and step 5's evidence is an INSERT attempted with
# this secret coming back as a permission error.
#
# The password is generated here rather than by RDS because RDS-managed secrets
# belong to a database user RDS itself creates, and mcp_readonly is created by
# our own SQL after the cluster exists.
# ---------------------------------------------------------------------------

resource "random_password" "mcp_readonly" {
  length = 40

  # Postgres accepts far more than this, but a password that survives being
  # pasted into a psql URL, a shell and a JSON document without escaping is one
  # fewer thing to debug at 2am.
  special          = true
  override_special = "-_=+"
}

resource "aws_secretsmanager_secret" "mcp_readonly" {
  name        = "bizdata/mcp-readonly"
  description = "SELECT-only Postgres role for the deployed MCP server. Read by the Lambda over the Data API."

  # No recovery window. The default is a seven-day scheduled deletion, which
  # means a destroy-and-reapply cycle inside that week fails on a name that is
  # gone but not gone. This cluster is expected to be rebuilt during the build.
  recovery_window_in_days = 0
}

# The Data API requires the secret to hold at least username and password, in
# this shape. The remaining fields are what RDS's own managed secrets carry, and
# matching that shape means any tool that understands one understands the other.
resource "aws_secretsmanager_secret_version" "mcp_readonly" {
  secret_id = aws_secretsmanager_secret.mcp_readonly.id

  secret_string = jsonencode({
    username            = "mcp_readonly"
    password            = random_password.mcp_readonly.result
    engine              = "postgres"
    host                = aws_rds_cluster.main.endpoint
    port                = aws_rds_cluster.main.port
    dbname              = var.db_name
    dbClusterIdentifier = aws_rds_cluster.main.cluster_identifier
  })
}
