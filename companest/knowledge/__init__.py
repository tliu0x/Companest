"""
Companest Knowledge Subsystem

External information that flows into companies and teams:

- feeds:   async fetchers for live sources (search, RSS, markets, forecasts)
- public:  shared public knowledge documents with an S3-backed index
- digests: company-private digest ingestion (positions, PnL, risk, ...)
- s3:      the S3 JSON client shared by public and digests

The FastAPI routers in `public` and `digests` import fastapi lazily, so this
package can be imported without the server extra.
"""
