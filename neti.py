#!/usr/bin/env python

import argparse
import boto
import logging
import os
import random
import re
import requests
import subprocess
import sys
import tempfile
import time

from ConfigParser import ConfigParser
from ipaddress import AddressValueError, IPv4Address, IPv4Network
from kazoo.client import KazooClient
from kazoo.exceptions import NodeExistsError, NoNodeError, ZookeeperError
from kazoo.recipe.party import ShallowParty

config = ConfigParser()
if os.path.exists("/etc/neti/neti.conf"):
    config_file = "/etc/neti/neti.conf"
elif os.path.exists("testing.conf"):  # Assumes testing
    config_file = "testing.conf"
else:
    print "Could not load config file in /etc/neti/neti.conf or testing.conf"
    sys.exit(1)

config.read(config_file)
logger = logging.getLogger('neti')
LOG_FILE = config.get("neti", "log_file")
hdlr = logging.FileHandler(LOG_FILE)
formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
hdlr.setFormatter(formatter)
logger.addHandler(hdlr)
logger.setLevel(logging.INFO)

MAX_IP_TRIES = 5
METADATA_URL = "http://169.254.169.254/latest/meta-data/"
INSTANCE_ID_PATH = "instance-id"
PUBLIC_ADDRESS_PATH = "public-ipv4"
PRIVATE_ADDRESS_PATH = "local-ipv4"
MAC_PATH = "mac"
VPCID_PATH = "network/interfaces/macs/%s/vpc-id"

class Connection(object):

    def __init__(self, dry_run=False):
        self.ec2_zk_hosts = config.get("ec2", "zk_hosts")
        self.vpc_zk_hosts = config.get("vpc", "zk_hosts")
        self.aws_access_key_id = config.get("neti", "aws_key")
        self.aws_secret_access_key = config.get("neti", "aws_secret_key")
        self.ec2_overlay_subnet = config.get("ec2", "overlay_subnet")
        self.vpc_overlay_subnet = config.get("vpc", "overlay_subnet")

        if self._is_vpc:
            self.local_zk = KazooClient(hosts=self.vpc_zk_hosts)
            self.remote_zk = KazooClient(hosts=self.ec2_zk_hosts)
            self.network = IPv4Network(unicode(self.vpc_overlay_subnet))
        else:
            self.local_zk = KazooClient(hosts=self.ec2_zk_hosts)
            self.remote_zk = KazooClient(hosts=self.vpc_zk_hosts)
            self.network = IPv4Network(unicode(self.ec2_overlay_subnet))

        self.instance_id = self._get_instance_id()
        self.public_ip = self._get_public_ip()
        self.private_ip = self._get_private_ip()

        self.local_zk.start()
        self.remote_zk.start()

    @property
    def _is_vpc(self):
        mac = str(requests.get("%s%s" % (METADATA_URL, MAC_PATH)).text)
        res = requests.get("%s%s" % (METADATA_URL, VPCID_PATH % mac))
        if res.status_code == 200:
            return True
        return False

    def _get_metadata(self, path):
        if not path:
            raise "Metadata path cannot be empty!"
        res = requests.get("%s%s" % (METADATA_URL, path))
        if res.status_code == 200:
            return str(res.text)
        else:
            raise MetadataError("Unable to get %s" % path)

    def _get_instance_id(self):
        return self._get_metadata(INSTANCE_ID_PATH)

    def _get_public_ip(self):
        return self._get_metadata(PUBLIC_ADDRESS_PATH)

    def _get_private_ip(self):
        return self._get_metadata(PRIVATE_ADDRESS_PATH)


class Registry(object):

    def __init__(self, conn, dry_run=False):
        self.zk_prefix = config.get("neti", "zk_prefix")
        self.zk_iptoid_path = "%s/%s" % (self.zk_prefix, config.get("neti", "zk_iptoid_node"))
        self.zk_idtoip_path = "%s/%s" % (self.zk_prefix, config.get("neti", "zk_idtoip_node"))
        self.zk_ip_map_path = "%s/%s" % (self.zk_prefix, config.get("neti", "zk_ip_map_node"))
        self.conn = conn
        self.dry_run = dry_run

    @property
    def _zk_id_path(self):
        """ :returns: ID to IP mapping path for this instance. """
        return "%s/%s" % (self.zk_idtoip_path, self.conn.instance_id)

    def _zk_ip_path(self, ip):
        """ :returns: IP to ID mapping path for this IP. """
        return "%s/%s" % (self.zk_iptoid_path, ip)

    def _tag_instance(self):
        """ Tag "overlay_ip" on instance with registered overlay_ip """
        ec2 = boto.connect_ec2(aws_access_key_id=self.conn.aws_access_key_id, aws_secret_access_key=self.conn.aws_secret_access_key)
        if not ec2.create_tags([self.conn.instance_id], {"overlay_ip": self.overlay_ip}):
            logger.error("Could not tag instance")

    def _choose_overlay_ip(self):
        """
        Get list of used IPs from ZK and randomly choose one from the rest of the subnet.
        :returns: String containing chosen IP.
        """
        try:
            used_ips = {IPv4Address(unicode(ip)) for ip in self.conn.local_zk.get_children(self.zk_iptoid_path)}
        except NoNodeError:
            used_ips = set()
        available_ips = set(self.conn.network.hosts()) - used_ips
        if not available_ips:
            logger.error("No available IPs found!")
            raise NoAvailableIPsError
        return str(random.sample(available_ips, 1)[0])

    def _find_available_overlay_ip(self):
        """
        Get a new IP, and attempt to register it.  If it already is taken, get another.  Retry this cycle for
        MAX_RETRIES. After that, fail on no IPs.
        :returns: String containing overlay IP.
        """
        retries = 0
        ip = self._choose_overlay_ip()
        while retries < MAX_IP_TRIES:
            retries += 1
            try:
                self.conn.local_zk.create(self._zk_id_path, ip)
                logger.info("Creating %s" % ip)
            except NoNodeError:
                self.conn.local_zk.ensure_path(self.zk_idtoip_path)
                logger.error("Path %s did not exist...creating and trying again" % self.zk_idtoip_path)
                continue
            except NodeExistsError:
                try:
                    zk_ip, _ = self.conn.local_zk.get(self._zk_id_path)
                    logger.error("IP %s already assigned to %s...using that" % (zk_ip, self.conn.instance_id))
                except NoNodeError:
                    logger.error("No IP found...trying again")
                    continue
                return zk_ip
            try:
                zk_ip, _ = self.conn.local_zk.get(self._zk_id_path)
            except NoNodeError:
                logger.error("IP %s did not get associated...trying again" % ip)
                continue
            if zk_ip == ip:
                logger.info("IP %s set" % ip)
                return ip
            else:
                logger.error("IP %s already assigned to %s...using that" % (zk_ip, self.conn.instance_id))
                return zk_ip

        logger.error("No available IPs found!")
        raise NoAvailableIPsError

    def _set_ip_to_id_map(self, ip):
        """ Sets the reverse map for IP-based lookups. """
        try:
            self.conn.local_zk.set(self._zk_ip_path(ip), self.conn.instance_id)
        except NoNodeError:
            logger.info("No IP to ID map node for %s" % ip)
            try:
                self.conn.local_zk.create(self._zk_ip_path(ip), self.conn.instance_id)
            except NoNodeError:
                self.conn.local_zk.ensure_path(self._zk_ip_path(ip))
                self._set_ip_to_id_map(ip)

    def register(self):
        """ Attempt to get registered overlay IP by instance ID.  If that succeeds, verify that the IP to ID map
        is set correctly, set the instance variable, and tag the instance.  If it fails, attempt to get a new one.
        :returns: String containing overlay IP. """
        try:
            self.overlay_ip, _ = self.conn.local_zk.get(self._zk_id_path)
        except NoNodeError:
            self.overlay_ip = self._find_available_overlay_ip()
            self._tag_instance()
        self._set_ip_to_id_map(self.overlay_ip)
        return self.overlay_ip

    def _get_ip_map(self):
        """ :returns: Mapping of all IPs for znode """
        return "%s|%s|%s|%d" % (self.conn.public_ip, self.conn.private_ip, self.overlay_ip, self.conn._is_vpc)

    def run(self):
        """ Connects to both ZKs, inserts an ephemeral node, and starts a watch for changes. """
        try:
            self.local_party = ShallowParty(self.conn.local_zk, self.zk_ip_map_path, identifier=self._get_ip_map())
            self.remote_party = ShallowParty(self.conn.remote_zk, self.zk_ip_map_path, identifier=self._get_ip_map())
            self.local_party.join()
            self.remote_party.join()

            @self.conn.local_zk.ChildrenWatch(self.zk_ip_map_path)
            def update_iptables(hosts):
                builder = IPtables(is_vpc=self.conn._is_vpc, dry_run=self.dry_run)
                builder.build(hosts)

            while True:
                time.sleep(600)

        except ZookeeperError as e:
            logger.error("ZookeeperError: %s" % e)
            self.run()


class InstanceIPBundle(object):
    """ Bundle object that holds all associated IPs for an instance. """

    uuid_delim = "-"
    ip_delim = "|"
    ip_labels = ["public_ip", "private_ip", "overlay_ip"]

    def __init__(self, entry):
        self.entry = entry
        self._parse_entry()

    def NAT_ips(self, is_vpc):
        """ :returns: dict of IPs needed for NAT rule. """
        ips = {"overlay_ip": self.overlay_ip}
        ips["dest_ip"] = self.private_ip if is_vpc == int(self.is_vpc) else self.public_ip
        return ips

    def filter_ip(self, is_vpc):
        """ :returns: IP string needed for filter rule. """
        if is_vpc == int(self.is_vpc):
            return self.private_ip
        return self.public_ip

    def _parse_entry(self):
        """ Matches ZK node to pattern and sets all IP instance variables. """
        try:
            _, ip_string = self.entry.split(self.uuid_delim)
        except ValueError:
            logger.error("Invalid ZK entry %s" % self.entry)
            raise IPPatternMismatchError(self.entry)
        binary_check = re.compile('[01]')
        try:
            self.is_vpc = ip_string.rsplit(self.ip_delim, 1)[1]
        except ValueError:
            logger.error("Invalid ZK entry %s" % self.entry)
            raise IPPatternMismatchError(self.entry)
        if not binary_check.match(self.is_vpc):
            raise IPPatternMismatchError(self.entry)
        try:
            ips = dict(zip(self.ip_labels, ip_string.split(self.ip_delim)))
        except ValueError:
            logger.error("Invalid ZK entry %s" % self.entry)
            raise IPPatternMismatchError(self.entry)
        if len(ips) != len(self.ip_labels):
            logger.error("Invalid ZK entry %s" % self.entry)
            raise IPPatternMismatchError(self.entry)

        for label, ip in ips.iteritems():
            try:
                IPv4Network(unicode(ip))
            except AddressValueError:
                logger.error("Invalid IP found - %s:%s" % (label, ip))
                raise IPPatternMismatchError(self.entry)
            else:
                setattr(self, label, ip)


class IPtables(object):

    IPTABLES_BASE = """
*filter
-N ec2_whitelist
-N ssh_whitelist
-A INPUT -i lo -j ACCEPT
-A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
-A INPUT -j ec2_whitelist
-A INPUT -j ssh_whitelist
"""
    ssh_whitelist = config.get("neti", "ssh_whitelist").split(",")
    open_80 = config.getboolean("neti", "open_80")
    reject_all = config.getboolean("neti", "reject_all")

    def __init__(self, is_vpc=False, dry_run=False):
        self._is_vpc = is_vpc
        self._get_binaries()
        self._check_compatibility()
        self.dry_run = dry_run

    def _get_binaries(self):
        try:
            self.iptables_bin = subprocess.check_output(["which", "iptables"]).strip()
            self.iptables_restore_bin = subprocess.check_output(["which", "iptables-restore"]).strip()
        except subprocess.CalledProcessError:
            logger.error("No iptables version found!")
            raise MissingIPtablesError

    def _check_compatibility(self):
        """ Checks iptables binary compatibility. """
        try:
            iptables_version_string = subprocess.check_output([self.iptables_bin, "-V"])
        except subprocess.CalledProcessError:
            raise MissingIPtablesError
        non_decimal = re.compile(r'[^\d.]+')
        version = non_decimal.sub('', iptables_version_string.split()[1])
        if tuple(version.split(".")) < (1, 2, 10):
            logger.error("iptables must be of version 1.2.10 or higher!")
            raise InvalidIPtablesVersionError

    def _push_live(self, temp):
        """ Runs a syntax check on the IPtables rule file and loads it into the system if it passes.  If _dry_run
            is set, it prints the rule file to stdout. """
        try:
            subprocess.check_output([self.iptables_restore_bin, "-t", temp.name])
        except subprocess.CalledProcessError, e:
            logger.error("Error in iptables rule file: %s" % e.output)
        if self.dry_run:
            temp.seek(0)
            print temp.read()
            sys.exit(0)
        else:
            try:
                subprocess.check_output([self.iptables_restore_bin, "-v", temp.name])
            except subprocess.CalledProcessError, e:
                logger.error("Error in iptables rule file: %s" % e.output)

    def _gen_rule_file(self, temp, bundles):
        """ Generates rule file for the iptables-restore command. """
        temp.write(self.IPTABLES_BASE)
        if self.open_80:
            temp.write("-A INPUT -p tcp --dport 80 -m state --state NEW,ESTABLISHED -j ACCEPT")
            temp.write("-A OUTPUT -o eth0 -p tcp --sport 80 -m state --state ESTABLISHED -j ACCEPT")
        if self.reject_all:
            temp.write("-A INPUT -p tcp -j DROP")
        for bundle in bundles:
            temp.write(str(FilterRule("ec2_whitelist", bundle.filter_ip(self._is_vpc))))
        if len(self.ssh_whitelist) > 0:
            for ip in self.ssh_whitelist:
                temp.write(str(FilterRule("ssh_whitelist", ip, dest_port=22)))
        if self._is_vpc:
            temp.write(str(FilterRule("ssh_whitelist", "10.0.0.0/8")))
        temp.write("COMMIT\n")

        temp.write("*nat\n")
        for bundle in bundles:
            nat_ips = bundle.NAT_ips(self._is_vpc)
            temp.write(str(NATRule("OUTPUT", nat_ips["overlay_ip"], nat_ips["dest_ip"])))
        temp.write("COMMIT\n")
        temp.flush()

    def _ips_from_entries(self, entries):
        """ Builds array of InstanceIPBundles from found ZK nodes.
            :returns: List of InstanceIPBundles. """
        ips = []
        for entry in entries:
            ips.append(InstanceIPBundle(entry))
        return ips

    def build(self, entries):
        """ Gets nodes from ZK, builds rule table, and pushes it live. """
        self._check_compatibility()
        logger.info("Generating new iptables rules")
        bundles = self._ips_from_entries(entries)
        num_entries = len(bundles)
        logger.info("%d party members" % num_entries)
        if num_entries > 0:
            with tempfile.NamedTemporaryFile() as temp:
                self._gen_rule_file(temp, bundles)
                self._push_live(temp)


class FilterRule(object):

    CHAINS = ["OUTPUT", "INPUT", "PREROUTING", "POSTROUTING", "ssh_whitelist", "ec2_whitelist"]

    def __init__(self, chain, source_ip, dest_port=None):
        self.chain = chain
        self.source_ip = source_ip
        self._validate()
        dport = ""
        if dest_port:
            dport = "-p tcp --dport %d" % dest_port
        self.rule = "-A %s -s %s %s -j ACCEPT\n" % (chain, self.source_ip, dport)

    def __str__(self):
        return self.rule

    def _validate(self):
        try:
            IPv4Network(unicode(self.source_ip))
        except AddressValueError:
            logger.error("Invalid IP specified in NAT rule!")
            raise InvalidIPError(self.source_ip)
        if self.chain not in self.CHAINS:
            logger.error("Invalid chain specified in NAT rule!")
            raise InvalidChainError(self.chain)


class NATRule(object):

    CHAINS = ["OUTPUT", "INPUT", "PREROUTING", "POSTROUTING"]

    def __init__(self, chain, source_ip, dest_ip):
        self.chain = chain
        self.source_ip = source_ip
        self.dest_ip = dest_ip
        self._validate()
        self.rule = "-A %s -d %s -j DNAT --to-destination %s\n" % (chain, self.source_ip, self.dest_ip)

    def __str__(self):
        return self.rule

    def _validate(self):
        for ip in [self.source_ip, self.dest_ip]:
            try:
                IPv4Network(unicode(ip))
            except AddressValueError:
                raise InvalidIPError(ip)
        if self.chain not in self.CHAINS:
            raise InvalidChainError(self.chain)


class NetiError(Exception):
    pass


class MetadataError(NetiError):
    pass


class MissingIPtablesError(NetiError):
    pass


class InvalidIPtablesVersionError(NetiError):
    pass


class InvalidChainError(NetiError):
    pass


class InvalidIPError(NetiError):
    pass


class NoAvailableIPsError(NetiError):
    pass


class IPPatternMismatchError(NetiError):
    pass


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("-c", dest="config_file", default="/etc/neti/neti.conf")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true")
    parser.set_defaults(dry_run=False)

    args = parser.parse_args()
    conn = Connection()
    registry = Registry(conn, dry_run=args.dry_run)
    registry.register()
    registry.run()

if __name__ == "__main__":
    main()
