import mysite.search.models
import mysite.search.views
import collections
import urllib
import re
from django.db.models import Q

class Query:
    
    def __init__(self, terms, active_facet_options=None, terms_string=None): 
        self.terms = terms
        # FIXME: Change the name to "active facets".
        self.active_facet_options = active_facet_options or {}
        self._terms_string = terms_string

    @property
    def terms_string(self):
        if self._terms_string is None:
            raise ValueError
        return self._terms_string 

    @staticmethod
    def split_into_terms(string):
        # We're given some query terms "between quotes"
        # and some glomped on with spaces.
        # Strategy: Find the strings validly inside quotes, and remove them
        # from the original string. Then split the remainder (and probably trim
        # whitespace from the remaining terms).
        # {{{
        ret = []
        splitted = re.split(r'(".*?")', string)

        for (index, word) in enumerate(splitted):
            if (index % 2) == 0:
                ret.extend(word.split())
            else:
                assert word[0] == '"'
                assert word[-1] == '"'
                ret.append(word[1:-1])

        return ret
        # }}}

    @staticmethod
    def create_from_GET_data(GET):
        possible_facets = ['language', 'toughness']

        active_facet_options = {}
        for facet in possible_facets:
            if GET.get(facet):
                active_facet_options[facet] = GET.get(facet)
        terms_string = GET.get('q', '')
        terms = Query.split_into_terms(terms_string)

        return Query(terms=terms, active_facet_options=active_facet_options, terms_string=terms_string)

    def get_bugs_unordered(self):
        return mysite.search.models.Bug.open_ones.filter(self.get_Q())

    def __nonzero__(self):
        if self.terms or self.active_facet_options:
            return 1
        return 0

    def get_Q(self, exclude_active_facets=False):
        """Get a Q object which can be passed to Bug.open_ones.filter()"""

        # Begin constructing a conjunction of Q objects (filters)
        q = Q()

        toughness_is_active = ('toughness' in self.active_facet_options.keys())
        exclude_toughness = exclude_active_facets and toughness_is_active
        if (self.active_facet_options.get('toughness', None) == 'bitesize'
                and not exclude_toughness):
            q &= Q(good_for_newcomers=True)

        language_is_active = ('language' in self.active_facet_options.keys())
        exclude_language = exclude_active_facets and language_is_active
        if 'language' in self.active_facet_options and not exclude_language: 
            q &= Q(project__language__iexact=self.active_facet_options['language'])

        for word in self.terms:
            whole_word = "[[:<:]]%s[[:>:]]" % (
                    mysite.base.controllers.mysql_regex_escape(word))
            terms_disjunction = (
                    Q(project__language__iexact=word) |
                    Q(title__iregex=whole_word) |
                    Q(description__iregex=whole_word) |

                    # 'firefox' grabs 'mozilla firefox'.
                    Q(project__name__iregex=whole_word)
                    )
            q &= terms_disjunction

        return q

    def get_possible_facets(self):

        bugs = mysite.search.models.Bug.open_ones.filter(self.get_Q())

        if not bugs:
            return {}
        
        bitesize_GET_data = dict(self.active_facet_options)
        bitesize_GET_data.update({
            'q': self.terms_string,
            'toughness': 'bitesize',
            })
        bitesize_query_string = urllib.urlencode(bitesize_GET_data)
        bitesize_query = Query.create_from_GET_data(bitesize_GET_data)
        bitesize_option = {
                'name': 'bitesize',
                'count': bitesize_query.get_bugs_unordered().count(),
                'query_string': bitesize_query_string}

        any_toughness_GET_data = {'q': self.terms_string}
        # Add all the active facet options
        any_toughness_GET_data.update(dict(self.active_facet_options))
        # Remove the toughness option
        if 'toughness' in any_toughness_GET_data:
            del any_toughness_GET_data['toughness']
        any_toughness_query = Query.create_from_GET_data(any_toughness_GET_data)
        any_toughness = {
                'name': 'any',
                'count': any_toughness_query.get_bugs_unordered().count(),
                'query_string': urllib.urlencode(any_toughness_GET_data)
                }

        import pdb; pdb.set_trace()

        # Figure out the query string for the facet option "any language"
        # Begin with the term
        any_language_GET_data = {'q': self.terms_string}
        # Add all the active facet options
        any_language_GET_data.update(dict(self.active_facet_options))
        # Remove the language option
        if 'language' in any_language_GET_data:
            del any_language_GET_data['language']
        any_language_query = Query.create_from_GET_data(any_language_GET_data)
        any_language = {
                'name': 'any',
                'count': any_language_query.get_bugs_unordered().count(),
                'query_string': urllib.urlencode(any_language_GET_data)
                }

        possible_facets = { 
                # The languages facet is based on the project languages, "for now"
                'language': {
                    'name_in_GET': "language",
                    'sidebar_name': "by main project language",
                    'description_above_results': "projects primarily coded in %s",
                    'options': [any_language],
                    },
                'toughness': {
                    'name_in_GET': "toughness",
                    'sidebar_name': "by toughness",
                    'description_above_results': "where toughness = %s",
                    'options': [bitesize_option, any_toughness]
                    }
                }

        distinct_language_columns = bugs.values('project__language').distinct()
        languages = [x['project__language'] for x in distinct_language_columns]
        for lang in sorted(languages):

            lang_GET_data = dict(self.active_facet_options)

            lang_GET_data.update({
                'q': self.terms_string,
                'language': lang,
                })
            lang_query = Query.create_from_GET_data(lang_GET_data)
            lang_query_string = urllib.urlencode(lang_GET_data)
            possible_facets['language']['options'].append({
                'name': lang,
                'count': lang_query.get_bugs_unordered().count(),
                'query_string': lang_query_string
                })

        return possible_facets