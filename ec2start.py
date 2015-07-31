#!/usr/bin/env python

import sys
import time

import boto3
import ipify

if len(sys.argv) != 3:
	raise Exception('Instance and host name arguments is required')

ec2 = boto3.resource('ec2')

print 'Getting instance'

instances = list(ec2.instances.filter(Filters=({'Name': 'tag:Name', 'Values': (sys.argv[1],)},)))

if len(instances) != 1:
	raise Exception('%d instances with that name found' % len(instances))

instance = instances[0]

security_groups = instance.security_groups

if len(security_groups) != 1:
	raise Exception('%d security groups found' % len(security_groups))

print 'Getting security group'

security_group = ec2.SecurityGroup(security_groups[0]['GroupId'])

if len(security_group.ip_permissions) > 0:
	print 'Removing old permissions from security group'

	security_group.revoke_ingress(IpPermissions=security_group.ip_permissions)

print 'Getting our public IP address'

ip = ipify.get_ip()

print 'Authorizing connections from %s' % ip

security_group.authorize_ingress(IpProtocol='tcp', FromPort=3389, ToPort=3389, CidrIp='%s/32' % ip)

r53 = boto3.client('route53')

host_name = sys.argv[2]

if not host_name.endswith('.'): host_name = host_name + '.'

print 'Looking for Route53 record for %s' % host_name

zones = r53.list_hosted_zones()

zone_id = None

for zone in zones['HostedZones']:
	if host_name.endswith(zone['Name']):
		zone_id = zone['Id']
		break

if zone_id is None:
	raise Exception('No Route53 zone found for %s' % host_name)

print 'Getting existing record\'s TTL'

ttl = r53.list_resource_record_sets(
		HostedZoneId=zone_id,StartRecordName=host_name,StartRecordType='A',MaxItems='1'
	)['ResourceRecordSets'][0]['TTL']

print 'Starting instance'

instance.start()

while instance.state['Name'] != 'running':
	print 'Waiting for instance to finish starting'
	time.sleep(5)
	instance.reload()

instance_ip = instance.public_ip_address

print 'Setting %s to point to %s with TTL %d' % (host_name, instance_ip, ttl)

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
	print 'Waiting for DNS update to propagate'
	time.sleep(15)
	response = r53.get_change(Id=response['ChangeInfo']['Id'])
