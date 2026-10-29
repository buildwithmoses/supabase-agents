---
id: api-rate-limits
title: Club API rate limits (club staff)
audience: staff
status: current
superseded_by: 
---

## Limits
Each API key can make 600 requests per minute and 10,000 requests per day.

## When you hit the limit
Requests over the limit get HTTP 429 with a Retry-After header saying how many seconds to wait.
