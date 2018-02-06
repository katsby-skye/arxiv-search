from unittest import TestCase, mock
from datetime import date, datetime
from pytz import timezone
from elasticsearch_dsl import Search, Q
from elasticsearch_dsl.query import Range, Match, Bool, Nested

from search.services import index
from search.domain import Query, FieldedSearchTerm, DateRange, Classification,\
    AdvancedQuery, FieldedSearchList, ClassificationList

EASTERN = timezone('US/Eastern')


class TestPrepare(TestCase):
    """
    Tests for :meth:`.index.SearchSession._prepare`.

    :meth:`.index.SearchSession._prepare` renders a :class:`.AdvancedQuery`
    to an ES :class:`.Search` using elasticsearch_dsl.
    """

    def setUp(self):
        """Get a :class:`.index.SearchSession` instance."""
        self.session = index.current_session()

    def test_to_es_dsl_returns_a_search(self):
        """Return a :class:`.Search`."""
        self.assertIsInstance(self.session._prepare(AdvancedQuery()), Search)

    def test_group_terms(self):
        """:meth:`._group_terms` groups terms using logical precedence."""
        query = AdvancedQuery({'terms': FieldedSearchList([
            FieldedSearchTerm(operator=None, field='title', term='muon'),
            FieldedSearchTerm(operator='OR', field='title', term='gluon'),
            FieldedSearchTerm(operator='NOT', field='title', term='foo'),
            FieldedSearchTerm(operator='AND', field='title', term='boson'),
        ])})
        expected = (
            FieldedSearchTerm(operator=None, field='title', term='muon'),
            'OR',
            (
              (
                FieldedSearchTerm(operator='OR', field='title', term='gluon'),
                'NOT',
                FieldedSearchTerm(operator='NOT', field='title', term='foo')
              ),
              'AND',
              FieldedSearchTerm(operator='AND', field='title', term='boson')
            )
        )
        try:
            terms = self.session._group_terms(query)
        except AssertionError:
            self.fail('Should result in a single group')
        self.assertEqual(expected, terms)

    def test_group_terms_all_and(self):
        """:meth:`._group_terms` groups terms using logical precedence."""
        query = AdvancedQuery({'terms': FieldedSearchList([
            FieldedSearchTerm(operator=None, field='title', term='muon'),
            FieldedSearchTerm(operator='AND', field='title', term='gluon'),
            FieldedSearchTerm(operator='AND', field='title', term='foo'),
        ])})
        expected = (
            (
              FieldedSearchTerm(operator=None, field='title', term='muon'),
              'AND',
              FieldedSearchTerm(operator='AND', field='title', term='gluon')
            ),
            'AND',
            FieldedSearchTerm(operator='AND', field='title', term='foo')
        )
        try:
            terms = self.session._group_terms(query)
        except AssertionError:
            self.fail('Should result in a single group')
        self.assertEqual(expected, terms)

    # def test_grouped_terms_to_q(self):
    #     """:meth:`._grouped_terms_to_q` builds a Bool from grouped terms."""
    #     query = AdvancedQuery({'terms': FieldedSearchList([
    #         FieldedSearchTerm(operator=None, field='title', term='muon'),
    #         FieldedSearchTerm(operator='OR', field='title', term='gluon'),
    #         FieldedSearchTerm(operator='NOT', field='title', term='foo'),
    #         FieldedSearchTerm(operator='AND', field='title', term='boson'),
    #     ])})
    #     expected = (Q('match', title='muon')
    #                 | (
    #                   (Q('match', title='gluon')
    #                    & ~Q('match', title='foo'))
    #                   & Q('match', title='boson'))
    #                 )
    #     q = self.session._grouped_terms_to_q(self.session._group_terms(query))
    #     self.assertIsInstance(q, Bool)
    #     self.assertEqual(expected, q)

    def test_daterange_to_q(self):
        """:meth:`._daterange_to_q` builds a Range from :class:`.DateRange`."""
        query = AdvancedQuery({
            'date_range': DateRange({
                'start_date': datetime(year=1996, month=2, day=5,
                                       hour=0, minute=0, second=0,
                                       tzinfo=EASTERN),
                'end_date': datetime(year=1996, month=3, day=5,
                                     hour=0, minute=0, second=0,
                                     tzinfo=EASTERN),
            })
        })
        q = self.session._daterange_to_q(query)
        self.assertIsInstance(q, Range)
        expected = Range(submitted_date={
            'gte': '1996-02-05T00:00:00-0456',
            'lt': '1996-03-05T00:00:00-0456'
        })
        self.assertEqual(q, expected)

    def test_classifications_to_q(self):
        query = AdvancedQuery({
            'primary_classification': [Classification({
                'group': 'physics',
                'archive': 'physics',
                'category': 'astro-ph'
            })]
        })
        q = self.session._classifications_to_q(query)

        self.assertIsInstance(q, Nested)
        expected = Nested(
            path='primary_classification',
            query=Bool(
                must=[
                    Match(primary_classification__group__id='physics'),
                    Match(primary_classification__archive__id='physics'),
                    Match(primary_classification__category__id='astro-ph')
                ]
            )
        )
        self.assertEqual(q, expected)

    def test_prepare(self):
        query = AdvancedQuery(
            terms=FieldedSearchList([
                FieldedSearchTerm(operator=None, field='title', term='muon'),
                FieldedSearchTerm(operator='OR', field='title', term='gluon')
            ]),
            order='title',
            date_range=DateRange(
                start_date=datetime(year=2006, month=2, day=5,
                                    hour=0, minute=0, second=0),
                end_date=datetime(year=2007, month=3, day=25,
                                  hour=0, minute=0, second=0)
            ),
            primary_classification=ClassificationList([
                Classification(group='cs')
            ])
        )
        expected = {'query': {'bool': {'should': [{'match': {'title.tex': 'muon'}}, {'match': {'title.english': 'muon'}}, {'match': {'title.tex': 'gluon'}}, {'match': {'title.english': 'gluon'}}], 'must': [{'range': {'submitted_date': {'gte': '2006-02-05T00:00:00', 'lt': '2007-03-25T00:00:00'}}}, {'nested': {'path': 'primary_classification', 'query': {'match': {'primary_classification.group.id': 'cs'}}}}], 'minimum_should_match': 1}}, 'sort': ['title']}

        search = self.session._prepare(query)
        self.assertDictEqual(search.to_dict(), expected)
