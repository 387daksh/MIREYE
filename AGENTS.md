# MIREYE Development Rules

## Architecture
- Read existing architecture before modifying anything
- Do not rewrite working components
- Reuse existing utilities and dependencies
- Do not introduce a dependency without justification

## Spatial stack
- DuckDB is the primary analytical engine
- GeoParquet is the primary spatial storage format
- Preserve parcel IDs as stable identifiers
- H3 is for coarse filtering only; parcel geometry is authoritative

## API
- Preserve existing /v1/ask behavior unless explicitly changing it
- /v1/screen is the candidate discovery endpoint
- /v1/grid handles interconnection/capacity intelligence
- Never silently truncate fields
- Never silently choose among ambiguous addresses

## Implementation
- Inspect existing code before creating new files
- Make the smallest change that satisfies the requirement
- Reuse existing abstractions
- Don't refactor unrelated code
- Don't generate documentation unless requested

## Testing
- Add/update tests for every behavioral change
- Run targeted tests before broad test suites
- Report failures rather than hiding them$caveman-stats