"""The AWS resources: one arm64 VM in a network of its own.

No port is open to the internet. The operator's ssh goes through a Session
Manager tunnel (the AWS-StartSSHSession document), which the instance role
allows and the security group never sees.
"""

import json

import pulumi
from pulumi_aws import dlm, ebs, ec2, iam, ssm

import userdata

UBUNTU_AMI = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
SSM_POLICY = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
SNAPSHOT_POLICY = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
NETWORK = "10.87.0.0/24"
BACKUP_TAG = "polymarket-tools-backup"

# The security group lets only TCP 443 out, and the image's apt sources are
# plain http on port 80. Ubuntu's own hosts serve the same archive over https.
# The regional EC2 mirrors do not all answer on 443, so the region prefix goes.
APT_SOURCES = "/etc/apt/sources.list.d/ubuntu.sources"
APT_OVER_HTTPS = (
    "sed -i -E 's#http://([a-z0-9.-]+\\.)?(ports|archive|security)\\.ubuntu\\.com"
    f"#https://\\2.ubuntu.com#' {APT_SOURCES}"
)


def deploy(config):
    name = f"{pulumi.get_project()}-{pulumi.get_stack()}"
    tags = {"Name": name}
    instance_type = config.get("instanceType") or "t4g.small"
    repo_url = config.get("repoUrl") or "https://github.com/burrrrrr/polymarket-tools.git"
    # Not every zone of a region offers every instance type.
    zones = sorted(ec2.get_instance_type_offerings(
        location_type="availability-zone",
        filters=[ec2.GetInstanceTypeOfferingsFilterArgs(
            name="instance-type", values=[instance_type])]).locations)
    if not zones:
        raise ValueError(f"no availability zone of this region offers {instance_type}")
    zone = zones[0]
    # The zone is chosen once. A later lookup that lists another zone first
    # must not ask to move the subnet and the volume, and the instance with them.
    stay_in_zone = pulumi.ResourceOptions(ignore_changes=["availability_zone"])

    vpc = ec2.Vpc("network", cidr_block=NETWORK, enable_dns_support=True,
                  enable_dns_hostnames=True, tags=tags)
    gateway = ec2.InternetGateway("gateway", vpc_id=vpc.id, tags=tags)
    subnet = ec2.Subnet("subnet", vpc_id=vpc.id, cidr_block=NETWORK, availability_zone=zone,
                        map_public_ip_on_launch=True, tags=tags, opts=stay_in_zone)
    routes = ec2.RouteTable(
        "routes", vpc_id=vpc.id, tags=tags,
        routes=[ec2.RouteTableRouteArgs(cidr_block="0.0.0.0/0", gateway_id=gateway.id)])
    way_out = ec2.RouteTableAssociation("routes", subnet_id=subnet.id, route_table_id=routes.id)
    firewall = ec2.SecurityGroup(
        "firewall", vpc_id=vpc.id, tags=tags,
        description="No inbound rule. Outbound HTTPS only.",
        ingress=[],
        egress=[ec2.SecurityGroupEgressArgs(protocol="tcp", from_port=443, to_port=443,
                                            cidr_blocks=["0.0.0.0/0"])])

    role = iam.Role("instance", assume_role_policy=trust("ec2.amazonaws.com"), tags=tags)
    iam.RolePolicyAttachment("instance-session-manager", role=role.name, policy_arn=SSM_POLICY)
    profile = iam.InstanceProfile("instance", role=role.name)
    key = ec2.KeyPair("operator", public_key=config.require("sshPublicKey"))

    volume = ebs.Volume(
        "data", availability_zone=zone, size=config.get_int("dataVolumeGb") or 20, type="gp3",
        encrypted=True, final_snapshot=True, tags={**tags, BACKUP_TAG: name},
        opts=pulumi.ResourceOptions.merge(stay_in_zone, pulumi.ResourceOptions(protect=True)))
    # A Nitro instance shows an EBS volume as an NVMe disk whose serial number
    # is the volume id without its dash.
    device = volume.id.apply(
        lambda volume_id: "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_"
                          + volume_id.replace("-", ""))

    instance = ec2.Instance(
        "vm",
        ami=ssm.get_parameter(name=UBUNTU_AMI).value,
        instance_type=instance_type,
        subnet_id=subnet.id,
        vpc_security_group_ids=[firewall.id],
        iam_instance_profile=profile.name,
        key_name=key.key_name,
        user_data=device.apply(
            lambda path: userdata.render(repo_url, path, first_commands=[APT_OVER_HTTPS])),
        metadata_options=ec2.InstanceMetadataOptionsArgs(
            http_endpoint="enabled", http_tokens="required", http_put_response_hop_limit=1),
        root_block_device=ec2.InstanceRootBlockDeviceArgs(
            volume_type="gp3", volume_size=16, encrypted=True),
        credit_specification=(ec2.InstanceCreditSpecificationArgs(cpu_credits="unlimited")
                              if instance_type.startswith("t") else None),
        disable_api_termination=True,
        tags=tags,
        opts=pulumi.ResourceOptions(
            protect=True,
            # Each of these would replace the instance, or stop and start it,
            # and that ends the keyed processes. A newer image, an edited
            # first-boot script or a new SSH key applies only to a VM made
            # from scratch.
            ignore_changes=["ami", "user_data", "key_name"],
            # The first boot installs packages and clones the repo, so the
            # subnet needs its route out before the instance exists.
            depends_on=[way_out]))
    # AWS can leave a volume stuck detaching while the instance has it mounted,
    # so a teardown shuts the instance down first. That also stops the farmer
    # the clean way, which cancels its orders.
    ec2.VolumeAttachment("data", device_name="/dev/sdf", volume_id=volume.id,
                         instance_id=instance.id, stop_instance_before_detaching=True)

    snapshots = iam.Role("snapshots", assume_role_policy=trust("dlm.amazonaws.com"), tags=tags)
    iam.RolePolicyAttachment("snapshots", role=snapshots.name, policy_arn=SNAPSHOT_POLICY)
    dlm.LifecyclePolicy(
        "snapshots", description=f"Daily snapshot of the {name} data volume",
        execution_role_arn=snapshots.arn, state="ENABLED", tags=tags,
        policy_details=dlm.LifecyclePolicyPolicyDetailsArgs(
            resource_types=["VOLUME"],
            target_tags={BACKUP_TAG: name},
            schedules=[dlm.LifecyclePolicyPolicyDetailsScheduleArgs(
                name="daily", copy_tags=True,
                create_rule=dlm.LifecyclePolicyPolicyDetailsScheduleCreateRuleArgs(
                    interval=24, interval_unit="HOURS", times="03:00"),
                retain_rule=dlm.LifecyclePolicyPolicyDetailsScheduleRetainRuleArgs(count=7))]))

    return {"instanceId": instance.id, "publicIp": instance.public_ip,
            "dataVolumeId": volume.id}


def trust(service):
    return json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}]})
