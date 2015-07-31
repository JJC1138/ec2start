#!/usr/bin/env python

import sys

import boto3

ec2 = boto3.resource('ec2')

instances = list(ec2.instances.filter(Filters=({'Name': 'tag:Name', 'Values': (sys.argv[1],)},)))

if len(instances) != 1:
	raise Exception('%d instances with that name found' % len(instances))

instance = instances[0]

security_groups = instance.security_groups

if len(security_groups) != 1:
	raise Exception('%d security groups found' % len(security_groups))
