terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" {
  region = var.aws_region
}

resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.name}-${var.environment}-artifacts"
}
resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_vpc" "platform" {
  cidr_block = "10.0.0.0/16"
  enable_dns_hostnames = true
}
resource "aws_db_subnet_group" "platform" {
  name = "${var.name}-${var.environment}"
  subnet_ids = var.private_subnet_ids
}
resource "aws_db_instance" "postgres" {
  identifier = "${var.name}-${var.environment}"
  engine = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class
  allocated_storage = 20
  db_name = "mireye"
  username = "mireye"
  manage_master_user_password = true
  db_subnet_group_name = aws_db_subnet_group.platform.name
  skip_final_snapshot = true
}
resource "aws_elasticache_subnet_group" "platform" {
  name = "${var.name}-${var.environment}"
  subnet_ids = var.private_subnet_ids
}
resource "aws_elasticache_replication_group" "redis" {
  replication_group_id = "${var.name}-${var.environment}"
  description = "Mireye ephemeral cache"
  node_type = "cache.t4g.micro"
  num_cache_clusters = 1
  engine = "redis"
  subnet_group_name = aws_elasticache_subnet_group.platform.name
}
