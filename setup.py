import setuptools

setuptools.setup(
    name = 'ec2start',
    version = '1.0.0dev',
    packages = setuptools.find_packages(),
    entry_points = {'console_scripts': [
        'ec2start = ec2start.__main__:main',
        'ec2reimage = ec2start.__main__:reimage',
    ]},
    install_requires = ['boto3', 'ipify'],
)
