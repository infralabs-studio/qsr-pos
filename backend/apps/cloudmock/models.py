from django.db import models


class CloudEvent(models.Model):
    """What the cloud has received. The autoincrement id records arrival order."""

    received_at = models.DateTimeField(auto_now_add=True)
    idempotency_key = models.CharField(max_length=128, unique=True)
    sequence = models.PositiveBigIntegerField()
    aggregate_type = models.CharField(max_length=64)
    aggregate_id = models.CharField(max_length=64)
    event_type = models.CharField(max_length=64)
    payload = models.JSONField()
    terminal_created_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["aggregate_type", "aggregate_id"])]
