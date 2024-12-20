# PLEX NOTIFIER SCRIPT v1.4
# Modernized by https://github.com/kk7ds/plex-inotifier
# Written by Talisto: https://forums.plex.tv/profile/talisto
# Modified heavily from https://codesourcery.wordpress.com/2012/11/29/more-on-the-synology-nas-automatically-indexing-new-files/

# Allowed file extensions
allowed_exts = [
    'jpg', 'jpeg', 'png', 'gif', 'bmp', 'tiff',
    'mp3', 'flac', 'aac', 'wma', 'ogg', 'ogv', 'wav', 'wma', 'aiff',
    'mpg', 'mp4', 'avi', 'mkv', 'm4a', 'mov', 'wmv', 'm2v', 'm4v', 'vob'
]

import argparse
import pyinotify
import sys
import os.path
from subprocess import call
import signal
import fnmatch
import urllib.request
import ssl
import xml.etree.ElementTree as ET
import json
import yaml
import logging

LOG = logging.getLogger('plex-inotify')

class EventHandler(pyinotify.ProcessEvent):

    def __init__(self, host, port, protocol, token, libraries, allowed_exts):
        self.modified_files = set()
        self.plex_host = host
        self.plex_port = port
        self.plex_account_token = token
        self.protocol = protocol
        self.libraries = libraries
        self.allowed_exts = allowed_exts

    def process_IN_CREATE(self, event):
        self.process_path(event, 'CREATE')

    def process_IN_MOVED_TO(self, event):
        self.process_path(event, 'MOVED TO')

    def process_IN_MOVED_FROM(self, event):
        self.process_path(event, 'MOVED FROM')

    def process_IN_DELETE(self, event):
        self.process_path(event, 'DELETE')

    def process_IN_MODIFY(self, event):
        if self.is_allowed_path(event.pathname, event.dir):
            self.modified_files.add(event.pathname)

    def process_IN_CLOSE_WRITE(self, event):
        # ignore close_write unlesss the file has previously been modified.
        if (event.pathname in self.modified_files):
            self.process_path(event, 'WRITE')

    def process_path(self, event, type):
        if self.is_allowed_path(event.pathname, event.dir):
            log("Notification: %s (%s)" % (event.pathname, type))

            for path in list(self.libraries.keys()):
                if fnmatch.fnmatch(event.pathname, path + "/*"):
                    log("Found match: %s matches Plex section ID: %d" % (
                        event.pathname,
                        self.libraries[path]
                    ))
                    self.update_section(self.libraries[path])

            # Remove from list of modified files.
            try:
                self.modified_files.remove(event.pathname)
            except KeyError as err:
                # Don't care.
                pass
        else:
            log("%s is not an allowed path" % event.pathname)

    def update_section(self, section):
        log('Updating section ID %d' % (section))
        response = url_open("%s://%s:%d/library/sections/%d/refresh" % (
            self.protocol,
            self.plex_host,
            self.plex_port,
            section
        ), self.plex_account_token)

    def is_allowed_path(self, filename, is_dir):
        # Don't check the extension for directories
        if not is_dir:
            ext = os.path.splitext(filename)[1][1:].lower()
            if ext not in self.allowed_exts:
                return False
        if filename.find('@eaDir') > 0:
            return False
        return True

def log(text):
    LOG.info(text)

def signal_handler(signal, frame):
    log("Exiting")
    sys.exit(0)

# custom urlopen() function to bypass SSL certificate validation
def url_open(url, token):
    if token:
        req = urllib.request.Request(url + '?X-Plex-Token=' + token)
    else:
        req = urllib.request.Request(url)
    if url.startswith('https'):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.urlopen(req, context=ctx)
    else:
        return urllib.request.urlopen(req)

###################################################
# MAIN PROGRAM STARTS HERE
###################################################

parser = argparse.ArgumentParser()
parser.add_argument('config',
                    help='Path to YAML config file')
parser.add_argument('--insecure', action='store_true', default=False,
                    help='Use HTTP to connect to plex')
parser.add_argument('-D', '--daemonize',
                    action='store_true', default=False,
                    help='Daemonize')
parser.add_argument('--log', help='Log to this file instead of stdout')
parser.add_argument('--pidfile', default='/var/run/plex-inotify.pid',
                    help='Write PID to this file')
args = parser.parse_args()
logging.basicConfig(level=logging.DEBUG, filename=args.log)

with open(args.config) as f:
    config = yaml.load(f, Loader=yaml.SafeLoader)

watch_events = pyinotify.IN_CLOSE_WRITE \
    | pyinotify.IN_DELETE \
    | pyinotify.IN_CREATE \
    | pyinotify.IN_MOVED_TO \
    | pyinotify.IN_MOVED_FROM

signal.signal(signal.SIGTERM, signal_handler)

if args.insecure:
    protocol = 'http'
else:
    protocol = 'https'

libraries = {}
response = url_open(
    "%s://%s:%d/library/sections" % (
        protocol,
        config['host'],
        config['port']
    ),
    config['plex_token']
)
tree = ET.fromstring(response.read().decode("utf-8"))
for directory in tree:
    for name, path_map in config['path_maps'].items():
        if directory.attrib['title'] == name:
            for path in path_map['paths']:
                libraries[path] = int(directory.attrib['key'])
log("Got Plex libraries: " + json.dumps(libraries))

handler = EventHandler(
    config['host'],
    config['port'],
    protocol,
    config['plex_token'],
    libraries,
    allowed_exts
)
wm = pyinotify.WatchManager()
notifier = pyinotify.Notifier(wm, handler)

log('Adding directories to inotify watch')

wdd = wm.add_watch(
    list(libraries.keys()),
    watch_events,
    rec=True,
    auto_add=True
)

log('Starting loop')

try:
    notifier.loop(daemonize=args.daemonize, pid_file=args.pidfile)
except pyinotify.NotifierError as err:
    print(err, file=sys.stderr)
