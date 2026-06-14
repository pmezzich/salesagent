# Test Review Patterns

Recurring patterns extracted from test code review history. Follow these
when writing or modifying tests.

## Vacuous / Tautological Assertions

Every assertion must be capable of failing when the code is wrong.

### OR Short-Circuit
```python
# WRONG — when X is True, Y never evaluates
assert not isinstance(response, CreateMediaBuyError) or all(
    pkg.status == "active" for pkg in response.packages
)

# CORRECT — split into two asserts
assert not isinstance(response, CreateMediaBuyError)
assert all(pkg.status == "active" for pkg in response.packages)
```

### hasattr on Concrete Object
```python
# WRONG — always True; method exists unconditionally on the class
assert hasattr(adapter, "get_products")

# CORRECT — assert something meaningful about behavior
result = adapter.get_products(brief="test")
assert result is not None
```

### Happy Path With Zero Assertions
```python
# WRONG — no assertion on the response
def test_create_media_buy():
    result = create_media_buy_impl(req)
    # ... nothing checked

# CORRECT — at minimum assert success
def test_create_media_buy():
    result = create_media_buy_impl(req)
    assert not isinstance(result, CreateMediaBuyError)
    assert result.media_buy_id is not None
```

### Always-True Inequality
```python
# WRONG — Pydantic model != dict is always True in Python
assert response != {"status": "active"}

# CORRECT — compare same representations
assert response.model_dump(mode="json") == {"status": "active"}
# or
assert response == ExpectedModel(status="active")
```

## Test DRY — Use Shared Helpers

When a helper exists in `tests/utils/` or `tests/helpers/`, use it.
Don't re-implement the same logic inline.

```python
# WRONG — hand-rolling setup that a factory already provides
tenant = Tenant(id="t1", name="test", ...)
principal = Principal(id="p1", tenant_id="t1", ...)

# CORRECT — use the shared factory
from tests.factories import TenantFactory, PrincipalFactory
tenant = TenantFactory()
principal = PrincipalFactory(tenant=tenant)
```

When adding a new test factory or fixture, check that existing tests in
the same file aren't still hand-rolling the same setup.

## BDD / Integration Assertion Strength

Don't weaken assertions when modifying BDD steps.

```python
# WRONG — OR where Gherkin implies AND
# Gherkin: "Then response contains error message AND field reference"
assert "error" in response or "field" in response  # OR lets one slide

# CORRECT — both conditions
assert "error" in response
assert "field" in response
```

- Don't replace field-identity checks (`response_names == registered_names`)
  with count checks (`len(response_names) == len(registered_names)`)
- Don't remove guards (`len(x) > 0`) before asserting on contents

## Error Tests — Assert on Wire Envelope, Not Reconstructed Exceptions

> See `tests/CLAUDE.md` § Error Verification Policy for the full policy,
> helpers, and migration path.

The test harness reconstructs `AdCPError` from wire responses, but this
reconstruction is lossy. Assert on the wire envelope as the primary authority.

```python
# WRONG — tests reconstruction, not the buyer-facing wire contract
assert isinstance(result.error, AdCPValidationError)
assert result.error.error_code == "VALIDATION_ERROR"

# CORRECT — tests actual wire shape
assert result.is_error
assert_envelope_shape(
    result.wire_error_envelope,
    "VALIDATION_ERROR",
    recovery="correctable",
)
```

Always verify the `recovery` field (`transient`, `correctable`, `terminal`)
— it drives buyer-agent retry semantics.

**Exception:** `_impl`-level unit tests (no wire involved) may use
`isinstance()` and `.error_code` since they test the raise site directly.

## Tests Must Fail When Production Is Mutated

A test that survives the production line being deleted or inverted is not
coverage. Before approving a test: read the assertion, mentally delete or
invert the line under test, then ask whether the assertion would fire.

Common failure modes:

- **Mock that bypasses the predicate under test.** A `scalars(...).all()`
  stubbed to a static list means the tenant-scoping `WHERE` can be reverted
  with no test failure.
- **`patch()` of the function under test that only ever raises.** Patching
  `_finalize_approval` to raise means the real function's branches are
  never exercised.
- **Assertion on a value the test built itself**, rather than on the real
  call site's output.

```python
# WRONG — stubbed query can't catch a scoping regression
mock_session.scalars.return_value.all.return_value = [row1, row2]
# delete the tenant_id filter in production → this test still passes

# CORRECT — drive the real query against a seeded DB (requires_db)
result = repo.get_by_tenant(tenant_id)
assert {r.id for r in result} == {row1.id}  # row2 belongs to another tenant
```

## Done Means Harness/Integration Coverage, Not Manual-Mock Units

A fix isn't done until an integration test (`@pytest.mark.requires_db`) or
an e2e/BDD test built on `tests/harness/` + `tests/factories/`:

1. drives the real entrypoint,
2. exercises the code path the fix touches, and
3. fails when the fix is reverted.

Manual `patch()` / `MagicMock` stacks let wire-correctness, JSONB
`flag_modified`, transaction ordering, and race conditions slip through —
they test the mock, not the system.

## No `pytest.mark.skip` / `xfail` to Bypass a Failure

> Project policy: CLAUDE.md "Test Integrity Policy".

The only acceptable `xfail` is a tracked stub for unimplemented work,
managed by the test-surfacing skill (`/surface`). Never `xfail` to mute a
real regression, an "infrastructure issue," or a "pre-existing failure."
Fix the code or the test; if you're blocked, report it — don't skip it.

## Verify a "Production Gap" xfail Against the Spec Before Implementing

An `xfail` labeled "production gap" is a claim, not a fact. Before
recommending that someone implement the behavior, verify the expectation
against the AdCP `error_code` enum and the source-of-truth hierarchy:

- Generated `BR-*.feature` files trace to the upstream AdCP requirements
  repo — that is authoritative.
- `docs/test-obligations/` is bootstrap scaffolding only, **not**
  authoritative.

Otherwise a wiring PR gets asked to implement non-spec behavior to satisfy
a test that was wrong to begin with.
