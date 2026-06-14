# Example: Identity Lineage Collapse

This example demonstrates the ARG Identity Proxy catching a non-delegated privilege escalation in a multi-agent execution chain.

## Scenario

A human user requests an automated financial summary. The primary agent spins up a sub-agent to query an internal database. That sub-agent calls a third-party translation API. The API encounters a prompt injection attack and alters the sub-agent next instruction, redirecting it to query payroll records the original user never authorized.

From the IAM provider perspective the token is valid, the signature is authentic, and the network path is open. Without ARG identity lineage tracking the request succeeds and the privilege escalation goes undetected.

With ARG the impersonation gate catches the semantic drift between the original financial summary scope and the payroll endpoint request, revokes the ephemeral token, and terminates the node before the unauthorized query executes.

## What This Example Shows

- How the Identity Lineage schema tracks the full parent-child execution chain
- How Semantic Context Matching detects the drift between initialization scope and live API call
- How the Ephemeral Attestation Token is revoked at the exact node where the escalation occurs
- How the audit ledger preserves the full chain of custody for the regulatory reconstruction

## The Gap This Closes

Standard access logs record the network connection. They record nothing about the intent. This example shows how ARG preserves both, giving compliance teams a signed, replayable evidence chain that proves not just what happened but what was authorized to happen.

## Coming Soon

Python reference implementation demonstrating real-time lineage tracking and semantic drift interception against the impersonation gate proxy.