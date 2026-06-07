"""
ARG Kafka Pipeline
==================
Asynchronous telemetry producer for financial governance events.

Critical design constraint: telemetry emission must NEVER block the
synchronous enforcement gate. Every publish is fire-and-forget from the
enforcement path. The pipeline handles retries and batching independently.

Supports:
  - Local Kafka (docker-compose dev environment)
  - Azure Event Hubs (production -- same Kafka protocol, different endpoint)

Reference: config/sidecar-proxy/kafka-pipeline.yaml
           config/sidecar-proxy/eventhubs-pipeline.yaml
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional

from circuit_breaker import BreakerEvent
from wallet import OperationType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Topic constants -- must match kafka-pipeline.yaml
# ---------------------------------------------------------------------------

TOPIC_TELEMETRY   = "arg.telemetry.events"
TOPIC_CIRCUIT_BREAKER = "arg.circuit_breaker.fired"
TOPIC_WALLET      = "arg.wallet.lifecycle"


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class KafkaPipeline:
    """
    ARG financial governance event pipeline.

    Wraps confluent-kafka producer with:
      - Non-blocking async publish
      - JSON serialization with ISO 8601 timestamps
      - Delivery callback logging
      - Graceful degradation when Kafka is unavailable (log-only fallback)

    Usage:
        pipeline = KafkaPipeline()                    # local Kafka
        pipeline = KafkaPipeline(use_event_hubs=True) # Azure Event Hubs

        pipeline.emit_breaker_event(event)
        pipeline.emit_spend_event(...)
        pipeline.flush()  # call before process exit
    """

    def __init__(
        self,
        bootstrap_servers: Optional[str] = None,
        use_event_hubs: bool = False,
    ) -> None:
        self._producer = None
        self._available = False

        config = self._build_config(bootstrap_servers, use_event_hubs)
        self._producer = self._init_producer(config)

    def _build_config(
        self,
        bootstrap_servers: Optional[str],
        use_event_hubs: bool,
    ) -> dict:
        if use_event_hubs:
            namespace = os.environ.get("ARG_EVENTHUBS_NAMESPACE", "")
            connection_string = os.environ.get("ARG_EVENTHUBS_CONNECTION_STRING", "")
            if not namespace or not connection_string:
                logger.warning(
                    "ARG_EVENTHUBS_NAMESPACE or ARG_EVENTHUBS_CONNECTION_STRING not set. "
                    "Falling back to log-only mode."
                )
                return {}
            return {
                "bootstrap.servers": f"{namespace}.servicebus.windows.net:9093",
                "security.protocol": "SASL_SSL",
                "sasl.mechanism": "PLAIN",
                "sasl.username": "$ConnectionString",
                "sasl.password": connection_string,
                "client.id": "arg-financial-governance",
                "acks": "all",
                "retries": 3,
                "linger.ms": 5,
                "compression.type": "snappy",
                "max.block.ms": 100,
            }
        else:
            servers = bootstrap_servers or os.environ.get(
                "ARG_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"
            )
            return {
                "bootstrap.servers": servers,
                "client.id": "arg-financial-governance",
                "acks": "all",
                "retries": 3,
                "linger.ms": 5,
                "compression.type": "snappy",
                # Critical: 100ms max block -- never block the enforcement gate
                "max.block.ms": 100,
            }

    def _init_producer(self, config: dict) -> Any:
        if not config:
            return None
        try:
            from confluent_kafka import Producer
            producer = Producer(config)
            self._available = True
            logger.info("Kafka producer initialized: %s", config.get("bootstrap.servers"))
            return producer
        except ImportError:
            logger.warning(
                "confluent-kafka not installed. "
                "Install with: pip install confluent-kafka\n"
                "Falling back to log-only mode."
            )
            return None
        except Exception as exc:
            logger.warning("Kafka unavailable (%s). Falling back to log-only mode.", exc)
            return None

    # ------------------------------------------------------------------
    # Emit methods
    # ------------------------------------------------------------------

    def emit_breaker_event(self, event: BreakerEvent) -> None:
        """Emit a circuit breaker trip event to the governance stream."""
        payload = {
            "event_id": event.event_id,
            "event_type": "CIRCUIT_BREAKER_FIRED",
            "wallet_id": event.wallet_id,
            "execution_tree_id": event.execution_tree_id,
            "timestamp": event.occurred_at.isoformat(),
            "circuit_breaker_detail": {
                "trigger_signal": event.trigger_signal.value,
                "previous_state": event.previous_state.value,
                "new_state": event.new_state.value,
                "call_count_in_window": event.call_count_in_window,
                "calls_per_minute_rate": event.calls_per_minute_rate,
                "unique_endpoints_in_window": event.unique_endpoints_in_window,
                "repeated_endpoint_pattern": event.repeated_endpoint_pattern,
                "mission_utilization_percent": event.mission_utilization_percent,
            },
        }
        self._publish(TOPIC_CIRCUIT_BREAKER, event.wallet_id, payload)

    def emit_spend_event(
        self,
        wallet_id: str,
        agent_id: str,
        tokens: int,
        cost_usd: float,
        operation_type: OperationType,
        endpoint: str,
    ) -> None:
        """Emit a wallet decrement event to the telemetry stream."""
        payload = {
            "event_id": str(uuid.uuid4()),
            "event_type": "WALLET_DECREMENTED",
            "wallet_id": wallet_id,
            "agent_id": agent_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "spend_snapshot": {
                "tokens_this_call": tokens,
                "cost_this_call_usd": cost_usd,
                "operation_type": operation_type.value,
                "endpoint": endpoint,
            },
        }
        self._publish(TOPIC_TELEMETRY, wallet_id, payload)

    def emit_wallet_lifecycle_event(
        self,
        event_type: str,
        wallet_id: str,
        execution_tree_id: str,
        detail: dict,
    ) -> None:
        """Emit a wallet lifecycle transition event."""
        payload = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "wallet_id": wallet_id,
            "execution_tree_id": execution_tree_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **detail,
        }
        self._publish(TOPIC_WALLET, wallet_id, payload)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _publish(self, topic: str, key: str, payload: dict) -> None:
        """
        Fire-and-forget publish. Never raises -- telemetry must not
        block or crash the enforcement path.
        """
        serialized = json.dumps(payload, default=str).encode("utf-8")

        if self._available and self._producer is not None:
            try:
                self._producer.produce(
                    topic=topic,
                    key=key.encode("utf-8"),
                    value=serialized,
                    on_delivery=self._delivery_callback,
                )
                # Non-blocking poll to serve delivery callbacks
                self._producer.poll(0)
            except Exception as exc:
                # Fallback to log -- never raise
                logger.error("Kafka publish failed (%s): %s", exc, payload.get("event_type"))
                self._log_event(topic, payload)
        else:
            self._log_event(topic, payload)

    @staticmethod
    def _delivery_callback(err: Any, msg: Any) -> None:
        if err:
            logger.error("Kafka delivery failed: %s", err)
        else:
            logger.debug(
                "Delivered to %s [%s] @ offset %s",
                msg.topic(),
                msg.partition(),
                msg.offset(),
            )

    @staticmethod
    def _log_event(topic: str, payload: dict) -> None:
        """Log-only fallback when Kafka is unavailable."""
        logger.info(
            "[ARG-EVENT] topic=%s event_type=%s wallet_id=%s",
            topic,
            payload.get("event_type", "UNKNOWN"),
            payload.get("wallet_id", "UNKNOWN"),
        )

    def flush(self, timeout: float = 5.0) -> None:
        """Flush pending messages. Call before process exit."""
        if self._available and self._producer is not None:
            remaining = self._producer.flush(timeout)
            if remaining > 0:
                logger.warning("%d messages not delivered before flush timeout", remaining)
