from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery("nexaagent", broker=settings.celery_broker_url)
celery_app.conf.update(task_track_started=True, task_serializer="json")

# Recordatorios: cada hora en punto; la tarea decide si toca mandar el resumen del
# día (app/workers/reminders.py). Lo lanza el beat embebido del worker (--beat en
# docker-compose.yml), así que no hace falta otro contenedor.
celery_app.conf.beat_schedule = {
    "daily-reminders": {"task": "send_daily_reminders", "schedule": crontab(minute=0)},
}

# Ensure tasks are registered when the worker boots.
celery_app.autodiscover_tasks(["app.workers"])
