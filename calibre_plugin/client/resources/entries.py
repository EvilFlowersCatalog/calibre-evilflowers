from typing import List, Dict
from uuid import UUID

from calibre_plugins.evilflowers.client.resources.base import BaseResource


class EntryResource(BaseResource):
    def list(self, catalog_id: str) -> List[Dict]:
        params = {
            'paginate': 'false',
            'catalog_id': catalog_id
        }

        return self._requestor.get(
            self._url('/api/v1/entries', params)
        )['items']

    def detail(self, catalog_id: UUID, entry_id: UUID) -> Dict:
        return self._requestor.get(
            self._url(f'/api/v1/catalogs/{catalog_id}/entries/{entry_id}')
        )['result']

    def update(self, catalog_id: UUID, entry_id: UUID, payload: Dict):
        return self._requestor.put(
            self._url(f'/api/v1/catalogs/{catalog_id}/entries/{entry_id}'),
            payload
        )['result']

    def create(self, catalog_id: UUID, payload: Dict):
        return self._requestor.post(
            self._url(f'/api/v1/catalogs/{catalog_id}/entries'),
            payload
        )['result']
