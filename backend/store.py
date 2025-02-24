import json
import uuid as uuid_builder
import os.path
from os import path
from threading import Lock

from copy import deepcopy

import logging
import time
import threading


# Is there an existing library to ensure some data store (JSON etc) is in sync with CRUD methods?
# Open a github issue if you know something :)
# https://stackoverflow.com/questions/6190468/how-to-trigger-function-on-value-change
class ChangeDetectionStore:
    lock = Lock()

    def __init__(self, datastore_path="/datastore", include_default_watches=True):
        self.needs_write = False
        self.datastore_path = datastore_path
        self.json_store_path = "{}/url-watches.json".format(self.datastore_path)
        self.stop_thread = False

        self.__data = {
            'note': "Hello! If you change this file manually, please be sure to restart your changedetection.io instance!",
            'watching': {},
            'settings': {
                'headers': {
                    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.66 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
                    'Accept-Encoding': 'gzip, deflate',  # No support for brolti in python requests yet.
                    'Accept-Language': 'en-GB,en-US;q=0.9,en;'
                },
                'requests': {
                    'timeout': 15,  # Default 15 seconds
                    'minutes_between_check': 3 * 60,  # Default 3 hours
                    'workers': 10  # Number of threads, lower is better for slow connections
                },
                'application': {
                    'password': False,
                    'notification_urls': [] # Apprise URL list
                }
            }
        }

        # Base definition for all watchers
        self.generic_definition = {
            'url': None,
            'tag': None,
            'last_checked': 0,
            'last_changed': 0,
            'paused': False,
            'last_viewed': 0,  # history key value of the last viewed via the [diff] link
            'newest_history_key': "",
            'title': None,
            'minutes_between_check': 3 * 60,  # Default 3 hours
            'previous_md5': "",
            'uuid': str(uuid_builder.uuid4()),
            'headers': {},  # Extra headers to send
            'history': {},  # Dict of timestamp and output stripped filename
            'ignore_text': [], # List of text to ignore when calculating the comparison checksum
            'notification_urls': [], # List of URLs to add to the notification Queue (Usually AppRise)
            'css_filter': "",
        }

        if path.isfile('backend/source.txt'):
            with open('backend/source.txt') as f:
                # Should be set in Dockerfile to look for /source.txt , this will give us the git commit #
                # So when someone gives us a backup file to examine, we know exactly what code they were running.
                self.__data['build_sha'] = f.read()

        try:
            # @todo retest with ", encoding='utf-8'"
            with open(self.json_store_path) as json_file:
                from_disk = json.load(json_file)

                # @todo isnt there a way todo this dict.update recursively?
                # Problem here is if the one on the disk is missing a sub-struct, it wont be present anymore.
                if 'watching' in from_disk:
                    self.__data['watching'].update(from_disk['watching'])

                if 'app_guid' in from_disk:
                    self.__data['app_guid'] = from_disk['app_guid']

                if 'settings' in from_disk:
                    if 'headers' in from_disk['settings']:
                        self.__data['settings']['headers'].update(from_disk['settings']['headers'])

                    if 'requests' in from_disk['settings']:
                        self.__data['settings']['requests'].update(from_disk['settings']['requests'])

                    if 'application' in from_disk['settings']:
                        self.__data['settings']['application'].update(from_disk['settings']['application'])

                # Reinitialise each `watching` with our generic_definition in the case that we add a new var in the future.
                # @todo pretty sure theres a python we todo this with an abstracted(?) object!
                for uuid, watch in self.__data['watching'].items():
                    _blank = deepcopy(self.generic_definition)
                    _blank.update(watch)
                    self.__data['watching'].update({uuid: _blank})
                    self.__data['watching'][uuid]['newest_history_key'] = self.get_newest_history_key(uuid)
                    print("Watching:", uuid, self.__data['watching'][uuid]['url'])

        # First time ran, doesnt exist.
        except (FileNotFoundError, json.decoder.JSONDecodeError):
            if include_default_watches:
                print("Creating JSON store at", self.datastore_path)

                self.add_watch(url='http://www.quotationspage.com/random.php', tag='test')
                self.add_watch(url='https://news.ycombinator.com/', tag='Tech news')
                self.add_watch(url='https://www.gov.uk/coronavirus', tag='Covid')
                self.add_watch(url='https://changedetection.io', tag='Tech news')

        self.__data['version_tag'] = "0.32"

        if not 'app_guid' in self.__data:
            import sys
            import os
            if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
                self.__data['app_guid'] = "test-" + str(uuid_builder.uuid4())
            else:
                self.__data['app_guid'] = str(uuid_builder.uuid4())

        self.needs_write = True

        # Finally start the thread that will manage periodic data saves to JSON
        save_data_thread = threading.Thread(target=self.save_datastore).start()

    # Returns the newest key, but if theres only 1 record, then it's counted as not being new, so return 0.
    def get_newest_history_key(self, uuid):
        if len(self.__data['watching'][uuid]['history']) == 1:
            return 0

        dates = list(self.__data['watching'][uuid]['history'].keys())
        # Convert to int, sort and back to str again
        dates = [int(i) for i in dates]
        dates.sort(reverse=True)
        if len(dates):
            # always keyed as str
            return str(dates[0])

        return 0

    def set_last_viewed(self, uuid, timestamp):
        self.data['watching'][uuid].update({'last_viewed': int(timestamp)})
        self.needs_write = True

    def update_watch(self, uuid, update_obj):

        # Skip if 'paused' state
        if self.__data['watching'][uuid]['paused']:
            return

        with self.lock:

            # In python 3.9 we have the |= dict operator, but that still will lose data on nested structures...
            for dict_key, d in self.generic_definition.items():
                if isinstance(d, dict):
                    if update_obj is not None and dict_key in update_obj:
                        self.__data['watching'][uuid][dict_key].update(update_obj[dict_key])
                        del (update_obj[dict_key])

            self.__data['watching'][uuid].update(update_obj)
            self.__data['watching'][uuid]['newest_history_key'] = self.get_newest_history_key(uuid)

        self.needs_write = True

    @property
    def data(self):

        has_unviewed = False
        for uuid, v in self.__data['watching'].items():
            self.__data['watching'][uuid]['newest_history_key'] = self.get_newest_history_key(uuid)
            if int(v['newest_history_key']) <= int(v['last_viewed']):
                self.__data['watching'][uuid]['viewed'] = True

            else:
                self.__data['watching'][uuid]['viewed'] = False
                has_unviewed = True

        self.__data['has_unviewed'] = has_unviewed

        return self.__data

    def get_all_tags(self):
        tags = []
        for uuid, watch in self.data['watching'].items():

            # Support for comma separated list of tags.
            for tag in watch['tag'].split(','):
                tag = tag.strip()
                if tag not in tags:
                    tags.append(tag)

        tags.sort()
        return tags

    def unlink_history_file(self, path):
        try:
            os.unlink(path)
        except (FileNotFoundError, IOError):
            pass

    # Delete a single watch by UUID
    def delete(self, uuid):
        with self.lock:
            if uuid == 'all':
                self.__data['watching'] = {}

                # GitHub #30 also delete history records
                for uuid in self.data['watching']:
                    for path in self.data['watching'][uuid]['history'].values():
                        self.unlink_history_file(path)

            else:
                for path in self.data['watching'][uuid]['history'].values():
                    self.unlink_history_file(path)

                del self.data['watching'][uuid]

            self.needs_write = True

    def url_exists(self, url):

        # Probably their should be dict...
        for watch in self.data['watching'].values():
            if watch['url'] == url:
                return True

        return False

    def get_val(self, uuid, val):
        # Probably their should be dict...
        return self.data['watching'][uuid].get(val)

    # Remove a watchs data but keep the entry (URL etc)
    def scrub_watch(self, uuid, limit_timestamp = False):

        import hashlib
        del_timestamps = []

        changes_removed = 0

        for timestamp, path in self.data['watching'][uuid]['history'].items():
            if not limit_timestamp or (limit_timestamp is not False and int(timestamp) > limit_timestamp):
                self.unlink_history_file(path)
                del_timestamps.append(timestamp)
                changes_removed += 1

                if not limit_timestamp:
                    self.data['watching'][uuid]['last_checked'] = 0
                    self.data['watching'][uuid]['last_changed'] = 0
                    self.data['watching'][uuid]['previous_md5'] = 0


        for timestamp in del_timestamps:
            del self.data['watching'][uuid]['history'][str(timestamp)]

            # If there was a limitstamp, we need to reset some meta data about the entry
            # This has to happen after we remove the others from the list
            if limit_timestamp:
                newest_key = self.get_newest_history_key(uuid)
                if newest_key:
                    self.data['watching'][uuid]['last_checked'] = int(newest_key)
                    # @todo should be the original value if it was less than newest key
                    self.data['watching'][uuid]['last_changed'] = int(newest_key)
                    try:
                        with open(self.data['watching'][uuid]['history'][str(newest_key)], "rb") as fp:
                            content = fp.read()
                        self.data['watching'][uuid]['previous_md5'] = hashlib.md5(content).hexdigest()
                    except (FileNotFoundError, IOError):
                        self.data['watching'][uuid]['previous_md5'] = False
                        pass

        self.needs_write = True
        return changes_removed

    def add_watch(self, url, tag):
        with self.lock:
            # @todo use a common generic version of this
            new_uuid = str(uuid_builder.uuid4())
            _blank = deepcopy(self.generic_definition)
            _blank.update({
                'url': url,
                'tag': tag,
                'uuid': new_uuid
            })

            self.data['watching'][new_uuid] = _blank

        # Get the directory ready
        output_path = "{}/{}".format(self.datastore_path, new_uuid)
        try:
            os.mkdir(output_path)
        except FileExistsError:
            print(output_path, "already exists.")

        self.sync_to_json()
        return new_uuid

    # Save some text file to the appropriate path and bump the history
    # result_obj from fetch_site_status.run()
    def save_history_text(self, uuid, result_obj, contents):

        output_path = "{}/{}".format(self.datastore_path, uuid)
        fname = "{}/{}-{}.stripped.txt".format(output_path, result_obj['previous_md5'], str(time.time()))
        with open(fname, 'w') as f:
            f.write(contents)
            f.close()

        # Update history with the stripped text for future reference, this will also mean we save the first
        # Should always be keyed by string(timestamp)
        self.update_watch(uuid, {"history": {str(result_obj["last_checked"]): fname}})

        return fname

    def sync_to_json(self):
        print("Saving..")
        data ={}

        try:
            data = deepcopy(self.__data)
        except RuntimeError:
            time.sleep(0.5)
            print ("! Data changed when writing to JSON, trying again..")
            self.sync_to_json()
            return
        else:
            with open(self.json_store_path, 'w') as json_file:
                json.dump(data, json_file, indent=4)
                logging.info("Re-saved index")

            self.needs_write = False

    # Thread runner, this helps with thread/write issues when there are many operations that want to update the JSON
    # by just running periodically in one thread, according to python, dict updates are threadsafe.
    def save_datastore(self):

        while True:
            if self.stop_thread:
                print("Shutting down datastore thread")
                return
            
            if self.needs_write:
                self.sync_to_json()
            time.sleep(3)

    # Go through the datastore path and remove any snapshots that are not mentioned in the index
    # This usually is not used, but can be handy.
    def remove_unused_snapshots(self):
        print ("Removing snapshots from datastore that are not in the index..")

        index=[]
        for uuid in self.data['watching']:
            for id in self.data['watching'][uuid]['history']:
                index.append(self.data['watching'][uuid]['history'][str(id)])

        import pathlib
        # Only in the sub-directories
        for item in pathlib.Path(self.datastore_path).rglob("*/*txt"):
            if not str(item) in index:
                print ("Removing",item)
                os.unlink(item)
