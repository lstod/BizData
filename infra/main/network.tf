# ---------------------------------------------------------------------------
# The only VPC in this design, and it exists solely because Aurora insists.
#
# Be precise about this when writing it up, because it is the kind of claim an
# interviewer will probe: the Aurora cluster is in a VPC, because an Aurora
# cluster is always in a VPC. What this architecture avoids is putting *Lambda*
# in one. The Lambda reaches the database over the RDS Data API, which is an
# HTTPS endpoint reached with IAM, so the function needs no subnet, no security
# group, no elastic network interface and no RDS Proxy.
#
# What that buys, in the order it matters:
#
#   - No NAT gateway. A Lambda inside a VPC has no route to the internet without
#     one, and a NAT gateway is about $32/month billed hourly whether or not
#     anything flows through it. It is the single most common way a demo account
#     goes from $2 to $40, and the budget alarm from step 0 would catch it a day
#     late. There is no NAT gateway here and nothing in this file can create one.
#   - No cold-start ENI attachment.
#   - Three or four hours of subnet and security group work that step 0's
#     decision record explicitly bought its way out of.
#
# A dedicated VPC rather than the account's default one. The default VPC would
# work and would be fewer lines, but it is a resource this repository did not
# create and cannot promise the shape of — it can be deleted, and in a hardened
# account often has been. "Every resource is in version control" was step 0's
# goal, and reading a default VPC out of a data source is not that.
# ---------------------------------------------------------------------------

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "bizdata"
  }
}

# Two subnets in two availability zones, because a DB subnet group requires at
# least two AZs even for a single-instance cluster.
#
# These are private in the only sense that matters: there is no internet gateway
# in this VPC at all, so there is no route to attach and nothing here is
# reachable from outside. That is a stronger statement than "the route table has
# no 0.0.0.0/0 entry", and it costs nothing.
resource "aws_subnet" "db" {
  count = 2

  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = {
    Name = "bizdata-db-${count.index + 1}"
  }
}

resource "aws_db_subnet_group" "main" {
  name        = "bizdata"
  description = "Aurora Serverless v2, reached only over the Data API."
  subnet_ids  = aws_subnet.db[*].id
}

# A security group with no rules whatsoever, and that is the correct
# configuration rather than an unfinished one.
#
# Nothing connects to this cluster over the network. The Data API is a
# regional AWS endpoint that reaches the cluster internally, so there is no
# client to allow in: no ingress rule, and no egress rule either. Terraform does
# not add the implicit allow-all egress rule that the console does, so what is
# declared here is what exists.
resource "aws_security_group" "aurora" {
  name        = "bizdata-aurora"
  description = "No ingress and no egress. Aurora is reached over the Data API, not the network."
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "bizdata-aurora"
  }
}
