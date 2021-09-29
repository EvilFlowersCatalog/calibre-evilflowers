import urllib


class OpdsClient(object):
    def __init__(self, base_plugin, api_url: str, token: str):
        self._plugin = base_plugin
        self._api_url = api_url
        self._token = token
        version = f"{self._plugin.version[0]}.{self._plugin.version[1]}.{self._plugin.version[2]}"
        self._user_agent = f"evilflowers_calibre/{version}"
    
    def complete(self):
        url = f"{self._api_url}/"
