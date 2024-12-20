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
import ssl
import xml.etree.ElementTree as ET
import json
import yaml
import logging
import threading
import time
import requests
import urllib3

LOG = logging.getLogger('plex-inotify')
logging.getLogger('urllib3.connectionpool').setLevel(logging.INFO)
urllib3.disable_warnings()

# A background thread that schedules the actual scans and:
# - Makes sure we only scan a library after some quiet period of updates
# - Makes sure that we leave some time for a scan to complete before starting
#   another.
class UpdateThread(threading.Thread):
    def __init__(self, event_handler, *a, **k):
        self._dwell_time = k.pop('dwell_time', 10)
        self._run_time = k.pop('run_time', 60)
        self._event_handler = event_handler
        super().__init__(*a, **k)
        self._lock = threading.Lock()
        self._pending = {}
        self._last = 0
        self.daemon = True
        self.log = logging.getLogger('worker')

    def queue_update(self, library_id):
        self.log.info('Queuing update for %i', library_id)
        with self._lock:
            self._pending[library_id] = time.monotonic()

    def _do(self):
        for library_id, last in self._pending.items():
            # Find the first candidate library, schedule it, and bail
            if time.monotonic() - last > self._dwell_time:
                self.log.info('Time to scan library %i', library_id)
                self._pending.pop(library_id)
                self._event_handler.update_section(library_id)
                self._last = time.monotonic()
                break

    def run(self):
        self.log.info('Alive')
        while True:
            # No pending updates, delay longer before checking again
            if not self._pending:
                time.sleep(self._dwell_time)
                continue
            # Last scan was recent, don't schedule any more for a while
            if time.monotonic() - self._last < self._run_time:
                self.log.debug('Waiting for scan...')
                time.sleep(self._dwell_time)
                continue
            # Pending updates, no timers, schedule one
            with self._lock:
                self._do()
            time.sleep(1)

class EventHandler(pyinotify.ProcessEvent):

    def __init__(self, host, port, protocol, token, libraries, allowed_exts):
        self.modified_files = set()
        self.plex_host = host
        self.plex_port = port
        self.plex_account_token = token
        self.protocol = protocol
        self.libraries = libraries
        self.allowed_exts = allowed_exts
        self._thread = UpdateThread(self)
        self._thread.start()

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
                    self._thread.queue_update(self.libraries[path])

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

def url_open(url, token):
    r = requests.get(url, params={'X-Plex-Token': token}, verify=False)
    if r.status_code != 200:
        LOG.error('Plex rejected request: %s %s', r.status_code, r.reason)
    return r

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
tree = ET.fromstring(response.content.decode("utf-8"))
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
