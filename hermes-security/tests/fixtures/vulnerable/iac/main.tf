resource "example_firewall" "admin" {
  port = 22
  cidr = "0.0.0.0/0"
}
