# Example: Financial Circuit Breaker

This example demonstrates the Financial Circuit Breaker catching a runaway agent loop before it exhausts its wallet allocation.

## Scenario

An agent enters a recursive loop, passing context between two sub-processes without making forward progress toward the objective. API call velocity spikes. Token consumption accelerates. The circuit breaker fires before the wallet allocation is exhausted.

## What This Example Shows

- Real-time token velocity monitoring through the Evaluation Block stream
- Semantic redundancy detection across consecutive outputs
- Circuit breaker firing and wallet signature revocation
- Parallel workflow isolation: other active agents continue unaffected

## The Stock Market Parallel

Stock markets have used circuit breakers since 1987 to halt trading automatically when prices move beyond safe thresholds. No human decision required. No delay. The ARG Financial Circuit Breaker works on the same principle applied to autonomous agent token consumption.

## Coming Soon

Reference implementation in Python demonstrating real-time velocity monitoring and circuit breaker enforcement against the Kafka telemetry stream.