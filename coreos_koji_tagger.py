#!/usr/bin/python3
import datetime
import fedora_messaging.api
import os
import re
import requests
from libpagure import Pagure
import logging
import json

import dnf.subject
import hawkey

import sys
import subprocess

# Set local logging 
logger = logging.getLogger(__name__)
sh = logging.StreamHandler()
sh.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logger.addHandler(sh)
logger.setLevel(logging.INFO)


# Connect to pagure and set it to point to our repo
PAGURE_REPO='dusty/failed-composes'

# URL for linking to koji tasks by ID
KOJI_TASK_URL='https://koji.fedoraproject.org/koji/taskinfo?taskID='

# The target tag where we want builds to end up. We'll check this tag
# to see if rpms are there.
KOJI_TARGET_TAG = 'coreos-pool'
KOJI_COREOS_USER = 'coreosbot'
KERBEROS_DOMAIN = 'FEDORAPROJECT.ORG'

# We are processing the io.pagure.prod.pagure.git.receive topic
# https://apps.fedoraproject.org/datagrepper/raw?topic=io.pagure.prod.pagure.git.receive&delta=100000
EXAMPLE_MESSAGE_BODY = json.loads("""
{
  "msg": {
    "forced": false,
    "agent": "dustymabe",
    "repo": {
      "custom_keys": [],
      "description": "coreos-koji-data",
      "parent": null,
      "date_modified": "1558714988",
      "access_users": {
        "admin": [],
        "commit": [],
        "ticket": [],
        "owner": [
          "dustymabe"
        ]
      },
      "namespace": "dusty",
      "priorities": {},
      "id": 6234,
      "access_groups": {
        "admin": [],
        "commit": [],
        "ticket": []
      },
      "milestones": {},
      "user": {
        "fullname": "Dusty Mabe",
        "name": "dustymabe"
      },
      "date_created": "1558714988",
      "fullname": "dusty/coreos-koji-data",
      "url_path": "dusty/coreos-koji-data",
      "close_status": [],
      "tags": [],
      "name": "coreos-koji-data"
    },
    "end_commit": "db5c806769a5ab35bfeb15e1ac7c727ec1275b23",
    "branch": "master",
    "authors": [
      {
        "fullname": "Dusty Mabe",
        "name": "dustymabe"
      }
    ],
    "total_commits": 1,
    "start_commit": "db5c806769a5ab35bfeb15e1ac7c727ec1275b23"
  }
}
"""
)


# Given a repo (and thus an input JSON) analyze existing koji tag set
# and tag in any missing packages

#json.loads("""
#        kernel
#        htop
#""")

    
class Consumer(object):
    def __init__(self):
        self.tag = KOJI_TARGET_TAG
        self.koji_user = KOJI_COREOS_USER
        self.kerberos_domain   = KERBEROS_DOMAIN
        self.token = os.getenv('PAGURE_TOKEN')

        # If a keytab was specified let's use it
        self.keytab_file = os.environ.get('COREOS_KOJI_TAGGER_KEYTAB_FILE')
        if self.keytab_file:
            logger.info(f'Authenticating with keytab: {self.keytab_file}')
            if os.path.exists(self.keytab_file):
                self.kinit()
            else:
                raise
        else:
            logger.info('No keytab file defined in '
                        '$COREOS_KOJI_TAGGER_KEYTAB_FILE')
            logger.info('Will not attempt koji write operations')

        if self.token:
            logger.info("Using detected token to talk to pagure.") 
            self.pg = Pagure(pagure_token=token)
        else:
            logger.info("No pagure token was detected.") 
            logger.info("This script will run but won't be able to create new issues.")
            self.pg = Pagure()

        # Set the repo to create new issues against
        self.pg.repo=PAGURE_REPO

        # Used for printing out a value when the day has changed
        self.date = datetime.date.today()

    def __call__(self, message: fedora_messaging.api.Message):
        logger.debug(message.topic)
        logger.debug(message.body)


       ## Grab the raw message body and the status from that
       #msg = message.body

        # set of desired rpms
        desired = {'kernel-5.0.17-300.fc30', 'coreos-installer-0-5.gitd3fc540.fc30', 'cowsay-3.04-12.fc30'}

        # Grab the list of packages that can be tagged into the tag
        pkgsintag = get_pkgs_in_tag(self.tag)

        # Grab the currently tagged builds and convert it into a set
        current = set(get_tagged_builds(self.tag))

        # Find out the difference between the current set of builds
        # that exist in the koji tag and the desired set of builds to
        # be added to the koji tag.
        buildstotag = list(desired.difference(current))


        # compute the package names of each build and determine whether
        # it is in the tag or not. If not we'll need to add the package
        # to the tag before we can add the specific build to the tag
        pkgstoadd = []
        for build in buildstotag:

            # Find the some defining information for this build.
            # Take the first item from the list returned by possibilites func
            subject = dnf.subject.Subject(build)
            buildinfo = subject.get_nevra_possibilities(forms=hawkey.FORM_NEVRA)[0]
            print(buildinfo.name)
            print(buildinfo.version)
            print(buildinfo.epoch)
            print(buildinfo.release)
            print(buildinfo.arch)

            # Check to see if the package is already covered by the tag
            if buildinfo.name not in pkgsintag:
                pkgstoadd.append(buildinfo.name)


        # Add the needed packages to the tag if we have credentials
        if pkgstoadd:
            logger.info(f'Adding packages to tag: {pkgstoadd}')
            if self.keytab_file:
                add_pkgs_to_tag(tag=self.tag,
                                pkgs=pkgstoadd,
                                owner=self.koji_user)

        # Perform the tagging if we have credentials
        if buildstotag:
            logger.info(f'Tagging builds into tag: {buildstotag}')
            if self.keytab_file:
                tag_builds(tag=self.tag, builds=buildstotag)

#       if self.token:
#           self.pg.create_issue(title=title, content=content)

    def kinit(self):
        cmd = f'/usr/bin/kinit -k -t {self.keytab_file}'
        cmd += f' {self.koji_user}@{self.kerberos_domain}'
        cp = subprocess.run(cmd.split(' '), check=True)


def grab_first_column(text: str) -> list:
    # The output is split by newlines (split \n) and contains an 
    # extra newline  at the end (rstrip). We only care about the 1st
    # column (split(' ')[0]) so just grab that and return a list.
    lines = text.rstrip().split('\n')
    return [b.split(' ')[0] for b in lines]


def get_tagged_builds(tag: str) -> list:
    if not tag:
        raise

    # Grab current builds in the koji tag
    # The output with `--quiet` is like this:
    # 
    #   coreos-installer-0-5.gitd3fc540.fc30      coreos-pool           dustymabe
    #   ignition-2.0.0-beta.3.git910e6c6.fc30     coreos-pool           jlebon
    #   kernel-5.0.10-300.fc30                    coreos-pool           labbott
    #   kernel-5.0.11-300.fc30                    coreos-pool           labbott
    # 
    # Usage: koji list-tagged [options] tag [package]
    cmd = f'/usr/bin/koji list-tagged {tag} --quiet'.split(' ')
    cp = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return grab_first_column(cp.stdout)

def get_pkgs_in_tag(tag: str) -> list:
    if not tag:
        raise
    # Usage: koji list-pkgs [options]
    cmd = f'/usr/bin/koji list-pkgs --tag={tag} --quiet'.split(' ')
    cp = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return grab_first_column(cp.stdout)

def tag_builds(tag: bool, builds: list):
    if not tag or not builds:
        raise
    # Usage: koji tag-build [options] <tag> <pkg> [<pkg>...]
    cmd = f'/usr/bin/koji tag-build {tag}'.split(' ')
    cmd.extend(builds)
    cp = subprocess.run(cmd, check=True)

def add_pkgs_to_tag(tag: str, pkgs: list, owner: str):
    if not tag or not pkgs or not owner:
        raise
    # Usage: koji add-pkg [options] tag package [package2 ...]
    cmd = f'/usr/bin/koji add-pkg {tag} --owner {owner}'.split(' ')
    cmd.extend(pkgs)
    cp = subprocess.run(cmd, check=True)

# If run directly we are just testing. So mock up some of
# the data and fake it.
if __name__ == '__main__':
    m = fedora_messaging.api.Message(
            topic = 'io.pagure.prod.pagure.git.receive',
            body = EXAMPLE_MESSAGE_BODY)
    c = Consumer()
    c.__call__(m)
