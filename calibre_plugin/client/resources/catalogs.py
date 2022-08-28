from typing import List, Dict
from uuid import UUID

from calibre_plugins.evilflowers.client.resources.base import BaseResource


class CatalogResource(BaseResource):
    def list(self) -> List[Dict]:
        params = {
            'paginate': 'false',
        }

        return self._requestor.get(
            self._url('/api/v1/catalogs', params)
        )['items']

    def detail(self, catalog_id: UUID) -> Dict:
        return self._requestor.get(
            self._url(f'/api/v1/catalogs/{catalog_id}')
        )['result']
