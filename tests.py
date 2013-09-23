import neti
import unittest

from ipaddress import IPv4Address, IPv4Network
from mock import Mock, patch

TESTING_CHROOT = "/testing/"


def setup_mocks():
    neti.Connection._get_metadata = Mock()
    neti.Connection._get_metadata.side_effect = lambda x: {neti.INSTANCE_ID_PATH: "i-6a554602", neti.PRIVATE_ADDRESS_PATH: "10.0.0.1", neti.PUBLIC_ADDRESS_PATH: "32.32.32.32"}[x]
    neti.Registry._tag_instance = Mock(return_value=True)


class NetiTestBase(unittest.TestCase):
    is_vpc = False

    def setUp(self):
        setup_mocks()
        with patch.object(neti.Connection, "_is_vpc") as mock_is_vpc:
            mock_is_vpc.__get__ = Mock(return_value=self.is_vpc)
            self.conn = neti.Connection()

    def tearDown(self):
        self.conn.zk.retry(self.conn.zk.delete, TESTING_CHROOT, recursive=True)


class NetiEC2Tests(NetiTestBase):

    def test_register_in_EC2(self):
        registry = neti.Registry(self.conn)
        ip = registry.register()
        self.assertEquals(IPv4Network(unicode(self.conn.overlay_subnet)), self.conn.network)
        self.assertIn(IPv4Address(unicode(ip)), self.conn.network)
        ip_to_id = self.conn.zk.get(registry._zk_ip_path(ip))[0]
        id_to_ip = self.conn.zk.get(registry._zk_id_path)[0]
        self.assertEquals(self.conn.instance_id, ip_to_id)
        self.assertEquals(ip, id_to_ip)


class NetiVPCTests(NetiTestBase):
    is_vpc = True

    def test_register_in_VPC(self):
        registry = neti.Registry(self.conn)
        ip = registry.register()
        self.assertEquals(IPv4Network(unicode(self.conn.overlay_subnet)), self.conn.network)
        self.assertIn(IPv4Address(unicode(ip)), self.conn.network)
        ip_to_id = self.conn.zk.get(registry._zk_ip_path(ip))[0]
        id_to_ip = self.conn.zk.get(registry._zk_id_path)[0]
        self.assertEquals(self.conn.instance_id, ip_to_id)
        self.assertEquals(ip, id_to_ip)


class NetiInstanceIPBundleTests(unittest.TestCase):

    def test_parse_entry(self):
        entry = "acf847a7c6804f6e8e3346a93386a654-32.32.32.32|10.8.2.1|192.168.0.2|0"
        self.bundle = neti.InstanceIPBundle(entry)
        local_nat_ips = {"overlay_ip": "192.168.0.2", "dest_ip": "10.8.2.1"}
        remote_nat_ips = {"overlay_ip": "192.168.0.2", "dest_ip": "32.32.32.32"}
        local_filter_ip = "10.8.2.1"
        remote_filter_ip = "32.32.32.32"
        self.assertEquals(self.bundle.is_vpc, "0")
        self.assertEquals(self.bundle.NAT_ips(False), local_nat_ips)
        self.assertEquals(self.bundle.NAT_ips(True), remote_nat_ips)
        self.assertEquals(self.bundle.filter_ip(False), local_filter_ip)
        self.assertEquals(self.bundle.filter_ip(True), remote_filter_ip)

    def test_bad_ip_entry(self):
        with self.assertRaises(neti.IPPatternMismatchError):
            entry = "acf847a7c6804f6e8e3346a93386a654-32.32.32.32|10.8.2|192.168.0.2|0"
            self.bundle = neti.InstanceIPBundle(entry)

    def test_bad_is_vpc_entry(self):
        with self.assertRaises(neti.IPPatternMismatchError):
            entry = "acf847a7c6804f6e8e3346a93386a654-32.32.32.32|10.8.2.1|192.168.0.2|2"
            self.bundle = neti.InstanceIPBundle(entry)

    def test_missing_uuid_entry(self):
        with self.assertRaises(neti.IPPatternMismatchError):
            entry = "32.32.32.32|10.8.2.1|192.168.0.2|2"
            self.bundle = neti.InstanceIPBundle(entry)

    def test_missing_ip_entry(self):
        with self.assertRaises(neti.IPPatternMismatchError):
            entry = "acf847a7c6804f6e8e3346a93386a654-|10.8.2.1|192.168.0.2|2"
            self.bundle = neti.InstanceIPBundle(entry)


class NetiIPtablesTests(NetiTestBase):

    def build_ip_maps(self):
        self.registry = neti.Registry(self.conn)
        entries = [
            "acf847a7c6804f6e8e3346a93386a654-32.32.32.32|10.8.2.1|192.168.0.1|0",
            "4d8e8617c68a46f89ceaef5bba33b3c0-32.32.32.33|10.8.2.2|192.168.0.2|0",
            "46a30a0bd5a44e7899a87f08ad79e6ff-32.32.32.34|10.8.2.3|192.168.0.3|1"
        ]
        self.conn.zk.ensure_path(self.registry.zk_ip_map_path)
        for entry in entries:
            self.conn.zk.create("%s/%s" % (self.registry.zk_ip_map_path, entry))

        def return_temp(temp):
            temp.seek(0)
            self.tempfile = temp.read()

        neti.IPtables._push_live = Mock(side_effect=return_temp)

    def build_file(self):
        self.build_ip_maps()
        entries = self.conn.zk.get_children(self.registry.zk_ip_map_path)
        builder = neti.IPtables(is_vpc=False, dry_run=True)
        builder.build(entries)

    def test_iptables_file(self):
        self.build_file()
        ips_in_ec2 = ["10.8.2.1", "10.8.2.2", "32.32.32.34"]
        ips_to_check = ips_in_ec2 + neti.IPtables.ssh_whitelist
        for ip in ips_to_check:
            self.assertIn(ip, self.tempfile)

    def test_reject_all(self):
        reject_all = neti.config.getboolean("neti", "reject_all")
        self.build_file()
        self.assertEquals(reject_all, "-j DROP" in self.tempfile)

    def test_open_ports(self):
        open_ports = neti.config.get("neti", "open_ports").split(",")
        self.build_file()
        for port in open_ports:
            self.assertTrue("dport %s" % port in self.tempfile)

    def test_nat_overrides(self):
        nat_overrides = neti.config.items("nat_overrides")
        self.build_file()
        for nat in nat_overrides:
            self.assertIn(nat[0], self.tempfile)


def main():
    unittest.main()

if __name__ == '__main__':
    main()
