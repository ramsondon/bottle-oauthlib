import bottle
from bottle import HTTPError
import functools
import logging
import json
from oauthlib.common import add_params_to_uri
from oauthlib.oauth2 import FatalClientError
from oauthlib.oauth2 import OAuth2Error
import requests
import sys


log = logging.getLogger(__name__)


def extract_params(bottle_request):
    """Extract bottle request informations to oauthlib implementation.
    HTTP Authentication Basic is read but overloaded by payload, if any.

    returns tuple of :
    - url
    - method
    - body (or dict)
    - headers (dict)
    """

    # this returns (None, None) for Bearer Token.
    username, password = bottle_request.auth if bottle_request.auth else (None, None)

    if "application/x-www-form-urlencoded" in bottle_request.content_type:
        client = {}
        if username is not None:
            client["client_id"] = username
        if password is not None:
            client["client_secret"] = password
        return \
            bottle_request.url, \
            bottle_request.method, \
            dict(client, **bottle_request.forms), \
            dict(bottle_request.headers)

    basic_auth = {}
    if username is not None:
        basic_auth = {
            "Authorization": requests.auth._basic_auth_str(username, password)
        }
    return \
        bottle_request.url, \
        bottle_request.method, \
        bottle_request.body, \
        dict(bottle_request.headers, **basic_auth)


def add_params_to_request(bottle_request, params):
    try:
        bottle_request.oauth
    except AttributeError:
        bottle_request.oauth = {}
    if params:
        for k, v in params.items():
            bottle_request.oauth[k] = v


def set_response(bottle_request, bottle_response, status, headers, body, force_json=False):
    """Set status/headers/body into bottle_response.

    Headers is a dict
    Body is ideally a JSON string (not dict).
    """
    if not isinstance(headers, dict):
        raise TypeError("a dict-like object is required, not {0}".format(type(headers)))

    bottle_response.status = status
    for k, v in headers.items():
        bottle_response.headers[k] = v

    """Determine if response should be in json or not, based on request:
    OAuth2.0 RFC recommands json, but older clients use form-urlencoded.

    Note also that force_json can be set to be compliant with specific
    endpoints like introspect, which always returns json.

    Examples:
    rauth: send Accept:*/* but work only with response in form-urlencoded.
    requests-oauthlib: send Accept:application/json but work with both
    responses types.
    """
    if not body:
        return

    if not isinstance(body, str):
        raise TypeError("a str-like object is required, not {0}".format(type(body)))

    try:
        values = json.loads(body)
    except json.decoder.JSONDecodeError:
        # consider body as string but not JSON, we stop here.
        bottle_response.body = body
    else:  # consider body as JSON
        # request want a json as response
        if force_json is True or (
                "Accept" in bottle_request.headers and
                "application/json" == bottle_request.headers["Accept"]):
            bottle_response["Content-Type"] = "application/json;charset=UTF-8"
            bottle_response.body = body
        else:
            from urllib.parse import quote

            bottle_response["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
            bottle_response.body = "&".join([
                "{0}={1}".format(
                    quote(k) if isinstance(k, str) else k,
                    quote(v) if isinstance(v, str) else v
                ) for k, v in values.items()
            ])


class BottleOAuth2(object):
    def __init__(self, bottle_server):
        self._bottle = bottle_server
        self._oauthlib = None

    def initialize(self, oauthlib_server):
        self._oauthlib = oauthlib_server

    def create_token_response(self, credentials=None):
        def decorator(f):
            @functools.wraps(f)
            def wrapper():
                assert self._oauthlib, "BottleOAuth2 not initialized with OAuthLib"

                # Get any additional creds
                try:
                    credentials_extra = credentials(bottle.request)
                except TypeError:
                    credentials_extra = credentials
                uri, http_method, body, headers = extract_params(bottle.request)
                headers, body, status = self._oauthlib.create_token_response(
                    uri, http_method, body, headers, credentials_extra
                )
                set_response(bottle.request, bottle.response, status, headers, body)
                func_response = f()
                if not func_response:
                    return bottle.response
                return func_response
            return wrapper
        return decorator

    def verify_request(self, scopes=None):
        def decorator(f):
            @functools.wraps(f)
            def wrapper():
                assert self._oauthlib, "BottleOAuth2 not initialized with OAuthLib"

                # Get the list of scopes
                try:
                    scopes_list = scopes(bottle.request)
                except TypeError:
                    scopes_list = scopes

                uri, http_method, body, headers = extract_params(bottle.request)
                valid, r = self._oauthlib.verify_request(uri, http_method, body, headers, scopes_list)

                # For convenient parameter access in the view
                add_params_to_request(bottle.request, {
                    'client': r.client,
                    'user': r.user,
                    'scopes': r.scopes
                })
                if valid:
                    return f()
                else:
                    # Framework specific HTTP 403
                    return HTTPError(403, "Permission denied")
            return wrapper
        return decorator

    def create_introspect_response(self):
        def decorator(f):
            @functools.wraps(f)
            def wrapper():
                assert self._oauthlib, "BottleOAuth2 not initialized with OAuthLib"

                uri, http_method, body, headers = extract_params(bottle.request)
                headers, body, status = self._oauthlib.create_introspect_response(
                    uri,
                    http_method,
                    body,
                    headers
                )
                set_response(bottle.request, bottle.response, status, headers,
                             body, force_json=True)
                func_response = f()
                if not func_response:
                    return bottle.response
                return func_response
            return wrapper
        return decorator

    def create_authorization_response(self):
        def decorator(f):
            @functools.wraps(f)
            def wrapper():
                assert self._oauthlib, "BottleOAuth2 not initialized with OAuthLib"

                uri, http_method, body, headers = extract_params(bottle.request)
                scope = bottle.request.params.get('scope', '').split(' ')

                resp_headers, resp_body, resp_status = self._oauthlib.create_authorization_response(
                    uri, http_method=http_method, body=body, headers=headers, scopes=scope
                )
                set_response(bottle.request, bottle.response, resp_status, resp_headers, resp_body)

                func_response = f()
                if func_response:
                    return func_response
                return bottle.response
            return wrapper
        return decorator
