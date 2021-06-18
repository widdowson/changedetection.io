import time
import hashlib
import os
import selenium
import json
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities

# Some common stuff here that can be moved to a base class
class perform_site_check():

    def __init__(self, *args, datastore, **kwargs):
        super().__init__(*args, **kwargs)
        self.datastore = datastore

    # Do we still need this after moving to Selenium?
    def strip_ignore_text(self, content, list_ignore_text):
        ignore = []
        for k in list_ignore_text:
            ignore.append(k.encode('utf8'))

        output = []
        for line in content.splitlines():
            line = line.encode('utf8')

            # Always ignore blank lines in this mode. (when this function gets called)
            if len(line.strip()):
                if not any(skip_text in line for skip_text in ignore):
                    output.append(line)

        return "\n".encode('utf8').join(output)

    def run(self, uuid):
        timestamp = int(time.time())  # used for storage etc too
        content = False
        changed_detected = False

        update_obj = {'previous_md5': self.datastore.data['watching'][uuid]['previous_md5'],
                      'history': {},
                      "last_checked": timestamp
                      }

        # @todo investigate use of https://github.com/wkeeling/selenium-wire to restore request header functionality.
        extra_headers = self.datastore.get_val(uuid, 'headers')

        # Tweak the base config with the per-watch ones
        request_headers = self.datastore.data['settings']['headers']
        request_headers.update(extra_headers)

        try:
            timeout = self.datastore.data['settings']['requests']['timeout']
        except KeyError:
            # @todo yeah this should go back to the default value in store.py, but this whole object should abstract off it
            timeout = 15

        try:
            url = self.datastore.get_val(uuid, 'url')
            current_md5 = self.datastore.get_val(uuid, 'previous_md5')
            output_path = "/datastore/{}".format(uuid)

            # @todo investigate reuse of the Remote object object across run() invocations. Would require a clean reset betwee .get()s.
            driver = selenium.webdriver.Remote(
                command_executor='http://selenium-hub:4444/wd/hub',
                desired_capabilities=DesiredCapabilities.CHROME)
            
            # @todo restore timeout functionality we used to have with urllib3 requests.
            driver.get(url)

            path = "/datastore/{}".format(uuid)
            try:
                os.stat(path)
            except:
                os.mkdir(path)

            S = lambda X: driver.execute_script('return document.body.parentNode.scroll' + X)
            driver.set_window_size(S('Width'), S(
                'Height'))  # May need manual adjustment
            driver.find_element_by_tag_name('body').screenshot("{}/{}.png".format(path, timestamp))

            content = driver.page_source

            driver.quit()

        except Exception as e:
            update_obj["last_error"] = str(e)
            print(str(e))

        else:
            # @todo investigate use of https://github.com/wkeeling/selenium-wire to restore response code logging.
            # update_obj["last_check_status"] = status
            update_obj["last_error"] = False

            if not len(content):
                update_obj["last_error"] = "Empty reply"

            fetched_md5 = hashlib.md5(content.encode('utf8')).hexdigest()

            # could be None or False depending on JSON type
            if self.datastore.data['watching'][uuid]['previous_md5'] != fetched_md5:
                changed_detected = True

                # Don't confuse people by updating as last-changed, when it actually just changed from None..
                if self.datastore.get_val(uuid, 'previous_md5'):
                    update_obj["last_changed"] = timestamp

                update_obj["previous_md5"] = fetched_md5

        return changed_detected, update_obj, content