# Test Failures API Documentation

This API provides access to the Buildbot test failure data. You can filter results using query parameters to retrieve specific test failures.

## Base URL

```
cr/api/testfailures/
```

## Health check

`/cr/health`

- returns 200 OK if the database is reachable
- returns 500 otherwise

---

## Query Parameters

You can filter the results using the following optional query parameters:

| Parameter      | Type      | Description                                       | Notes                  |
|----------------|----------|---------------------------------------------------|-----------------------|
| `branch`       | string   | Filter by branch name                             | Max length: 100       |
| `commit`       | string   | Filter by commit hash (revision)                 | Max length: 100       |
| `builder_name` | string   | Filter by platform or builder name               | Max length: 100       |
| `min_date`     | datetime | Filter results from this date and time           | Format: ISO 8601      |
| `max_date`     | datetime | Filter results through this date and time        | Format: ISO 8601. A date without a time includes that whole day |
| `test_type`    | string   | Filter by test type                               | Max length: 100       |
| `test_name`    | string   | Filter by test name                               | Max length: 255       |
| `test_variant` | string   | Filter by test variant                            | Max length: 255       |
| `limit`        | integer  | Limit the number of results returned             | Min: 1, Max: 200      |
| `sort_order`   | string   | Sort results by run time                          | `desc` or `asc`       |

---

## Example Requests

### Get all test failures
```
GET cr/api/testfailures/
```

### Filter by branch and commit
```
GET cr/api/testfailures/?branch=10.11&commit=95782e5cf2dbd977473f91a59bdb47da4bf6261e
```

### Filter by builder name and test type with limit
```
GET cr/api/testfailures/?builder_name=amd64-ubasan-clang-20-debug&test_type=nm&limit=50
```

### Filter by date range, oldest first
```
GET cr/api/testfailures/?min_date=2026-01-01&max_date=2026-01-31&sort_order=asc&limit=50
```

---

## Response Format

The API returns JSON `list[dict]` where each item is a unique test failure.

```json
[
    {
        "builder_name": "amd64-ubasan-clang-20-debug",
        "commit": "95782e5cf2dbd977473f91a59bdb47da4bf6261e",
        "branch": "10.11",
        "dt": "2026-01-31T11:07:55Z",
        "test_name": "main.sp-error",
        "test_variant": "",
        "info_text": null,
        "failure_text": "\ncreate procedure proc_36510()\nbegin\ndeclare should_be_illegal condition for 0;\ndeclare continue handler for should_be_illegal set @x=0;\nend$$\nERROR HY000: Incorrect CONDITION value: '0'\ncreate procedure proc_36510()\nbegin\ndeclare continue handler for 0 set @x=0;\nend$$\nERROR HY000: Incorrect CONDITION value: '0'\nset @old_recursion_depth = @@max_sp_recursion_depth;\nset @@max_sp_recursion_depth = 255;\ncreate procedure p1(a int)\nbegin\ndeclare continue handler for 1436 -- ER_STACK_OVERRUN_NEED_MORE\nselect 'exception';\ncall p1(a+1);\nend|\ncall p1(1);\n\n\n\t\t\t"
    },
    {
        "builder_name": "amd64-ubasan-clang-20-debug",
        "commit": "95782e5cf2dbd977473f91a59bdb47da4bf6261e",
        "branch": "10.11",
        "dt": "2026-01-31T11:07:55Z",
        "test_name": "roles.acl_load_mutex-5170",
        "test_variant": "",
        "info_text": null,
        "failure_text": "\ncreate user user1@localhost;\ncreate role r1 with admin user1@localhost;\ngrant all on test.* to r1;\nflush tables;\nselect 1;\n1\n1\ndrop role r1;\ndrop user user1@localhost;\nline\n==333573==ERROR: LeakSanitizer: detected memory leaks\nSUMMARY: AddressSanitizer: 456 byte(s) leaked in 2 allocation(s).\n250912 11:07:55 [ERROR] /home/buildbot/bld/sql/mariadbd got signal 6 ;\nAttempting backtrace. Include this in the bug report.\n^ Found warnings in /dev/shm/normal/16/log/mysqld.1.err\nok\n\n\n\t\t\t"
    }
]
```

---

## Usage Example (Python)

```python
import requests

BASE_URL = "https://buildbot.mariadb.org/cr/api/testfailures/"

params = {
    "branch": "main",
    "commit": "abc123",
    "limit": 10
}

response = requests.get(BASE_URL, params=params)
data = response.json()
print(data)
```

---

## Notes

- All query parameters are optional. Omitting them will return results up to a default limit of 50.
- Dates should be provided in ISO 8601 format: `YYYY-MM-DDTHH:MM:SSZ`.
- Results are newest first by default. Use `sort_order=asc` for oldest first.
- A search that runs longer than the database time limit (50 seconds by default)
  is stopped and returns `503` with `{"detail": "The search took too long and was
  stopped. Narrow it down with a date range, branch, platform or test name."}`.
  See [Search time limit](#search-time-limit).

---

## URL Patterns

| Path                  | Description                  |
|-----------------------|------------------------------|
| `cr/`                   | Index page                   |
| `cr/api/testfailures/`  | Test Failures API endpoint   |


## Filtering

The same filter semantics apply whether the user submits the fields via the GUI or via the API.
All inputs are treated as **strings**; an empty string (or `null`) means “ignore this field”.

Rules for every filter:
- Spaces around a value are ignored, except in Failure Output and Fail Info,
  which are searched as typed.
- A value made only of `*` means no filter.
- A value that matches none of the filter's patterns below is still applied,
  never skipped: it is matched exactly as typed. Failure Output and Fail Info
  search for it as a substring (leading and trailing `*` removed). A Build
  Number that is not a number matches nothing.

### Branch (`filters.branch`)
Backed by `test_run_id__branch`.

Supported inputs:

1. **Exact match (special syntax)**
   - Input: `=branch_name`, anything after the `=`
   - Lookup: `branch__exact`
   - Example: `=10.6`, `=refs/pull/1131/merge`
2. **Major.minor prefix (question mark)**
   - Input: `10.?`
   - Lookup: `branch__istartswith` with `?` stripped
   - Example: `10.?` → matches branches starting with `10.`
3. **Exact numeric branch**
   - Input: `10.6`
   - Lookup: `branch__exact`
4. **Substring match (surrounded by `*`)**
   - Input: `*10.6*`, `*main*`, any text without `?` between the `*`
   - Lookup: `branch__icontains` with `*` stripped
   - Example: `*10.6*` → `10.6` and feature branches such as `bb-10.6-...`
5. **Substring match with `?` (surrounded by `*`)**
   - Input: `*10.?*`
   - Lookup: `branch__icontains` with `*` and `?` stripped
   - Example: `*10.?*` → searches for substring `10.`

6. **Default (free text, no wildcards)**
   - Input: any text without `*` or `?`, e.g. `feature_x`, `10.6.1`, `refs/pull/1131`
   - Lookup: `branch__icontains`

---

### Revision (`filters.revision`)
Backed by `test_run_id__revision`.

- Input: `abc123`
- Lookup: `revision__istartswith`
- Allowed characters: letters and digits only (`[a-zA-Z0-9]`)

---

### Platform (`filters.platform`)
Backed by `test_run_id__platform`.

1. **Exact match**
   - Input: `amd64-centos-7-bintar`
   - Lookup: `platform__exact`
2. **Substring match**
   - Input: `*bintar*`
   - Lookup: `platform__icontains` with `*` stripped

---

### Min Date (`filters.dt`)
Backed by `test_run_id__dt`.

- Input formats accepted:
  - `YYYY-MM-DD` (e.g., `2026-01-01`)
  - `YYYY-MM-DDTHH:MM` (e.g., `2026-01-01T12:30`)
  - `YYYY-MM-DDTHH:MM:SS` (e.g., `2026-01-01T12:30:00`)
  - `YYYY-MM-DDTHH:MM:SSZ` (e.g., `2026-01-01T12:30:00Z`)
  - `YYYY-MM-DD HH:MM:SS` (e.g., `2026-01-01 12:30:00`)
- Lookup: `dt__gte` (inclusive)

If parsing fails, the date filter is not applied.

---

### Max Date (`filters.max_dt`)
Backed by `test_run_id__dt`.

- Input formats accepted:
  - `YYYY-MM-DD` (e.g., `2026-01-31`)
  - `YYYY-MM-DDTHH:MM` (e.g., `2026-01-31T12:30`)
  - `YYYY-MM-DDTHH:MM:SS` (e.g., `2026-01-31T12:30:00`)
  - `YYYY-MM-DDTHH:MM:SSZ` (e.g., `2026-01-31T12:30:00Z`)
  - `YYYY-MM-DD HH:MM:SS` (e.g., `2026-01-31 12:30:00`)
- Lookup: `dt__lte` (inclusive). A date without a time (`YYYY-MM-DD`) includes that whole day.

If parsing fails, the date filter is not applied.

---

### Build Number (`filters.bbnum`)
Backed by `test_run_id__bbnum`. GUI only.

- Input: `1234`
- Lookup: `bbnum__exact`
- Most useful together with an exact Platform.
- Input that is not a number matches nothing.

---

### Type (`filters.typ`)
Backed by `test_run_id__typ`.

1. **Exact**
   - Input: `nm`, `debug-ps`
   - Lookup: `typ__exact`
2. **Prefix**
   - Input: `rocks*`
   - Lookup: `typ__istartswith` with `*` stripped

---

### Run Info (`filters.info`)
Backed by `test_run_id__info`. GUI only. In the current data it is empty for
every run.

1. **Exact**
   - Input: `x`
   - Lookup: `info__exact`
2. **Prefix**
   - Input: `x*`
   - Lookup: `info__istartswith` with `*` stripped

---

### Test Name (`filters.test_name`)
Backed by `test_failure.test_name`.

1. **Exact**
   - Input: `spider.auto_increment`, `galera.galera#500`
   - Lookup: `test_name__exact`

2. **Ends with**
   - Input: `*auto_increment`
   - Lookup: `test_name__iendswith`
   - Example: `*auto_increment` → matches any `test_name` that ends with `auto_increment`

3. **Contains**
   - Input: `*increment*`
   - Lookup: `test_name__icontains`
   - Example: `*increment*` → matches any `test_name` that contains `increment`

4. **Starts with**
   - Input: `spider.auto*`
   - Lookup: `test_name__istartswith`
   - Example: `spider.auto*` → matches any `test_name` that starts with `spider.auto`

Notes:
- `*` is only supported as a wildcard at the beginning and/or end of the input.
  A `*` anywhere else is matched literally.
- Empty input means no `test_name` filter is applied.

---

### Test Variant (`filters.test_variant`)
Backed by `test_failure.test_variant`.

1. **Exact**
   - Input: `innodb,row`
   - Lookup: `test_variant__exact`
2. **Substring match**
   - Input: `*row*`
   - Lookup: `test_variant__icontains` with `*` stripped

---

### Failure Output (`filters.failure_text`)
Backed by `test_failure.failure_text`.

1. **Default substring search**
   - Input: `Unknown error -11`, as typed, spaces and symbols included
   - Lookup: `failure_text__icontains`

2. **Leading and trailing `*` are removed**
   - Input: `*timeout*`, `Assertion*`, `*two words*`
   - Lookup: `failure_text__icontains` with the outer `*` stripped

Notes:
- No index can serve this search, see [Wildcard patterns](#wildcard-patterns).
  Combine it with a test name, date range, branch or platform to keep it fast.

---

### Info Text (`filters.info_text`)
Backed by `test_failure.info_text`. GUI only. Same inputs as Failure Output.

---

### Limit (`filters.limit`)
- Default: `50`
- Input: numeric string (e.g., `100`)
- Maximum: `500` in the GUI, `200` in the API.
- Applied as slice after ordering.

---

### Sort Order (`filters.sort_order`)
- Default: `desc`
- `desc`: newest first (`order_by('-test_run_id__dt')[:limit]`)
- `asc`: oldest first (`order_by('test_run_id__dt')[:limit]`)

---

### Wildcard patterns

`LIKE '%...'` cannot use an index. Substring and suffix patterns on Branch
(including the free text default, e.g. `main`), Platform and Test Name
(`*10.6*`, `*bintar*`, `*increment*`, `*auto_increment`) are therefore
resolved first to the list of matching distinct values (`SELECT DISTINCT ...`),
and the search then filters with `IN (...)`. If no value matches, no further
query is made. A lookup may take 5 seconds; if it takes longer, e.g. on a
database without the `test_name` index, the search keeps the `LIKE` filter.

Test names that failed often keep the `LIKE` filter too: when the matching
tests have more than 10,000 failures, walking runs by date finds `limit` of them
at once, while sorting all their failures is slow (`*rpl*`: 984 tests, 876k
failures, over 20 s as `IN`, 0.01 s as `LIKE`). Rarer tests use the index
(`*mroonga*`: 5k failures, 0.1 s as `IN`, 1-3 s as `LIKE`).

Substring searches on Failure Output, Info Text and Test Variant (`*x*`) are
not resolved this way and cannot use an index: the search walks test runs by
date until it has `limit` matches. When matches are rare or do not exist, it
reads the whole history (hundreds of GB of failure output). Other filters
narrow that walk: with a test name, only that test's failures are checked.

---

### How a search runs

1. Wildcard lookups, see [Wildcard patterns](#wildcard-patterns).
2. The keys of the matching failures, sorted by date and limited:
   `SELECT test_run_id, test_name, test_variant ... ORDER BY test_run.dt LIMIT n`.
3. Those rows in full, by primary key, with their test run.

Sorting full rows would read the failure output of every match before the
limit applies: for a test with 127k failures, 127k failure outputs to show 50
(39 s on a copy of production, 1.3 s with keys first).

The keys query sets `optimizer_join_limit_pref_ratio=100` (off by default). It
lets the optimizer read test runs in date order and stop once it has `limit`
matches when that is much cheaper than sorting all matches, e.g. for `main.*`
(over 60 s without it, 0.01 s with it). It stays off when the test name is
selective, an exact name or an `IN` list of rarely failing tests: starting from
the `test_name` index is cheap then, while the date walk can read every run of
a builder or branch where the test rarely fails (18 s instead of 1.5 s).
MariaDB 10.11.10 has this variable; on a server without it, searches fail with
"Unknown system variable".

---

### Search time limit

The `buildbot` database connection sets MariaDB's `max_statement_time`, so the
database itself aborts a query that runs too long. Without it, gunicorn kills
the worker after 60 seconds (`-t 60`) but the query keeps running in the
database for a client that is gone.

A search shares one time budget between its queries:
- each wildcard lookup: up to 5 seconds, then the search keeps the `LIKE` filter;
- the keys query: what is left of the budget;
- fetching the rows found: up to 5 seconds more.

- Default budget: `50` seconds, set with `DJANGO_DB_MAX_STATEMENT_TIME_BB`. Keep it
  about 10 seconds below the gunicorn worker timeout. `0` means no limit.
- GUI: the page shows "The search took too long and was stopped. Narrow it
  down with a date range, branch, platform or test name."
- API: `503` with the same message in `detail`.
