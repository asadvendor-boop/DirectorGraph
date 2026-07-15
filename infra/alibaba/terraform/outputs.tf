output "ecs_public_ip" {
  value = alicloud_instance.app.public_ip
}

output "web_url" {
  value = "http://${alicloud_instance.app.public_ip}:3000"
}

output "api_health_url" {
  value = "http://${alicloud_instance.app.public_ip}:8000/api/health"
}

output "oss_bucket" {
  value = alicloud_oss_bucket.media.bucket
}
