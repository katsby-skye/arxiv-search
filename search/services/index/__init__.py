"""
Provides integration with an ElasticSearch cluster.

The primary entrypoint to this module is :func:`.search`, which handles
:class:`search.domain.Query` instances passed by controllers, and returns a
:class:`.DocumentSet` containing search results. :func:`.get_document` is
available for future use, e.g. as part of a search API.

In addition, :func:`.add_document` and :func:`.bulk_add_documents` are provided
for indexing (e.g. by the
:mod:`search.agent.consumer.MetadataRecordProcessor`).

:class:`.SearchSession` encapsulates configuration parameters and a connection
to the Elasticsearch cluster for thread-safety. The functions mentioned above
load the appropriate instance of :class:`.SearchSession` depending on the
context of the request.
"""

import json
import urllib3
from contextlib import contextmanager
from typing import Any, Optional, Tuple, Union, List, Generator
from functools import reduce, wraps
from operator import ior
from elasticsearch import Elasticsearch, ElasticsearchException, \
                          SerializationError, TransportError, NotFoundError, \
                          helpers
from elasticsearch.connection import Urllib3HttpConnection
from elasticsearch.helpers import BulkIndexError

from elasticsearch_dsl import Search, Q

from search.context import get_application_config, get_application_global
from arxiv.base import logging
from search.domain import Document, DocumentSet, Query, AdvancedQuery, \
    SimpleQuery, asdict

from .exceptions import QueryError, IndexConnectionError, DocumentNotFound, \
    IndexingError, OutsideAllowedRange, MappingError
from .util import MAX_RESULTS

from . import prepare, results

logger = logging.getLogger(__name__)

# Disable the Elasticsearch logger. When enabled, the Elasticsearch logger
# dumps entire Tracebacks prior to propagating exceptions. Thus we end up with
# tracebacks in the logs even for handled exceptions.
logging.getLogger('elasticsearch').disabled = True


ALL_SEARCH_FIELDS = ['author', 'title', 'abstract', 'comments', 'journal_ref',
                     'acm_class', 'msc_class', 'report_num', 'paper_id', 'doi',
                     'orcid', 'author_id']


@contextmanager
def handle_es_exceptions() -> Generator:
    """Handle common ElasticSearch-related exceptions."""
    try:
        yield
    except NotFoundError as e:
        raise DocumentNotFound('No such document') from e
    except TransportError as e:
        logger.error(e.error)
        if e.error == 'resource_already_exists_exception':
            logger.debug('Index already exists; move along')
            return
        elif e.error == 'mapper_parsing_exception':
            logger.error('Invalid document mapping; create index failed')
            logger.debug(str(e.info))
            raise MappingError('Invalid mapping: %s' % str(e.info)) from e
        elif e.error == 'index_not_found_exception':
            create_index()
        elif e.error == 'parsing_exception':
            raise QueryError(e.info) from e
        logger.error('Problem communicating with ES: %s' % e.error)
        raise IndexConnectionError(
            'Problem communicating with ES: %s' % e.error
        ) from e
    except SerializationError as e:
        logger.error("SerializationError: %s", e)
        raise IndexingError('Problem serializing document: %s' % e) from e
    except BulkIndexError as e:
        logger.error("BulkIndexError: %s", e)
        raise IndexingError('Problem with bulk indexing: %s' % e) from e
    except Exception as e:
        logger.error('Unhandled exception: %s')


class SearchSession(object):
    """Encapsulates session with Elasticsearch host."""

    # TODO: we need to take on security considerations here. Presumably we will
    # use SSL. Presumably we will use HTTP Auth, or something else.

    def __init__(self, host: str, index: str, port: int=9200,
                 scheme: str='http', user: Optional[str]=None,
                 password: Optional[str]=None, mapping: Optional[str]=None,
                 verify: bool=True, **extra: Any) -> None:
        """
        Initialize the connection to Elasticsearch.

        Parameters
        ----------
        host : str
        index : str
        port : int
            Default: 9200
        scheme: str
            Default: 'http'
        user: str
            Default: None
        password: str
            Default: None

        Raises
        ------
        IndexConnectionError
            Problem communicating with Elasticsearch host.


        """
        self.index = index
        self.mapping = mapping
        use_ssl = True if scheme == 'https' else False
        http_auth = '%s:%s' % (user, password) if user else None

        logger.debug(
            f'init ES session for index "{index}" at {scheme}://{host}:{port}'
            f' with verify={verify} and ssl={use_ssl}'
        )

        try:
            self.es = Elasticsearch([{'host': host, 'port': port,
                                      'use_ssl': use_ssl,
                                      'http_auth': http_auth,
                                      'verify_certs': verify}],
                                    connection_class=Urllib3HttpConnection,
                                    **extra)
        except ElasticsearchException as e:
            logger.error('ElasticsearchException: %s', e)
            raise IndexConnectionError(
                'Could not initialize ES session: %s' % e
            ) from e

    def _base_search(self) -> Search:
        return Search(using=self.es, index=self.index)

    def cluster_available(self) -> bool:
        """
        Determine whether or not the ES cluster is available.

        Returns
        -------
        bool
        """
        try:
            self.es.cluster.health(wait_for_status='yellow', request_timeout=1)
            return True
        except urllib3.exceptions.HTTPError as e:
            logger.debug('Health check failed: %s', str(e))
            return False
        except Exception as e:
            logger.debug('Health check failed: %s', str(e))
            return False

    def create_index(self) -> None:
        """
        Create the search index.

        Parameters
        ----------
        mappings : dict
            See
            elastic.co/guide/en/elasticsearch/reference/current/mapping.html

        """
        logger.debug('create ES index "%s"', self.index)
        if not self.mapping or type(self.mapping) is not str:
            raise IndexingError('Mapping not set')
        with handle_es_exceptions():
            with open(self.mapping) as f:
                mappings = json.load(f)
            self.es.indices.create(self.index, mappings)

    def add_document(self, document: Document) -> None:
        """
        Add a document to the search index.

        Uses ``paper_id_v`` as the primary identifier for the document. If the
        document is already indexed, will quietly overwrite.

        Parameters
        ----------
        document : :class:`.Document`
            Must be a valid search document, per ``schema/Document.json``.

        Raises
        ------
        :class:`.IndexConnectionError`
            Problem communicating with Elasticsearch host.
        :class:`.QueryError`
            Problem serializing ``document`` for indexing.

        """
        if not self.es.indices.exists(index=self.index):
            self.create_index()

        with handle_es_exceptions():
            ident = document.id if document.id else document.paper_id
            logger.debug(f'{ident}: index document')
            self.es.index(index=self.index, doc_type='document',
                          id=ident, body=document)

    def bulk_add_documents(self, documents: List[Document],
                           docs_per_chunk: int = 500) -> None:
        """
        Add documents to the search index using the bulk API.

        Parameters
        ----------
        document : :class:`.Document`
            Must be a valid search document, per ``schema/Document.json``.
        docs_per_chunk: int
            Number of documents to send to ES in a single chunk
        Raises
        ------
        IndexConnectionError
            Problem communicating with Elasticsearch host.
        BulkIndexingError
            Problem serializing ``document`` for indexing.

        """
        if not self.es.indices.exists(index=self.index):
            logger.debug('index does not exist')
            self.create_index()
            logger.debug('created index')

        with handle_es_exceptions():
            actions = ({
                '_index': self.index,
                '_type': 'document',
                '_id': document.id if document.id else document.paper_id,
                '_source': asdict(document)
            } for document in documents)

            helpers.bulk(client=self.es, actions=actions,
                         chunk_size=docs_per_chunk)
            logger.debug('added %i documents to index', len(documents))

    def get_document(self, document_id: int) -> Document:
        """
        Retrieve a document from the index by ID.

        Uses ``metadata_id`` as the primary identifier for the document.

        Parameters
        ----------
        doument_id : int
            Value of ``metadata_id`` in the original document.

        Returns
        -------
        :class:`.Document`

        Raises
        ------
        IndexConnectionError
            Problem communicating with the search index.
        QueryError
            Invalid query parameters.

        """
        with handle_es_exceptions():
            record = self.es.get(index=self.index, doc_type='document',
                                 id=document_id)

        if not record:
            logger.error("No such document: %s", document_id)
            raise DocumentNotFound('No such document')
        return Document(**record['_source'])    # type: ignore
        # See https://github.com/python/mypy/issues/3937

    def search(self, query: Query) -> DocumentSet:
        """
        Perform a search.

        Parameters
        ----------
        query : :class:`.Query`

        Returns
        -------
        :class:`.DocumentSet`

        Raises
        ------
        IndexConnectionError
            Problem communicating with the search index.
        QueryError
            Invalid query parameters.

        """
        # Make sure that the user is not requesting a nonexistant page.
        max_pages = int(MAX_RESULTS/query.page_size)
        if query.page > max_pages:
            _message = f'Requested page {query.page}, but max is {max_pages}'
            logger.error(_message)
            raise OutsideAllowedRange(_message)

        # Perform the search.
        logger.debug('got current_search request %s', str(query))
        current_search = self._base_search()
        try:
            if isinstance(query, AdvancedQuery):
                current_search = prepare.advanced(current_search, query)
            elif isinstance(query, SimpleQuery):
                current_search = prepare.simple(current_search, query)
        except TypeError as e:
            raise QueryError('Malformed query') from e

        # Highlighting is performed by Elasticsearch; here we include the
        # fields and configuration for highlighting.
        current_search = prepare.highlight(current_search)

        with handle_es_exceptions():
            # Slicing the search adds pagination parameters to the request.
            resp = current_search[query.page_start:query.page_end].execute()

        # Perform post-processing on the search results.
        return results.to_documentset(query, resp)


def init_app(app: object = None) -> None:
    """Set default configuration parameters for an application instance."""
    config = get_application_config(app)
    config.setdefault('ELASTICSEARCH_HOST', 'localhost')
    config.setdefault('ELASTICSEARCH_PORT', '9200')
    config.setdefault('ELASTICSEARCH_INDEX', 'arxiv')
    config.setdefault('ELASTICSEARCH_USER', None)
    config.setdefault('ELASTICSEARCH_PASSWORD', None)
    config.setdefault('ELASTICSEARCH_MAPPING', 'mappings/DocumentMapping.json')
    config.setdefault('ELASTICSEARCH_VERIFY', 'true')


# TODO: consider making this private.
def get_session(app: object = None) -> SearchSession:
    """Get a new session with the search index."""
    config = get_application_config(app)
    host = config.get('ELASTICSEARCH_HOST', 'localhost')
    port = config.get('ELASTICSEARCH_PORT', '9200')
    scheme = config.get('ELASTICSEARCH_SCHEME', 'http')
    index = config.get('ELASTICSEARCH_INDEX', 'arxiv')
    verify = config.get('ELASTICSEARCH_VERIFY', 'true') == 'true'
    user = config.get('ELASTICSEARCH_USER', None)
    password = config.get('ELASTICSEARCH_PASSWORD', None)
    mapping = config.get('ELASTICSEARCH_MAPPING',
                         'mappings/DocumentMapping.json')
    return SearchSession(host, index, port, scheme, user, password, mapping,
                         verify=verify)


# TODO: consider making this private.
def current_session() -> SearchSession:
    """Get/create :class:`.SearchSession` for this context."""
    g = get_application_global()
    if not g:
        return get_session()
    if 'search' not in g:
        g.search = get_session()    # type: ignore
    return g.search     # type: ignore


@wraps(SearchSession.search)
def search(query: Query) -> DocumentSet:
    """Retrieve search results."""
    return current_session().search(query)


@wraps(SearchSession.add_document)
def add_document(document: Document) -> None:
    """Add Document."""
    return current_session().add_document(document)


@wraps(SearchSession.bulk_add_documents)
def bulk_add_documents(documents: List[Document]) -> None:
    """Add Documents."""
    return current_session().bulk_add_documents(documents)


@wraps(SearchSession.get_document)
def get_document(document_id: int) -> Document:
    """Retrieve arxiv document by id."""
    return current_session().get_document(document_id)


@wraps(SearchSession.cluster_available)
def cluster_available() -> bool:
    """Check whether the cluster is available."""
    return current_session().cluster_available()


@wraps(SearchSession.create_index)
def create_index() -> None:
    """Create the search index."""
    current_session().create_index()


def ok() -> bool:
    """Health check."""
    try:
        current_session()
    except Exception:    # TODO: be more specific.
        return False
    return True