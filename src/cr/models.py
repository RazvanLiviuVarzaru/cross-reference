from django.conf import settings
from django.db import models, connections, OperationalError
from django.db.models import Q
from pprint import pprint
import re
import time
from django.db import connection
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

# Maximum number of results a single search can return.
MAX_LIMIT = 500

# Lookups that compile to LIKE '%...', which no index can serve
NON_SARGABLE_LOOKUPS = ('__icontains', '__iendswith')

# MariaDB error: Query execution was interrupted (max_statement_time exceeded)
ER_STATEMENT_TIMEOUT = 1969

# Shown when a search is aborted by max_statement_time
SEARCH_TIMEOUT_MESSAGE = ('The search took too long and was stopped. Narrow it '
                          'down with a date range, branch, platform or test name.')

# Seconds a wildcard lookup (see RESOLVE_FIRST) may take. When it takes longer,
# e.g. before the test_name index exists, the search keeps the LIKE filter.
LOOKUP_TIME_LIMIT = 5

# Seconds always allowed for fetching the rows a search found, even when the
# search used up its time: at most `limit` rows, read by primary key.
FETCH_TIME_LIMIT = 5

# optimizer_join_limit_pref_ratio for searches (0, off, by default): lets the
# optimizer read runs in date order and stop at the LIMIT when that looks much
# cheaper than sorting all matches, e.g. for `main.*`. Not used when the test
# name is selective (an exact name, or a wildcard matching rarely failing
# tests): starting from the test_name index is always cheap enough then, while
# the walk can read every run of a builder or branch where those tests rarely
# fail (18 s instead of 1.5 s on a copy of production).
JOIN_LIMIT_PREF_RATIO = 100

# A wildcard test name whose tests failed more often than this keeps its LIKE
# filter: walking runs by date finds `limit` matches at once, while sorting all
# their failures is slow (*rpl*: 984 tests, 876k failures, over 20 s). Rarer
# tests are searched through the test_name index (*mroonga*: 5k failures,
# 0.1 s instead of 1-3 s).
FREQUENT_FAILURES = 10000

# Filters on test_run columns, looked up through test_failure.test_run_id
TEST_RUN_FILTERS = ('branch', 'revision', 'platform', 'bbnum', 'typ', 'info')

# Free text filters: searched as typed, spaces included
TEXT_FILTERS = ('failure_text', 'info_text')

class Builder(models.Model):
    id = models.IntegerField(primary_key=True)
    name = models.TextField()
    description = models.TextField(null=True, blank=True)
    name_hash = models.CharField(max_length=40, unique=True)

    class Meta:
        managed = False
        db_table = 'builders'

class TestRun(models.Model):
  branch = models.CharField(max_length=100, blank=True, null=True)
  revision = models.CharField(max_length=256, blank=True, null=True)
  platform = models.CharField(max_length=100)
  dt = models.DateTimeField()
  bbnum = models.IntegerField()
  typ = models.CharField(max_length=32)
  info = models.CharField(max_length=255, blank=True, null=True)

  class Meta:
    managed = False
    db_table = 'test_run'

class TestFailure(models.Model):
  test_run_id = models.OneToOneField(TestRun,
    null=False,
    blank=False,
    db_column='test_run_id',
    primary_key=True,
    on_delete=models.DO_NOTHING
  )
  test_name = models.CharField(max_length=100)
  test_variant = models.CharField(max_length=64)
  info_text = models.CharField(max_length=255, blank=True, null=True)
  failure_text = models.TextField(blank=True, null=True)

  @property
  def platform(self):
    return self.test_run_id.platform

  class Meta:
    managed = False
    db_table = 'test_failure'
    unique_together = (('test_run_id', 'test_name', 'test_variant'),)


def attach_builder_ids(test_failures):
  """
  Set `builder_id` (the Buildbot builder id used in "Build details" links) on
  each test failure, using a single query instead of one query per row.
  """
  platforms = {f.test_run_id.platform for f in test_failures}
  builder_ids = {}
  if platforms:
    # Ordered by descending id so that, for duplicate names, the lowest id wins
    builder_ids = dict(Builder.objects.using('buildbot')
                       .filter(name__in=platforms)
                       .order_by('-id')
                       .values_list('name', 'id'))
  for f in test_failures:
    f.builder_id = builder_ids.get(f.test_run_id.platform)


def _parse_filter_date(value):
  """
  Returns (datetime in UTC, date_only) or (None, False) if value is invalid.
  """
  for dt_format in (
    '%Y-%m-%d',
    '%Y-%m-%dT%H:%M',
    '%Y-%m-%dT%H:%M:%S',
    '%Y-%m-%dT%H:%M:%SZ',
    '%Y-%m-%d %H:%M:%S',
  ):
    try:
      parsed = datetime.strptime(value, dt_format).replace(tzinfo=dt_timezone.utc)
      return parsed, dt_format == '%Y-%m-%d'
    except ValueError:
      continue

  return None, False


def is_statement_timeout(error):
  """
  True if `error` is MariaDB aborting a query that ran longer than
  max_statement_time (ER_STATEMENT_TIMEOUT), see the buildbot DB settings.
  """
  return isinstance(error, OperationalError) and error.args[:1] == (ER_STATEMENT_TIMEOUT,)


def _parse_limit(value):
  try:
    limit = int(value)
  except (TypeError, ValueError):
    return 50
  return max(1, min(limit, MAX_LIMIT))


# Filters whose wildcard patterns (*x*, *x) are resolved to the list of
# matching distinct values before the main query: (model, column, prefix of
# the column in TestFailure lookups). Reading the distinct values only needs
# the column's index, and the main query can then use that index with IN (...)
# instead of walking test_run by date until it finds `limit` matches, which
# reads the whole history when the matches are rare or do not exist.
RESOLVE_FIRST = {
  'branch': (TestRun, 'branch', 'test_run_id__'),
  'platform': (TestRun, 'platform', 'test_run_id__'),
  'test_name': (TestFailure, 'test_name', ''),
}


def _set_time_limit(seconds, pref_ratio=None):
  """
  Sets max_statement_time for the next queries on the buildbot connection,
  None for no limit. 0 would also mean no limit, so it is at least 1 ms.
  Also sets optimizer_join_limit_pref_ratio when `pref_ratio` is given.
  """
  sql = 'SET SESSION max_statement_time = %s'
  params = [0 if seconds is None else max(seconds, 0.001)]
  if pref_ratio is not None:
    sql += ', optimizer_join_limit_pref_ratio = %s'
    params.append(pref_ratio)
  with connections['buildbot'].cursor() as cursor:
    cursor.execute(sql, params)


def _resolve_values(key, lookups, search_string, time_limit):
  """
  Returns the distinct values of the `key` column matching `lookups`, a list
  of (TestFailure lookup, 'AND' / 'OR') as in select_test_failures, or None
  if that takes longer than `time_limit` seconds.
  """
  model, column, prefix = RESOLVE_FIRST[key]
  q_objects = Q()
  for lookup, operator in lookups:
    q = Q(**{lookup[len(prefix):]: search_string})
    q_objects = q_objects | q if operator == 'OR' else q_objects & q
  _set_time_limit(time_limit)
  try:
    return list(model.objects.using('buildbot')
                .filter(q_objects)
                .order_by()
                .values_list(column, flat=True)
                .distinct())
  except OperationalError as e:
    if not is_statement_timeout(e):
      raise
    return None


def _frequent_tests(names, time_limit):
  """
  True if the tests in `names` failed more than FREQUENT_FAILURES times, or if
  counting takes longer than `time_limit` seconds.
  """
  _set_time_limit(time_limit)
  try:
    return (TestFailure.objects.using('buildbot')
            .filter(test_name__in=names)[:FREQUENT_FAILURES + 1]
            .count()) > FREQUENT_FAILURES
  except OperationalError as e:
    if not is_statement_timeout(e):
      raise
    return True


def _fetch_failures(keys):
  """
  Returns the TestFailure rows, with their TestRun, for `keys`, a list of
  (test_run_id, test_name, test_variant) primary keys, in the same order.
  """
  if not keys:
    return []
  q_objects = Q()
  for run_id, name, variant in keys:
    q_objects |= Q(test_run_id=run_id, test_name=name, test_variant=variant)
  rows = {(f.test_run_id.id, f.test_name, f.test_variant): f
          for f in TestFailure.objects.using('buildbot')
                   .filter(q_objects)
                   .select_related('test_run_id')}
  return [rows[k] for k in keys if k in rows]


def select_test_failures(filters, include_failures=True):
  available_filters = ["branch", "revision", "platform", "dt", "bbnum", "typ",
                       "info", "test_name", "test_variant", "info_text", "failure_text"]

  # Time the whole search may spend in the database, see README "Search time
  # limit". 0 means no limit, as for max_statement_time.
  time_limit = settings.BUILDBOT_SEARCH_TIME_LIMIT
  deadline = time.monotonic() + time_limit if time_limit else None

  # New implementation
  limit = 50
  if 'limit' in filters and filters['limit'] != '':
    limit = _parse_limit(filters['limit'])

  sort_order = filters.get('sort_order', 'desc')
  order_by = 'test_run_id__dt' if sort_order == 'asc' else '-test_run_id__dt'

  test_run_filters = None
  test_failure_filters = TestFailure.objects.using('buildbot').all()
  selective_test_name = False

  reg_exp = {
    'branch': [
      # Support exact match via a special syntax `=branch_name``
      {
        'pattern': '^=(.+)$',
        'filter': [('test_run_id__branch__exact', 'AND')],
        'replace': True
      },
      # Pattern: 10.?
      {
        'pattern': '^([0-9]{1,2}\\.)(\\?)$',
        'filter': [('test_run_id__branch__istartswith', 'AND')],
        'replace': True
      },
      # Pattern: 10.1, 10.2, 5.5...
      {
        'pattern': '^([0-9]{1,2}\\.)([0-9]{1,2})$',
        'filter': [('test_run_id__branch__exact', 'AND')],
        'replace': False
      },
      # Pattern: *10.6*, *main*
      {
        'pattern': '^\\*[^*?]+\\*$',
        'filter': [('test_run_id__branch__icontains', 'AND')],
        'replace': True
      },
      # Pattern *10.?*
      {
        'pattern': '^\\*([0-9]{1,2}\\.)(\\?)\\*$',
        'filter': [('test_run_id__branch__icontains', 'AND')],
        'replace': True
      },
      # Free text without wildcards: main, refs/pull/1131/merge
      {
        'pattern': '^[^*?]+$',
        'filter': [('test_run_id__branch__icontains', 'AND')],
        'replace': False
      }
    ],
    'revision': [
      {
        'pattern': '^[a-zA-Z0-9]*$',
        'filter': [('test_run_id__revision__istartswith', 'AND')],
        'replace': False
      }
    ],
    'platform': [
      {
        'pattern': '^[^*]+$',
        'filter': [('test_run_id__platform__exact', 'AND')],
        'replace': False
      },
      {
        'pattern': '^\\*[^*]+\\*$',
        'filter': [('test_run_id__platform__icontains', 'AND')],
        'replace': True
      }
    ],
    'bbnum': [
      {
        'pattern': '^[0-9]+$',
        'filter': [('test_run_id__bbnum__exact', 'AND')],
        'replace': False
      }
    ],
    'typ': [
      {
        'pattern': '^[^*]+$',
        'filter': [('test_run_id__typ__exact', 'AND')],
        'replace': False
      },
      {
        'pattern': '^[^*]+\\*$',
        'filter': [('test_run_id__typ__istartswith', 'AND')],
        'replace': True
      }
    ],
    'info': [
      {
        'pattern': '^[^*]+$',
        'filter': [('test_run_id__info__exact', 'AND')],
        'replace': False
      },
      {
        'pattern': '^[^*]+\\*$',
        'filter': [('test_run_id__info__istartswith', 'AND')],
        'replace': True
      }
    ],
    'test_name': [
      # contains: *increment*
      {
        'pattern': r'^\*[^*]+\*$',
        'filter': [('test_name__icontains', 'AND')],
        'replace': True
      },
      # endswith: *auto_increment
      {
        'pattern': r'^\*[^*]+$',
        'filter': [('test_name__iendswith', 'AND')],
        'replace': True
      },
      # startswith: spider.auto*
      {
        'pattern': r'^[^*]+\*$',
        'filter': [('test_name__istartswith', 'AND')],
        'replace': True
      },
      # exact match: spider.auto_increment, galera.galera#500
      {
        'pattern': r'^[^*]+$',
        'filter': [('test_name__exact', 'AND')],
        'replace': False
      }
    ],
    'test_variant': [
      # exact match: innodb,row
      {
        'pattern': '^[^*]+$',
        'filter': [('test_variant__exact', 'AND')],
        'replace': False
      },
      {
        'pattern': '^\\*[^*]+\\*$',
        'filter': [('test_variant__icontains', 'AND')],
        'replace': True
      }
    ],
    'failure_text': [
      {
        'pattern': '^[^*](.*[^*])?$',
        'filter': [('failure_text__icontains', 'AND')],
        'replace': False
      },
      {
        'pattern': '^\\*[a-zA-Z0-9]*\\*$',
        'filter': [('failure_text__icontains', 'AND')],
        'replace': True
      }
    ],
    'info_text': [
      {
        'pattern': '^[^*](.*[^*])?$',
        'filter': [('info_text__icontains', 'AND')],
        'replace': False
      },
      {
        'pattern': '^\\*[a-zA-Z0-9]*\\*$',
        'filter': [('info_text__icontains', 'AND')],
        'replace': True
      }
    ],

  }

  # Loop each dropdown input
  for key in filters:
    if filters[key] and key in available_filters:
      match = None
      search_string = filters[key]
      q_objects = Q()

      # If the dropdown is found in the Regex rule dict
      if key in reg_exp:
        # Spaces around a name are not part of it, free text is kept as typed
        if key not in TEXT_FILTERS:
          search_string = search_string.strip()
        # Only wildcards: matches everything
        if not search_string.strip('*'):
          continue

        # Loop through each possible Regex pattern until one matches
        for expression in reg_exp[key]:
          # Try matching the Regex pattern with the input of the dropdown
          match = re.search(expression['pattern'], search_string)
          if match is None:
            continue

          # If the input contains ? or * then eliminate them
          # This is the case for multiple Regex rules. Example: 10.?, *timeout* etc.
          if expression['replace']:
              # Special syntax: =branch_name -> exact match on branch_name (group 1)
              if key == "branch" and search_string.startswith('=') and match and match.lastindex:
                  search_string = match.group(1).strip()
              else:
                  # ? is a wildcard in branch patterns only (10.?)
                  wildcards = '[*?]' if key == 'branch' else '[*]'
                  search_string = re.sub(wildcards, '', search_string)

          # Loop through all the filters of a pattern
          # The filters are used for the database columns
          for f in expression['filter']:
            # Filtering columns is done through Q objects
            if f[1] == 'OR':
              q_objects |= Q(**{f[0]: search_string})
            else:
              q_objects &= Q(**{f[0]: search_string})

          # Replace a wildcard pattern by the list of values it matches
          if key in RESOLVE_FIRST and any(f[0].endswith(NON_SARGABLE_LOOKUPS)
                                          for f in expression['filter']):
            lookup_limit = LOOKUP_TIME_LIMIT
            if deadline:
              lookup_limit = min(lookup_limit, deadline - time.monotonic())
            values = _resolve_values(key, expression['filter'], search_string, lookup_limit)
            # None: the lookup took too long, keep the LIKE filter
            if values is not None:
              if not values:
                return {'test_runs': []}
              # Frequently failing tests keep the LIKE filter, see FREQUENT_FAILURES
              if key != 'test_name' or not _frequent_tests(values, lookup_limit):
                _, column, prefix = RESOLVE_FIRST[key]
                q_objects = Q(**{prefix + column + '__in': values})

          # The first matching pattern wins
          break

        # No pattern matched: search for the value as typed. Skipping the
        # filter would return unfiltered results that look like matches.
        if match is None:
          if key == 'bbnum':
            # Not a number, no build can match
            return {'test_runs': []}
          if key in TEXT_FILTERS:
            q_objects = Q(**{key + '__icontains': search_string.strip('*')})
          else:
            prefix = 'test_run_id__' if key in TEST_RUN_FILTERS else ''
            q_objects = Q(**{prefix + key + '__exact': search_string})

        # An exact name or a list of rarely failing tests, see JOIN_LIMIT_PREF_RATIO
        if key == 'test_name':
          selective_test_name = any(
            isinstance(child, tuple) and child[0] in ('test_name__exact', 'test_name__in')
            for child in q_objects.children)

        test_failure_filters = test_failure_filters.filter(q_objects)

  # Min Date dropdown filtering
  min_date = None
  if filters.get('dt'):
    min_date, _ = _parse_filter_date(filters['dt'])

  # Max Date dropdown filtering, a date without a time includes the whole day
  max_date = None
  if filters.get('max_dt'):
    max_date, date_only = _parse_filter_date(filters['max_dt'])
    if max_date and date_only:
      max_date += timedelta(days=1) - timedelta(microseconds=1)

  if min_date:
    test_failure_filters = test_failure_filters.filter(Q(test_run_id__dt__gte=min_date))
  if max_date:
    test_failure_filters = test_failure_filters.filter(Q(test_run_id__dt__lte=max_date))

  # Sort and limit on the keys only, then fetch those rows in full. Sorting
  # full rows reads the failure output of every match first, e.g. all 127k
  # failures of a frequent test to show 50 of them.
  _set_time_limit(deadline - time.monotonic() if deadline else None,
                  0 if selective_test_name else JOIN_LIMIT_PREF_RATIO)
  keys = list(test_failure_filters.order_by(order_by)
              .values_list('test_run_id', 'test_name', 'test_variant')[:limit])
  _set_time_limit(FETCH_TIME_LIMIT if deadline else None)

  return {'test_runs': _fetch_failures(keys)}
