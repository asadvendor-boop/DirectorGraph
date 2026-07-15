provider "alicloud" {
  region = var.region
}

data "alicloud_images" "ubuntu" {
  owners      = "system"
  name_regex  = "^ubuntu_22_04.*64"
  most_recent = true
}

resource "alicloud_vpc" "main" {
  vpc_name   = "${var.project_name}-vpc"
  cidr_block = "10.42.0.0/16"
}

resource "alicloud_vswitch" "main" {
  vswitch_name = "${var.project_name}-vswitch"
  vpc_id       = alicloud_vpc.main.id
  cidr_block   = "10.42.1.0/24"
  zone_id      = var.zone
}

resource "alicloud_security_group" "main" {
  name   = "${var.project_name}-sg"
  vpc_id = alicloud_vpc.main.id
}

resource "alicloud_security_group_rule" "ssh" {
  type              = "ingress"
  ip_protocol       = "tcp"
  nic_type          = "intranet"
  policy            = "accept"
  port_range        = "22/22"
  priority          = 1
  security_group_id = alicloud_security_group.main.id
  cidr_ip           = "0.0.0.0/0"
}

resource "alicloud_security_group_rule" "web" {
  type              = "ingress"
  ip_protocol       = "tcp"
  nic_type          = "intranet"
  policy            = "accept"
  port_range        = "3000/3000"
  priority          = 1
  security_group_id = alicloud_security_group.main.id
  cidr_ip           = "0.0.0.0/0"
}

resource "alicloud_security_group_rule" "api" {
  type              = "ingress"
  ip_protocol       = "tcp"
  nic_type          = "intranet"
  policy            = "accept"
  port_range        = "8000/8000"
  priority          = 1
  security_group_id = alicloud_security_group.main.id
  cidr_ip           = "0.0.0.0/0"
}

resource "alicloud_key_pair" "main" {
  key_pair_name = "${var.project_name}-key"
  public_key    = var.ssh_public_key
}

resource "random_id" "suffix" {
  byte_length = 4
}

resource "alicloud_oss_bucket" "media" {
  bucket        = "${var.project_name}-${random_id.suffix.hex}"
  storage_class = "Standard"
  acl           = "private"
}

resource "alicloud_instance" "app" {
  availability_zone          = var.zone
  security_groups            = [alicloud_security_group.main.id]
  instance_type              = var.instance_type
  system_disk_category       = "cloud_essd"
  system_disk_size           = 40
  image_id                   = data.alicloud_images.ubuntu.images[0].id
  instance_name              = "${var.project_name}-app"
  vswitch_id                 = alicloud_vswitch.main.id
  internet_max_bandwidth_out = 10
  key_name                   = alicloud_key_pair.main.key_pair_name
  user_data                  = file("${path.module}/../cloud-init.yaml")
  tags = {
    Project   = "DirectorGraph"
    Hackathon = "QwenCloud-2026"
  }
}
