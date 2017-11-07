"""Integration with search index."""

from elasticsearch import Elasticsearch, ElasticsearchException, \
                          SerializationError, TransportError
from elasticsearch.connection import Urllib3HttpConnection

from search.context import get_application_config, get_application_global
from search import logging

logger = logging.getLogger(__name__)


class SearchSession(object):
    """Encapsulates session with Elasticsearch host."""

    # TODO: we need to take on security considerations here. Presumably we will
    # use SSL. Presumably we will use HTTP Auth, or something else.
    def __init__(self, host: str, index: str, port: int=9200, **extra) -> None:
        """
        Initialize the connection to Elasticsearch.

        Parameters
        ----------
        host : str
        index : str
        port : int
            Default: 9200

        Raises
        ------
        IOError
            Problem communicating with Elasticsearch host.
        """
        logger.debug('init ES session for index "%s" at %s:%s',
                     index, host, port)
        self.index = index
        try:
            self.es = Elasticsearch([{'host': host, 'port': port}],
                                    connection_class=Urllib3HttpConnection,
                                    **extra)
        except ElasticsearchException as e:
            raise IOError('Could not initialize ES session: %s' % e) from e

    def create_index(self, mappings: dict) -> None:
        """
        Create the search index.

        Parameters
        ----------
        mappings : dict
            See
            elastic.co/guide/en/elasticsearch/reference/current/mapping.html
        """
        logger.debug('create ES index "%s"', self.index)
        self.es.indices.create(self.index, mappings, ignore=400)

    def add_document(self, document: dict) -> None:
        """
        Add a document to the search index.

        Uses ``metadata_id`` as the primary identifier for the document. If the
        document is already indexed, will quietly overwrite.

        Paramters
        ---------
        document : dict
            Must be a valid search document, per ``schema/Document.json``.

        Raises
        ------
        IOError
            Problem communicating with Elasticsearch host.
        ValueError
            Problem serializing ``document`` for indexing.
        """
        try:
            self.es.index(index=self.index, doc_type='arxiv',
                          id=document['metadata_id'], body=document)
        except SerializationError as e:
            raise ValueError('Problem serializing document: %s' % e) from e
        except TransportError as e:
            raise IOError('Problem communicating with ES: %s' % e) from e

    def get_document(self, document_id: int) -> dict:
        """
        Retrieve a document from the index by ID.

        Uses ``metadata_id`` as the primary identifier for the document.

        Parameters
        ----------
        doument_id : int
            Value of ``metadata_id`` in the original document.

        Returns
        -------
        dict

        Raises
        ------
        IOError
        """
        try:
            record = self.es.get(index=self.index, doc_type='arxiv',
                                 id=document_id)
        except SerializationError as e:
            raise ValueError('Problem serializing document: %s' % e) from e
        except TransportError as e:
            raise IOError('Problem communicating with ES: %s' % e) from e
        if not record:
            return
        return record['_source']

    # TODO: this needs some work. We need to think more about how we want to
    # structure our queries.
    def search(self, **query) -> dict:
        """
        Perform a search.

        Parameters
        ----------
        query : kwargs
            Fields and values to use for the search.

        Returns
        -------
        dict
            Includes metadata about the query.

        Raises
        ------
        IOError
            Problem communicating with the search index.
        ValueError
            Invalid query parameters.
        """
        logger.debug('got search request for %s', str(query))
        try:
            results = self.es.search(index=self.index, doc_type='arxiv',
                                     body={'query': {'term': query}})
        except TransportError as e:
            if e.error == 'parsing_exception':
                raise ValueError(e.info) from e
            raise IOError('Problem communicating with ES: %s' % e) from e
        return {
            'count': results['hits']['total'],
            'results': list(map(self._transform, results['hits']['hits']))
        }

    def _transform(self, raw: dict) -> dict:
        """Transform an ES search result back into a familiar struct."""
        result = raw['_source']
        result['score'] = raw['_score']
        result['type'] = raw['_type']
        return result


def init_app(app: object = None) -> None:
    """Set default configuration parameters for an application instance."""
    config = get_application_config(app)
    config.setdefault('ELASTICSEARCH_HOST', 'localhost')
    config.setdefault('ELASTICSEARCH_PORT', '9200')
    config.setdefault('ELASTICSEARCH_INDEX', 'arxiv')
    config.setdefault('ELASTICSEARCH_USER', 'elastic')
    config.setdefault('ELASTICSEARCH_PASSWORD', 'changeme')


def get_session(app: object = None) -> SearchSession:
    """Get a new session with the search index."""
    config = get_application_config(app)
    host = config.get('ELASTICSEARCH_HOST', 'localhost')
    port = config.get('ELASTICSEARCH_PORT', '9200')
    index = config.get('ELASTICSEARCH_INDEX', 'arxiv')
    user = config.get('ELASTICSEARCH_USER', 'elastic')
    password = config.get('ELASTICSEARCH_PASSWORD', 'changeme')
    return SearchSession(host, index, port,
                         http_auth='%s:%s' % (user, password))


def current_session():
    """Get/create :class:`.SearchSession` for this context."""
    g = get_application_global()
    if not g:
        return get_session()
    if 'search' not in g:
        g.search = get_session()
    return g.search


def search(**query) -> dict:
    """"""
    return current_session().search(**query)


def ok() -> bool:
    """Health check."""
    try:
        current_session()
    except Exception as e:    # TODO: be more specific.
        return False
    return True
