# ---------------------------------------------------------------------------
# Aurora Serverless v2 PostgreSQL, scale-to-zero, with the Data API on.
#
# The two settings that make this design work are min_capacity = 0 and
# enable_http_endpoint. Together they mean an idle cluster costs storage only
# — about a dollar a month at this size — and is reached without a VPC.
#
# There is no custom parameter group here, and the reason is a small finding
# rather than an omission.
#
# Step 3 measured list_engagements at 2,599ms against 211ms with `set jit = off`
# and concluded that step 5 would need `jit` as an Aurora cluster parameter,
# since the Data API gives every call its own session and a SET does not survive
# to the next statement. That turned out to be impossible and unnecessary, in
# that order. Impossible because `jit` is not a modifiable parameter in the
# aurora-postgresql16 family at all — not in the cluster group (128 parameters)
# and not in the instance group (324), checked rather than assumed. Unnecessary
# because `show jit` on this cluster returns off already: Aurora PostgreSQL
# 16.10 ships JIT disabled where community Postgres 16 ships it enabled, which
# is precisely why there is no parameter exposed to change it.
#
# So step 3's problem does not exist on this engine. db/seeds/mcp_readonly.sql
# still pins it with `alter role mcp_readonly set jit = off`, a role-level GUC
# applied at session start, because an engine default is a fact about a version
# rather than a promise about the next one. Same for `timezone`, whose cluster
# default in this family is already UTC.
# ---------------------------------------------------------------------------

resource "aws_rds_cluster" "main" {
  cluster_identifier = "bizdata"
  engine             = "aurora-postgresql"
  engine_version     = var.engine_version

  # "provisioned" reads like the opposite of serverless and is not. Serverless
  # v2 is a capacity type on a provisioned-mode cluster; engine_mode
  # "serverless" is v1, which is a different and now legacy product.
  engine_mode = "provisioned"

  database_name   = var.db_name
  master_username = "bizdata_admin"

  # RDS generates the master password and owns the secret, so it is never in
  # Terraform state, never in a variable, and never in this repository. The seed
  # script reads it from Secrets Manager by ARN.
  manage_master_user_password = true

  # The Data API. Without this the whole no-VPC design collapses.
  enable_http_endpoint = true

  storage_encrypted    = true
  db_subnet_group_name = aws_db_subnet_group.main.name

  vpc_security_group_ids = [aws_security_group.aurora.id]

  # One day of backups because every row in here is synthetic and regenerating
  # the whole database is `python scripts/seed.py`, about a second of work. Zero
  # is not permitted for Aurora.
  backup_retention_period = 1
  skip_final_snapshot     = true

  # This cluster gets torn down and rebuilt during a two-week build, and a
  # deletion-protected cluster turns that into a console visit.
  deletion_protection = false

  apply_immediately = true

  serverlessv2_scaling_configuration {
    min_capacity = 0
    max_capacity = var.max_capacity

    # Required when min_capacity is 0, and rejected when it is not.
    seconds_until_auto_pause = var.seconds_until_auto_pause
  }
}

resource "aws_rds_cluster_instance" "main" {
  identifier         = "bizdata-1"
  cluster_identifier = aws_rds_cluster.main.id

  # The instance class that means "take capacity from the cluster's serverless
  # range" rather than a fixed size.
  instance_class = "db.serverless"

  engine         = aws_rds_cluster.main.engine
  engine_version = aws_rds_cluster.main.engine_version

  publicly_accessible = false

  # Performance Insights holds a paused cluster awake in some configurations and
  # is not free past the retention default. Nothing here needs it.
  performance_insights_enabled = false

  apply_immediately = true
}
