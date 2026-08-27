variable "aws_region" {
  type = string
}
variable "name" {
  type = string
  default = "mireye"
}
variable "environment" {
  type = string
}
variable "private_subnet_ids" {
  type = list(string)
}
variable "db_instance_class" {
  type = string
  default = "db.t4g.medium"
}
