#!/usr/bin/env python3

import argparse
import decimal
import datetime
import enum
import re
import sys
import time

import boto3
import ipify

ec2 = boto3.resource('ec2')

def get_ami(name_tag):
    images = list(ec2.images.filter(Filters=[{'Name': 'tag:Name', 'Values': [name_tag]}]))

    if len(images) != 1:
        raise Exception('%d AMIs found' % len(images))

    image = images[0]

    return image

def main():
    if len(sys.argv) == 3:
        instance_name = sys.argv[1]
    elif len(sys.argv) == 6:
        instance_name = None
        ami_name_tag = sys.argv[1]
        instance_type = sys.argv[3]
        bid_price = decimal.Decimal(sys.argv[4])
        security_group_name = sys.argv[5]
    else:
        raise Exception('At least instance and host name arguments are required')

    host_name = sys.argv[2]

    class Platform(enum.Enum):
        linux = 'Linux'
        windows = 'Windows'
        @classmethod
        def from_string(cls, string):
            if not string:
                return Platform.linux
            elif string == 'windows':
                return Platform.windows
            else:
                raise Exception('Unknown instance platform %s' % string)

    if instance_name:
        print('Getting instance')

        instances = list(ec2.instances.filter(
            Filters=[{'Name': 'tag:Name', 'Values': [instance_name]}]))

        if len(instances) != 1:
            raise Exception('%d instances with that name found' % len(instances))

        instance = instances[0]

        platform = Platform.from_string(instance.platform)

        security_groups = instance.security_groups

        if len(security_groups) != 1:
            raise Exception('%d security groups found' % len(security_groups))

        print('Getting security group')

        security_group = ec2.SecurityGroup(security_groups[0]['GroupId'])

    else:
        print('Getting AMI')

        image = get_ami(ami_name_tag)

        platform = Platform.from_string(image.platform)

        print('Getting security group')

        security_groups = list(ec2.security_groups.filter(GroupNames=[security_group_name]))

        if len(security_groups) != 1:
            # I believe it's possible to have more than one security group with the same name if they
            # are for different VPCs.
            raise Exception('%d security groups found' % len(security_groups))

        security_group = security_groups[0]

    print('Detected platform: %s' % platform.value)

    if len(security_group.ip_permissions) > 0:
        print('Removing old permissions from security group')

        security_group.revoke_ingress(IpPermissions=security_group.ip_permissions)

    print('Getting our public IP address')

    ip = ipify.get_ip()

    print('Authorizing connections from %s' % ip)

    port = 22 if platform == Platform.linux else 3389
    security_group.authorize_ingress(IpProtocol='tcp', FromPort=port, ToPort=port, CidrIp='%s/32' % ip)

    r53 = boto3.client('route53')

    if not host_name.endswith('.'): host_name = host_name + '.'

    print('Looking for Route53 record for %s' % host_name)

    zones = r53.list_hosted_zones()

    zone_id = None

    longest_matching_zone_name_length = -1

    for zone in zones['HostedZones']:
        zone_name = zone['Name']
        if host_name.endswith(zone_name):
            # It's quite possible that a user might have more than one plausible matching zone, because they might have e.g. example.com and sub.example.com. If we have more than one match then we want to use the most-specific one, which will be the longest one:
            zone_name_length = len(zone_name)
            if zone_name_length > longest_matching_zone_name_length:
                zone_id = zone['Id']
                longest_matching_zone_name_length = zone_name_length

    if zone_id is None:
        raise Exception('No Route53 zone found for %s' % host_name)

    print('Getting existing record\'s TTL')

    ttl = 60 # default to use if the record doesn't exist

    record_sets = r53.list_resource_record_sets(
        HostedZoneId=zone_id, StartRecordName=host_name, StartRecordType='A', MaxItems='1'
        )['ResourceRecordSets']
    if len(record_sets) > 0:
        record_set = record_sets[0]
        # Because we're only using StartRecordName to filter, if the record doesn't exist then we might have some other record:
        if record_set['Name'] == host_name:
            ttl = record_set['TTL']

    if instance_name:
        print('Starting instance')

        instance.start()

    else:
        print('Getting current spot prices')

        ec2client = boto3.client('ec2')

        response = ec2client.describe_spot_price_history(
            InstanceTypes=[instance_type],
            ProductDescriptions=['Linux/UNIX' if platform == Platform.linux else 'Windows'],
            StartTime=datetime.datetime.utcnow())

        spot_prices = [decimal.Decimal(i['SpotPrice']) for i in response['SpotPriceHistory']]

        if len(spot_prices) == 0:
            raise Exception('No spot prices found for instance type %s' % instance_type)

        lowest_spot_price = min(spot_prices)

        print('Lowest current spot price: %s' % lowest_spot_price)

        if bid_price < lowest_spot_price:
            raise Exception('Bid price %s is too low' % bid_price)

        print('Requesting spot instance')

        spot_instance_response = ec2client.request_spot_instances(
            SpotPrice=str(bid_price),
            LaunchSpecification = {
                'ImageId': image.id,
                'InstanceType': instance_type,
                'SecurityGroupIds': [security_group.id],
            })

        spot_instance_request_id = \
            spot_instance_response['SpotInstanceRequests'][0]['SpotInstanceRequestId']

        while spot_instance_response['SpotInstanceRequests'][0]['State'] == 'open':
            print('Waiting for spot instance request to be fulfilled')
            time.sleep(5)
            spot_instance_response = ec2client.describe_spot_instance_requests(
                SpotInstanceRequestIds=[spot_instance_request_id])

        request = spot_instance_response['SpotInstanceRequests'][0]

        if request['State'] != 'active':
            raise Exception('Spot instance request wasn\'t fulfilled')

        instance_id = request['InstanceId']

        print('Getting instance')

        instance = next(iter(ec2.instances.filter(InstanceIds=[instance_id])))

    while instance.state['Name'] != 'running':
        print('Waiting for instance to finish starting')
        time.sleep(5)
        instance.reload()

    instance_ip = instance.public_ip_address

    print('Setting %s to point to %s with TTL %d' % (host_name, instance_ip, ttl))

    response = r53.change_resource_record_sets(
        HostedZoneId = zone_id,
        ChangeBatch = {
            'Changes': [
                {
                    'Action': 'UPSERT',
                    'ResourceRecordSet': {
                        'Name': host_name,
                        'Type': 'A',
                        'TTL': ttl,
                        'ResourceRecords' : [
                            {
                                'Value': instance_ip
                            },
                        ],
                    }
                },
            ]
        })

    while response['ChangeInfo']['Status'] != 'INSYNC':
        print('Waiting for DNS update to propagate')
        time.sleep(15)
        response = r53.get_change(Id=response['ChangeInfo']['Id'])

def reimage():
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument('--delete-old', action='store_true', help="Delete the old AMI and its EBS snapshot after the new AMI is created")
    arg_parser.add_argument('--terminate', action='store_true', help="Terminate the instance the new AMI is created from after creation finishes")
    arg_parser.add_argument('ami_name_tag', metavar='ami-name-tag')
    if len(sys.argv) == 1:
        sys.argv.append('-h')
    args = arg_parser.parse_args()

    print('Getting AMI')

    old_image = get_ami(args.ami_name_tag)

    old_version_number = 1 # default

    m = re.match('%s \((?P<version_number>[0-9])+\)' % re.escape(args.ami_name_tag), old_image.name)
    if m:
        try:
            old_version_number = int(m.group('version_number'))
        except ValueError:
            pass

    version_number = old_version_number + 1

    print('Getting instance')

    instances = list(ec2.instances.filter(Filters=[
        {'Name': 'image-id', 'Values': [old_image.id]},
        {'Name': 'instance-state-name', 'Values': ['running', 'stopping', 'stopped']},
    ]))

    if len(instances) != 1:
        raise Exception('%d instances of that AMI found' % len(instances))

    instance = instances[0]

    ami_name = '%s (%d)' % (args.ami_name_tag, version_number)

    print('Creating new AMI')

    image = instance.create_image(Name=ami_name)

    while image.state != 'available':
        print("Waiting for new AMI to become available (it is currently %s)" % image.state)
        time.sleep(5)
        image.load()

    def set_tag_name(resource, tag_name):
        resource.create_tags(Tags=[{'Key': 'Name', 'Value': tag_name}])

    print("Setting new AMI's Name tag")

    set_tag_name(image, args.ami_name_tag)

    old_image_name_tag = '%s (Old)' % args.ami_name_tag

    print("Setting old AMI's Name tag to: %s" % old_image_name_tag)

    set_tag_name(old_image, old_image_name_tag)

    if args.delete_old:
        old_image_snapshot_id = old_image.block_device_mappings[0]['Ebs']['SnapshotId']

        print("Deleting old AMI")
        old_image.deregister()

        print("Deleting old AMI's EBS Snapshot")
        snapshot = ec2.Snapshot(old_image_snapshot_id)
        snapshot.delete()

    if args.terminate:
        print("Terminating instance")
        instance.terminate()
