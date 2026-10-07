# Decisions

## Local dashboard

The monitor serves one personal workflow. There is no cloud schedule or
notification channel. The dashboard binds to loopback and rejects requests
from other browser origins. It is not intended for public hosting.

## Rule-based scoring

Scores are configurable keyword heuristics, not probabilities or learned
predictions. Board locations can be incomplete; unknown locations remain
visible for manual review.

## Stable deduplication

A posting is identified by company, title, and URL. Dates are excluded so the
same posting is not rediscovered on every scan. State expires after the
configured retention window.

## Public configuration

The example configuration contains no personal target list. Live board URLs
must be verified before use. Search state, logs, and reports stay local.

## Failure recovery

Retries use backoff, requests are spaced by domain, and repeatedly failing
boards trigger an in-memory circuit breaker. A new run retries recovered boards.
