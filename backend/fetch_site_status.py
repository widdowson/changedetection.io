from threading import Thread
import time
import requests
import hashlib

import os

# Hmm Polymorphism datastore, thread, etc
class perform_site_check(Thread):
    def __init__(self, *args, uuid=False, datastore, **kwargs):
        super().__init__(*args, **kwargs)
        self.timestamp = int(time.time())  # used for storage etc too
        self.uuid = uuid
        self.datastore = datastore
        self.url = datastore.get_val(uuid, 'url')
        self.current_md5 = datastore.get_val(uuid, 'previous_md5')
        self.output_path = "/datastore/{}".format(self.uuid)


    def run(self):


        self.datastore.update_watch(self.uuid, 'last_error', False)
#        self.datastore.update_watch(self.uuid, 'last_check_status', r.status_code)

 #       fetched_md5 = hashlib.md5(stripped_text_from_html.encode('utf-8')).hexdigest()

        import selenium
        from selenium.webdriver.common.desired_capabilities import DesiredCapabilities

        driver = selenium.webdriver.Remote(
            command_executor='http://selenium-hub:4444/wd/hub',
            desired_capabilities=DesiredCapabilities.CHROME)

        driver.get(self.url)

        path = "/datastore/{}".format(self.uuid)
        try:
            os.stat(path)
        except:
            os.mkdir(path)


        S = lambda X: driver.execute_script('return document.body.parentNode.scroll' + X)
        driver.set_window_size(S('Width'), S(
            'Height'))  # May need manual adjustment
        driver.find_element_by_tag_name('body').screenshot("{}/{}.png".format(path, self.timestamp))

        driver.quit()

        self.datastore.update_watch(self.uuid, 'last_changed', self.timestamp)
        self.datastore.update_watch(self.uuid, 'previous_md5', 'aaa')



        self.datastore.update_watch(self.uuid, 'last_checked', int(time.time()))
        pass
