#!/usr/bin/python2 -tt
# -*- coding: utf-8 -*-
#
# Copyright (C) 2013  Red Hat, Inc.
# This file is part of python-fedora
#
# python-fedora is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# python-fedora is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with python-fedora; if not, see <http://www.gnu.org/licenses/>
#

"""Base client for application relying on OpenID for authentication.

.. moduleauthor:: Pierre-Yves Chibon <pingou@fedoraproject.org>
.. moduleauthor:: Toshio Kuratomi <toshio@fedoraproject.org>

.. versionadded: 0.3.33

"""

# Deps:
# python-beautifulsoup4
# python-requests

# Need a credential store.
# In the store, we'll have:
# A cookie for the openid server
# A cookie per service

# We play only one part here: the part of the browser.  the openid consumer is
# the service we're actually interested in connecting to.  The openid producer
# is the openid server.


import logging
import os
import sqlite3
from collections import defaultdict
from functools import partial, wraps

# :F0401: Unable to import : Disabled because these will either import on py3
#   or py2 not both.
# :E0611: No name $X in module: This was renamed in python3

# pylint: disable-msg=F0401
try:
    # pylint: disable-msg=E0611
    # Python 3
    from urllib.parse import urljoin
except ImportError:
    # Python 2
    from urlparse import urljoin
# pylint: enable-msg=F0401,E0611

import bs4
import requests

from fedora import __version__

log = logging.getLogger(__name__)

b_SESSION_DIR = path.join(path.expanduser('~'), '.fedora')
b_SESSION_FILE = path.join(b_SESSION_DIR, 'baseclient-sessions.sqlite')

def _parse_service_form(response):
    """ Retrieve the attributes from the html form. """
    parsed = bs4.BeautifulSoup(response.text)
    inputs = {}
    for child in parsed.form.find_all(name='input'):
        if child.attrs['type'] == 'submit':
            continue
        inputs[child.attrs['name']] = child.attrs['value']
    return (parsed.form.attrs['action'], inputs)


def _parse_openid_login_form(response):
    """ Parse the OpenID login html form. """
    parsed = bs4.BeautifulSoup(response.text)
    inputs = {}
    for child in parsed.form.find_all(name='input'):
        if 'type' in child.attrs and child.attrs['type'] == 'submit':
            if not ('name' in child.attrs and
                    child.attrs['name'].startswith('decided_')):
                continue
        if 'value' in child.attrs:
            value = child.attrs['value']
        else:
            value = None
        inputs[child.attrs['name']] = value
    return (parsed.form.attrs['action'], inputs)


def absolute_url(beginning, end):
    """ Join two urls parts if the last part does not start with the first
    part specified """
    if not end.startswith(beginning):
        end = urljoin(beginning, end)
    return end


class LoginRequiredException(Exception):

    """ Exception raised when the call requires a logged-in user. """

    pass


def requires_login(func):
    """ Decorator function for get or post requests requiring login. """
    def _decorator(request, *args, **kwargs):
        """ Run the function and check if it redirected to the openid form.
        """
        output = func(request, *args, **kwargs)
        if output and \
                '<title>OpenID transaction in progress</title>' in output.text:
            raise LoginRequiredException(
                '{0} requires a logged in user'.format(output.url))
        return output
    return wraps(func)(_decorator)


class OpenIdBaseClient(OpenIdProxyClient):

    """ A client for interacting with web services relying on openid auth. """

    def __init__(self, base_url, login_url=None, useragent=None, debug=False,
                 insecure=False, username=None, httpauth=None,
                 session_id=None, session_name='tg-visit',
                 openid_session_id=None, openid_session_name='FAS_OPENID',
                 cache_session=True, retries=None, timeout=None):
        """Client for interacting with web services relying on fas_openid auth.

        :arg base_url: Base of every URL used to contact the server
        :kwarg login_url: The url to the login endpoint of the application.
            If none are specified, it uses the default `/login`.
        :kwarg useragent: Useragent string to use.  If not given, default to
            "Fedora OpenIdBaseClient/VERSION"
        :kwarg debug: If True, log debug information
        :kwarg insecure: If True, do not check server certificates against
            their CA's.  This means that man-in-the-middle attacks are
            possible against the `BaseClient`. You might turn this option on
            for testing against a local version of a server with a self-signed
            certificate but it should be off in production.
        :kwarg username: Username for establishing authenticated connections
        :kwarg session_id: id of the user's session
        :kwarg session_name: name of the cookie to use with session handling
        :kwarg openid_session_id: id of the user's openid session
        :kwarg openid_session_name: name of the cookie to use with openid
            session handling
        :kwarg cache_session: If set to true, cache the user's session data on
            the filesystem between runs
        :kwarg retries: if we get an unknown or possibly transient error from
            the server, retry this many times.  Setting this to a negative
            number makes it try forever.  Defaults to zero, no retries.
        :kwarg timeout: A float describing the timeout of the connection. The
            timeout only affects the connection process itself, not the
            downloading of the response body. Defaults to 120 seconds.

        """

        # These are also needed by OpenIdProxyClient
        self.useragent = useragent or 'Fedora BaseClient/%(version)s' % {
            'version': __version__}
        self.base_url = base_url
        self.login_url = login_url or urljoin(self.base_url, '/login')
        self.debug = debug
        self.insecure = insecure
        self.retries = retries
        self.timeout = timeout
        self.session_name = session_name
        self.openid_session_name = openid_session_name

        # These are specific to OpenIdBaseClient
        self.username = username
        self.cache_session = cache_session

        # Make sure the database for storing the session cookies exists
        if cache_session:
            self._db = self._initialize_session_cache()
            if not self._db:
                self.cache_session = False

        # Session cookie that identifies this user to the application
        self._session_id_map = defaultdict(str)
        if session_id:
            self.session_id = session_id
        if openid_session_id:
            self.openid_session_id = openid_session_id

        # python-requests session.  Holds onto cookies
        self._session = requests.session()

    def _initialize_session_cache(self):
        # Note -- fallback to returning None on any problems as this isn't
        # critical.  It just makes it so that we don't have to ask the user
        # for their password over and over.
        if not os.path.isdir(b_SESSION_DIR):
            try:
                os.makedirs(b_SESSION_DIR, mode=0o755)
            except OSError as e:
                log.warning('Unable to create {file}: {error}').format(
                    file=b_SESSION_DIR, error=e)
                return None

        if not os.path.exists(b_SESSION_FILE):
            db = sqlite3.connect(b_SESSION_FILE)
            cursor = db.cursor()
            try:
                cursor.execute('create table sessions (username text,'
                               ' base_url text, session_id text,'
                               ' primary key (username, base_url))')
            except sqlite3.DatabaseError as e:
                # Probably not a database
                log.warning('Unable to initialize {file}: {error}'.format(
                    file=b_SESSION_FILE, error=e))
                return None
            db.commit()
        else:
            try:
                db = sqlite3.connect(b_SESSION_FILE)
            except sqlite3.OperationalError as e:
                # Likely permission denied
                log.warning('Unable to connect to {file}: {error}'.format(
                    file=b_SESSION_FILE, error=e))
                return None
        return db

    def _get_id(self, base_url=None):
        # base_url is only sent as a param if we're looking for the openid
        # session
        base_url = base_url or self.base_url

        username = self.username or ''

        session_id_key = '{}:{}'.format(base_url, username)
        if self._session_id_map[session_id_key]:
            # Cached in memory
            return self._session_id_map[session_id_key]

        if username and self.cache_session:
            # Look for a session on disk
            cursor = self._db.cursor()
            cursor.execute('select * from sessions where'
                        ' username = ? and base_url = ?',
                        (username, base_url))
            found_sessions = cursor.fetchall()
            if found_sessions:
                 self._session_id_map[session_id_key] = found_sessions[0]

        if not self._session_id_map[session_id_key]:
            log.debug('No session cached for "{username}"'.format(
                username=self.username)
        return self._session_id_map[session_id_key]

    def _set_id(self, session_id, base_url=None):
        if not self.username:
        # base_url is only sent as a param if we're looking for the openid
        # session
        base_url = base_url or self.base_url

        username = self.username or ''

        # Cache in memory
        session_id_key = '{}:{}'.format(base_url, username)
        self._session_id_map[session_id_key] = session_id

        if username and self.cache_session:
            # Save to disk as well
            cursor = self._db.cursor()
            try:
                cursor.exectue('insert into sessions'
                               ' (session_id, username, base_url)'
                               ' values (?, ?, ?)',
                               (session_id, username, base_url))
            except sqlite3.IntegrityError:
                # Record already exists for that username and url
                cursor.execute('update sessions set session_id = ? where'
                               ' username = ? and base_url = ?',
                               (session_id, username, base_url))
            db.commit()

    def _del_id(self, base_url=None):
        # base_url is only sent as a param if we're looking for the openid
        # session
        base_url = base_url or self.base_url

        username = self.username or ''

        # Remove from the in-memory cache
        session_id_key = '{}:{}'.format(base_url, username)
        del self._session_id_map[session_id_key]

        if username and self.cache_session:
            # Remove from the disk cache as well
            cursor = self._db.cursor()
            cursor.execute('delete from sessions where'
                           ' username = ? and base_url = ?',
                           (username, base_url))

    session_id = property(_get_id, _set_id, _del_id,
        """The session_id.

        The session id is saved in a file in case it is needed in consecutive
        runs of BaseClient.
        """)

    openid_session_id = property(functools.partial(_get_id, base_url='FAS_OPENID'),
                                 functools.partial(_set_id, base_url='FAS_OPENID'),
                                 functools.partial(_del_id, base_url='FAS_OPENID'),
        """The openid_session_id.

        The openid session id is saved in a file in case it is needed in consecutive
        runs of BaseClient.
        """)

    def send_request(self, method, auth=False, verb='POST', **kwargs):
        """Make an HTTP request to a server method.

        The given method is called with any parameters set in req_params.  If
        auth is True, then the request is made with an authenticated session
        cookie.

        :arg method: Method to call on the server.  It's a url fragment that
            comes after the :attr:`base_url` set in :meth:`__init__`.
        :kwarg retries: if we get an unknown or possibly transient error from
            the server, retry this many times.  Setting this to a negative
            number makes it try forever.  Default to use the :attr:`retries`
            value set on the instance or in :meth:`__init__` (which defaults
            to zero, no retries).
        :kwarg timeout: A float describing the timeout of the connection. The
            timeout only affects the connection process itself, not the
            downloading of the response body. Default to use the
            :attr:`timeout` value set on the instance or in :meth:`__init__`
            (which defaults to 120s).
        :kwarg auth: If True perform auth to the server, else do not.
        :kwarg req_params: Extra parameters to send to the server.
        :kwarg file_params: dict of files where the key is the name of the
            file field used in the remote method and the value is the local
            path of the file to be uploaded.  If you want to pass multiple
            files to a single file field, pass the paths as a list of paths.
        :kwarg verb: HTTP verb to use.  GET and POST are currently supported.
            POST is the default.
        """
        # Decide on the set of auth cookies to use

        self._authed_verb_dispatcher = {(False, 'POST'): self.session.post,
                                        (False, 'GET'): self.session.get,
                                        (True, 'POST'): self._authed_post,
                                        (True, 'GET'): self._authed_get}
        if auth:
            auth_params = {'session_id': self.session_id,
                           'openid_session_id': self.openid_session_id}
            try:
                self._authed_verb_dispatcher[(auth, verb)](method, auth_params=auth_params, **kwargs)
            except KeyError:
                raise Exception('Unknown HTTP verb')
            except LoginRequiredException:
                raise AuthError()

    def login(self, username, password, otp=None):
        """ Open a session for the user.

        Log in the user with the specified username and password
        against the FAS OpenID server.

        :arg username: the FAS username of the user that wants to log in
        :arg password: the FAS password of the user that wants to log in
        :kwarg otp: currently unused.  Eventually a way to send an otp to the
            API that the API can use.

        """
        # Requests session needed to persist the cookies that are created
        # during redirects on the openid provider
        # Log into the service
        response = self.session.get(self.login_url)
        if not '<title>OpenID transaction in progress</title>' \
                in response.text:
            return
        # requests.session should hold onto this for us....
        openid_url, data = _parse_service_form(response)

        # Contact openid provider
        response = self.session.post(openid_url, data)
        action, data = _parse_openid_login_form(response)

        action = absolute_url(openid_url, action)
        if 'username' in data:
            # Not logged into openid
            data['username'] = username
            data['password'] = password
            response = self.session.post(action, data)
            action, data = _parse_openid_login_form(response)
            action = absolute_url(openid_url, action)

        # Verify that we want the openid server to send the requested data
        # (Only return decided_allow)
        del(data['decided_deny'])
        response = self.session.post(action, data)

        service_url, data = _parse_service_form(response)
        response = self.session.post(service_url, data)
        return response



class OpenIdProxyClient(object):
    def __init__(self):
        pass







    @requires_login
    def _authed_post(self, url, params=None, data=None):
        """ Return the request object of a post query."""
        response = self.session.post(url, params=params, data=data)
        return response

    @requires_login
    def _authed_get(self, url, params=None, data=None):
        """ Return the request object of a get query."""
        response = self.session.get(url, params=params, data=data)
        return response

    pass
    def test(self):
        """ Dummy action - only exists locally."""
        url = urljoin(BASE_URL, '/api/collection/f20/status/')
        response = self._authed_post(url,
                                   data={'collection_branchname': 'f20',
                                         'collection_status': 'EOL'})
        return response.text

if __name__ == '__main__':

    # BaseClient API => send_request(auth=True); ie: programmer specifies
    # which calls to the service API need authentication at call time.
    #
    # OpenIdBaseClient => @requires_login decorator; ie: programmer specifies
    # which functions require authentication at function definition time.


    # here's what we want the end user to do for an authentication-needed
    # request:
    #
    # 1) Give username to the API
    # 2) try action
    # 3) If fails, get password and five to API
    # 4) try action again
    #
    # Here's what the library needs to do for each step
    # 1) Give username to API
    #   A) BaseClient saves Username into an instance variable
    # 2) Try action
    #   A) BaseClient looks for saved service cookies.
    #   B) If no cookie, try action. return AuthError if service says auth
    #      required
    #   C) If cookie, try action.  If success return response
    #   D) If failure, look for saved openid cookie
    #   E) If no cookie. return AuthError
    #   F) If cookie, try logging into service via openid cookie
    #   G) If openid cookie expired: return AuthError
    #   H) If successful login via openid, get service cookie
    #   I) Save service cookie
    #   J) Try action; return action response
    # 3) Give password to the API
    #   A) BaseClient logs in to the service with password
    #   B) Save openid cookie
    #   C) Save service cookie
    # 4) Try action again -- same as 2) but should end in success

    # Try the action, remembering to send the session cookie for the service

    import getpass

    PKGDB = BaseOpenIdClient(BASE_URL)

    # If that fails, login
    FAS_NAME = raw_input('Username: ')
    FAS_PASS = getpass.getpass('FAS password: ')
    try:
        print PKGDB.test()
    except LoginRequiredException, err:
        print err.message
    print PKGDB.login(FAS_NAME, FAS_PASS)
    # Retry the action
    print PKGDB.login(FAS_NAME, FAS_PASS)
    print PKGDB.test()
