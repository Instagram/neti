# Neti
Neti firewall sync service for EC2-VPC migration

## Requirements
Currently, Neti works on Ubuntu/Debian, but should work on many more platforms with a few tweaks to the package management.  Also, if you don't use AWS, you can just stop reading now...not even sure how you got here. You'll need a set of AWS keys that allows instance metadata modification. You will need to create a Zookeeper cluster, so familiarity with Zookeeper is necessary, but all that is needed is a very basic installation.

## Building Neti
Neti is buildable as a pip package.

## Deploying and Using Neti
Neti should be installed using a configuration management solution like Chef. In fact, there is also [neti-cookbook](https://github.com/Instagram/neti-cookbook) which you can use as a template to install Neti in your infrastructure.

Neti is provided as-is, and, while it worked wonders for Instagram's EC2-VPC migration, it is tailored for our use, and may not work for you without a number of tweaks. Here are some of the major considerations for making this work in your infrastructure:

### Prerequisites/Assumptions

#### Security groups
Neti works best with a relatively flat security group architecture.  This doesn't mean that it can't work with a complex architecture, but the main mechanism of Neti opens up communication channels between all of your hosts on EC2 and VPC.  Neti, in conjunction with Neti-cookbook can give full control over which ports should be open **to the public Internet** on each instance/instance type (using `open_ports` in the config), but if you need more control in terms of which other security groups can access these ports, you'll need to use AWS security groups on top of that, and the configuration might get messy.

#### Safeguards
You can define a set of IP ranges from which SSH is always allowed, which makes sure that if anything screws up in the rule tables you will (most likely) be able to still get into the instances with SSH.  (We never had to fall back on this during our migration, but we kept a few options open at all times in case the worst happened).

#### Zookeeper
As mentioned above, you need to be able to bring up a Zookeeper cluster in VPC for this to work.  Defaults on the cluster should be fine.  You will turn up this cluster in VPC, and then use the Neti-cookbook's zkneti recipe to bring up an identical number of hosts in EC2, which will proxy all zookeeper requests to the cluster in VPC.  Both of these clusters must **NOT** run Neti, and must have their own security groups on either side.  These clusters should have Elastic IPs assigned to them, and these EIPs must be configured on both sides to allow Zookeeper traffic from the other side.  There's a helper script (`scripts/update_zkneti_groups.py`) that will set this for you, as long as you fill in the necessary data about the security groups on both sides once the hosts are alive.

You have complete control over the node naming in Zookeeper, but leaving the defaults should work just fine.

You will need to enter in both the EC2 and VPC host IPs and ports for the clusters into the `neti.yml` config, under `zk_hosts`.

#### Addressing
The overlay subnet is defaulted to 192.168.0.0/18.  This won't conflict with your EC2 addresses, as they will be in the 10.0.0.0/8 range, and you must **make sure that the subnets you configure on the VPC side do not overlap with this subnet** either.

In the case that you have some NATs defined on certain hosts already, and you don't want Neti to overwrite them with its own rules, you can add them using the nat_overrides parameter in the config file, which just takes a hash of `source: dest` IPs.


#### VPC Quirks
When you create instances in VPC, you will need to give every one of them a public interface.  This is a flag, known as "Auto-assign Public IP" in the AWS console, and `:associate_public_ip` in knife-ec2.  You cannot add these interfaces after creating the instances, so make sure your tools for creating instances are configured to set this flag.  Also, make sure that the tools you use support it.  For example, knife-ec2 has to be at least v0.8.0 for support.

### Procedure

Here is a rough list of what you need to do to get Neti up and running:

* Build VPC.  This is going to be configured differently for everyone.
* Build ZK cluster and ZKNeti Proxy cluster
    * Create them with associated Elastic IPs in different security groups
    * Run `scripts/update_zkneti_groups.py` to sync their rules
* Build Neti installation and configuration into your configuration management infra
    * Again, this is difficult to define, as everyone's going to have different setups.  The one that worked for us was using pip to install Neti, and configuring using Chef.  We added Neti into our base role (after testing on a few hosts, of course).
* Test
* Test
* Test some more
* Bring up Neti on your entire EC2 infra, making sure to keep the `reject_all` flag in the config set to False.  This should allow your entire infrastructure to operate without any changes.  Your AWS security groups are still in place. All that's happening at this point is that all the hosts are registered with Neti, and all should be accessible using their overlay IPs.  **TEST THAT THIS IS TRUE**.
* Convert your DB/app/cache/etc configuration scripts to use overlay IPs.  If you autogenerate your configs, then you'll be happy!  If not, this is a good time to start.
* Bring up a few test hosts in the VPC and verify that they register correctly.
* Use `scripts/neti_util.py` to your benefit...it will tell you what is left in your infra that isn't running Neti.
* Turn `reject_all` to True on a single host.  If it still can connect to everything it's supposed to, things are going well.
* Slowly roll out the `reject_all = True` change across your infrastructure...test with each tier, etc.  **Be careful.**
* Once Neti is running everywhere, `scripts/neti_util.py` runs clean, and `reject_all` is enabled everywhere, change your security groups to allow all access from any AWS IP range.  The `scripts/ip_ranges.py` tool is useful for this.
* At this point, here is the state of things:
    * Neti knows about all of your instances in both EC2 and VPC.
    * Each instance has specific access rules allowing communication from every other host to it.
    * IPtables is rejecting everything else, except from the ports in `open_ports`.
    * All traffic to your instances from any other IP in the public AWS-owned ranges is allowed in, but gets blocked at IPtables.
    * You should finally be able to communicate between your hosts in VPC and those in EC2.
* You're "done"...now you can actually do the migration.



## Testing
Use nosetests to run the tests in the tests directory.  They're basic tests, but they cover the main functionality.  Please know that tests passing does not mean that you can inadequately test in your environment!  These tests only make sure that Neti registers and builds things correctly...they **don't** and **can't** make sure that it opens that port you forgot Nginx listens on.


## How Neti works
See <blog post>

See the CONTRIBUTING file for how to help out.

## License
Neti is BSD-licensed. We also provide an additional patent grant.
