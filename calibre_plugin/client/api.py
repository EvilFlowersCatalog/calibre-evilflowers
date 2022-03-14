from calibre_plugins.evilflowers.client.requestor import Requestor
from calibre_plugins.evilflowers.client.resources.users import UserResource
from calibre_plugins.evilflowers.client.resources.status import StatusResource
from calibre_plugins.evilflowers.client.resources.entries import EntryResource


class ApiClient:
    def __init__(self, base_plugin):
        self._plugin = base_plugin
        self._requestor = Requestor(base_plugin)

        self._users = UserResource(self._requestor)
        self._entries = EntryResource(self._requestor)
        self._status = StatusResource(self._requestor)

    @property
    def users(self) -> UserResource:
        return self._users

    @property
    def entries(self) -> EntryResource:
        return self._entries

    @property
    def status(self) -> StatusResource:
        return self._status
