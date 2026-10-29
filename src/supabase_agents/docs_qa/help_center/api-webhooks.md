---
id: api-webhooks
title: Club API webhooks (club staff)
audience: staff
status: current
superseded_by: 
---

## Events
Webhooks are sent for booking.created, booking.cancelled and payment.succeeded.

## Signatures
Every webhook is signed with HMAC-SHA256. The signature is in the Courtline-Signature header.

## Retries
Failed deliveries are retried 8 times over 24 hours with exponential backoff.
