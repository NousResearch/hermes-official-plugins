resource "example_firewall" "admin" {
  port = 22
  cidr = "192.0.2.0/24"
}
