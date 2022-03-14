from typing import List, Dict

from calibre_plugins.evilflowers.client.resources.base import BaseResource


class EntryResource(BaseResource):
    def list(self) -> List[Dict]:
        params = {
            'paginate': 'false'
        }

        return self._requestor.get(
            self._url('/api/v1/entries', params)
        )['items']

