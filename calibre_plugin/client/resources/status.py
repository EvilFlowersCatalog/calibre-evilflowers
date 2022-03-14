from typing import Dict

from calibre_plugins.evilflowers.client.resources.base import BaseResource


class StatusResource(BaseResource):
    def status(self) -> Dict:
        return self._requestor.get(
            self._url('/api/v1/status')
        )['response']
