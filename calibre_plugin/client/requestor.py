import json
import urllib.request
import urllib.error
from enum import Enum
from http import HTTPStatus
from typing import Dict, Optional

from calibre_plugins.evilflowers.config import prefs


class RequestError(Exception):
    pass


class Requestor:
    class Method(Enum):
        GET = 'GET'
        POST = 'POST'
        PUT = 'PUT'
        DELETE = 'DELETE'

    def __init__(self, base_plugin):
        self._plugin = base_plugin

    def get(self, url: str):
        return self._perform(self.Method.GET, url)

    def post(self, url: str, payload: Dict):
        return self._perform(self.Method.POST, url, payload)

    def put(self, url: str, payload: Dict):
        return self._perform(self.Method.PUT, url, payload)

    def delete(self, url: str):
        return self._perform(self.Method.DELETE, url)

    def _perform(self, method: Method, url: str, payload: Optional[Dict] = None) -> Optional[Dict]:
        result = None
        req = urllib.request.Request(
            url=url,
            data=bytes(json.dumps(payload).encode('utf-8')),
            method=method.value
        )

        version = f"{self._plugin.version[0]}.{self._plugin.version[1]}.{self._plugin.version[2]}"
        req.add_header("User-Agent", f"evilflowers_calibre/{version}")

        if prefs.get('api_key'):
            req.add_header("Authorization", f"Bearer {prefs['api_key']}")

        if payload:
            req.add_header("Content-type", "application/json; charset=UTF-8")
            req.data = bytes(json.dumps(payload).encode('utf-8'))

        try:
            response = urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            status_code = HTTPStatus(e.code)
            if e.headers.get('Content-Type') == 'application/json':
                content = json.loads(e.read().decode())
                raise RequestError(content['title'], status_code.value)
            else:
                raise RequestError(e.reason, status_code.value)

        except urllib.error.URLError as e:
            raise RequestError(e.reason)
        else:
            if response.headers.get('Content-Type') != 'application/json':
                raise RequestError("Invalid content type")

        content = response.read().decode()

        if content:
            result = json.loads(content)

        return result
