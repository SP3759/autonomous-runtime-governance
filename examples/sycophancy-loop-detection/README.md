# Example: Sycophancy Loop Detection

This example demonstrates how the ARG telemetry schema and drift detection configuration work together to identify and terminate a sycophancy loop in a multi-turn session.

## Scenario

A model running inside a longitudinal tutoring platform begins validating a learner's incorrect assumptions because the learner's emotional tone keeps rewarding agreement over correction. No individual response flags. The drift compounds turn by turn until the circuit breaker fires.

## What This Example Shows

- How the Lineage Block captures the prompt version change that initiated the drift
- How the Evaluation Block tracks vector drift scores across session turns
- How the drift detection circuit breaker fires three turns after the threshold is crossed
- How the Context Block links the same drift pattern across a learner cohort

## Files

- `session-payload-sample.json` - Sample telemetry payload across 12 session turns
- `drift-detection-config.yaml` - Drift detection configuration for this scenario
- `forensic-reconstruction.md` - Step-by-step forensic reconstruction using the schema

## Coming Soon

Reference implementation in Python demonstrating real-time drift monitoring against the Kafka telemetry stream.