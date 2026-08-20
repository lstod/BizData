# This configuration runs on LOCAL state, deliberately.
#
# It is the configuration that creates the S3 bucket every other BizData
# configuration uses as its remote backend, so at the moment it first runs
# there is nowhere remote for its own state to live. Everything else in this
# repo uses the S3 backend defined in infra/main/.

terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.61"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project = "bizdata"
    }
  }
}
