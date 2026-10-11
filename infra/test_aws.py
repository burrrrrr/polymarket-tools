"""What the Pulumi program asks for on AWS, checked without AWS or the Pulumi CLI.

    pip install -r requirements.txt && python -m unittest -v test_aws

pulumi.runtime.set_mocks stands in for the engine. The program in __main__.py
runs to its end, and each test reads what it registered: a resource's inputs as
the provider would receive them, and the options the engine would act on.
"""

import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path

import pulumi
from pulumi.runtime.mocks import MockMonitor

import aws

PROGRAM = str(Path(__file__).parent / "__main__.py")
PROJECT = "polymarket-tools"
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIVerifyOnlyNotARealKey verify"
VOLUME_ID = "vol-0123456789abcdef0"
SSM_POLICY = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
ZONES_OFFERING = {"t4g.small": ["zone-c", "zone-b"], "m7g.medium": ["zone-a"]}

INSTANCE = "aws:ec2/instance:Instance"
VOLUME = "aws:ebs/volume:Volume"
SECURITY_GROUP = "aws:ec2/securityGroup:SecurityGroup"
ATTACHMENT = "aws:iam/rolePolicyAttachment:RolePolicyAttachment"


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args):
        state = {"name": args.name, "keyName": args.name, "arn": f"arn:aws:mock:::{args.name}",
                 **args.inputs}
        return (VOLUME_ID if args.typ == VOLUME else f"{args.name}-id"), state

    def call(self, args):
        if args.token == "aws:ssm/getParameter:getParameter":
            return {"value": "ami-mock"}
        if args.token == "aws:ec2/getInstanceTypeOfferings:getInstanceTypeOfferings":
            (by_type,) = args.args["filters"]
            return {"locations": ZONES_OFFERING.get(by_type["values"][0], [])}
        return {}


class Recorder(MockMonitor):
    """Keeps each registration request, which carries the resource options
    that Mocks.new_resource is not shown."""

    def __init__(self, mocks):
        super().__init__(mocks)
        self.requests = []

    def RegisterResource(self, request):
        self.requests.append(request)
        return super().RegisterResource(request)


class Stack:
    def __init__(self, recorder, outputs):
        self.requests = [r for r in recorder.requests if r.type != "pulumi:pulumi:Stack"]
        self.outputs = outputs

    def inputs(self, typ):
        """The inputs of every resource of one type."""
        return [pulumi.runtime.rpc.deserialize_properties(r.object)
                for r in self.requests if r.type == typ]

    def one(self, typ):
        found = self.inputs(typ)
        assert len(found) == 1, f"{len(found)} resources of type {typ}"
        return found[0]

    def options(self, typ):
        (request,) = [r for r in self.requests if r.type == typ]
        return request


def run_program(**config):
    recorder = Recorder(Mocks())
    pulumi.runtime.set_mocks(recorder.mocks, project=PROJECT, stack="test", preview=False,
                             monitor=recorder)
    pulumi.runtime.set_all_config({f"{PROJECT}:{key}": value for key, value in config.items()})
    resolved = {}

    @pulumi.runtime.test
    def program():
        outputs = runpy.run_path(PROGRAM)["outputs"]
        return pulumi.Output.all(**outputs).apply(resolved.update)

    program()
    return Stack(recorder, resolved)


class AwsStack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = run_program(sshPublicKey=PUBLIC_KEY)

    def test_security_group_has_no_ingress_rule(self):
        self.assertEqual(self.stack.one(SECURITY_GROUP)["ingress"], [])

    def test_no_other_resource_adds_a_firewall_rule(self):
        types = {r.type for r in self.stack.requests}
        self.assertEqual([t for t in types if "SecurityGroupRule" in t or "IngressRule" in t
                          or "EgressRule" in t], [])

    def test_security_group_lets_only_tcp_443_out(self):
        self.assertEqual(self.stack.one(SECURITY_GROUP)["egress"], [
            {"protocol": "tcp", "fromPort": 443, "toPort": 443, "cidrBlocks": ["0.0.0.0/0"]}])

    def test_instance_sits_behind_that_security_group_alone(self):
        self.assertEqual(self.stack.one(INSTANCE)["vpcSecurityGroupIds"], ["firewall-id"])

    def test_imdsv2_is_required_with_hop_limit_1(self):
        metadata = self.stack.one(INSTANCE)["metadataOptions"]
        self.assertEqual(metadata["httpTokens"], "required")
        self.assertEqual(metadata["httpPutResponseHopLimit"], 1)

    def test_root_and_data_volumes_are_encrypted(self):
        self.assertIs(self.stack.one(INSTANCE)["rootBlockDevice"]["encrypted"], True)
        self.assertIs(self.stack.one(VOLUME)["encrypted"], True)

    def test_instance_role_has_exactly_the_session_manager_policy(self):
        profile = self.stack.one("aws:iam/instanceProfile:InstanceProfile")
        self.assertEqual(self.stack.one(INSTANCE)["iamInstanceProfile"], "instance")
        attached = [a["policyArn"] for a in self.stack.inputs(ATTACHMENT)
                    if a["role"] == profile["role"]]
        self.assertEqual(attached, [SSM_POLICY])

    def test_no_role_gets_a_policy_any_other_way(self):
        iam_types = {r.type for r in self.stack.requests if r.type.startswith("aws:iam/")}
        self.assertEqual(iam_types, {"aws:iam/role:Role", ATTACHMENT,
                                     "aws:iam/instanceProfile:InstanceProfile"})
        for role in self.stack.inputs("aws:iam/role:Role"):
            self.assertNotIn("inlinePolicies", role)
            self.assertNotIn("managedPolicyArns", role)

    def test_no_resource_is_an_s3_bucket(self):
        self.assertEqual([r.type for r in self.stack.requests if r.type.startswith("aws:s3")], [])

    def test_instance_ignores_ami_user_data_and_key_changes(self):
        self.assertEqual(set(self.stack.options(INSTANCE).ignoreChanges),
                         {"ami", "userData", "keyName"})

    def test_instance_and_data_volume_are_protected_from_deletion(self):
        self.assertTrue(self.stack.options(INSTANCE).protect)
        self.assertTrue(self.stack.options(VOLUME).protect)
        self.assertIs(self.stack.one(INSTANCE)["disableApiTermination"], True)

    def test_user_data_runs_the_host_bootstrap_with_the_data_device(self):
        user_data = self.stack.one(INSTANCE)["userData"]
        self.assertIn(
            "DATA_DEVICE=/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol0123456789abcdef0"
            " bash /opt/polymarket-tools/infra/host/bootstrap.sh\n", user_data)
        self.assertIn("git clone https://github.com/burrrrrr/polymarket-tools.git "
                      "/opt/polymarket-tools\n", user_data)

    def test_data_volume_is_attached_to_the_instance(self):
        attachment = self.stack.one("aws:ec2/volumeAttachment:VolumeAttachment")
        self.assertEqual((attachment["volumeId"], attachment["instanceId"]), (VOLUME_ID, "vm-id"))

    def test_teardown_stops_the_instance_before_it_detaches_the_data_volume(self):
        attachment = self.stack.one("aws:ec2/volumeAttachment:VolumeAttachment")
        self.assertIs(attachment["stopInstanceBeforeDetaching"], True)

    def test_subnet_and_data_volume_share_a_zone_that_offers_the_instance_type(self):
        self.assertEqual(self.stack.one("aws:ec2/subnet:Subnet")["availabilityZone"], "zone-b")
        self.assertEqual(self.stack.one(VOLUME)["availabilityZone"], "zone-b")

    def test_subnet_and_data_volume_keep_the_zone_they_were_made_in(self):
        self.assertEqual(list(self.stack.options("aws:ec2/subnet:Subnet").ignoreChanges),
                         ["availabilityZone"])
        self.assertEqual(list(self.stack.options(VOLUME).ignoreChanges), ["availabilityZone"])

    def test_data_volume_leaves_a_final_snapshot(self):
        self.assertIs(self.stack.one(VOLUME)["finalSnapshot"], True)

    def test_daily_snapshots_select_the_data_volume_and_keep_7(self):
        details = self.stack.one("aws:dlm/lifecyclePolicy:LifecyclePolicy")["policyDetails"]
        (schedule,) = details["schedules"]
        self.assertEqual(details["resourceTypes"], ["VOLUME"])
        self.assertLessEqual(details["targetTags"].items(), self.stack.one(VOLUME)["tags"].items())
        self.assertEqual((schedule["createRule"]["interval"], schedule["createRule"]["intervalUnit"]),
                         (24, "HOURS"))
        self.assertEqual(schedule["retainRule"]["count"], 7)

    def test_operator_ssh_key_comes_from_config(self):
        self.assertEqual(self.stack.one("aws:ec2/keyPair:KeyPair")["publicKey"], PUBLIC_KEY)
        self.assertEqual(self.stack.one(INSTANCE)["keyName"], "operator")

    def test_defaults(self):
        self.assertEqual(self.stack.one(INSTANCE)["instanceType"], "t4g.small")
        self.assertEqual(self.stack.one(INSTANCE)["creditSpecification"], {"cpuCredits": "unlimited"})
        self.assertEqual(self.stack.one(VOLUME)["size"], 20)

    def test_exports(self):
        self.assertEqual(set(self.stack.outputs), {"instanceId", "publicIp", "dataVolumeId"})
        self.assertEqual(self.stack.outputs["instanceId"], "vm-id")
        self.assertEqual(self.stack.outputs["dataVolumeId"], VOLUME_ID)


class AptSources(unittest.TestCase):
    """The one line the AWS module adds to the first boot, run by the shell."""

    def rewritten(self, uri):
        with tempfile.TemporaryDirectory() as folder:
            sources = Path(folder) / "ubuntu.sources"
            sources.write_text(f"Types: deb\nURIs: {uri}\nSuites: noble\n")
            subprocess.run(["bash", "-c", aws.APT_OVER_HTTPS.replace(aws.APT_SOURCES, str(sources))],
                           check=True)
            return sources.read_text().splitlines()[1]

    def test_regional_arm64_mirror_becomes_the_https_archive(self):
        self.assertEqual(self.rewritten("http://eu-west-1.ec2.ports.ubuntu.com/ubuntu-ports/"),
                         "URIs: https://ports.ubuntu.com/ubuntu-ports/")

    def test_regional_x86_mirror_becomes_the_https_archive(self):
        self.assertEqual(self.rewritten("http://us-east-1.ec2.archive.ubuntu.com/ubuntu/"),
                         "URIs: https://archive.ubuntu.com/ubuntu/")

    def test_security_and_plain_hosts_become_https(self):
        self.assertEqual(self.rewritten("http://security.ubuntu.com/ubuntu/"),
                         "URIs: https://security.ubuntu.com/ubuntu/")
        self.assertEqual(self.rewritten("http://ports.ubuntu.com/ubuntu-ports/"),
                         "URIs: https://ports.ubuntu.com/ubuntu-ports/")

    def test_user_data_rewrites_the_sources_before_the_first_apt_call(self):
        user_data = run_program(sshPublicKey=PUBLIC_KEY).one(INSTANCE)["userData"]
        self.assertLess(user_data.index(aws.APT_OVER_HTTPS), user_data.index("apt-get"))


class Config(unittest.TestCase):
    def test_config_overrides_the_defaults(self):
        stack = run_program(sshPublicKey=PUBLIC_KEY, instanceType="m7g.medium",
                            dataVolumeGb="50", repoUrl="https://example.invalid/fork.git")
        self.assertEqual(stack.one(INSTANCE)["instanceType"], "m7g.medium")
        self.assertNotIn("creditSpecification", stack.one(INSTANCE))
        self.assertEqual(stack.one(VOLUME)["size"], 50)
        self.assertIn("git clone https://example.invalid/fork.git ", stack.one(INSTANCE)["userData"])

    def test_instance_type_that_no_zone_offers_is_refused(self):
        with self.assertRaisesRegex(ValueError, "x9.none"):
            run_program(sshPublicKey=PUBLIC_KEY, instanceType="x9.none")

    def test_missing_ssh_key_is_refused(self):
        with self.assertRaisesRegex(Exception, "sshPublicKey"):
            run_program()

    def test_unknown_provider_is_refused(self):
        with self.assertRaisesRegex(ValueError, "digitalocean"):
            run_program(sshPublicKey=PUBLIC_KEY, provider="digitalocean")


if __name__ == "__main__":
    unittest.main()
