from typing import Dict

from calibre_plugins.evilflowers.client.resources.base import BaseResource


class UserResource(BaseResource):
    def me(self) -> Dict:
        return self._requestor.get(
            self._url('/api/v1/users/me')
        )['response']
