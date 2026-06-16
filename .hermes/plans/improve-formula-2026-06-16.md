# Plan: Cải tạo "công thức" (Formula Improvement) — 2026-06-16

> Từ discussion 16/06 trong session `20260616_132431_2a0d9f24`.

## 🎉 Final report (17/06 +07)

| Step | Result |
|---|---|
| Commit `17e8842` (Phase 1+2) | pushed + deployed via GH Actions |
| Migration 004 | `open_hc_side TEXT` added, idempotent |
| Backfill apply | **649/654 rows updated** (5 NULL = raw_features thiếu side) |
| Distribution | home 372 / away 174 / level 103 / NULL 5 |
| Test on container | `test_database_tier.py` ALL PASSED |
| Test on container | `test_ai_helpers.py` ALL PASSED |
| **Commit `070e9b4` (Phase 3+4)** | local — not pushed yet, deploy via GH Actions |
| **Phase 3** | `tag_outcome_validate()` + `wilson_ci95()` + aggregate_patterns enriched |
| **Phase 4** | `weighted_avg_confidence` display-only |
| **Test on local** | `test_tag_outcome.py` ALL PASSED (16 cases) |
| **Test all 3 suites** | ALL PASSED |
| **GitNexus re-index** | 655 nodes, 2,534 edges, 31 clusters, 56 flows |
| **UI (Phase A)** | Page mới `/formula` — render tag × outcome với Wilson CI95 + insufficient guard |
| **File thêm** | `app/templates/formula.html` (10K), route `GET /formula` trong `analyzer/views.py`, link trong `data.html` nav |

## Mục tiêu

4 hướng cải tạo cấu trúc `aggregate_patterns()` + test coverage cho AI helpers:

1. **Đóng vòng "công thức"** — `tag_outcome_validate()` JOIN tags ↔ outcome thật
2. **Calibrate confidence theo sample size** — display-only weighted avg
3. **Bucket theo dải HC/OU** — tier song song exact-line
4. **Test cho LLM-facing helpers** — 6 pure function, P0: `_parse_completion` reasoning-only

## Decisions frozen

- **`open_hc_side`**: thêm column mới (option b), backfill từ `raw_features->>'opening_hc_side'`
- **Tier**: tính on-the-fly trong query, không denormalize (low cardinality, dễ đổi mapping)
- **Migration**: chạy manual qua `docker exec` + script, không qua CI (Phase 0+ deploy safety)
- **Plan file**: lưu tại `.hermes/plans/improve-formula-2026-06-16.md`

## Phase 0: Pre-work (~30 min)

| # | Việc | File | Output |
|---|---|---|---|
| 0.1 | Tạo `app/migrations/` folder + convention README | `app/migrations/README.md` | folder + doc |
| 0.2 | Migration `add_open_hc_side` (idempotent) | `app/migrations/004_add_open_hc_side.sql` | ALTER TABLE IF NOT EXISTS pattern |
| 0.3 | Backfill script | `scripts/backfill_open_hc_side.py` | idempotent + dry-run mode |

**Schema addition**:
```sql
ALTER TABLE match_patterns ADD COLUMN IF NOT EXISTS open_hc_side TEXT;
-- 'home' = home is favorite (line < 0 means home gives handicap)
-- 'away' = away is favorite
-- 'level' = line = 0 (no favorite)
-- NULL = old pattern before this migration
```

**Backfill**:
```sql
UPDATE match_patterns
SET open_hc_side = raw_features->>'opening_hc_side'
WHERE open_hc_side IS NULL
  AND raw_features->>'opening_hc_side' IS NOT NULL;
```

## Phase 1: Hướng 3 — Tier bucketing (~2h)

| # | Việc | File | Test |
|---|---|---|---|
| 1.1 | `tier_hc(line) -> str` (pure function) | `app/database.py` | `app/test_database_tier.py` |
| 1.2 | `tier_ou(line) -> str` (pure function) | `app/database.py` | cùng file |
| 1.3 | Mở rộng `compute_pattern_stats()`: `by_open_hc_tier` song song `by_open_hc_exact` | `app/database.py:1708-1823` | integration test |

**Tier mapping (frozen)**:
```python
def tier_hc(line: Optional[float]) -> Optional[str]:
    if line is None: return None
    if -0.25 <= line <= 0.25: return "kèo_nhỏ"
    if -0.75 <= line <= -0.5 or 0.5 <= line <= 0.75: return "kèo_vừa"
    if line <= -1.0 or line >= 1.0: return "kèo_lớn"
    return None  # edge case

def tier_ou(line: Optional[float]) -> Optional[str]:
    if line is None: return None
    if line <= 2.5: return "thấp"
    if 2.75 <= line <= 3.0: return "vừa"
    if line >= 3.25: return "cao"
    return None
```

## Phase 2: Hướng 4 — Tests (~3-4h, song song Phase 1)

| # | Helper | File:Line | Priority | Fixture |
|---|---|---|---|---|
| 2.1 | `_split_quarter` | `database.py:1608-1613` | P0 | hand-crafted |
| 2.2 | `_cover_score` + `_ou_score` | `database.py:1616-1636` | P0 | hand-crafted table |
| 2.3 | `_parse_completion` | `ai_client.py:242-299` | P0 | capture SSE thật |
| 2.4 | `_parse_json_loose` | `ai_pattern.py:193-221` | P1 | hand-crafted edge cases |
| 2.5 | `_norm_tags` + `_trim_stats_for_prompt` | `ai_pattern.py:76+,223-240` | P2 | hand-crafted |

**P0 test (silent failure) — quan trọng nhất**:
```python
def test_parse_completion_reasoning_only():
    """Model emit reasoning_content only → content='', không crash."""
    sse = (
        'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}\n\n'
        'data: {"choices":[{"delta":{"reasoning_content":" more"}}]}\n\n'
        'data: {"choices":[{"finish_reason":"stop","delta":{}}]}\n\n'
        'data: [DONE]\n\n'
    )
    out = _parse_completion(sse)
    assert out["choices"][0]["message"]["content"] == ""
    assert "thinking" in out["choices"][0]["message"]["reasoning_content"]
```

## Phase 3: Hướng 1 — `tag_outcome_validate` (~4h, cần SSH verify)

| # | Việc | File | Output |
|---|---|---|---|
| 3.1 | `wilson_ci95(p, n) -> (center, half)` | `app/database.py` | pure, dễ test |
| 3.2 | `tag_outcome_validate()` core | `app/database.py` | dict |
| 3.3 | 2-tier guard: `n<15` → `insufficient_data=True` | `tag_outcome_validate` | behavior spec |
| 3.4 | Refactor `aggregate_patterns()`: enrich `top_tags` | `app/database.py:1990-2028` | API backward compat |
| 3.5 | Update `api_patterns_aggregate` nếu cần | `app/analyzer/views.py:666-672` | |
| 3.6 | **Test trên data thật qua SSH**: top 5 tags, verify n | VPS | spreadsheet note |

**Wilson CI** (z=1.96):
```python
def wilson_ci95(p: float, n: int) -> tuple[float, float]:
    if n == 0: return (0.0, 0.0)
    z = 1.96
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return (center, half)
```

## Phase 4: Hướng 2 — display-only weighted confidence (~1h)

| # | Việc | File |
|---|---|---|
| 4.1 | `weighted_avg_confidence` trong `aggregate_patterns()` | `app/database.py:2023-2028` |
| 4.2 | Weight = `sqrt(n/30)` per tag, average | pure |
| 4.3 | UI: template update nếu dùng | `app/templates/analyzer/...` |

**Không sửa** `analyze_and_store` — confidence per-pattern giữ raw.

## Execution order (parallel where possible)

```
Phase 0 (0.5h) ──→ Phase 1 (2h) ──┐
                                  ├──→ Phase 3 (4h, needs SSH) ──→ Phase 4 (1h)
              Phase 2 (3-4h) ─────┘
```

## Verification (self-checklist theo rule mới)

Khi code xong:
- [ ] `python -m pytest app/analyzer/test_ai_helpers.py` PASS
- [ ] `python -m pytest app/test_database_tier.py` PASS
- [ ] `python -c "from app.database import tier_hc, wilson_ci95; print(tier_hc(-0.5))"` works
- [ ] GitNexus re-index: `npx -y gitnexus@latest analyze --skip-agents-md`
- [ ] Commit + push → wait 5p → verify CI green
- [ ] SSH VPS → run migration → verify schema → backfill

## Rollback plan

- Migration: `ALTER TABLE match_patterns DROP COLUMN IF EXISTS open_hc_side;` (idempotent)
- Backfill: `UPDATE match_patterns SET open_hc_side = NULL WHERE ...` (irreversible chỉ khi data đã ghi đè)
- Code: revert commit, redeploy

## Notes

- Plan này KHÔNG thay đổi schema cũ, chỉ ADD column. Pattern cũ giữ NULL cho `open_hc_side` → có thể filter `WHERE open_hc_side IS NOT NULL` cho tag validate.
- Wilson CI half-width tại n=15 p=0.5 ≈ ±0.226, tại n=100 ≈ ±0.096. 2-tier guard:
  - `n < 15` → hide metric
  - `15 ≤ n < 75` → show with CI
  - `n ≥ 75` → show normally
