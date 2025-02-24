#!/usr/bin/python3


# @todo logging
# @todo extra options for url like , verify=False etc.
# @todo enable https://urllib3.readthedocs.io/en/latest/user-guide.html#ssl as option?
# @todo option for interval day/6 hour/etc
# @todo on change detected, config for calling some API
# @todo make tables responsive!
# @todo fetch title into json
# https://distill.io/features
# proxy per check
#  - flask_cors, itsdangerous,MarkupSafe

import time
import os
import timeago
import flask_login
from flask_login import login_required

import threading
from threading import Event

import queue

from flask import Flask, render_template, request, send_from_directory, abort, redirect, url_for

from feedgen.feed import FeedGenerator
from flask import make_response
import datetime
import pytz

datastore = None

# Local
running_update_threads = []
ticker_thread = None

messages = []
extra_stylesheets = []

update_q = queue.Queue()

notification_q = queue.Queue()

app = Flask(__name__, static_url_path="/var/www/change-detection/backend/static")

# Stop browser caching of assets
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

app.config.exit = Event()

app.config['NEW_VERSION_AVAILABLE'] = False

app.config['LOGIN_DISABLED'] = False

# Disables caching of the templates
app.config['TEMPLATES_AUTO_RELOAD'] = True


# We use the whole watch object from the store/JSON so we can see if there's some related status in terms of a thread
# running or something similar.
@app.template_filter('format_last_checked_time')
def _jinja2_filter_datetime(watch_obj, format="%Y-%m-%d %H:%M:%S"):
    # Worker thread tells us which UUID it is currently processing.
    for t in running_update_threads:
        if t.current_uuid == watch_obj['uuid']:
            return "Checking now.."

    if watch_obj['last_checked'] == 0:
        return 'Not yet'

    return timeago.format(int(watch_obj['last_checked']), time.time())


# @app.context_processor
# def timeago():
#    def _timeago(lower_time, now):
#        return timeago.format(lower_time, now)
#    return dict(timeago=_timeago)

@app.template_filter('format_timestamp_timeago')
def _jinja2_filter_datetimestamp(timestamp, format="%Y-%m-%d %H:%M:%S"):
    return timeago.format(timestamp, time.time())
    # return timeago.format(timestamp, time.time())
    # return datetime.datetime.utcfromtimestamp(timestamp).strftime(format)


class User(flask_login.UserMixin):
    id=None

    def set_password(self, password):
        return True
    def get_user(self, email="defaultuser@changedetection.io"):
        return self
    def is_authenticated(self):

        return True
    def is_active(self):
        return True
    def is_anonymous(self):
        return False
    def get_id(self):
        return str(self.id)

    def check_password(self, password):

        import hashlib
        import base64

        # Getting the values back out
        raw_salt_pass = base64.b64decode(datastore.data['settings']['application']['password'])
        salt_from_storage = raw_salt_pass[:32]  # 32 is the length of the salt

        # Use the exact same setup you used to generate the key, but this time put in the password to check
        new_key = hashlib.pbkdf2_hmac(
            'sha256',
            password.encode('utf-8'),  # Convert the password to bytes
            salt_from_storage,
            100000
        )
        new_key =  salt_from_storage + new_key

        return new_key == raw_salt_pass

    pass

def changedetection_app(conig=None, datastore_o=None):
    global datastore
    datastore = datastore_o

    app.config.update(dict(DEBUG=True))
    #app.config.update(config or {})

    login_manager = flask_login.LoginManager(app)
    login_manager.login_view = 'login'


    # Setup cors headers to allow all domains
    # https://flask-cors.readthedocs.io/en/latest/
    #    CORS(app)

    @login_manager.user_loader
    def user_loader(email):
        user = User()
        user.get_user(email)
        return user

    @login_manager.unauthorized_handler
    def unauthorized_handler():
        # @todo validate its a URL of this host and use that
        return redirect(url_for('login', next=url_for('index')))

    @app.route('/logout')
    def logout():
        flask_login.logout_user()
        return redirect(url_for('index'))

    # https://github.com/pallets/flask/blob/93dd1709d05a1cf0e886df6223377bdab3b077fb/examples/tutorial/flaskr/__init__.py#L39
    # You can divide up the stuff like this
    @app.route('/login', methods=['GET', 'POST'])
    def login():

        global messages

        if request.method == 'GET':
            output = render_template("login.html", messages=messages)
            # Show messages but once.
            messages = []
            return output

        user = User()
        user.id = "defaultuser@changedetection.io"

        password = request.form.get('password')

        if (user.check_password(password)):
            flask_login.login_user(user, remember=True)
            next = request.args.get('next')
            #            if not is_safe_url(next):
            #                return flask.abort(400)
            return redirect(next or url_for('index'))

        else:
            messages.append({'class': 'error', 'message': 'Incorrect password'})

        return redirect(url_for('login'))

    @app.before_request
    def do_something_whenever_a_request_comes_in():
        # Disable password  loginif there is not one set
        app.config['LOGIN_DISABLED'] = datastore.data['settings']['application']['password'] == False

    @app.route("/", methods=['GET'])
    @login_required
    def index():
        global messages
        limit_tag = request.args.get('tag')

        pause_uuid = request.args.get('pause')

        if pause_uuid:
            try:
                datastore.data['watching'][pause_uuid]['paused'] ^= True
                datastore.needs_write = True

                return redirect(url_for('index', tag = limit_tag))
            except KeyError:
                pass


        # Sort by last_changed and add the uuid which is usually the key..
        sorted_watches = []
        for uuid, watch in datastore.data['watching'].items():

            if limit_tag != None:
                # Support for comma separated list of tags.
                for tag_in_watch in watch['tag'].split(','):
                    tag_in_watch = tag_in_watch.strip()
                    if tag_in_watch == limit_tag:
                        watch['uuid'] = uuid
                        sorted_watches.append(watch)

            else:
                watch['uuid'] = uuid
                sorted_watches.append(watch)

        sorted_watches.sort(key=lambda x: x['last_changed'], reverse=True)

        existing_tags = datastore.get_all_tags()
        rss = request.args.get('rss')

        if rss:
            fg = FeedGenerator()
            fg.title('changedetection.io')
            fg.description('Feed description')
            fg.link(href='https://changedetection.io')

            for watch in sorted_watches:
                if not watch['viewed']:
                    fe = fg.add_entry()
                    fe.title(watch['url'])
                    fe.link(href=watch['url'])
                    fe.description(watch['url'])
                    fe.guid(watch['uuid'], permalink=False)
                    dt = datetime.datetime.fromtimestamp(int(watch['newest_history_key']))
                    dt = dt.replace(tzinfo=pytz.UTC)
                    fe.pubDate(dt)

            response = make_response(fg.rss_str())
            response.headers.set('Content-Type', 'application/rss+xml')
            return response

        else:
            output = render_template("watch-overview.html",
                                     watches=sorted_watches,
                                     messages=messages,
                                     tags=existing_tags,
                                     active_tag=limit_tag,
                                     has_unviewed=datastore.data['has_unviewed'])

            # Show messages but once.
            messages = []

        return output

    @app.route("/scrub", methods=['GET', 'POST'])
    @login_required
    def scrub_page():

        global messages
        import re

        if request.method == 'POST':
            confirmtext = request.form.get('confirmtext')
            limit_date = request.form.get('limit_date')

            try:
                limit_date = limit_date.replace('T', ' ')
                # I noticed chrome will show '/' but actually submit '-'
                limit_date = limit_date.replace('-', '/')
                # In the case that :ss seconds are supplied
                limit_date = re.sub('(\d\d:\d\d)(:\d\d)', '\\1', limit_date)

                str_to_dt = datetime.datetime.strptime(limit_date, '%Y/%m/%d %H:%M')
                limit_timestamp = int(str_to_dt.timestamp())

                if limit_timestamp > time.time():
                    messages.append({'class': 'error',
                                     'message': "Timestamp is in the future, cannot continue."})
                    return redirect(url_for('scrub_page'))

            except ValueError:
                messages.append({'class': 'ok', 'message': 'Incorrect date format, cannot continue.'})
                return redirect(url_for('scrub_page'))

            if confirmtext == 'scrub':
                changes_removed = 0
                for uuid, watch in datastore.data['watching'].items():
                    if limit_timestamp:
                        changes_removed += datastore.scrub_watch(uuid, limit_timestamp=limit_timestamp)
                    else:
                        changes_removed += datastore.scrub_watch(uuid)

                messages.append({'class': 'ok',
                                 'message': "Cleared snapshot history ({} snapshots removed)".format(
                    changes_removed)})
            else:
                messages.append({'class': 'error', 'message': 'Incorrect confirmation text.'})

            return redirect(url_for('index'))

        output =  render_template("scrub.html", messages=messages)
        messages = []
        return output


    # If they edited an existing watch, we need to know to reset the current/previous md5 to include
    # the excluded text.
    def get_current_checksum_include_ignore_text(uuid):

        import hashlib
        from backend import fetch_site_status

        # Get the most recent one
        newest_history_key = datastore.get_val(uuid, 'newest_history_key')

        # 0 means that theres only one, so that there should be no 'unviewed' history availabe
        if newest_history_key == 0:
            newest_history_key = list(datastore.data['watching'][uuid]['history'].keys())[0]

        if newest_history_key:
            with open(datastore.data['watching'][uuid]['history'][newest_history_key],
                      encoding='utf-8') as file:
                raw_content = file.read()

                handler = fetch_site_status.perform_site_check(datastore=datastore)
                stripped_content = handler.strip_ignore_text(raw_content,
                                                             datastore.data['watching'][uuid]['ignore_text'])

                checksum = hashlib.md5(stripped_content).hexdigest()
                return checksum

        return datastore.data['watching'][uuid]['previous_md5']

    @app.route("/edit/<string:uuid>", methods=['GET', 'POST'])
    @login_required
    def edit_page(uuid):
        global messages
        import validators

        # More for testing, possible to return the first/only
        if uuid == 'first':
            uuid = list(datastore.data['watching'].keys()).pop()

        if request.method == 'POST':

            url = request.form.get('url').strip()
            tag = request.form.get('tag').strip()

            minutes_recheck = request.form.get('minutes')
            if minutes_recheck:
                minutes = int(minutes_recheck.strip())
                if minutes >= 1:
                    datastore.data['watching'][uuid]['minutes_between_check'] = minutes
                else:
                    messages.append(
                        {'class': 'error', 'message': "Must be atleast 1 minute."})



            # Extra headers
            form_headers = request.form.get('headers').strip().split("\n")
            extra_headers = {}
            if form_headers:
                for header in form_headers:
                    if len(header):
                        parts = header.split(':', 1)
                        if len(parts) == 2:
                            extra_headers.update({parts[0].strip(): parts[1].strip()})

            update_obj = {'url': url,
                          'tag': tag,
                          'headers': extra_headers
                          }

            # Notification URLs
            form_notification_text = request.form.get('notification_urls')
            notification_urls = []
            if form_notification_text:
                for text in form_notification_text.strip().split("\n"):
                    text = text.strip()
                    if len(text):
                        notification_urls.append(text)

            datastore.data['watching'][uuid]['notification_urls'] = notification_urls

            # Ignore text
            form_ignore_text = request.form.get('ignore-text')
            ignore_text = []
            if form_ignore_text:
                for text in form_ignore_text.strip().split("\n"):
                    text = text.strip()
                    if len(text):
                        ignore_text.append(text)

                datastore.data['watching'][uuid]['ignore_text'] = ignore_text

                # Reset the previous_md5 so we process a new snapshot including stripping ignore text.
                if len(datastore.data['watching'][uuid]['history']):
                    update_obj['previous_md5'] = get_current_checksum_include_ignore_text(uuid=uuid)


            # CSS Filter
            css_filter = request.form.get('css_filter')
            if css_filter:
                datastore.data['watching'][uuid]['css_filter'] = css_filter.strip()

                # Reset the previous_md5 so we process a new snapshot including stripping ignore text.
                if len(datastore.data['watching'][uuid]['history']):
                    update_obj['previous_md5'] = get_current_checksum_include_ignore_text(uuid=uuid)


            validators.url(url)  # @todo switch to prop/attr/observer
            datastore.data['watching'][uuid].update(update_obj)
            datastore.needs_write = True

            messages.append({'class': 'ok', 'message': 'Updated watch.'})

            # Queue the watch for immediate recheck
            update_q.put(uuid)

            trigger_n = request.form.get('trigger-test-notification')
            if trigger_n:
                n_object = {'watch_url': url,
                            'notification_urls': notification_urls}
                notification_q.put(n_object)

                messages.append({'class': 'ok', 'message': 'Notifications queued.'})

            return redirect(url_for('index'))

        else:
            output = render_template("edit.html", uuid=uuid, watch=datastore.data['watching'][uuid], messages=messages)

        return output

    @app.route("/settings", methods=['GET', "POST"])
    @login_required
    def settings_page():
        global messages

        if request.method == 'GET':
            if request.values.get('notification-test'):
                url_count = len(datastore.data['settings']['application']['notification_urls'])
                if url_count:
                    import apprise
                    apobj = apprise.Apprise()
                    apobj.debug = True

                    # Add each notification
                    for n in datastore.data['settings']['application']['notification_urls']:
                        apobj.add(n)
                    outcome = apobj.notify(
                        body='Hello from the worlds best and simplest web page change detection and monitoring service!',
                        title='Changedetection.io Notification Test',
                    )

                    if outcome:
                        messages.append(
                            {'class': 'notice', 'message': "{} Notification URLs reached.".format(url_count)})
                    else:
                        messages.append(
                            {'class': 'error', 'message': "One or more Notification URLs failed"})

                return redirect(url_for('settings_page'))

            if request.values.get('removepassword'):
                from pathlib import Path

                datastore.data['settings']['application']['password'] = False
                messages.append({'class': 'notice', 'message': "Password protection removed."})
                flask_login.logout_user()

                return redirect(url_for('settings_page'))

        if request.method == 'POST':

            password = request.values.get('password')
            if password:
                import hashlib
                import base64
                import secrets

                # Make a new salt on every new password and store it with the password
                salt = secrets.token_bytes(32)

                key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
                store = base64.b64encode(salt + key).decode('ascii')
                datastore.data['settings']['application']['password'] = store
                messages.append({'class': 'notice', 'message': "Password protection enabled."})
                flask_login.logout_user()
                return redirect(url_for('index'))

            try:
                minutes = int(request.values.get('minutes').strip())
            except ValueError:
                messages.append({'class': 'error', 'message': "Invalid value given, use an integer."})

            else:
                if minutes >= 1:
                    datastore.data['settings']['requests']['minutes_between_check'] = minutes
                    datastore.needs_write = True
                else:
                    messages.append(
                        {'class': 'error', 'message': "Must be atleast 1 minute."})

            # 'validators' package doesnt work because its often a non-stanadard protocol. :(
            datastore.data['settings']['application']['notification_urls'] = []
            trigger_n = request.form.get('trigger-test-notification')

            for n in request.values.get('notification_urls').strip().split("\n"):
                url = n.strip()
                datastore.data['settings']['application']['notification_urls'].append(url)
                datastore.needs_write = True

            if trigger_n:
                n_object = {'watch_url': "Test from changedetection.io!",
                            'notification_urls': datastore.data['settings']['application']['notification_urls']}
                notification_q.put(n_object)

                messages.append({'class': 'ok', 'message': 'Notifications queued.'})

        output = render_template("settings.html", messages=messages,
                                 minutes=datastore.data['settings']['requests']['minutes_between_check'],
                                 notification_urls="\r\n".join(
                                     datastore.data['settings']['application']['notification_urls']))
        messages = []

        return output

    @app.route("/import", methods=['GET', "POST"])
    @login_required
    def import_page():
        import validators
        global messages
        remaining_urls = []

        good = 0

        if request.method == 'POST':
            urls = request.values.get('urls').split("\n")
            for url in urls:
                url = url.strip()
                if len(url) and validators.url(url):
                    new_uuid = datastore.add_watch(url=url.strip(), tag="")
                    # Straight into the queue.
                    update_q.put(new_uuid)
                    good += 1
                else:
                    if len(url):
                        remaining_urls.append(url)

            messages.append({'class': 'ok', 'message': "{} Imported, {} Skipped.".format(good, len(remaining_urls))})

            if len(remaining_urls) == 0:
                # Looking good, redirect to index.
                return redirect(url_for('index'))

        # Could be some remaining, or we could be on GET
        output = render_template("import.html",
                                 messages=messages,
                                 remaining="\n".join(remaining_urls)
                                 )
        messages = []

        return output

    # Clear all statuses, so we do not see the 'unviewed' class
    @app.route("/api/mark-all-viewed", methods=['GET'])
    @login_required
    def mark_all_viewed():

        # Save the current newest history as the most recently viewed
        for watch_uuid, watch in datastore.data['watching'].items():
            datastore.set_last_viewed(watch_uuid, watch['newest_history_key'])

        messages.append({'class': 'ok', 'message': "Cleared all statuses."})
        return redirect(url_for('index'))

    @app.route("/diff/<string:uuid>", methods=['GET'])
    @login_required
    def diff_history_page(uuid):
        global messages

        # More for testing, possible to return the first/only
        if uuid == 'first':
            uuid = list(datastore.data['watching'].keys()).pop()

        extra_stylesheets = ['/static/styles/diff.css']
        try:
            watch = datastore.data['watching'][uuid]
        except KeyError:
            messages.append({'class': 'error', 'message': "No history found for the specified link, bad link?"})
            return redirect(url_for('index'))

        dates = list(watch['history'].keys())
        # Convert to int, sort and back to str again
        dates = [int(i) for i in dates]
        dates.sort(reverse=True)
        dates = [str(i) for i in dates]

        if len(dates) < 2:
            messages.append(
                {'class': 'error', 'message': "Not enough saved change detection snapshots to produce a report."})
            return redirect(url_for('index'))

        # Save the current newest history as the most recently viewed
        datastore.set_last_viewed(uuid, dates[0])

        newest_file = watch['history'][dates[0]]
        with open(newest_file, 'r') as f:
            newest_version_file_contents = f.read()

        previous_version = request.args.get('previous_version')

        try:
            previous_file = watch['history'][previous_version]
        except KeyError:
            # Not present, use a default value, the second one in the sorted list.
            previous_file = watch['history'][dates[1]]

        with open(previous_file, 'r') as f:
            previous_version_file_contents = f.read()

        output = render_template("diff.html", watch_a=watch,
                                 messages=messages,
                                 newest=newest_version_file_contents,
                                 previous=previous_version_file_contents,
                                 extra_stylesheets=extra_stylesheets,
                                 versions=dates[1:],
                                 newest_version_timestamp=dates[0],
                                 current_previous_version=str(previous_version),
                                 current_diff_url=watch['url'])

        return output

    @app.route("/preview/<string:uuid>", methods=['GET'])
    @login_required
    def preview_page(uuid):
        global messages

        # More for testing, possible to return the first/only
        if uuid == 'first':
            uuid = list(datastore.data['watching'].keys()).pop()

        extra_stylesheets = ['/static/styles/diff.css']

        try:
            watch = datastore.data['watching'][uuid]
        except KeyError:
            messages.append({'class': 'error', 'message': "No history found for the specified link, bad link?"})
            return redirect(url_for('index'))

        print(watch)
        with open(list(watch['history'].values())[-1], 'r') as f:
            content = f.readlines()

        output = render_template("preview.html", content=content, extra_stylesheets=extra_stylesheets)
        return output


    @app.route("/favicon.ico", methods=['GET'])
    def favicon():
        return send_from_directory("/app/static/images", filename="favicon.ico")

    # We're good but backups are even better!
    @app.route("/backup", methods=['GET'])
    @login_required
    def get_backup():

        import zipfile
        from pathlib import Path

        # Remove any existing backup file, for now we just keep one file
        for previous_backup_filename in Path(app.config['datastore_path']).rglob('changedetection-backup-*.zip'):
            os.unlink(previous_backup_filename)

        # create a ZipFile object
        backupname = "changedetection-backup-{}.zip".format(int(time.time()))

        # We only care about UUIDS from the current index file
        uuids = list(datastore.data['watching'].keys())
        backup_filepath = os.path.join(app.config['datastore_path'], backupname)

        with zipfile.ZipFile(backup_filepath, "w",
                             compression=zipfile.ZIP_DEFLATED,
                             compresslevel=8) as zipObj:

            # Be sure we're written fresh
            datastore.sync_to_json()

            # Add the index
            zipObj.write(os.path.join(app.config['datastore_path'], "url-watches.json"), arcname="url-watches.json")

            # Add the flask app secret
            zipObj.write(os.path.join(app.config['datastore_path'], "secret.txt"), arcname="secret.txt")

            # Add any snapshot data we find, use the full path to access the file, but make the file 'relative' in the Zip.
            for txt_file_path in Path(app.config['datastore_path']).rglob('*.txt'):
                parent_p = txt_file_path.parent
                if parent_p.name in uuids:
                    zipObj.write(txt_file_path,
                                 arcname=str(txt_file_path).replace(app.config['datastore_path'], ''),
                                 compress_type=zipfile.ZIP_DEFLATED,
                                 compresslevel=8)

            # Create a list file with just the URLs, so it's easier to port somewhere else in the future
            list_file = os.path.join(app.config['datastore_path'], "url-list.txt")
            with open(list_file, "w") as f:
                for uuid in datastore.data['watching']:
                    url = datastore.data['watching'][uuid]['url']
                    f.write("{}\r\n".format(url))

            # Add it to the Zip
            zipObj.write(list_file,
                         arcname="url-list.txt",
                         compress_type=zipfile.ZIP_DEFLATED,
                         compresslevel=8)

        return send_from_directory(app.config['datastore_path'], backupname, as_attachment=True)

    @app.route("/static/<string:group>/<string:filename>", methods=['GET'])
    def static_content(group, filename):
        # These files should be in our subdirectory
        full_path = os.path.realpath(__file__)
        p = os.path.dirname(full_path)

        try:
            return send_from_directory("{}/static/{}".format(p, group), filename=filename)
        except FileNotFoundError:
            abort(404)

    @app.route("/api/add", methods=['POST'])
    @login_required
    def api_watch_add():
        global messages

        url = request.form.get('url').strip()
        if datastore.url_exists(url):
            messages.append({'class': 'error', 'message': 'The URL {} already exists'.format(url)})
            return redirect(url_for('index'))

        # @todo add_watch should throw a custom Exception for validation etc
        new_uuid = datastore.add_watch(url=url, tag=request.form.get('tag').strip())
        # Straight into the queue.
        update_q.put(new_uuid)

        messages.append({'class': 'ok', 'message': 'Watch added.'})
        return redirect(url_for('index'))

    @app.route("/api/delete", methods=['GET'])
    @login_required
    def api_delete():
        global messages
        uuid = request.args.get('uuid')
        datastore.delete(uuid)
        messages.append({'class': 'ok', 'message': 'Deleted.'})

        return redirect(url_for('index'))

    @app.route("/api/checknow", methods=['GET'])
    @login_required
    def api_watch_checknow():

        global messages

        tag = request.args.get('tag')
        uuid = request.args.get('uuid')
        i = 0

        running_uuids = []
        for t in running_update_threads:
            running_uuids.append(t.current_uuid)

        # @todo check thread is running and skip

        if uuid:
            if uuid not in running_uuids:
                update_q.put(uuid)
            i = 1

        elif tag != None:
            # Items that have this current tag
            for watch_uuid, watch in datastore.data['watching'].items():
                if (tag != None and tag in watch['tag']):
                    if watch_uuid not in running_uuids and not datastore.data['watching'][watch_uuid]['paused']:
                        update_q.put(watch_uuid)
                        i += 1

        else:
            # No tag, no uuid, add everything.
            for watch_uuid, watch in datastore.data['watching'].items():

                if watch_uuid not in running_uuids and not datastore.data['watching'][watch_uuid]['paused']:
                    update_q.put(watch_uuid)
                    i += 1

        messages.append({'class': 'ok', 'message': "{} watches are rechecking.".format(i)})
        return redirect(url_for('index', tag=tag))

    # @todo handle ctrl break
    ticker_thread = threading.Thread(target=ticker_thread_check_time_launch_checks).start()

    threading.Thread(target=notification_runner).start()

    # Check for new release version
    threading.Thread(target=check_for_new_version).start()
    return app


# Check for new version and anonymous stats
def check_for_new_version():
    import requests

    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    while not app.config.exit.is_set():
        try:
            r = requests.post("https://changedetection.io/check-ver.php",
                              data={'version': datastore.data['version_tag'],
                                    'app_guid': datastore.data['app_guid']},

                              verify=False)
        except:
            pass

        try:
            if "new_version" in r.text:
                app.config['NEW_VERSION_AVAILABLE'] = True
        except:
            pass

        # Check daily
        app.config.exit.wait(86400)

def notification_runner():

    while not app.config.exit.is_set():
        try:
            # At the moment only one thread runs (single runner)
            n_object = notification_q.get(block=False)
        except queue.Empty:
            time.sleep(1)
            pass

        else:
            import apprise

            # Create an Apprise instance
            try:
                apobj = apprise.Apprise()
                for url in n_object['notification_urls']:
                    apobj.add(url.strip())

                n_body = n_object['watch_url']

                # 65 - Append URL of instance to the notification if it is set.
                base_url = os.getenv('BASE_URL')
                if base_url != None:
                    n_body += "\n" + base_url

                apobj.notify(
                    body=n_body,
                    # @todo This should be configurable.
                    title="ChangeDetection.io Notification - {}".format(n_object['watch_url'])
                )

            except Exception as e:
                print("Watch URL: {}  Error {}".format(n_object['watch_url'],e))


# Thread runner to check every minute, look for new watches to feed into the Queue.
def ticker_thread_check_time_launch_checks():
    from backend import update_worker

    # Spin up Workers.
    for _ in range(datastore.data['settings']['requests']['workers']):
        new_worker = update_worker.update_worker(update_q, notification_q, app, datastore)
        running_update_threads.append(new_worker)
        new_worker.start()

    while not app.config.exit.is_set():

        # Get a list of watches by UUID that are currently fetching data
        running_uuids = []
        for t in running_update_threads:
            if t.current_uuid:
                running_uuids.append(t.current_uuid)

        # Check for watches outside of the time threshold to put in the thread queue.
        for uuid, watch in datastore.data['watching'].items():

            # If they supplied an individual entry minutes to threshold.
            if 'minutes_between_check' in watch:
                max_time = watch['minutes_between_check'] * 60
            else:
                # Default system wide.
                max_time = datastore.data['settings']['requests']['minutes_between_check'] * 60

            threshold = time.time() - max_time

            # Yeah, put it in the queue, it's more than time.
            if not watch['paused'] and watch['last_checked'] <= threshold:
                if not uuid in running_uuids and uuid not in update_q.queue:
                    update_q.put(uuid)

        # Wait a few seconds before checking the list again
        time.sleep(3)

        # Should be low so we can break this out in testing
        app.config.exit.wait(1)
