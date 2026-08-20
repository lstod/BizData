terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.61"
    }
  }

  # Partial backend configuration. The concrete values live in backend.hcl,
  # which is gitignored because the bucket name embeds the account id and
  # this repo is public. Initialise with:
  #
  #   terraform init -backend-config=backend.hcl
  #
  # use_lockfile is native S3 locking, generally available since Terraform
  # 1.11. It writes a <key>.tflock object beside the state for the duration
  # of an apply. There is no DynamoDB table here on purpose: the
  # dynamodb_table argument is deprecated and slated for removal.
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project = "bizdata"
    }
  }
}
