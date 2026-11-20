terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region  = "us-east-1"
  profile = "aldi-antigravity"
}

variable "image_uri" {
  type = string
}

variable "anthropic_api_key" {
  type      = string
  sensitive = true
}

locals {
  account_id  = "575554646331"
  bucket_name = "aldi-price-forensics-575554646331"
}

# ---------------- S3 ----------------
resource "aws_s3_bucket" "data" {
  bucket        = local.bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    id     = "expire-raw-videos"
    status = "Enabled"
    filter {
      prefix = "raw-videos/"
    }
    expiration {
      days = 2
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  cors_rule {
    allowed_methods = ["GET", "PUT", "POST", "HEAD"]
    allowed_origins = ["*"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

# ---------------- SQS ----------------
resource "aws_sqs_queue" "vision" {
  name                       = "aldi-vision-queue"
  visibility_timeout_seconds = 300
}

# ---------------- IAM (Lambda) ----------------
resource "aws_iam_role" "lambda" {
  name = "aldi-price-forensics-lambda-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "aldi-price-forensics-policy"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        Resource = ["arn:aws:s3:::aldi-*", "arn:aws:s3:::aldi-*/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
        Resource = "arn:aws:sqs:us-east-1:575554646331:aldi-*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["arn:aws:logs:us-east-1:575554646331:log-group:/aws/lambda/aldi-*", "arn:aws:logs:us-east-1:575554646331:log-group:/aws/lambda/aldi-*:*"]
      },
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = "arn:aws:lambda:us-east-1:575554646331:function:aldi-*"
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:Converse"]
        Resource = "*"
      }
    ]
  })
}

# ---------------- Lambda ----------------
resource "aws_lambda_function" "frame_extractor" {
  function_name = "aldi-frame-extractor"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = var.image_uri
  memory_size   = 2048
  timeout       = 300
  architectures = ["x86_64"]
  ephemeral_storage {
    size = 4096
  }
  image_config {
    command = ["frame_extractor.handler"]
  }
  environment {
    variables = {
      VISION_QUEUE_URL = aws_sqs_queue.vision.url
    }
  }
  depends_on = [aws_iam_role_policy.lambda]
}

resource "aws_lambda_function" "vision_worker" {
  function_name = "aldi-vision-worker"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = var.image_uri
  memory_size   = 512
  timeout       = 180
  architectures = ["x86_64"]
  image_config {
    command = ["vision_worker.handler"]
  }
  environment {
    variables = {
      MATCHER_FUNCTION_NAME = "aldi-catalog-matcher"
      ANTHROPIC_API_KEY     = var.anthropic_api_key
    }
  }
  depends_on = [aws_iam_role_policy.lambda]
}

resource "aws_lambda_function" "catalog_matcher" {
  function_name = "aldi-catalog-matcher"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = var.image_uri
  memory_size   = 512
  timeout       = 180
  architectures = ["x86_64"]
  image_config {
    command = ["catalog_matcher.handler"]
  }
  depends_on = [aws_iam_role_policy.lambda]
}

# ---------------- Triggers ----------------
resource "aws_lambda_permission" "s3_invoke" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.frame_extractor.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.data.arn
}

resource "aws_s3_bucket_notification" "raw_videos" {
  bucket = aws_s3_bucket.data.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.frame_extractor.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "raw-videos/"
  }

  depends_on = [aws_lambda_permission.s3_invoke]
}

resource "aws_lambda_event_source_mapping" "vision" {
  event_source_arn = aws_sqs_queue.vision.arn
  function_name    = aws_lambda_function.vision_worker.arn
  batch_size       = 1
}

# ---------------- Cloudflare signer user ----------------
resource "aws_iam_user" "cf" {
  name = "cloudflare-grocery-signer"
}

resource "aws_iam_user_policy" "cf" {
  name = "cloudflare-grocery-signer-policy"
  user = aws_iam_user.cf.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:PutObject", "s3:GetObject"]
      Resource = "arn:aws:s3:::aldi-price-forensics-575554646331/*"
    }]
  })
}

resource "aws_iam_access_key" "cf" {
  user = aws_iam_user.cf.name
}

# ---------------- Outputs ----------------
output "s3_bucket_name" {
  value = aws_s3_bucket.data.bucket
}

output "cf_access_key_id" {
  value = aws_iam_access_key.cf.id
}

output "cf_secret_access_key" {
  value     = aws_iam_access_key.cf.secret
  sensitive = true
}
