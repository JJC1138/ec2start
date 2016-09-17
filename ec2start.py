#!/usr/bin/env python3

import decimal
import datetime
import sys
import time

import boto3
import ipify

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

ec2 = boto3.resource('ec2')

if instance_name:
    print('Getting instance')

    instances = list(ec2.instances.filter(
        Filters=({'Name': 'tag:Name', 'Values': (instance_name,)},)))

    if len(instances) != 1:
        raise Exception('%d instances with that name found' % len(instances))

    instance = instances[0]

    security_groups = instance.security_groups

    if len(security_groups) != 1:
        raise Exception('%d security groups found' % len(security_groups))

    print('Getting security group')

    security_group = ec2.SecurityGroup(security_groups[0]['GroupId'])

else:
    print('Getting security group')

    security_groups = list(ec2.security_groups.filter(GroupNames=(security_group_name,)))

    if len(security_groups) != 1:
        # I believe it's possible to have more than one security group with the same name if they
        # are for different VPCs.
        raise Exception('%d security groups found' % len(security_groups))

    security_group = security_groups[0]

if len(security_group.ip_permissions) > 0:
    print('Removing old permissions from security group')

    security_group.revoke_ingress(IpPermissions=security_group.ip_permissions)

print('Getting our public IP address')

ip = ipify.get_ip()

print('Authorizing connections from %s' % ip)

security_group.authorize_ingress(IpProtocol='tcp', FromPort=3389, ToPort=3389, CidrIp='%s/32' % ip)

r53 = boto3.client('route53')

if not host_name.endswith('.'): host_name = host_name + '.'

print('Looking for Route53 record for %s' % host_name)

zones = r53.list_hosted_zones()

zone_id = None

for zone in zones['HostedZones']:
    if host_name.endswith(zone['Name']):
        zone_id = zone['Id']
        break

if zone_id is None:
    raise Exception('No Route53 zone found for %s' % host_name)

print('Getting existing record\'s TTL')

ttl = r53.list_resource_record_sets(
        HostedZoneId=zone_id, StartRecordName=host_name, StartRecordType='A', MaxItems='1'
    )['ResourceRecordSets'][0]['TTL']

if instance_name:
    print('Starting instance')

    instance.start()

else:
    print('Getting AMI')

    images = list(ec2.images.filter(Filters=({'Name': 'tag:Name', 'Values': (ami_name_tag,)},)))

    if len(images) != 1:
        raise Exception('%d AMIs found' % len(images))

    image = images[0]

    print('Getting current spot prices')

    ec2client = boto3.client('ec2')

    response = ec2client.describe_spot_price_history(
        InstanceTypes=(instance_type,),
        ProductDescriptions=('Windows',),
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
            'SecurityGroupIds': (security_group.id,),
        })

    spot_instance_request_id = \
        spot_instance_response['SpotInstanceRequests'][0]['SpotInstanceRequestId']

    while spot_instance_response['SpotInstanceRequests'][0]['State'] == 'open':
        print('Waiting for spot instance request to be fulfilled')
        time.sleep(5)
        spot_instance_response = ec2client.describe_spot_instance_requests(
            SpotInstanceRequestIds=(spot_instance_request_id,))

    request = spot_instance_response['SpotInstanceRequests'][0]

    if request['State'] != 'active':
        raise Exception('Spot instance request wasn\'t fulfilled')

    instance_id = request['InstanceId']

    print('Getting instance')

    instance = next(iter(ec2.instances.filter(InstanceIds=(instance_id,))))

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
