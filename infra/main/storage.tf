# ---------------------------------------------------------------------------
# The runs/ archive, built now because the Terraform is open now.
#
# Nothing in step 5 writes to this bucket. Step 9's publish_pack does, minting a
# presigned PUT scoped to a single key under runs/<run_id>/ so that the agent
# never holds an AWS credential. Creating it here costs one apply and saves
# opening the infrastructure again mid-way through the output work.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "runs" {
  bucket = "bizdata-runs-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "runs" {
  bucket = aws_s3_bucket.runs.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "runs" {
  bucket = aws_s3_bucket.runs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }

    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "runs" {
  bucket = aws_s3_bucket.runs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "runs" {
  bucket = aws_s3_bucket.runs.id

  rule {
    id     = "glacier-at-ninety-days"
    status = "Enabled"

    filter {}

    transition {
      days          = 90
      storage_class = "GLACIER"
    }

    # Versioning is on, so old versions accumulate silently otherwise. A pack
    # republished for the same run is the case this covers.
    noncurrent_version_transition {
      noncurrent_days = 90
      storage_class   = "GLACIER"
    }
  }
}
