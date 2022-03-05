from typing import Dict

from calibre_plugins.evilflowers.config import prefs
from calibre_plugins.evilflowers.client.requestor import Requestor


class ApiClient:
    def __init__(self, base_plugin):
        self._plugin = base_plugin
        self._requestor = Requestor(base_plugin)

    def _url(self, url: str) -> str:
        return f"{prefs['base_url']}{url}"

    def me(self) -> Dict:
        return self._requestor.get(
            self._url('/api/v1/users/me')
        )['response']

    def status(self) -> Dict:
        return self._requestor.get(
            self._url('/api/v1/status')
        )['response']
