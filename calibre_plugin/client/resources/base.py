from abc import ABC
from typing import Optional, Dict
from urllib.parse import urlencode

from calibre_plugins.evilflowers.config import prefs
from calibre_plugins.evilflowers.client.requestor import Requestor


class BaseResource(ABC):
    def __init__(self, requestor: Requestor):
        self._requestor = requestor

    def _url(self, url: str, params: Optional[Dict] = None) -> str:
        result = f"{prefs['base_url']}{url}"
        if params:
            result = f"{result}?{urlencode(params)}"
        return result
