# Modules
import boto3
import pymysql
import logging
import os
import re
import shutil
import sys
import zipfile

# Vars from variables.py
from variables import domain
from variables import app_name
from variables import env_id
from variables import env_name
from variables import bucket
from variables import hosted_zone_id
from variables import eb_url
from variables import rds_hostname
from variables import rds_username
from variables import rds_password
from variables import rds_port
from datetime import datetime

# AWS vars
eb = boto3.client('elasticbeanstalk')
s3 = boto3.resource('s3')
r53 = boto3.client('route53')

# Some vars
dst_path = '/tmp'
object_list = '{0}/object_list.txt'.format(dst_path)

# Logging
formatter = logging.Formatter(
    '%(asctime)s | %(levelname)s - %(funcName)s - %(message)s',
    datefmt='%Y-%b-%d %I:%M:%S %p'
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def clean_up(extraction_path):
    """
    Perform clean up
    """
    # noinspection PyBroadException
    try:
        os.unlink(object_list)
        shutil.rmtree(extraction_path)
    except:
        pass


def check_version():
    """
    Find the latest version of the object in the S3 bucket
    """
    try:
        # Get the current version from Elastic Beanstalk
        logger.info('Getting app version')

        response = eb.describe_instances_health(
            EnvironmentName=env_name,
            AttributeNames=[
                'Deployment'
            ]
        )

        current_version = (
            response['InstanceHealthList'][0]['Deployment']['VersionLabel']
        )

        logger.info('Current app version: {}'.format(current_version))

        # Find the zip file that matches the current version
        s3_bucket = s3.Bucket(bucket)

        logger.info('Finding the app version in {}'.format(bucket))

        # Save the list of objects to a text file
        with open(object_list, 'a') as fh:
            for obj in s3_bucket.objects.all():
                fh.write('{0}\n'.format(obj.key))

        # Open the object_list.txt and check the last_modified date
        with open(object_list, 'r') as fh:
            for key in fh:
                if re.search('{0}.zip'.format(current_version), key):
                    stripped_key = key.strip()

                    return stripped_key
    except Exception as e:
        logger.error(e, exc_info=True)
        sys.exit(1)


def download_from_s3(key):
    """
    Get the object from the S3 bucket
    """
    try:
        file_path = '{0}/{1}'.format(dst_path, key)
        extraction_path = format(os.path.splitext(file_path)[0])

        logger.info('Downloading app version to {}'.format(extraction_path))

        s3.meta.client.download_file(bucket, key, file_path)

        # Create an extraction directory
        os.makedirs(extraction_path)

        # Extract the file downloaded from S3
        with zipfile.ZipFile(file_path, 'r') as zip_ref:
            zip_ref.extractall(extraction_path)

        logger.info('App downloaded')
        return extraction_path
    except Exception as e:
        logger.error(e, exc_info=True)
        sys.exit(1)


def create_vhost(customer, extraction_path):
    """
    Create a new VirtualHost config for the new customer
    """
    try:
        ebextensions_path = '{}/.ebextensions'.format(extraction_path)
        vhosts_path = '{}/vhosts'.format(ebextensions_path)

        logger.info('Creating VirtualHost config')

        # Write the VirtualHost config and env variables to customer.conf
        with open('{0}/{1}.conf'.format(vhosts_path, customer), 'w') as fh:
            fh.write("""\
<VirtualHost *:80>
    ServerName {0}.{1}
    ServerAlias www.{0}.{1}
    DocumentRoot /var/www/clients/{0}

    SetEnv RDS_HOSTNAME "{2}"
    SetEnv RDS_DB_NAME "{0}"
    SetEnv RDS_USERNAME "{3}"
    SetEnv RDS_PASSWORD "{4}"
    SetEnv RDS_PORT "{5}"
</VirtualHost>
""".format(
                    customer,
                    domain,
                    rds_hostname,
                    rds_username,
                    rds_password,
                    rds_port
                )
            )

        logger.info('VirtualHost config created')
    except Exception as e:
        logger.error(e, exc_info=True)
        sys.exit(1)


def deploy_app(dir_name):
    """
    Deploy application to Elastic Beanstalk
    """
    now = datetime.now()
    date = '{:04d}{:02d}{:02d}-{:02d}{:02d}{:02d}'.format(
        now.year,
        now.month,
        now.day,
        now.hour,
        now.minute,
        now.second
    )
    new_app_version = '{0}-{1}'.format(domain, date)
    output_filename = '{0}/{1}'.format(dst_path, new_app_version)

    try:
        # Compress the app directory
        shutil.make_archive(output_filename, 'zip', dir_name)

        logger.info('Deploying {} to Elastic Beanstalk'
                    .format(new_app_version))

        # Upload the compressed file to S3
        s3.Bucket(bucket).upload_file('{0}.zip'.format(
            output_filename),
            '{0}.zip'.format(new_app_version)
        )

        # Create a new application version
        eb.create_application_version(
            ApplicationName=app_name,
            VersionLabel=new_app_version,
            SourceBundle={
                'S3Bucket': bucket,
                'S3Key': '{0}.zip'.format(new_app_version)
            },
            AutoCreateApplication=False,
            Process=True
        )

        # Check the application status
        while True:
            response = eb.describe_application_versions(
                ApplicationName=app_name,
                VersionLabels=[
                    new_app_version
                ]
            )
            status = 'PROCESSED'
            app_status = response['ApplicationVersions'][0]['Status']

            if status == app_status:
                break

        # Deploy the app to Elastic Beanstalk
        eb.update_environment(
            ApplicationName=app_name,
            EnvironmentId=env_id,
            VersionLabel=new_app_version
        )

        logger.info('New app version ({}) is now deployed to EB'.format(new_app_version))
        clean_up(dir_name)
        return new_app_version
    except Exception as e:
        logger.error(e, exc_info=True)
        sys.exit(1)


def create_db(customer):
    """
    Create a new database in RDS
    """
    db_name = customer
    db = pymysql.connect(
        rds_hostname,
        rds_username,
        rds_password
    )
    cursor = db.cursor()

    try:
        logger.info('Creating {0} in {1}...'.format(db_name, rds_hostname))
        cursor.execute('CREATE DATABASE {}'.format(db_name))
        cursor.execute('GRANT ALL PRIVILEGES ON {0}.* TO {1}@"%"'.format(db_name, rds_username))
    except Exception as e:
        logger.error(e, exc_info=True)
        db.rollback()

    db.close()


def create_dns_record(customer_url):
    """
    Create a new DNS record in Route 53
    """
    try:
        r_type = 'CNAME'
        action = 'CREATE'
        ttl = 300

        logger.info('Creating {}'.format(customer_url))

        r53.change_resource_record_sets(
            HostedZoneId=hosted_zone_id,
            ChangeBatch={
                'Changes': [
                    {
                        'Action': action,
                        'ResourceRecordSet': {
                            'Name': customer_url,
                            'Type': r_type,
                            'TTL': ttl,
                            'ResourceRecords': [
                                {
                                    'Value': eb_url
                                }
                            ]
                        }
                    }
                ]
            }
        )

        logger.info('Created {}'.format(customer_url))
        return customer_url
    except Exception as e:
        logger.error(e, exc_info=True)
        sys.exit(1)


# noinspection PyUnusedLocal
def main(event, context):
    """
    Main function that will invoke other functions
    """
    try:
        customer = event['params']['path']['customer']
        customer_url = '{0}.{1}'.format(customer, domain)
        key = check_version()
        extraction_path = download_from_s3(key)

        create_vhost(customer, extraction_path)
        create_db(customer)

        new_app_version = deploy_app(extraction_path)
        create_dns_record(customer_url)

        output = {
            'CustomerName': customer,
            'DeploymentVersion': new_app_version,
            'Status': 'deployed',
            'RdsHostname': rds_hostname,
            'RdsDbName': customer,
            'RdsPort': rds_port,
            'Domain': customer_url,
            'EnvId': env_id,
            'EnvName': env_name
        }

        return output
    except Exception as e:
        logger.error(e, exc_info=True)
        sys.exit(1)
